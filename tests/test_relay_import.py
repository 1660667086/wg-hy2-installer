"""Transfer validation with synthetic credentials; no services are started."""
import base64
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import import_relay_token as imp
import wg_hy2_generate as gen


class RelayImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        key = base64.b64encode(bytes(32)).decode()
        cert, private = ['test-cert'], ['test-private']
        server = gen.server_common()
        server['inbounds'] = [gen.hy_in(443, 'test-password', cert, private)]
        server['route'] = {'rules': [{'inbound': 'hy-in', 'network': 'udp',
            'ip_cidr': ['192.0.2.1/32'], 'port': [51820], 'action': 'route',
            'outbound': 'direct', 'override_address': '1.1.1.1', 'override_port': 51820},
            {'action': 'reject'}]}
        self.token = {'schema': 1, 'relay': {'server': server, 'manifest': {
            'schema': 1, 'role': 'relay', 'hy2_port': 443, 'wg_port': None, 'relay_ip': None}},
            'client': gen.client_config('8.8.8.8', 443, 51820, 'dual', 'test-password', cert,
                    '10.238.5', {'PublicKey': key}, {'PrivateKey': key}, key, '9.9.9.9')}

    def run_unpack(self, payload):
        token_path = self.root / 'token'
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        token_path.write_text('\n'.join(encoded[i:i+76] for i in range(0, len(encoded), 76)))
        output = self.root / 'out'
        output.mkdir()
        imp.unpack(token_path, output)
        return output

    def test_multiline_valid_token_and_private_files(self):
        output = self.run_unpack(self.token)
        self.assertEqual(json.loads((output / 'client.json').read_text()), self.token['client'])
        for path in (output / 'client.json', output / 'relay/server.json', output / 'relay/manifest.json'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_reject_unexpected_client_route(self):
        bad = copy.deepcopy(self.token)
        bad['client']['route']['final'] = 'direct'
        with self.assertRaises(ValueError):
            self.run_unpack(bad)

    def test_reject_extra_relay_inbound(self):
        bad = copy.deepcopy(self.token)
        bad['relay']['server']['inbounds'].append({'type': 'mixed', 'listen_port': 1080})
        with self.assertRaises(Exception):
            self.run_unpack(bad)


if __name__ == '__main__':
    unittest.main()
