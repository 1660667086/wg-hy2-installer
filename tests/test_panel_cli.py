"""Test installation planning and rollback without touching systemd."""
import argparse
from contextlib import ExitStack, redirect_stderr
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import panel_cli as cli
from panel_server import valid_password


class PanelCLITest(unittest.TestCase):
    def test_bad_public_addresses_and_ports_are_rejected(self):
        for address in ('10.1.2.3', '127.0.0.1', '224.0.0.1', '::1', 'bad'):
            with self.subTest(address=address), self.assertRaises(ValueError):
                cli.parse_ip(address)
        for port in ('0', '65536', '-1', 'bad'):
            with self.subTest(port=port), self.assertRaises(ValueError):
                cli.parse_port(port)

    def test_setup_and_failed_start_leave_managed_service_untouched(self):
        for fail_start in (False, True):
            with self.subTest(fail_start=fail_start), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                root, unit_dir = base / 'panel-install', base / 'units'
                unit_dir.mkdir()
                config, binary, template = (base / 'server.json', base / 'sing-box', base / 'client.json')
                for path in (config, binary, template):
                    path.write_text('{}')
                binary.chmod(0o700)
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.bind(('127.0.0.1', 0))
                    port = sock.getsockname()[1]
                calls = []

                def fake_run(*args, capture=False, check=True):
                    calls.append(args)
                    if args[:2] == ('openssl', 'req'):
                        (root / 'cert.pem').write_text('certificate')
                        (root / 'key.pem').write_text('key')
                    if fail_start and args == ('systemctl', 'start', cli.PANEL_UNIT):
                        raise subprocess.CalledProcessError(1, args)
                    stdout = 'not-found\n' if args[:2] == ('systemctl', 'show') else 'SHA256 Fingerprint=TEST\n'
                    inactive = fail_start and args[:3] == ('systemctl', 'is-active', '--quiet')
                    return SimpleNamespace(returncode=3 if inactive else 0, stdout=stdout)

                original_exists = Path.exists
                def fake_exists(path):
                    return True if str(path) == '/run/systemd/system' else original_exists(path)

                args = argparse.Namespace(role='single', template=None, public_ip='8.8.8.8',
                                          port=str(port), enable_panel=False)
                with ExitStack() as stack:
                    stack.enter_context(patch.object(cli, 'ROOT', root))
                    stack.enter_context(patch.object(cli, 'UNIT_DIR', unit_dir))
                    stack.enter_context(patch.object(cli.sys, 'platform', 'linux'))
                    stack.enter_context(patch.object(cli.os, 'geteuid', return_value=0))
                    stack.enter_context(patch.object(cli.shutil, 'which', return_value='/usr/bin/tool'))
                    stack.enter_context(patch.object(Path, 'exists', fake_exists))
                    stack.enter_context(patch.object(cli, 'paths', return_value=(config, binary, template,
                                                                               'wg-hy2-single.service')))
                    stack.enter_context(patch.object(cli, 'create_store', return_value=SimpleNamespace(list_users=lambda: [])))
                    stack.enter_context(patch.object(cli, 'run', side_effect=fake_run))
                    stack.enter_context(patch.object(cli.getpass, 'getpass', return_value='long unique password'))
                    if fail_start:
                        with self.assertRaises(subprocess.CalledProcessError):
                            cli.setup(args)
                        self.assertFalse(root.exists())
                        self.assertFalse(list(unit_dir.iterdir()))
                    else:
                        cli.setup(args)
                        self.assertEqual((root / 'settings.json').stat().st_mode & 0o777, 0o600)
                        self.assertNotIn('long unique password', (root / 'settings.json').read_text())
                        self.assertEqual((root / 'key.pem').stat().st_mode & 0o777, 0o600)
                        self.assertTrue((unit_dir / cli.TIMER_UNIT).exists())
                        self.assertIn(('systemctl', 'enable', '--now', cli.TIMER_UNIT), calls)
                    self.assertFalse(any('wg-hy2-single.service' in call for call in calls))

    def test_password_prompt_allows_retrying_typos(self):
        answers = iter(('short', 'short', 'long unique password', 'mistyped password',
                        'long unique password', 'long unique password'))
        stderr = io.StringIO()
        with patch.object(cli.getpass, 'getpass', side_effect=lambda _prompt: next(answers)), \
                redirect_stderr(stderr):
            record = cli.ask_password()
        self.assertTrue(valid_password('long unique password', record))
        self.assertIn('12 到 1024', stderr.getvalue())
        self.assertIn('两次密码不一致', stderr.getvalue())

    def test_password_prompt_gives_up_after_three_attempts(self):
        with patch.object(cli.getpass, 'getpass', return_value='short'), redirect_stderr(io.StringIO()):
            with self.assertRaises(ValueError):
                cli.ask_password()

    def test_firewall_hint_for_panel_port(self):
        with patch.object(cli, 'run', return_value=SimpleNamespace(returncode=0)):
            self.assertIn('--add-port=8443/tcp', cli.firewall_hint(8443))
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / 'ufw.conf'
            conf.write_text('# comment\nENABLED=yes\n')
            original = Path

            def fake_path(value, *rest):
                return conf if value == '/etc/ufw/ufw.conf' else original(value, *rest)

            with patch.object(cli, 'run', return_value=SimpleNamespace(returncode=3)), \
                    patch.object(cli, 'Path', side_effect=fake_path), \
                    patch.object(cli.shutil, 'which', return_value='/usr/sbin/ufw'):
                self.assertIn('ufw allow 8443/tcp', cli.firewall_hint(8443))
                conf.write_text('ENABLED=no\n')
                self.assertIsNone(cli.firewall_hint(8443))


if __name__ == '__main__':
    unittest.main()
