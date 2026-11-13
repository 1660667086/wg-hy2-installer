#!/usr/bin/env python3
"""HTTPS-only administrative interface for the WG/HY2 panel.

The caller must explicitly supply trusted Host headers, a TLS certificate and
private key, a PBKDF2 password record, and the user-management backend.
"""
from __future__ import annotations

import base64
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import ssl
import threading
import time
from urllib.parse import urlsplit

from panel_backend import PanelError

COOKIE = '__Host-wghy2'
SESSION_SECONDS = 3600
MAX_BODY = 16384
ITERATIONS = 600000
USER_ID = re.compile(r'^[A-Za-z0-9_-]{1,80}$')


def password_record(password: str) -> str:
    if not isinstance(password, str) or len(password) < 12 or len(password) > 1024:
        raise ValueError('管理密码须为 12 到 1024 个字符')
    salt = secrets.token_bytes(24)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, ITERATIONS)
    return 'pbkdf2_sha256${}${}${}'.format(ITERATIONS, base64.b64encode(salt).decode(),
                                           base64.b64encode(digest).decode())


def valid_password(password: str, record: str) -> bool:
    try:
        algorithm, iterations, salt, expected = record.split('$')
        rounds = int(iterations)
        if algorithm != 'pbkdf2_sha256' or not 300000 <= rounds <= 2000000:
            return False
        salt = base64.b64decode(salt, validate=True)
        expected = base64.b64decode(expected, validate=True)
        if len(salt) < 16 or len(expected) != 32 or not isinstance(password, str) or len(password) > 1024:
            return False
        value = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, rounds)
        return hmac.compare_digest(value, expected)
    except (ValueError, TypeError, UnicodeError):
        return False


class PanelServer(ThreadingHTTPServer):
    """Threaded TLS server; `allowed_hosts` are exact public host:port values."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, manager, host, port, cert_path, key_path, password_hash, allowed_hosts):
        if not allowed_hosts or any(not re.fullmatch(r'[a-zA-Z0-9.\-]+(?::[0-9]{1,5})?', value)
                                    for value in allowed_hosts):
            raise ValueError('必须提供明确的 HTTPS Host 白名单')
        if not valid_password_record(password_hash):
            raise ValueError('无效的管理密码记录')
        self.manager = manager
        self.password_hash = password_hash
        self.allowed_hosts = frozenset(value.lower() for value in allowed_hosts)
        self._sessions = {}
        self._failures = {}
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(32)
        super().__init__((host, port), PanelHandler, bind_and_activate=False)
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(str(cert_path), str(key_path))
            self.socket = context.wrap_socket(self.socket, server_side=True, do_handshake_on_connect=False)
            self.server_bind()
            self.server_activate()
        except BaseException:
            self.server_close()
            raise

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(8)
        return sock, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        # Request content may contain credentials; do not emit it into logs.
        pass

    def session(self, token):
        now = time.monotonic()
        with self._lock:
            self._sessions = {k: v for k, v in self._sessions.items() if v[1] > now}
            return self._sessions.get(token)

    def create_session(self):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self._lock:
            now = time.monotonic()
            self._sessions = {k: v for k, v in self._sessions.items() if v[1] > now}
            if len(self._sessions) >= 128:
                raise RuntimeError('session limit')
            self._sessions[token] = (csrf, now + SESSION_SECONDS)
        return token, csrf

    def remove_session(self, token):
        with self._lock:
            self._sessions.pop(token, None)

    def admit_login(self, address):
        """Permit at most five attempts per address and twenty total per 15 min."""
        now = time.monotonic()
        with self._lock:
            self._failures = {k: [t for t in v if t > now - 900]
                              for k, v in self._failures.items() if any(t > now - 900 for t in v)}
            mine = self._failures.get(address, [])
            if len(mine) >= 5 or sum(map(len, self._failures.values())) >= 20:
                return False
            self._failures.setdefault(address, []).append(now)
            return True


def valid_password_record(value):
    try:
        algorithm, rounds, salt, digest = value.split('$')
        return (algorithm == 'pbkdf2_sha256' and 300000 <= int(rounds) <= 2000000
                and len(base64.b64decode(salt, validate=True)) >= 16
                and len(base64.b64decode(digest, validate=True)) == 32)
    except (ValueError, TypeError, AttributeError):
        return False


class PanelHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'WGPanel'
    sys_version = ''

    def log_message(self, *args):
        pass

    def _headers(self, length, content_type='application/json; charset=utf-8', extra=None):
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
                         "base-uri 'none'; form-action 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _json(self, status, value, extra=None):
        body = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        self.send_response(status)
        self._headers(len(body), extra=extra)
        self.wfile.write(body)

    def _reject(self, status, text):
        self.close_connection = True
        self._json(status, {'error': text})

    def _host_ok(self):
        hosts = self.headers.get_all('Host', [])
        return len(hosts) == 1 and hosts[0].lower() in self.server.allowed_hosts

    def _origin_ok(self):
        origins = self.headers.get_all('Origin', [])
        hosts = self.headers.get_all('Host', [])
        return len(origins) == len(hosts) == 1 and origins[0] == 'https://' + hosts[0]

    def _token(self):
        try:
            value = cookies.SimpleCookie()
            value.load(self.headers.get('Cookie', ''))
            return value[COOKIE].value if COOKIE in value else ''
        except cookies.CookieError:
            return ''

    def _authorize(self, modifying=False):
        session = self.server.session(self._token())
        if not session:
            self._reject(401, '请先登录')
            return False
        if modifying:
            tokens = self.headers.get_all('X-CSRF-Token', [])
            if (not self._origin_ok() or len(tokens) != 1
                    or not hmac.compare_digest(tokens[0], session[0])):
                self._reject(403, '请求来源或令牌无效')
                return False
        return True

    def _body(self):
        if self.headers.get('Transfer-Encoding'):
            raise ValueError('transfer encoding unsupported')
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,5}', lengths[0]):
            raise ValueError('invalid content length')
        length = int(lengths[0])
        if length > MAX_BODY or self.headers.get_content_type() != 'application/json':
            raise ValueError('invalid content')
        def unique(pairs):
            out = {}
            for key, value in pairs:
                if key in out:
                    raise ValueError('duplicate key')
                out[key] = value
            return out
        body = json.loads(self.rfile.read(length), object_pairs_hook=unique)
        if not isinstance(body, dict):
            raise ValueError('object required')
        return body

    def _path(self):
        if len(self.path) > 2048:
            return ''
        return urlsplit(self.path).path

    def do_GET(self):
        if not self._host_ok():
            return self._reject(400, 'Host 无效')
        path = self._path()
        if path == '/':
            try:
                body = (Path(__file__).resolve().parent / 'panel' / 'index.html').read_bytes()
            except OSError:
                return self._reject(503, '管理页面不可用')
            self.send_response(200)
            self._headers(len(body), 'text/html; charset=utf-8')
            self.wfile.write(body)
            return
        if path == '/api/session':
            session = self.server.session(self._token())
            return self._json(200, {'authenticated': bool(session), 'csrf': session[0] if session else None})
        if not self._authorize():
            return
        try:
            if path == '/api/users':
                return self._json(200, {'users': self.server.manager.list_users()})
            match = re.fullmatch(r'/api/users/([A-Za-z0-9_-]{1,80})/client', path)
            if match:
                return self._json(200, {'config': self.server.manager.export_client(match[1])})
            self._reject(404, '未找到')
        except PanelError as exc:
            self._reject(400, str(exc))
        except Exception:
            self._reject(400, '操作失败，请检查用户状态')

    def _mutate(self):
        if not self._host_ok():
            return self._reject(400, 'Host 无效')
        path = self._path()
        if path == '/api/login' and self.command == 'POST':
            if not self._origin_ok():
                return self._reject(403, '请求来源无效')
            if not self.server.admit_login(self.client_address[0]):
                return self._reject(429, '登录尝试过多，请稍后重试')
            try:
                data = self._body()
                if set(data) != {'password'} or not valid_password(data['password'], self.server.password_hash):
                    return self._reject(401, '密码错误')
                token, csrf = self.server.create_session()
                cookie = f'{COOKIE}={token}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}'
                return self._json(200, {'csrf': csrf}, {'Set-Cookie': cookie})
            except (ValueError, TypeError, RuntimeError):
                return self._reject(400, '登录请求无效')
        if not self._authorize(modifying=True):
            return
        try:
            data = self._body()
            manager = self.server.manager
            if path == '/api/logout' and self.command == 'POST' and not data:
                self.server.remove_session(self._token())
                return self._json(200, {'ok': True}, {'Set-Cookie': f'{COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0'})
            if path == '/api/users' and self.command == 'POST' and set(data) == {'name', 'expires_at'}:
                return self._json(200, manager.add_user(data['name'], data['expires_at']))
            match = re.fullmatch(r'/api/users/([A-Za-z0-9_-]{1,80})', path)
            if match and self.command == 'PATCH' and set(data) == {'expires_at'}:
                return self._json(200, manager.set_expiry(match[1], data['expires_at']))
            match = re.fullmatch(r'/api/users/([A-Za-z0-9_-]{1,80})/(disable|enable)', path)
            if match and self.command == 'POST' and not data:
                method = manager.disable_user if match[2] == 'disable' else manager.enable_user
                return self._json(200, method(match[1]))
            self._reject(404, '操作不存在')
        except PanelError as exc:
            self._reject(400, str(exc))
        except (ValueError, TypeError, KeyError, OSError):
            self._reject(400, '操作失败，请检查输入和服务状态')
        except Exception:
            self._reject(500, '操作失败，请检查服务状态')

    def do_POST(self):
        self._mutate()

    def do_PATCH(self):
        self._mutate()
