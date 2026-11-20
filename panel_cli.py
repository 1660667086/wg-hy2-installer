#!/usr/bin/env python3
"""Install and run a local WG/HY2 administration panel and expiry timer.

Setup is interactive and never starts or enables the managed WG service.
"""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

from panel_backend import PanelError, PanelStore
from panel_server import PanelServer, password_record

ROOT = Path('/opt/wg-hy2-panel')
UNIT_DIR = Path('/etc/systemd/system')
PANEL_UNIT = 'wg-hy2-panel.service'
TIMER_UNIT = 'wg-hy2-panel-expire.timer'
EXPIRE_UNIT = 'wg-hy2-panel-expire.service'
FILES = ('panel_cli.py', 'panel_backend.py', 'panel_server.py', 'wg_hy2_generate.py')


def run(*args, capture=False, check=True):
    return subprocess.run(args, check=check, text=True, timeout=45,
                          stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                          stderr=subprocess.PIPE if capture else subprocess.DEVNULL)


def write_new(path, content, mode):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, mode)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def parse_ip(value):
    try:
        addr = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise ValueError('请输入公网 IPv4 地址') from exc
    if not addr.is_global or addr.is_multicast or addr.is_unspecified:
        raise ValueError('请输入公网单播 IPv4 地址')
    return str(addr)


def parse_port(value):
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError('端口必须为 1–65535') from exc
    if not 1 <= port <= 65535:
        raise ValueError('端口必须为 1–65535')
    return port


def paths(role, template=None):
    if role not in ('single', 'landing'):
        raise ValueError('只支持 single 或 landing')
    service = 'wg-hy2-' + role + '.service'
    config = Path('/opt/wg-hy2-' + role + '/server.json')
    binary = Path('/opt/wg-hy2-' + role + '/sing-box')
    default_template = (Path('/root/wg-hy2-single/bundle/client/client.json') if role == 'single'
                        else Path('/root/wg-hy2-landing-handoff/bundle/client/client.json'))
    template_path = Path(template) if template else default_template
    if not template_path.is_absolute():
        raise ValueError('客户端模板路径必须是绝对路径')
    return config, binary, template_path, service


def ask_password(attempts=3):
    """Prompt until a valid, confirmed password is entered, instead of
    abandoning the whole setup on the first typo."""
    problem = '未设置密码'
    for remaining in range(attempts, 0, -1):
        password = getpass.getpass('设置面板密码（至少 12 位）：')
        confirm = getpass.getpass('再次输入面板密码：')
        if password != confirm:
            problem = '两次密码不一致'
        else:
            try:
                return password_record(password)
            except ValueError as exc:
                problem = str(exc)
        if remaining > 1:
            print(problem + '，请重新输入。', file=sys.stderr)
    raise ValueError(problem)


def firewall_hint(port):
    """The panel adds no firewall rule; say how to open its port where a host
    firewall would otherwise silently drop the HTTPS connection."""
    try:
        if run('systemctl', 'is-active', '--quiet', 'firewalld', check=False).returncode == 0:
            return ('检测到 firewalld，还需放行面板端口：'
                    f'firewall-cmd --permanent --add-port={port}/tcp && firewall-cmd --reload')
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        ufw_conf = Path('/etc/ufw/ufw.conf').read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None
    if shutil.which('ufw') and any(line.strip() == 'ENABLED=yes' for line in ufw_conf.splitlines()):
        return f'检测到 ufw 已启用，还需放行面板端口：ufw allow {port}/tcp'
    return None


def create_store(cfg):
    role = cfg['role']
    config, binary, template, service = paths(role, cfg['template'])
    return PanelStore(config, template, ROOT / 'users.json', binary, service, role)


def unit_texts():
    return {
        PANEL_UNIT: f'''[Unit]
Description=WG HY2 management panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/env python3 {ROOT}/panel_cli.py serve
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
UMask=0077

[Install]
WantedBy=multi-user.target
''',
        EXPIRE_UNIT: f'''[Unit]
Description=WG HY2 expiry reconciliation
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/bin/env python3 {ROOT}/panel_cli.py expire
NoNewPrivileges=true
UMask=0077
''',
        TIMER_UNIT: '''[Unit]
Description=Periodic WG HY2 expiry reconciliation

[Timer]
OnBootSec=10s
OnUnitActiveSec=30s
AccuracySec=1s
Unit=wg-hy2-panel-expire.service

[Install]
WantedBy=timers.target
''',
    }


def setup(args):
    if os.geteuid() != 0 or sys.platform != 'linux':
        raise ValueError('请在 Linux 服务器上以 root 运行')
    for tool in ('systemctl', 'openssl'):
        if not shutil.which(tool):
            raise ValueError('缺少 ' + tool)
    if not Path('/run/systemd/system').exists():
        raise ValueError('需要 systemd')
    config, binary, template, service = paths(args.role, args.template)
    for item in (config, binary, template):
        if not item.is_file():
            raise ValueError('缺少必要文件：' + str(item))
    if not os.access(binary, os.X_OK):
        raise ValueError('sing-box 不可执行')
    run(str(binary), 'check', '-c', str(config))
    run(str(binary), 'check', '-c', str(template))
    public_ip, port = parse_ip(args.public_ip), parse_port(args.port)
    if os.path.lexists(ROOT):
        raise ValueError('面板目录已存在，拒绝覆盖：' + str(ROOT))
    for unit in unit_texts():
        if os.path.lexists(UNIT_DIR / unit):
            raise ValueError('面板服务已存在：' + unit)
        info = run('systemctl', 'show', unit, '-p', 'LoadState', '--value', capture=True, check=False)
        if info.returncode != 0 or info.stdout.strip() != 'not-found':
            raise ValueError('无法确认服务名称可用：' + unit)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('0.0.0.0', port))

    record = ask_password()

    made_units = []
    made_dir = False
    touched_systemd = False
    source = Path(__file__).resolve().parent
    try:
        ROOT.mkdir(mode=0o700)
        made_dir = True
        for name in FILES:
            shutil.copyfile(source / name, ROOT / name)
            os.chmod(ROOT / name, 0o600)
        (ROOT / 'panel').mkdir(mode=0o700)
        shutil.copyfile(source / 'panel/index.html', ROOT / 'panel/index.html')
        os.chmod(ROOT / 'panel/index.html', 0o600)
        cfg = {'role': args.role, 'template': str(template), 'public_ip': public_ip,
               'port': port, 'password_hash': record}
        write_new(ROOT / 'settings.json', json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', 0o600)
        run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '365',
            '-subj', '/CN=' + public_ip, '-addext', 'subjectAltName=IP:' + public_ip,
            '-addext', 'extendedKeyUsage=serverAuth', '-keyout', str(ROOT / 'key.pem'),
            '-out', str(ROOT / 'cert.pem'))
        os.chmod(ROOT / 'key.pem', 0o600)
        os.chmod(ROOT / 'cert.pem', 0o600)
        # Validate the existing managed configuration and template before
        # exposing the panel or creating any systemd units.
        try:
            create_store(cfg).list_users()
        except PanelError as exc:
            raise ValueError('客户端模板与服务不匹配，或 OpenSSL 不支持 X25519；需要 OpenSSL 1.1.1 或更新版本') from exc
        for unit, text in unit_texts().items():
            write_new(UNIT_DIR / unit, text, 0o644)
            made_units.append(unit)
        run('systemctl', 'daemon-reload')
        touched_systemd = True
        run('systemctl', 'enable', '--now', TIMER_UNIT)
        run('systemctl', 'start', PANEL_UNIT)
        run('systemctl', 'is-active', '--quiet', PANEL_UNIT)
        if args.enable_panel:
            run('systemctl', 'enable', PANEL_UNIT)
    except BaseException:
        if touched_systemd:
            run('systemctl', 'disable', '--now', TIMER_UNIT, check=False)
            run('systemctl', 'disable', '--now', PANEL_UNIT, check=False)
            for unit in (PANEL_UNIT, EXPIRE_UNIT):
                result = run('systemctl', 'is-active', '--quiet', unit, check=False)
                if result.returncode != 3:
                    raise PanelError('安装回滚时无法确认面板已停止；保留文件，请人工检查')
        for unit in reversed(made_units):
            (UNIT_DIR / unit).unlink(missing_ok=True)
        if made_units:
            run('systemctl', 'daemon-reload', check=False)
        if made_dir:
            shutil.rmtree(ROOT)
        raise
    fingerprint = run('openssl', 'x509', '-in', str(ROOT / 'cert.pem'), '-noout',
                      '-fingerprint', '-sha256', capture=True).stdout.strip()
    print(f'面板地址：https://{public_ip}:{port}/')
    print('证书 ' + fingerprint)
    print('请从可信 SSH 通道核对证书指纹；防火墙和云安全组需允许面板 TCP 端口。')
    hint = firewall_hint(port)
    if hint:
        print(hint)
    print('到期检查每 30 秒运行一次；面板默认不开机自启。' if not args.enable_panel
          else '到期检查每 30 秒运行一次；面板已设置开机自启。')


def load_settings():
    cfg = json.loads((ROOT / 'settings.json').read_text(encoding='utf-8'))
    if set(cfg) != {'role', 'template', 'public_ip', 'port', 'password_hash'}:
        raise ValueError('面板设置文件无效')
    cfg['public_ip'] = parse_ip(cfg['public_ip'])
    cfg['port'] = parse_port(str(cfg['port']))
    return cfg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    setup_parser = sub.add_parser('setup')
    setup_parser.add_argument('--role', choices=('single', 'landing'), required=True)
    setup_parser.add_argument('--public-ip', required=True)
    setup_parser.add_argument('--port', default='8443')
    setup_parser.add_argument('--template', help='可信客户端模板绝对路径（双机自动部署可选）')
    setup_parser.add_argument('--enable-panel', action='store_true')
    sub.add_parser('serve')
    sub.add_parser('expire')
    args = parser.parse_args()
    try:
        if args.command == 'setup':
            setup(args)
        else:
            if os.geteuid() != 0:
                raise ValueError('请以 root 运行')
            cfg = load_settings()
            manager = create_store(cfg)
            manager.expire_users()
            if args.command == 'serve':
                host = cfg['public_ip'] + ':' + str(cfg['port'])
                if cfg['port'] == 443:
                    host = cfg['public_ip']
                with PanelServer(manager, '0.0.0.0', cfg['port'], ROOT / 'cert.pem', ROOT / 'key.pem',
                                 cfg['password_hash'], [host]) as server:
                    server.serve_forever()
    except (ValueError, OSError, subprocess.SubprocessError, PanelError) as exc:
        print('面板错误：' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
