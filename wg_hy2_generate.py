#!/usr/bin/env python3
"""Create independent sing-box WireGuard-over-Hysteria2 deployment bundles.

The output contains private credentials. Generate on a trusted machine and
transfer each role only to its intended server over an authenticated channel.
"""
from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile

REQUIRED_TAGS = ('with_gvisor', 'with_quic', 'with_wireguard')
VERSION = '1.14.4'
ALIAS = '192.0.2.1'
BLOCKED_DESTINATIONS = ['0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10',
                        '127.0.0.0/8', '169.254.0.0/16', '172.16.0.0/12',
                        '192.168.0.0/16', '198.18.0.0/15', '224.0.0.0/4',
                        '240.0.0.0/4', '::1/128', 'fc00::/7', 'fe80::/10', 'ff00::/8']


def public_ipv4(text: str) -> str:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('请输入有效的 IPv4 地址') from exc
    if (not isinstance(ip, ipaddress.IPv4Address) or not ip.is_global or
            ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_link_local):
        raise argparse.ArgumentTypeError('必须填写实际服务器的公网 IPv4 地址')
    return str(ip)


def port_number(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('端口必须为整数') from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError('端口须在 1–65535 范围')
    return port


def run_checked(args: list[str], *, capture: bool = False) -> str:
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                            stderr=subprocess.PIPE, timeout=45, check=False)
    if result.returncode:
        # Avoid printing config contents or generated secrets.
        raise RuntimeError(f'{Path(args[0]).name} 操作失败（退出码 {result.returncode}）')
    return result.stdout if capture else ''


def verify_binary(binary: str) -> str:
    selected = shutil.which(binary) if '/' not in binary else binary
    if not selected or not os.access(selected, os.X_OK):
        raise ValueError('找不到可执行的 sing-box，请通过 --sing-box 指定')
    path = str(Path(selected).resolve(strict=True))
    version = run_checked([path, 'version'], capture=True)
    if not re.search(r'^sing-box version ' + re.escape(VERSION) + r'(?:\s|$)', version):
        raise ValueError(f'需要 sing-box {VERSION}')
    if not all(tag in version for tag in REQUIRED_TAGS):
        raise ValueError('sing-box 需要 with_gvisor、with_quic 和 with_wireguard 编译标签')
    return path


def keypair(binary: str) -> dict[str, str]:
    result = run_checked([binary, 'generate', 'wg-keypair'], capture=True)
    pairs = dict(re.findall(r'^(PrivateKey|PublicKey):\s*(\S+)\s*$', result, re.MULTILINE))
    if set(pairs) != {'PrivateKey', 'PublicKey'}:
        raise RuntimeError('生成 WireGuard 密钥失败')
    for value in pairs.values():
        if len(base64.b64decode(value, validate=True)) != 32:
            raise RuntimeError('生成了无效的 WireGuard 密钥')
    return pairs


def make_certificate(ip: str, temp: Path) -> tuple[list[str], list[str]]:
    openssl = shutil.which('openssl')
    if not openssl:
        raise ValueError('需要安装 openssl')
    cert, key = temp / 'cert.pem', temp / 'key.pem'
    run_checked([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '3650',
                 '-subj', '/CN=' + ip, '-addext', 'subjectAltName=IP:' + ip,
                 '-addext', 'extendedKeyUsage=serverAuth', '-keyout', str(key), '-out', str(cert)])
    os.chmod(key, 0o600)
    return cert.read_text().splitlines(), key.read_text().splitlines()


def write_json(path: Path, content: object) -> None:
    with path.open('x', encoding='utf-8') as f:
        json.dump(content, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.chmod(path, 0o600)


def server_common() -> dict:
    return {'log': {'level': 'warn', 'timestamp': True},
            'outbounds': [{'type': 'direct', 'tag': 'direct'}]}


def hy_in(port: int, password: str, cert: list[str], key: list[str]) -> dict:
    return {'type': 'hysteria2', 'tag': 'hy-in', 'listen': '0.0.0.0', 'listen_port': port,
            'users': [{'name': 'client', 'password': password}],
            'tls': {'enabled': True, 'certificate': cert, 'key': key}}


def wg_in(port: int, network: str, server_keys: dict, client_keys: dict, psk: str) -> dict:
    return {'type': 'wireguard', 'tag': 'wg-in', 'system': False, 'address': [network + '.1/32'],
            'listen_port': port, 'private_key': server_keys['PrivateKey'], 'peers': [{
                'public_key': client_keys['PublicKey'], 'pre_shared_key': psk,
                'allowed_ips': [network + '.2/32']}]}


def client_config(entry_ip: str, hy_port: int, wg_port: int, mode: str,
                  password: str, cert: list[str], network: str,
                  server_keys: dict, client_keys: dict, psk: str, dns_ip: str) -> dict:
    dns_name = {'1.1.1.1': 'cloudflare-dns.com', '9.9.9.9': 'dns.quad9.net',
                '8.8.8.8': 'dns.google'}[dns_ip]
    return {
        'log': {'level': 'warn', 'timestamp': True},
        'dns': {'servers': [{'type': 'https', 'tag': 'dns-remote', 'server': dns_ip,
                             'path': '/dns-query', 'tls': {'enabled': True, 'server_name': dns_name},
                             'detour': 'wg'}],
                'final': 'dns-remote', 'strategy': 'ipv4_only'},
        'inbounds': [{'type': 'tun', 'tag': 'tun-in', 'address': ['172.30.248.1/30'],
                      'auto_route': True, 'strict_route': False}],
        'outbounds': [
            {'type': 'hysteria2', 'tag': 'hy2', 'server': entry_ip, 'server_port': hy_port,
             'password': password, 'tls': {'enabled': True, 'server_name': entry_ip, 'certificate': cert}},
            {'type': 'direct', 'tag': 'direct'}],
        'endpoints': [{'type': 'wireguard', 'tag': 'wg', 'system': False,
                       'address': [network + '.2/32'], 'private_key': client_keys['PrivateKey'],
                       'peers': [{'address': '127.0.0.1' if mode == 'single' else ALIAS,
                                  'port': wg_port, 'public_key': server_keys['PublicKey'],
                                  'pre_shared_key': psk, 'allowed_ips': ['0.0.0.0/0'],
                                  'persistent_keepalive_interval': 25}], 'detour': 'hy2'}],
        'route': {'rules': [{'action': 'sniff'}, {'protocol': 'dns', 'action': 'hijack-dns'},
                            {'ip_is_private': True, 'outbound': 'direct'}],
                  'final': 'wg', 'auto_detect_interface': True},
    }


def validate_args(args: argparse.Namespace) -> None:
    if args.mode == 'dual' and not args.exit_ip:
        raise ValueError('双机模式必须指定 --exit-ip')
    if args.mode == 'single' and args.exit_ip:
        raise ValueError('单机模式不使用 --exit-ip')
    if args.mode == 'dual' and args.entry_ip == args.exit_ip:
        raise ValueError('双机模式的入口和落地必须是不同公网地址')
    if args.mode == 'single' and args.hy2_port == args.wg_port:
        raise ValueError('单机模式中 HY2 和 WG 端口不能相同')


def build(args: argparse.Namespace) -> None:
    validate_args(args)
    binary = verify_binary(args.sing_box)
    output = Path(args.output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('输出目录已经存在，拒绝覆盖')
    if not output.parent.is_dir():
        raise ValueError('输出目录的父目录不存在')
    # Stage in the same filesystem, retain secrets only in mode-0700 directories.
    tmp = Path(tempfile.mkdtemp(prefix='.wg-hy2-stage-', dir=output.parent))
    try:
        sk, ck = keypair(binary), keypair(binary)
        psk = base64.b64encode(secrets.token_bytes(32)).decode('ascii')
        password = secrets.token_urlsafe(32)
        network = '10.238.%d' % secrets.randbelow(256)
        cert, key = make_certificate(args.entry_ip, tmp)
        (tmp / 'cert.pem').unlink()
        (tmp / 'key.pem').unlink()
        client_dir = tmp / 'client'
        client_dir.mkdir(mode=0o700)
        dns_ip = next(ip for ip in ('1.1.1.1', '9.9.9.9', '8.8.8.8')
                      if ip not in (args.entry_ip, args.exit_ip))
        client = client_config(args.entry_ip, args.hy2_port, args.wg_port, args.mode,
                               password, cert, network, sk, ck, psk, dns_ip)
        write_json(client_dir / 'client.json', client)
        if args.mode == 'single':
            roles = [('single', None)]
            service = server_common()
            service['inbounds'] = [hy_in(args.hy2_port, password, cert, key)]
            service['endpoints'] = [wg_in(args.wg_port, network, sk, ck, psk)]
            service['route'] = {'rules': [
                {'inbound': 'hy-in', 'network': 'udp', 'ip_cidr': ['127.0.0.1/32'],
                 'port': [args.wg_port], 'outbound': 'direct'},
                {'inbound': 'hy-in', 'action': 'reject'},
                {'inbound': 'wg-in', 'ip_cidr': BLOCKED_DESTINATIONS, 'action': 'reject'},
                {'inbound': 'wg-in', 'outbound': 'direct'}, {'action': 'reject'}]}
            configs = {'single': service}
        else:
            roles = [('relay', None), ('landing', args.entry_ip)]
            relay = server_common()
            relay['inbounds'] = [hy_in(args.hy2_port, password, cert, key)]
            relay['route'] = {'rules': [
                {'inbound': 'hy-in', 'network': 'udp', 'ip_cidr': [ALIAS + '/32'],
                 'port': [args.wg_port], 'action': 'route', 'outbound': 'direct',
                 'override_address': args.exit_ip, 'override_port': args.wg_port},
                {'action': 'reject'}]}
            landing = server_common()
            landing['endpoints'] = [wg_in(args.wg_port, network, sk, ck, psk)]
            landing['route'] = {'rules': [
                {'inbound': 'wg-in', 'ip_cidr': BLOCKED_DESTINATIONS, 'action': 'reject'},
                {'inbound': 'wg-in', 'outbound': 'direct'}, {'action': 'reject'}]}
            configs = {'relay': relay, 'landing': landing}
            def has_exit(value: object) -> bool:
                if isinstance(value, str):
                    return value == args.exit_ip or value == args.exit_ip + '/32'
                if isinstance(value, dict):
                    return any(has_exit(v) for v in value.values())
                if isinstance(value, list):
                    return any(has_exit(v) for v in value)
                return False
            if has_exit(client):
                raise RuntimeError('客户端配置意外包含落地公网地址')
        for role, relay_ip in roles:
            role_dir = tmp / role
            role_dir.mkdir(mode=0o700)
            manifest = {'schema': 1, 'role': role,
                        'hy2_port': args.hy2_port if role in ('single', 'relay') else None,
                        'wg_port': args.wg_port if role in ('single', 'landing') else None,
                        'relay_ip': relay_ip}
            write_json(role_dir / 'server.json', configs[role])
            write_json(role_dir / 'manifest.json', manifest)
        # All files, including temporary validation files, are owner-only.
        for conf in [client_dir / 'client.json'] + [tmp / role / 'server.json' for role, _ in roles]:
            run_checked([binary, 'check', '-c', str(conf)])
        if output.exists() or output.is_symlink():
            raise ValueError('输出目录已经存在，拒绝覆盖')
        os.rename(tmp, output)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    print('已生成独立部署包：' + str(output))
    print('该目录包含私钥和连接凭据，请使用安全方式分别传给对应服务器。')
    if args.mode == 'dual':
        print('先安装 landing，再安装 relay；客户端配置不包含落地公网 IP。')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('single', 'dual'), required=True)
    parser.add_argument('--entry-ip', type=public_ipv4, required=True,
                        help='单机服务器或双机中转机的公网 IPv4')
    parser.add_argument('--exit-ip', type=public_ipv4,
                        help='双机落地服务器的公网 IPv4')
    parser.add_argument('--hy2-port', type=port_number, default=443, help='HY2 UDP 端口（默认 443）')
    parser.add_argument('--wg-port', type=port_number, default=51820, help='WG UDP 端口（默认 51820）')
    parser.add_argument('--sing-box', help='本机 sing-box 1.14.4 可执行文件路径')
    parser.add_argument('--output', help='新建输出目录，不能已经存在')
    parser.add_argument('--check-only', action='store_true',
                        help='只检查地址和端口参数，不需要 sing-box，也不生成文件')
    args = parser.parse_args()
    if not args.check_only and (not args.sing_box or not args.output):
        parser.error('生成配置需要 --sing-box 和 --output')
    os.umask(0o077)
    try:
        if args.check_only:
            validate_args(args)
            return
        build(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print('错误：' + str(exc), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
