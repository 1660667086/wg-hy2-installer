#!/usr/bin/env python3
"""Local, unprivileged end-to-end tests against sing-box 1.14.4.

Set SING_BOX to the executable. No system routes, firewall rules, or existing
services are changed. The WG endpoint binds a random UDP port on all local
interfaces briefly; test keys are freshly generated and discarded afterwards.
The generated TUN is replaced with a loopback SOCKS inbound *only in test copies*.
"""
from __future__ import annotations

import contextlib
import copy
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest

REPOSITORY = Path(__file__).resolve().parents[1]
GENERATOR = REPOSITORY / 'wg_hy2_generate.py'
TEST_DESTINATION = '198.18.0.1'  # RFC 2544 benchmark address; never sent outside this host.
TEST_BODY = b'WG_HY2_LOCAL_TEST'


def load_generator():
    spec = importlib.util.spec_from_file_location('wg_hy2_generate', GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def free_port(sock_type):
    with socket.socket(socket.AF_INET, sock_type) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def read_exact(sock, count):
    data = bytearray()
    while len(data) < count:
        part = sock.recv(count - len(data))
        if not part:
            raise OSError('socket closed before SOCKS5 reply completed')
        data.extend(part)
    return bytes(data)


def socks_request(proxy_port, destination_port, timeout=4):
    """Return True only if the synthetic HTTP server responds over SOCKS5."""
    try:
        with socket.create_connection(('127.0.0.1', proxy_port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b'\x05\x01\x00')
            if read_exact(sock, 2) != b'\x05\x00':
                return False
            request = b'\x05\x01\x00\x01' + socket.inet_aton(TEST_DESTINATION) + struct.pack('!H', destination_port)
            sock.sendall(request)
            ver, status, reserved, atyp = read_exact(sock, 4)
            if (ver, status, reserved) != (5, 0, 0):
                return False
            if atyp == 1:
                read_exact(sock, 4)
            elif atyp == 4:
                read_exact(sock, 16)
            elif atyp == 3:
                read_exact(sock, read_exact(sock, 1)[0])
            else:
                return False
            read_exact(sock, 2)
            sock.sendall(b'GET / HTTP/1.1\r\nHost: test.invalid\r\nConnection: close\r\n\r\n')
            response = bytearray()
            while len(response) < 65536:
                part = sock.recv(4096)
                if not part:
                    break
                response.extend(part)
            return response.startswith(b'HTTP/1.') and response.endswith(TEST_BODY)
    except (OSError, ValueError):
        return False


class LocalHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', str(len(TEST_BODY)))
        self.end_headers()
        self.wfile.write(TEST_BODY)

    def log_message(self, *_args):
        pass


class GeneratedTunnelIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        value = os.environ.get('SING_BOX')
        if not value:
            raise unittest.SkipTest('Set SING_BOX to a sing-box 1.14.4 executable with WG, QUIC and gVisor support')
        cls.bin = str(Path(value).resolve())
        cls.mod = load_generator()
        cls.mod.verify_binary(cls.bin)
        cls.temp = tempfile.TemporaryDirectory(prefix='wg-hy2-integration-')
        cls.root = Path(cls.temp.name)
        cls.http = http.server.ThreadingHTTPServer(('127.0.0.1', 0), LocalHandler)
        cls.http_thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.http_thread.start()
        cls.http_port = cls.http.server_port
        cls.bundles = {}
        try:
            for mode in ('single', 'dual'):
                hy2_port, wg_port = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_DGRAM)
                while wg_port == hy2_port:
                    wg_port = free_port(socket.SOCK_DGRAM)
                path = cls.root / mode
                # Direct function call bypasses the production CLI's public-IP validation.
                args = type('Args', (), {
                    'mode': mode, 'entry_ip': '127.0.0.1',
                    'exit_ip': '127.0.0.2' if mode == 'dual' else None,
                    'hy2_port': hy2_port, 'wg_port': wg_port,
                    'sing_box': cls.bin, 'output': str(path),
                })()
                with contextlib.redirect_stdout(io.StringIO()):
                    cls.mod.build(args)
                cls.bundles[mode] = path
        except BaseException:
            cls.http.shutdown()
            cls.http.server_close()
            cls.temp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.http_thread.join(timeout=3)
        cls.temp.cleanup()

    def write_json(self, path, data):
        path.write_text(json.dumps(data), encoding='utf-8')
        os.chmod(path, 0o600)

    @contextlib.contextmanager
    def running(self, configs):
        processes = []
        try:
            for config in configs:
                with tempfile.NamedTemporaryFile(mode='w', suffix='.json', dir=self.root, delete=False) as f:
                    json.dump(config, f)
                    path = Path(f.name)
                self.addCleanup(path.unlink, missing_ok=True)
                subprocess.run([self.bin, 'check', '-c', str(path)], capture_output=True, check=True, timeout=30)
                processes.append(subprocess.Popen([self.bin, 'run', '-c', str(path)],
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            time.sleep(0.6)
            if any(p.poll() is not None for p in processes):
                self.fail('An isolated sing-box process failed to start')
            yield
        finally:
            for p in processes:
                if p.poll() is None:
                    p.terminate()
            for p in processes:
                try:
                    p.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()

    def fixture(self, mode, *, allow_test_destination=True):
        path = self.bundles[mode]
        client = json.loads((path / 'client/client.json').read_text(encoding='utf-8'))
        server_role = 'single' if mode == 'single' else 'landing'
        exit_config = json.loads((path / server_role / 'server.json').read_text(encoding='utf-8'))
        if allow_test_destination:
            # Sole test-only exception: map the benchmark destination to the
            # ephemeral HTTP server on loopback. No real external packets leave.
            exception = {'inbound': 'wg-in', 'network': 'tcp',
                         'ip_cidr': [TEST_DESTINATION + '/32'], 'port': [self.http_port],
                         'action': 'route', 'outbound': 'direct', 'override_address': '127.0.0.1'}
            index = next(i for i, rule in enumerate(exit_config['route']['rules'])
                         if rule.get('inbound') == 'wg-in' and rule.get('action') == 'reject')
            exit_config['route']['rules'].insert(index, exception)
        configs = [exit_config]
        if mode == 'dual':
            relay = json.loads((path / 'relay/server.json').read_text(encoding='utf-8'))
            relay['inbounds'][0]['listen'] = '127.0.0.1'
            # Some operating systems do not assign 127.0.0.2 to loopback.
            # Only this process-local test copy rewrites the WG destination.
            relay['route']['rules'][0]['override_address'] = '127.0.0.1'
            configs.append(relay)
            self.assertNotIn('127.0.0.2', json.dumps(client))
        else:
            exit_config['inbounds'][0]['listen'] = '127.0.0.1'
        return client, configs

    def request(self, original_client):
        client = copy.deepcopy(original_client)
        port = free_port(socket.SOCK_STREAM)
        client['inbounds'] = [{'type': 'mixed', 'tag': 'qa-in', 'listen': '127.0.0.1', 'listen_port': port}]
        client['route'] = {'rules': [{'inbound': 'qa-in', 'outbound': 'wg'}],
                           'final': 'wg', 'auto_detect_interface': True}
        with self.running([client]):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                        break
                except OSError:
                    time.sleep(0.15)
            else:
                return False
            return socks_request(port, self.http_port)

    def test_single_success(self):
        client, configs = self.fixture('single')
        with self.running(configs):
            self.assertTrue(self.request(client))

    def test_dual_success_and_hidden_exit(self):
        client, configs = self.fixture('dual')
        with self.running(configs):
            self.assertTrue(self.request(client))

    def test_incorrect_server_certificate_fails(self):
        client, configs = self.fixture('single')
        cert_dir = self.root / 'wrong-cert'
        cert_dir.mkdir(exist_ok=True)
        cert, _ = self.mod.make_certificate('127.0.0.1', cert_dir)
        client['outbounds'][0]['tls']['certificate'] = cert
        with self.running(configs):
            self.assertFalse(self.request(client))

    def test_incorrect_wireguard_key_fails(self):
        client, configs = self.fixture('single')
        client['endpoints'][0]['peers'][0]['public_key'] = self.mod.keypair(self.bin)['PublicKey']
        with self.running(configs):
            self.assertFalse(self.request(client))

    def test_exit_blocks_private_destinations(self):
        client, configs = self.fixture('single', allow_test_destination=False)
        with self.running(configs):
            self.assertFalse(self.request(client))

    def test_relay_rejects_other_destination(self):
        client, configs = self.fixture('dual')
        client['endpoints'][0]['peers'][0]['address'] = '192.0.2.2'
        with self.running(configs):
            self.assertFalse(self.request(client))

    def test_panel_users_connect_and_revocation_preserves_original(self):
        """Exercise panel-generated peers over real local HY2 and WireGuard.

        The fake service control represents a stopped systemd service: each
        temporary sing-box server is explicitly restarted between phases.
        Neither systemd nor host routes or firewall rules are touched.
        """
        import panel_backend

        class StoppedControl:
            def active(self, _name):
                return False

            def restart(self, _name):
                raise AssertionError('A stopped service must not be started')

        for mode in ('single', 'dual'):
            with self.subTest(mode=mode):
                role = 'single' if mode == 'single' else 'landing'
                bundle = self.bundles[mode]
                scratch = self.root / ('panel-' + mode)
                scratch.mkdir(mode=0o700)
                server_path = scratch / 'server.json'
                server = json.loads((bundle / role / 'server.json').read_text())
                self.write_json(server_path, server)
                original_client, runtime = self.fixture(mode)
                # The local test environment may ship LibreSSL without
                # OpenSSL's X25519 PKCS#8 support.  These two mappings are
                # taken only from the matching, freshly generated fixture;
                # production public-key derivation is covered separately.
                known_public = {
                    server['endpoints'][0]['private_key']:
                        original_client['endpoints'][0]['peers'][0]['public_key'],
                    original_client['endpoints'][0]['private_key']:
                        server['endpoints'][0]['peers'][0]['public_key'],
                }
                now = [1000]
                store = panel_backend.PanelStore(
                    server_path, bundle / 'client/client.json', scratch / 'state.json',
                    self.bin, 'wg-hy2-' + role + '.service', role,
                    control=StoppedControl(), clock=lambda: now[0],
                    public_key=lambda private: known_public[private],
                )
                alice = store.add_user('alice', None)
                bob = store.add_user('bob', 1100)
                alice_client = store.export_client(alice['id'])
                bob_client = store.export_client(bob['id'])
                self.assertNotEqual(alice_client['endpoints'][0]['private_key'],
                                    bob_client['endpoints'][0]['private_key'])
                self.assertNotEqual(alice_client['endpoints'][0]['address'],
                                    bob_client['endpoints'][0]['address'])
                if mode == 'single':
                    self.assertNotEqual(alice_client['outbounds'][0]['password'],
                                        bob_client['outbounds'][0]['password'])

                def runtime_configs():
                    configs = copy.deepcopy(runtime)
                    managed = json.loads(server_path.read_text())
                    configs[0]['endpoints'] = managed['endpoints']
                    if role == 'single':
                        configs[0]['inbounds'] = managed['inbounds']
                        configs[0]['inbounds'][0]['listen'] = '127.0.0.1'
                    return configs

                with self.running(runtime_configs()):
                    self.assertTrue(self.request(original_client), 'original client before changes')
                    self.assertTrue(self.request(alice_client), 'new alice client')
                    self.assertTrue(self.request(bob_client), 'new bob client')

                store.disable_user(alice['id'])
                now[0] = 1200
                self.assertEqual(store.expire_users(), 1)
                with self.running(runtime_configs()):
                    self.assertTrue(self.request(original_client), 'original client after changes')
                    self.assertFalse(self.request(alice_client), 'disabled client must fail')
                    self.assertFalse(self.request(bob_client), 'expired client must fail')


if __name__ == '__main__':
    unittest.main(verbosity=2)
