"""Panel state tests use temporary files and never touch system services."""
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import panel_backend as panel
import wg_hy2_generate as gen


class Control:
    def __init__(self):
        self.running = True
        self.restarts = 0
        self.fail_once = False

    def active(self, _name):
        return self.running

    def restart(self, _name):
        self.restarts += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError('injected restart failure')


class PanelBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control = Control()
        self.now = 1000
        self.key = base64.b64encode(b'x' * 32).decode()
        self.server_keys = {'PrivateKey': self.key, 'PublicKey': self.key}
        self.client_keys = {'PrivateKey': self.key, 'PublicKey': self.key}

    def store(self, role):
        config = gen.server_common()
        config['endpoints'] = [gen.wg_in(51820, '10.238.5', self.server_keys, self.client_keys, self.key)]
        config['route'] = {'rules': [{'inbound': 'wg-in', 'ip_cidr': gen.BLOCKED_DESTINATIONS,
                                      'action': 'reject'}, {'inbound': 'wg-in', 'outbound': 'direct'},
                                     {'action': 'reject'}]}
        if role == 'single':
            config['inbounds'] = [gen.hy_in(443, 'original-password', ['cert'], ['private'])]
            config['route']['rules'][:0] = [{'inbound': 'hy-in', 'network': 'udp',
                'ip_cidr': ['127.0.0.1/32'], 'port': [51820], 'outbound': 'direct'},
                {'inbound': 'hy-in', 'action': 'reject'}]
        template = gen.client_config('8.8.8.8', 443, 51820, 'single' if role == 'single' else 'dual',
                        'original-password', ['cert'], '10.238.5', self.server_keys,
                        self.client_keys, self.key, '9.9.9.9')
        config_file = self.root / (role + '.json')
        template_file = self.root / (role + '-client.json')
        config_file.write_text(json.dumps(config))
        template_file.write_text(json.dumps(template))
        result = panel.PanelStore(config_file, template_file, self.root / (role + '-state.json'),
                 '/fake/sing-box', 'wg-hy2-' + role + '.service', role, control=self.control,
                 clock=lambda: self.now, validate=lambda _x: None,
                 keypair=lambda: {'PublicKey': self.key, 'PrivateKey': self.key}, public_key=lambda key: key)
        return result, config

    def test_single_users_expiry_and_renewal_preserve_original(self):
        store, original = self.store('single')
        user = store.add_user('alice', 1100)
        conf = json.loads(store.config.read_text())
        self.assertEqual(conf['endpoints'][0]['peers'][0], original['endpoints'][0]['peers'][0])
        self.assertEqual(len(conf['endpoints'][0]['peers']), 2)
        self.assertEqual(len(conf['inbounds'][0]['users']), 2)
        client = store.export_client(user['id'])
        self.assertEqual(client['endpoints'][0]['address'], ['10.238.5.3/32'])
        self.assertNotEqual(client['outbounds'][0]['password'], 'original-password')
        self.assertEqual(store.state.stat().st_mode & 0o777, 0o600)
        self.now = 1200
        self.assertEqual(store.expire_users(), 1)
        self.assertEqual(len(json.loads(store.config.read_text())['endpoints'][0]['peers']), 1)
        restarts = self.control.restarts
        self.assertEqual(store.expire_users(), 0)
        self.assertEqual(self.control.restarts, restarts)
        store.set_expiry(user['id'], 1500)
        store.enable_user(user['id'])
        self.assertEqual(len(json.loads(store.config.read_text())['endpoints'][0]['peers']), 2)

    def test_landing_reuses_hy2_but_has_distinct_peer(self):
        store, original = self.store('landing')
        user = store.add_user('bob', None)
        client = store.export_client(user['id'])
        self.assertEqual(client['outbounds'][0]['password'], 'original-password')
        self.assertEqual(len(json.loads(store.config.read_text())['endpoints'][0]['peers']), 2)
        store.disable_user(user['id'])
        self.assertEqual(json.loads(store.config.read_text())['endpoints'], original['endpoints'])
        with self.assertRaises(panel.PanelError):
            store.export_client(user['id'])

    def test_restart_failure_restores_previous_configuration(self):
        store, _ = self.store('single')
        old = store.config.read_bytes()
        self.control.fail_once = True
        with self.assertRaises(panel.PanelError):
            store.add_user('bob', None)
        self.assertEqual(store.config.read_bytes(), old)
        self.assertFalse(store.state.exists())
        self.assertFalse(store.journal.exists())
        self.assertEqual(self.control.restarts, 2)

    def test_external_changes_are_not_overwritten(self):
        store, _ = self.store('single')
        store.add_user('bob', None)
        changed = store.config.read_text() + ' '
        store.config.write_text(changed)
        with self.assertRaises(panel.PanelError):
            store.disable_user(store.list_users()[0]['id'])
        self.assertEqual(store.config.read_text(), changed)

    def test_stopped_service_not_started(self):
        store, _ = self.store('single')
        self.control.running = False
        store.add_user('bob', None)
        self.assertEqual(self.control.restarts, 0)

    def test_template_must_match_server_and_existing_peer(self):
        for role in ('single', 'landing'):
            for mutation in ('server_public', 'client_private', 'psk', 'port'):
                with self.subTest(role=role, mutation=mutation):
                    store, _ = self.store(role)
                    client = json.loads(store.template.read_text())
                    peer = client['endpoints'][0]['peers'][0]
                    if mutation == 'server_public':
                        peer['public_key'] = 'unrelated'
                    elif mutation == 'client_private':
                        client['endpoints'][0]['private_key'] = 'unrelated'
                    elif mutation == 'psk':
                        peer['pre_shared_key'] = 'unrelated'
                    else:
                        peer['port'] += 1
                    store.template.write_text(json.dumps(client))
                    with self.assertRaises(panel.PanelError):
                        store.add_user('new', None)
                    self.assertFalse(store.state.exists())

    def test_public_key_derivation_rfc7748_vector(self):
        private = bytes.fromhex('77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a')
        expected = bytes.fromhex('8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a')
        try:
            result = panel.wg_public_key(base64.b64encode(private).decode())
        except panel.PanelError:
            self.skipTest('OpenSSL with X25519 DER support is unavailable')
        self.assertEqual(result, base64.b64encode(expected).decode())

    def test_public_key_derivation_uses_der_over_stdin(self):
        private = bytes(range(32))
        expected = bytes(reversed(range(32)))
        prefix = bytes.fromhex('302a300506032b656e032100')
        with patch.object(panel.subprocess, 'run', return_value=SimpleNamespace(stdout=prefix + expected)) as run:
            result = panel.wg_public_key(base64.b64encode(private).decode())
        self.assertEqual(result, base64.b64encode(expected).decode())
        args, kwargs = run.call_args
        self.assertEqual(args[0], ['openssl', 'pkey', '-inform', 'DER', '-pubout', '-outform', 'DER'])
        self.assertEqual(kwargs['input'], bytes.fromhex('302e020100300506032b656e04220420') + private)

    def test_existing_peer_broad_subnet_rejected_before_allocating(self):
        store, _ = self.store('single')
        server = json.loads(store.config.read_text())
        server['endpoints'][0]['peers'].append({'allowed_ips': ['10.238.5.0/24'], 'public_key': self.key})
        store.config.write_text(json.dumps(server))
        with self.assertRaises(panel.PanelError):
            store.add_user('new', None)
        self.assertFalse(store.state.exists())

    def test_concurrent_additions_keep_both_users(self):
        import concurrent.futures
        store, _ = self.store('single')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda name: store.add_user(name), ('alice', 'bob')))
        self.assertEqual(len({row['id'] for row in results}), 2)
        self.assertEqual(len(json.loads(store.config.read_text())['endpoints'][0]['peers']), 3)
        self.assertEqual(len(store.list_users()), 2)


if __name__ == '__main__':
    unittest.main()
