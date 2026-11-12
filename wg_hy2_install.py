#!/usr/bin/env python3
"""Install a generated server bundle as an isolated systemd service.

This installer deliberately does not download or build sing-box.  Supply a
locally obtained, verified sing-box 1.14.4 binary with the required tags.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VERSION = "1.14.4"
TAGS = ("with_quic", "with_wireguard", "with_gvisor")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,30}\Z")


class InstallError(Exception):
    pass


def run(argv, *, capture=False, check=True):
    try:
        return subprocess.run(argv, check=check, text=True,
                              stdout=subprocess.PIPE if capture else None,
                              stderr=subprocess.STDOUT if capture else None,
                              timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError(f"Could not run {argv[0]}: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        # Avoid printing the configuration: it contains private keys.
        raise InstallError(f"{argv[0]} exited with status {exc.returncode}") from exc


def read_object(path):
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            raise InstallError(f"{path.name} exceeds 4 MiB")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallError(f"Cannot read valid JSON from {path.name}") from exc
    if not isinstance(value, dict):
        raise InstallError(f"{path.name} must contain a JSON object")
    return value


def port(value, field):
    if type(value) is not int or not 1 <= value <= 65535:
        raise InstallError(f"{field} must be an integer in 1..65535")
    return value


def parse_manifest(value):
    expected = {"schema", "role", "hy2_port", "wg_port", "relay_ip"}
    if set(value) != expected or type(value.get("schema")) is not int or value.get("schema") != 1:
        raise InstallError("Unsupported manifest schema or fields")
    role = value.get("role")
    if role not in ("single", "relay", "landing"):
        raise InstallError("Manifest role must be single, relay or landing")
    hp, wp, relay = value.get("hy2_port"), value.get("wg_port"), value.get("relay_ip")
    if role in ("single", "relay"):
        hp = port(hp, "hy2_port")
    elif hp is not None:
        raise InstallError("Landing must not contain hy2_port")
    if role in ("single", "landing"):
        wp = port(wp, "wg_port")
    elif wp is not None:
        raise InstallError("Relay must not contain wg_port")
    if hp is not None and hp == wp:
        raise InstallError("HY2 and WireGuard must use different UDP ports")
    if role == "landing":
        if not isinstance(relay, str):
            raise InstallError("relay_ip must be a single IPv4 address string")
        try:
            addr = ipaddress.IPv4Address(relay)
        except (ipaddress.AddressValueError, TypeError) as exc:
            raise InstallError("relay_ip must be a single IPv4 address") from exc
        if not addr.is_global or addr.is_multicast or addr.is_unspecified or addr.is_loopback or addr.is_link_local:
            raise InstallError("relay_ip must be a public unicast IPv4 address")
        relay = str(addr)
    elif relay is not None:
        raise InstallError("relay_ip is only allowed for landing")
    return {"role": role, "hy2_port": hp, "wg_port": wp, "relay_ip": relay}


def check_config_shape(config, manifest):
    """Refuse unrelated bundles whose listeners do not match the firewall."""
    if set(config) - {"log", "inbounds", "endpoints", "outbounds", "route"}:
        raise InstallError("Unsupported server configuration section")
    log = config.get("log", {})
    if not isinstance(log, dict) or set(log) - {"level", "timestamp"}:
        raise InstallError("Server log output paths and extra log settings are not accepted")
    if config.get("outbounds") != [{"type": "direct", "tag": "direct"}]:
        raise InstallError("Server bundle must have exactly one direct outbound")
    incoming, endpoints = config.get("inbounds", []), config.get("endpoints", [])
    if not isinstance(incoming, list) or not isinstance(endpoints, list):
        raise InstallError("Invalid inbounds or endpoints")
    role = manifest["role"]
    if role in ("single", "relay"):
        if len(incoming) != 1 or not isinstance(incoming[0], dict) or incoming[0].get("type") != "hysteria2":
            raise InstallError("Bundle must have exactly one HY2 inbound")
        item = incoming[0]
        if item.get("tag") != "hy-in" or item.get("listen") != "0.0.0.0" or item.get("listen_port") != manifest["hy2_port"]:
            raise InstallError("HY2 listener does not match the manifest")
        tls = item.get("tls") or {}
        if not isinstance(tls, dict) or not tls.get("enabled") or not tls.get("certificate") or not tls.get("key"):
            raise InstallError("HY2 server must embed its TLS certificate and key")
        if "certificate_path" in tls or "key_path" in tls:
            raise InstallError("External TLS key or certificate paths are not accepted")
    elif incoming:
        raise InstallError("Landing must not have additional inbounds")
    if role in ("single", "landing"):
        if len(endpoints) != 1 or not isinstance(endpoints[0], dict) or endpoints[0].get("type") != "wireguard":
            raise InstallError("Bundle must have exactly one WireGuard endpoint")
        ep = endpoints[0]
        if ep.get("tag") != "wg-in" or ep.get("system") is not False or ep.get("listen_port") != manifest["wg_port"]:
            raise InstallError("WireGuard listener does not match the manifest")
    elif endpoints:
        raise InstallError("Relay must not have additional endpoints")
    route = config.get("route")
    if not isinstance(route, dict) or set(route) != {"rules"} or not isinstance(route["rules"], list):
        raise InstallError("Invalid server routing rules")
    rules = route["rules"]
    reject_all = {"action": "reject"}
    if not rules or rules[-1] != reject_all:
        raise InstallError("Server routing must reject unmatched traffic")
    # The generated service restricts authenticated tunnel users from probing
    # local services, cloud metadata and private networks through its WG exit.
    protected = {"0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
                 "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16",
                 "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4",
                 "::1/128", "fc00::/7", "fe80::/10", "ff00::/8"}
    if role in ("single", "landing"):
        tail = rules[2:] if role == "single" else rules
        if (len(tail) != 3 or not isinstance(tail[0], dict)
                or set(tail[0]) != {"inbound", "ip_cidr", "action"}
                or tail[0]["inbound"] != "wg-in" or tail[0]["action"] != "reject"
                or not isinstance(tail[0]["ip_cidr"], list)
                or any(not isinstance(item, str) for item in tail[0]["ip_cidr"])
                or not protected.issubset(set(tail[0]["ip_cidr"]))
                or tail[1] != {"inbound": "wg-in", "outbound": "direct"}
                or tail[2] != reject_all):
            raise InstallError("WireGuard exit must block private destinations and reject unmatched traffic")
    if role == "single":
        allowed = {"inbound": "hy-in", "network": "udp", "ip_cidr": ["127.0.0.1/32"],
                   "port": [manifest["wg_port"]], "outbound": "direct"}
        if len(rules) != 5 or rules[0] != allowed or rules[1] != {"inbound": "hy-in", "action": "reject"}:
            raise InstallError("Single-server HY2 must only forward to the local WireGuard port")
    elif role == "relay":
        if len(rules) != 2 or not isinstance(rules[0], dict):
            raise InstallError("Relay must have a single restricted forwarding rule")
        first = rules[0]
        expected = {"inbound", "network", "ip_cidr", "port", "action", "outbound", "override_address", "override_port"}
        if not isinstance(first.get("override_address"), str):
            raise InstallError("Relay override must be an IPv4 address string")
        try:
            target = ipaddress.IPv4Address(first.get("override_address"))
        except (ipaddress.AddressValueError, TypeError) as exc:
            raise InstallError("Relay override must be an IPv4 address") from exc
        target_port = port(first.get("override_port"), "relay override_port")
        if (set(first) != expected or first["inbound"] != "hy-in" or first["network"] != "udp"
                or first["ip_cidr"] != ["192.0.2.1/32"] or first["port"] != [target_port]
                or first["action"] != "route" or first["outbound"] != "direct"
                or not target.is_global or target.is_multicast):
            raise InstallError("Relay forwarding rule must target only the specified public WG endpoint")


def check_binary(binary):
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise InstallError("--sing-box must be an existing executable")
    result = run([str(binary), "version"], capture=True)
    if not re.search(r"(?m)^sing-box version " + re.escape(VERSION) + r"\s*$", result.stdout):
        raise InstallError(f"Expected sing-box {VERSION}")
    missing = [tag for tag in TAGS if tag not in result.stdout]
    if missing:
        raise InstallError("sing-box is missing required build tags: " + ", ".join(missing))


def firewall_rules(manifest, name):
    """Return insertion-order rules; every rule carries a unique comment."""
    marker = "wg-hy2-" + name
    role, hp, wp, relay = (manifest[x] for x in ("role", "hy2_port", "wg_port", "relay_ip"))
    rules = []
    def add(family, *spec):
        rules.append((family, [*spec, "-m", "comment", "--comment", marker]))
    if wp is not None:
        # Insert DROP first; subsequent source / loopback accepts land above it.
        add(4, "!", "-i", "lo", "-p", "udp", "--dport", str(wp), "-j", "DROP")
        add(6, "!", "-i", "lo", "-p", "udp", "--dport", str(wp), "-j", "DROP")
        add(4, "-i", "lo", "-p", "udp", "--dport", str(wp), "-j", "ACCEPT")
        if role == "landing":
            add(4, "!", "-i", "lo", "-p", "udp", "-s", relay + "/32", "--dport", str(wp), "-j", "ACCEPT")
    if hp is not None:
        add(4, "-p", "udp", "--dport", str(hp), "-j", "ACCEPT")
    return rules


def firewall_script(rules, ip4, ip6):
    lines = ["#!/bin/sh", "set -eu", "cleanup() {"]
    for family, spec in reversed(rules):
        argv = [ip4 if family == 4 else ip6, "-w", "-D", "INPUT", *spec]
        lines.append("  " + shlex.join(argv) + " >/dev/null 2>&1 || true")
    lines += ["}", 'case "${1:-}" in', "  add)", "    trap cleanup EXIT HUP INT TERM"]
    for family, spec in rules:
        base = ip4 if family == 4 else ip6
        check = shlex.join([base, "-w", "-C", "INPUT", *spec])
        insert = shlex.join([base, "-w", "-I", "INPUT", "1", *spec])
        lines.append(f"    {check} >/dev/null 2>&1 || {insert}")
    lines += ["    trap - EXIT HUP INT TERM", "    ;;", "  remove) cleanup ;;", '  *) echo "usage: firewall.sh add|remove" >&2; exit 2 ;;', "esac", ""]
    return "\n".join(lines)


def available_udp(port_number):
    """Check IPv4 and, where supported, IPv6 bind without keeping a port open."""
    for family, addr in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        try:
            sock = socket.socket(family, socket.SOCK_DGRAM)
        except OSError as exc:
            if family == socket.AF_INET6 and exc.errno in (97, 93):
                continue
            raise InstallError(f"Cannot probe UDP {port_number}: {exc}") from exc
        try:
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind((addr, port_number))
        except OSError as exc:
            raise InstallError(f"UDP port {port_number} is not available: {exc}") from exc
        finally:
            sock.close()


def ensure_unit_absent(systemctl, unit_name):
    """Check systemd's full unit search path, not only /etc/systemd/system."""
    result = run([systemctl, "show", unit_name, "-p", "LoadState", "--value"], capture=True, check=False)
    if result.returncode != 0 or result.stdout.strip() != "not-found":
        raise InstallError("systemd unit already exists or its absence could not be verified")


def unit_text(path):
    return f"""[Unit]
Description=WG over Hysteria2 ({path.name})
Wants=network-online.target
After=network-online.target netfilter-persistent.service ufw.service

[Service]
Type=simple
ExecStartPre={path}/firewall.sh add
ExecStart={path}/sing-box run -c {path}/server.json -D {path}
ExecStopPost={path}/firewall.sh remove
Restart=on-failure
RestartSec=3
LimitNOFILE=65535
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
"""


def write_exclusive(path, data, mode):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(data)
        os.chmod(path, mode)
    except BaseException:
        # os.open used O_EXCL, so this file is ours even on a partial write.
        try:
            path.unlink(missing_ok=True)
        finally:
            raise


def perform_install(*, target, unit, unit_name, binary, config, manifest, rules, systemctl, ip4, ip6, no_enable):
    """Install after preflight; paths are parameters so rollback can be tested."""
    made_dir = made_unit = False
    try:
        os.mkdir(target, 0o700)
        made_dir = True
        shutil.copyfile(binary, target / "sing-box")
        os.chmod(target / "sing-box", 0o755)
        write_exclusive(target / "server.json", json.dumps(config, indent=2) + "\n", 0o600)
        write_exclusive(target / "manifest.json", json.dumps({"schema": 1, **manifest}, indent=2) + "\n", 0o600)
        write_exclusive(target / "firewall.sh", firewall_script(rules, ip4, ip6), 0o700)
        write_exclusive(unit, unit_text(target), 0o644)
        made_unit = True
        run([systemctl, "daemon-reload"])
        run([systemctl, "start", unit_name])
        time.sleep(2)
        run([systemctl, "is-active", "--quiet", unit_name])
        if not no_enable:
            run([systemctl, "enable", unit_name])
    except BaseException:
        if made_unit:
            run([systemctl, "disable", "--now", unit_name], capture=True, check=False)
            active = run([systemctl, "is-active", "--quiet", unit_name], capture=True, check=False).returncode == 0
            if active:
                raise InstallError(f"Rollback could not stop {unit_name}; files retained for manual recovery")
            if made_dir:
                run([str(target / "firewall.sh"), "remove"], capture=True, check=False)
            unit.unlink(missing_ok=True)
        if made_dir:
            shutil.rmtree(target)
        if made_unit:
            run([systemctl, "daemon-reload"], capture=True, check=False)
        raise
    print(f"Installed and started {unit_name}; data is in {target}.")
    if manifest["role"] in ("single", "relay"):
        print(f"Allow UDP {manifest['hy2_port']} in the cloud security group for your clients.")
    else:
        print(f"Allow UDP {manifest['wg_port']} in the cloud security group only from {manifest['relay_ip']}/32.")
    if no_enable:
        print("Boot autostart is disabled (--no-enable).")


def install(args):
    if not NAME_RE.fullmatch(args.name):
        raise InstallError("--name must be 1–31 ASCII letters, digits, '_' or '-' and start with a letter/digit")
    bundle = Path(args.bundle).resolve()
    manifest = parse_manifest(read_object(bundle / "manifest.json"))
    config_path = bundle / "server.json"
    config = read_object(config_path)
    check_config_shape(config, manifest)
    binary = Path(args.sing_box).resolve()
    check_binary(binary)
    # Validate the parsed snapshot rather than re-reading a potentially changed
    # bundle file between checking and installation.
    with tempfile.TemporaryDirectory(prefix="wg-hy2-check-") as tmp:
        snapshot = Path(tmp) / "server.json"
        write_exclusive(snapshot, json.dumps(config) + "\n", 0o600)
        run([str(binary), "check", "-c", str(snapshot)], capture=True)
    target = Path("/opt") / ("wg-hy2-" + args.name)
    unit_name = "wg-hy2-" + args.name + ".service"
    unit = Path("/etc/systemd/system") / unit_name
    if args.dry_run:
        print(f"Bundle validated: {manifest['role']}; planned service {unit_name}; no changes made.")
        return
    if platform.system() != "Linux" or os.geteuid() != 0:
        raise InstallError("Installation requires root on Linux with systemd")
    systemctl = shutil.which("systemctl")
    ip4, ip6 = shutil.which("iptables"), shutil.which("ip6tables")
    if not systemctl or not ip4 or not ip6 or not Path("/run/systemd/system").exists():
        raise InstallError("systemd, iptables and ip6tables are required")
    ensure_unit_absent(systemctl, unit_name)
    if os.path.lexists(target) or os.path.lexists(unit):
        raise InstallError("Installation directory or unit already exists; refusing to overwrite")
    for wants in Path("/etc/systemd/system").glob("*.wants"):
        if os.path.lexists(wants / unit_name):
            raise InstallError("An existing systemd enablement link uses this name")
    marker = "wg-hy2-" + args.name
    for tool in (ip4, ip6):
        existing = run([tool, "-w", "-S", "INPUT"], capture=True).stdout
        if marker in existing:
            raise InstallError("An existing firewall rule uses this installation name")
    for n in {manifest["hy2_port"], manifest["wg_port"]} - {None}:
        available_udp(n)
    rules = firewall_rules(manifest, args.name)
    perform_install(target=target, unit=unit, unit_name=unit_name, binary=binary,
                    config=config, manifest=manifest, rules=rules, systemctl=systemctl,
                    ip4=ip4, ip6=ip6, no_enable=args.no_enable)


def exit_on_signal(signum, _frame):
    # SystemExit passes through the transactional rollback without converting
    # an interrupted installation into a successful exit.
    raise SystemExit(128 + signum)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, help="generated server bundle directory")
    parser.add_argument("--sing-box", required=True, help="path to verified sing-box 1.14.4 executable")
    parser.add_argument("--name", required=True, help="unique instance name")
    parser.add_argument("--no-enable", action="store_true", help="start now without enabling at boot")
    parser.add_argument("--dry-run", action="store_true", help="validate inputs, without modifying the system")
    args = parser.parse_args()
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), exit_on_signal)
    try:
        install(args)
    except InstallError as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print("error: installation failed: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
