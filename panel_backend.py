#!/usr/bin/env python3
"""Private multi-user state for the project's single or landing WG service.

Call expire_users periodically from a service independent of the web UI.
Client profiles contain private keys. Never expose them to logs or URLs.
"""
from __future__ import annotations

import base64
import copy
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
import uuid

import wg_hy2_generate as gen


class PanelError(Exception):
    pass


class ServiceControl:
    def active(self, name):
        result = subprocess.run(['systemctl', 'is-active', '--quiet', name], timeout=20)
        if result.returncode == 0:
            return True
        if result.returncode == 3:
            return False
        raise PanelError('无法确认服务状态')

    def restart(self, name):
        subprocess.run(['systemctl', 'restart', name], check=True, timeout=45,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not self.active(name):
            raise PanelError('服务未能启动')


def atomic(path, content):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.panel-write-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def encode(obj):
    return (json.dumps(obj, ensure_ascii=False, indent=2) + '\n').encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def valid_expiry(value):
    if value is not None and (type(value) is not int or value < 0 or value > 253402300799):
        raise PanelError('到期时间应为 UTC Unix 秒或空值')
    return value


def wg_public_key(private):
    """Derive X25519 public key without exposing private bytes in argv or logs."""
    try:
        raw = base64.b64decode(private, validate=True)
        if len(raw) != 32:
            raise ValueError('invalid key size')
        # RFC 8410 PKCS#8 wrapper, followed by the raw 32-byte X25519 key.
        encoded = bytes.fromhex('302e020100300506032b656e04220420') + raw
        proc = subprocess.run(['openssl', 'pkey', '-inform', 'DER', '-pubout', '-outform', 'DER'],
                              input=encoded, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=15, check=True)
        prefix = bytes.fromhex('302a300506032b656e032100')
        if not proc.stdout.startswith(prefix) or len(proc.stdout) != len(prefix) + 32:
            raise ValueError('invalid public key encoding')
        return base64.b64encode(proc.stdout[len(prefix):]).decode('ascii')
    except (ValueError, TypeError, subprocess.SubprocessError, OSError) as exc:
        raise PanelError('无法推导 WireGuard 公钥，请检查密钥及 OpenSSL X25519 支持') from exc


class PanelStore:
    """All file paths must be selected by the trusted caller, not HTTP input."""
    def __init__(self, config_path, template_path, state_path, binary, service_name, role,
                 control=None, clock=time.time, validate=None, keypair=None, public_key=None):
        if role not in ('single', 'landing'):
            raise PanelError('只支持单机或落地服务')
        if not re.fullmatch(r'wg-hy2-[A-Za-z0-9_-]+\.service', service_name):
            raise PanelError('服务名称无效')
        self.config = Path(config_path)
        self.template = Path(template_path)
        self.state = Path(state_path)
        self.binary = str(binary)
        self.service = service_name
        self.role = role
        self.control = control or ServiceControl()
        self.clock = clock
        self.validate = validate or self._validate
        self.keypair = keypair or (lambda: gen.keypair(self.binary))
        self.public_key = public_key or wg_public_key
        self.lockfile = self.state.with_name(self.state.name + '.lock')
        self.journal = self.state.with_name(self.state.name + '.pending')

    def _validate(self, config):
        with tempfile.TemporaryDirectory(prefix='wg-hy2-panel-check-') as tmp:
            path = Path(tmp) / 'server.json'
            atomic(path, encode(config))
            try:
                subprocess.run([self.binary, 'check', '-c', str(path)], timeout=30,
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (subprocess.SubprocessError, OSError) as exc:
                raise PanelError('sing-box 配置校验失败') from exc

    def _load(self):
        if self.journal.exists():
            raise PanelError('存在未完成的事务，请先人工恢复备份再继续')
        raw = self.config.read_bytes()
        config = json.loads(raw)
        if self.state.exists():
            state = json.loads(self.state.read_text())
            if state.get('schema') != 1 or state.get('role') != self.role or digest(raw) != state.get('server_hash'):
                raise PanelError('服务配置与面板记录不一致，拒绝覆盖')
            return state, raw
        template = json.loads(self.template.read_text())
        try:
            ep = config['endpoints'][0]
            client = template['endpoints'][0]
            if (len(config['endpoints']) != 1 or ep['type'] != 'wireguard' or ep['tag'] != 'wg-in'
                    or client['type'] != 'wireguard' or not isinstance(ep['peers'], list)
                    or len(client['peers']) != 1):
                raise ValueError()
            server_interface = ipaddress.IPv4Interface(ep['address'][0])
            client_interface = ipaddress.IPv4Interface(client['address'][0])
            server_addr, client_addr = server_interface.ip, client_interface.ip
            if (len(ep['address']) != 1 or len(client['address']) != 1
                    or server_interface.network.prefixlen != 32
                    or client_interface.network.prefixlen != 32
                    or server_addr.packed[-1] != 1 or ep.get('system') is not False):
                raise ValueError()
            if str(server_addr).rsplit('.', 1)[0] != str(client_addr).rsplit('.', 1)[0]:
                raise ValueError()
            used_addresses = set()
            for existing in ep['peers']:
                for allowed in existing['allowed_ips']:
                    net = ipaddress.ip_network(allowed, strict=True)
                    if (not isinstance(net, ipaddress.IPv4Network) or net.prefixlen != 32
                            or str(net.network_address).rsplit('.', 1)[0] != str(server_addr).rsplit('.', 1)[0]
                            or net.network_address == server_addr or net.network_address in used_addresses):
                        raise ValueError()
                    used_addresses.add(net.network_address)
            client_peer = client['peers'][0]
            if (client_peer['public_key'] != self.public_key(ep['private_key'])
                    or client_peer['port'] != ep['listen_port']):
                raise ValueError()
            matching = [peer for peer in ep['peers']
                        if peer.get('public_key') == self.public_key(client['private_key'])
                        and peer.get('allowed_ips') == [f'{client_addr}/32']
                        and peer.get('pre_shared_key') == client_peer.get('pre_shared_key')]
            if len(matching) != 1:
                raise ValueError()
            if self.role == 'single':
                hy = config['inbounds'][0]
                if (len(config['inbounds']) != 1 or hy['type'] != 'hysteria2' or hy['tag'] != 'hy-in'
                        or template['outbounds'][0]['password'] not in [u['password'] for u in hy['users']]
                        or template['outbounds'][0]['server_port'] != hy['listen_port']
                        or template['outbounds'][0]['tls']['certificate'] != hy['tls']['certificate']):
                    raise ValueError()
            elif config.get('inbounds'):
                raise ValueError()
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise PanelError('不支持的服务配置或客户端模板') from exc
        return {'schema': 1, 'role': self.role, 'server_hash': digest(raw),
                'base': config, 'template': template, 'users': []}, raw

    def _locked(self, action, write=False):
        self.state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.lockfile, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state, original = self._load()
            before = encode(state)
            result = action(state)
            if write and encode(state) != before:
                self._commit(state, original)
            return result

    def _commit(self, state, original):
        config = copy.deepcopy(state['base'])
        for user in state['users']:
            if user['enabled'] and (user['expires_at'] is None or user['expires_at'] > int(self.clock())):
                config['endpoints'][0]['peers'].append(user['peer'])
                if self.role == 'single':
                    config['inbounds'][0]['users'].append(user['hy_user'])
        self.validate(config)
        new_config = encode(config)
        state['server_hash'] = digest(new_config)
        old_state = self.state.read_bytes() if self.state.exists() else None
        was_active = self.control.active(self.service)
        pending = {'schema': 1, 'old_config': base64.b64encode(original).decode(),
                   'old_state': base64.b64encode(old_state).decode() if old_state is not None else None,
                   'was_active': was_active}
        atomic(self.journal, encode(pending))
        try:
            atomic(self.config, new_config)
            atomic(self.state, encode(state))
            if was_active:
                self.control.restart(self.service)
        except BaseException as exc:
            try:
                atomic(self.config, original)
                if old_state is None:
                    self.state.unlink(missing_ok=True)
                else:
                    atomic(self.state, old_state)
                if was_active:
                    self.control.restart(self.service)
                self.journal.unlink()
            except BaseException as recovery:
                raise PanelError('更新及回滚失败；保留事务文件，请人工检查') from recovery
            raise PanelError('更新失败，已恢复原配置') from exc
        self.journal.unlink()

    @staticmethod
    def _public(user, now):
        return {'id': user['id'], 'name': user['name'], 'expires_at': user['expires_at'],
                'enabled': user['enabled'],
                'expired': user['expires_at'] is not None and user['expires_at'] <= now}

    def list_users(self):
        return self._locked(lambda s: [self._public(u, int(self.clock())) for u in s['users']])

    def add_user(self, name, expires_at=None):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64 or any(ord(c) < 32 for c in name):
            raise PanelError('用户名应为 1–64 个可打印字符')
        valid_expiry(expires_at)
        if expires_at is not None and expires_at <= int(self.clock()):
            raise PanelError('到期时间必须晚于当前时间')
        def add(s):
            if any(u['name'] == name.strip() for u in s['users']):
                raise PanelError('用户名已存在')
            prefix = str(ipaddress.IPv4Interface(s['base']['endpoints'][0]['address'][0]).ip).rsplit('.', 1)[0]
            used = {str(ipaddress.ip_network(a, strict=False).network_address)
                    for p in s['base']['endpoints'][0]['peers'] for a in p['allowed_ips']}
            used.update(u['address'] for u in s['users'])
            address = next((f'{prefix}.{i}' for i in range(2, 255) if f'{prefix}.{i}' not in used), None)
            if address is None:
                raise PanelError('用户地址池已满')
            keys = self.keypair()
            psk = base64.b64encode(secrets.token_bytes(32)).decode()
            ident = uuid.uuid4().hex
            client = copy.deepcopy(s['template'])
            client['endpoints'][0]['address'] = [address + '/32']
            client['endpoints'][0]['private_key'] = keys['PrivateKey']
            client['endpoints'][0]['peers'][0]['pre_shared_key'] = psk
            hy_user = None
            if self.role == 'single':
                hy_user = {'name': 'panel-' + ident, 'password': secrets.token_urlsafe(32)}
                client['outbounds'][0]['password'] = hy_user['password']
            user = {'id': ident, 'name': name.strip(), 'expires_at': expires_at, 'enabled': True,
                    'address': address, 'peer': {'public_key': keys['PublicKey'], 'pre_shared_key': psk,
                                                'allowed_ips': [address + '/32']},
                    'hy_user': hy_user, 'client': client}
            s['users'].append(user)
            return self._public(user, int(self.clock()))
        return self._locked(add, True)

    def _find(self, state, ident):
        for user in state['users']:
            if user['id'] == ident:
                return user
        raise PanelError('用户不存在')

    def set_expiry(self, ident, expires_at):
        valid_expiry(expires_at)
        def update(s):
            user = self._find(s, ident)
            user['expires_at'] = expires_at
            return self._public(user, int(self.clock()))
        return self._locked(update, True)

    def disable_user(self, ident):
        def disable(s):
            user = self._find(s, ident)
            user['enabled'] = False
            return self._public(user, int(self.clock()))
        return self._locked(disable, True)

    def enable_user(self, ident):
        def enable(s):
            user = self._find(s, ident)
            if user['expires_at'] is not None and user['expires_at'] <= int(self.clock()):
                raise PanelError('请先将到期时间调整到未来')
            user['enabled'] = True
            return self._public(user, int(self.clock()))
        return self._locked(enable, True)

    def expire_users(self):
        """Reconcile expired users even if no HTTP requests arrive."""
        def expire(s):
            now = int(self.clock())
            changed = 0
            for user in s['users']:
                if user['enabled'] and user['expires_at'] is not None and user['expires_at'] <= now:
                    user['enabled'] = False
                    changed += 1
            return changed
        return self._locked(expire, True)

    def export_client(self, ident):
        def export(s):
            user = self._find(s, ident)
            if not user['enabled'] or (user['expires_at'] is not None and user['expires_at'] <= int(self.clock())):
                raise PanelError('用户已停用或到期')
            return copy.deepcopy(user['client'])
        return self._locked(export)
