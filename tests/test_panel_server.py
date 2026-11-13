"""Exercise HTTPS authentication and request boundaries without touching services."""
import http.client
import json
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import panel_server
from panel_backend import PanelError


class FakeManager:
    def __init__(self):
        self.changed = []

    def list_users(self):
        return [{'id': 'a', 'name': '测试', 'expires_at': None, 'enabled': True}]

    def add_user(self, name, expiry):
        self.changed.append((name, expiry))
        return {'ok': True}

    def export_client(self, user_id):
        return {'tag': user_id}

    def enable_user(self, user_id):
        raise PanelError('请先将到期时间调整到未来')


class PanelHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='wg-panel-test-')
        path = Path(cls.tmp.name)
        cls.cert, cls.key = path / 'cert.pem', path / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=127.0.0.1', '-keyout', str(cls.key), '-out', str(cls.cert)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        cls.manager = FakeManager()
        cls.server = panel_server.PanelServer(cls.manager, '127.0.0.1', 0, cls.cert, cls.key,
                                               panel_server.password_record('correct horse battery'), ['127.0.0.1'])
        cls.port = cls.server.server_port
        cls.host = f'127.0.0.1:{cls.port}'
        cls.origin = f'https://{cls.host}'
        cls.server.allowed_hosts = frozenset([cls.host])
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.tmp.cleanup()

    def request(self, method, path, body=None, **headers):
        connection = http.client.HTTPSConnection('127.0.0.1', self.port,
                                                context=ssl._create_unverified_context(), timeout=5)
        try:
            if body is not None:
                body = json.dumps(body).encode()
                headers.setdefault('Content-Type', 'application/json')
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            connection.close()

    def login(self):
        status, headers, data = self.request('POST', '/api/login',
                                             {'password': 'correct horse battery'}, Origin=self.origin)
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie']
        self.assertIn('Secure', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        return cookie.split(';', 1)[0], data['csrf']

    def test_authentication_origin_csrf_and_host(self):
        self.assertEqual(self.request('GET', '/api/users')[0], 401)
        self.assertEqual(self.request('GET', '/api/session', Host='evil.invalid')[0], 400)
        self.assertEqual(self.request('POST', '/api/login', {'password': 'x'},
                                      Origin='https://evil.invalid')[0], 403)
        cookie, csrf = self.login()
        self.assertEqual(self.request('GET', '/api/users', Cookie=cookie)[2]['users'][0]['name'], '测试')
        self.assertEqual(self.request('POST', '/api/users', {'name': 'n', 'expires_at': None},
                                      Cookie=cookie, Origin=self.origin)[0], 403)
        self.assertEqual(self.request('POST', '/api/users', {'name': 'n', 'expires_at': None},
                                      Cookie=cookie, Origin='https://evil.invalid',
                                      **{'X-CSRF-Token': csrf})[0], 403)
        self.assertEqual(self.request('POST', '/api/users', {'name': 'n', 'expires_at': None},
                                      Cookie=cookie, Origin=self.origin,
                                      **{'X-CSRF-Token': csrf})[0], 200)
        self.assertEqual(self.manager.changed[-1], ('n', None))
        self.assertEqual(self.request('GET', '/api/users/a/client', Cookie=cookie)[2], {'config': {'tag': 'a'}})
        status, _, error = self.request('POST', '/api/users/a/enable', {}, Cookie=cookie, Origin=self.origin,
                                        **{'X-CSRF-Token': csrf})
        self.assertEqual(status, 400)
        self.assertEqual(error['error'], '请先将到期时间调整到未来')
        self.assertEqual(self.request('POST', '/api/logout', {}, Cookie=cookie, Origin=self.origin,
                                      **{'X-CSRF-Token': csrf})[0], 200)
        self.assertEqual(self.request('GET', '/api/users', Cookie=cookie)[0], 401)

    def test_password_record_validation(self):
        record = panel_server.password_record('a sufficiently long password')
        self.assertTrue(panel_server.valid_password('a sufficiently long password', record))
        self.assertFalse(panel_server.valid_password('wrong password', record))
        self.assertFalse(panel_server.valid_password_record('invalid'))


if __name__ == '__main__':
    unittest.main()
