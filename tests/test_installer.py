"""Unprivileged installer safety tests; no systemd or firewall is modified."""

import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("wg_hy2_install", ROOT / "wg_hy2_install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


FAKE_IPTABLES = '''#!/usr/bin/env python3
import json, os, pathlib, sys
path = pathlib.Path(os.environ["IPT_STATE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
if args[0] == "-w":
    args.pop(0)
op = args.pop(0)
assert args.pop(0) == "INPUT"
if op == "-I":
    assert args.pop(0) == "1"
key = pathlib.Path(sys.argv[0]).name + ":" + json.dumps(args)
if op == "-C":
    sys.exit(0 if key in state else 1)
if op == "-I":
    count = state.get("insert_count", 0) + 1
    state["insert_count"] = count
    if count == int(os.environ.get("IPT_FAIL_INSERT", "0")):
        path.write_text(json.dumps(state))
        sys.exit(2)
    state[key] = True
elif op == "-D":
    if key not in state:
        sys.exit(1)
    del state[key]
else:
    sys.exit(2)
path.write_text(json.dumps(state))
'''


def manifest(role):
    return {
        "role": role,
        "hy2_port": 443 if role != "landing" else None,
        "wg_port": 51820 if role != "relay" else None,
        "relay_ip": "8.8.8.8" if role == "landing" else None,
    }


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wg-hy2-installer-test-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.state = self.path / "firewall.json"
        self.state.write_text(json.dumps({"unrelated": ["preserved"]}))
        stub = self.path / "fake-iptables"
        stub.write_text(FAKE_IPTABLES)
        stub.chmod(0o700)
        self.ip4, self.ip6 = self.path / "fake4", self.path / "fake6"
        self.ip4.symlink_to(stub)
        self.ip6.symlink_to(stub)
        self.env = os.environ.copy()
        self.env["IPT_STATE"] = str(self.state)

    def rules(self, role):
        return installer.firewall_rules(manifest(role), "test")

    def firewall(self, role):
        path = self.path / "firewall.sh"
        path.write_text(installer.firewall_script(self.rules(role), str(self.ip4), str(self.ip6)))
        path.chmod(0o700)
        return path

    def current_rules(self):
        return {k for k in json.loads(self.state.read_text()) if k.startswith("fake")}

    def test_all_roles_idempotent_and_preserve_other_rules(self):
        for role in ("single", "relay", "landing"):
            with self.subTest(role=role):
                script = self.firewall(role)
                subprocess.run([script, "add"], env=self.env, check=True)
                self.assertEqual(len(self.current_rules()), len(self.rules(role)))
                subprocess.run([script, "add"], env=self.env, check=True)
                self.assertEqual(len(self.current_rules()), len(self.rules(role)))
                subprocess.run([script, "remove"], env=self.env, check=True)
                self.assertFalse(self.current_rules())
                self.assertEqual(json.loads(self.state.read_text())["unrelated"], ["preserved"])

    def test_partial_firewall_failure_removes_own_rules(self):
        script = self.firewall("landing")
        env = dict(self.env, IPT_FAIL_INSERT="3")
        result = subprocess.run([script, "add"], env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.current_rules())
        self.assertEqual(json.loads(self.state.read_text())["unrelated"], ["preserved"])

    def test_service_failures_remove_created_files_and_request_rule_cleanup(self):
        binary = self.path / "sing-box"
        binary.write_bytes(b"test binary")
        for fail_op in ("start", "enable"):
            with self.subTest(fail_op=fail_op):
                target = self.path / ("install-" + fail_op)
                unit = self.path / ("unit-" + fail_op + ".service")
                calls = []

                def fake_run(argv, **_kwargs):
                    calls.append(argv)
                    op = argv[1] if len(argv) > 1 else ""
                    if op == fail_op:
                        raise installer.InstallError("injected failure")
                    return SimpleNamespace(returncode=1 if op == "is-active" else 0, stdout="")

                with patch.object(installer, "run", side_effect=fake_run), patch.object(installer.time, "sleep"):
                    with self.assertRaises(installer.InstallError):
                        installer.perform_install(
                            target=target, unit=unit, unit_name=unit.name, binary=binary,
                            config={}, manifest=manifest("relay"), rules=self.rules("relay"),
                            systemctl="systemctl", ip4=str(self.ip4), ip6=str(self.ip6), no_enable=False,
                        )
                self.assertFalse(target.exists())
                self.assertFalse(unit.exists())
                self.assertTrue(any(len(c) > 1 and c[1] == "disable" for c in calls))
                self.assertTrue(any(str(c[0]).endswith("firewall.sh") and c[1] == "remove" for c in calls))

    def test_interruptions_roll_back_and_propagate(self):
        binary = self.path / "sing-box"
        binary.write_bytes(b"test binary")
        for original in (KeyboardInterrupt(), SystemExit(128 + signal.SIGTERM), SystemExit(128 + signal.SIGHUP)):
            with self.subTest(error=type(original).__name__, code=getattr(original, "code", None)):
                target = self.path / "interrupted"
                unit = self.path / "interrupted.service"
                calls = []

                def fake_run(argv, **_kwargs):
                    calls.append(argv)
                    op = argv[1] if len(argv) > 1 else ""
                    if op == "start":
                        raise original
                    return SimpleNamespace(returncode=1 if op == "is-active" else 0, stdout="")

                with patch.object(installer, "run", side_effect=fake_run), patch.object(installer.time, "sleep"):
                    with self.assertRaises(type(original)) as raised:
                        installer.perform_install(
                            target=target, unit=unit, unit_name=unit.name, binary=binary,
                            config={}, manifest=manifest("relay"), rules=self.rules("relay"),
                            systemctl="systemctl", ip4=str(self.ip4), ip6=str(self.ip6), no_enable=False,
                        )
                self.assertIs(raised.exception, original)
                self.assertFalse(target.exists())
                self.assertFalse(unit.exists())
                self.assertTrue(any(len(c) > 1 and c[1] == "disable" for c in calls))
                self.assertTrue(any(str(c[0]).endswith("firewall.sh") and c[1] == "remove" for c in calls))

    def test_signal_exit_is_nonzero(self):
        for signum in (signal.SIGTERM, signal.SIGHUP):
            with self.assertRaises(SystemExit) as raised:
                installer.exit_on_signal(signum, None)
            self.assertEqual(raised.exception.code, 128 + signum)

    def test_systemd_full_search_path_preflight(self):
        for stdout, returncode, should_pass in (
            ("not-found\n", 0, True), ("loaded\n", 0, False),
            ("not-found\n", 1, False), ("", 0, False),
        ):
            with self.subTest(stdout=stdout, returncode=returncode):
                with patch.object(installer, "run", return_value=SimpleNamespace(stdout=stdout, returncode=returncode)):
                    if should_pass:
                        installer.ensure_unit_absent("systemctl", "wg-hy2-test.service")
                    else:
                        with self.assertRaises(installer.InstallError):
                            installer.ensure_unit_absent("systemctl", "wg-hy2-test.service")

    def test_udp_ipv4_port_collision(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            with self.assertRaises(installer.InstallError):
                installer.available_udp(sock.getsockname()[1])

    def test_udp_ipv6_port_collision(self):
        if not socket.has_ipv6:
            self.skipTest("IPv6 is unavailable")
        try:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind(("::1", 0))
        except OSError as exc:
            self.skipTest(f"IPv6 loopback unavailable: {exc}")
        with sock:
            with self.assertRaises(installer.InstallError):
                installer.available_udp(sock.getsockname()[1])

    def test_relay_source_must_be_public_unicast_ipv4(self):
        for value in ("224.0.0.1", "127.0.0.1", "10.1.2.3", "::1", "bad;id", 134744072):
            with self.subTest(value=value):
                raw = {"schema": 1, **manifest("landing"), "relay_ip": value}
                with self.assertRaises(installer.InstallError):
                    installer.parse_manifest(raw)
        self.assertEqual(installer.parse_manifest({"schema": 1, **manifest("landing")})["relay_ip"], "8.8.8.8")

    def test_manifest_rejects_boolean_and_port_collision(self):
        for raw in (
            {"schema": True, **manifest("single")},
            {"schema": 1, **manifest("single"), "hy2_port": True},
            {"schema": 1, **manifest("single"), "hy2_port": 51820},
        ):
            with self.subTest(raw=raw), self.assertRaises(installer.InstallError):
                installer.parse_manifest(raw)

    def test_exclusive_write_does_not_overwrite_existing_file(self):
        path = self.path / "existing"
        path.write_text("keep")
        with self.assertRaises(FileExistsError):
            installer.write_exclusive(path, "replace", 0o600)
        self.assertEqual(path.read_text(), "keep")

    def test_binary_can_be_resolved_from_path_or_explicit_path(self):
        binary = self.path / "sing-box"
        binary.write_bytes(b"binary")
        binary.chmod(0o700)
        self.assertEqual(installer.resolve_binary(str(binary)), binary.resolve())
        with patch.object(installer.shutil, "which", return_value=str(binary)):
            self.assertEqual(installer.resolve_binary("sing-box"), binary.resolve())
        with patch.object(installer.shutil, "which", return_value=None):
            with self.assertRaises(installer.InstallError):
                installer.resolve_binary("missing-sing-box")


if __name__ == "__main__":
    unittest.main()
