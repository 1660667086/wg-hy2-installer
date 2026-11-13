#!/usr/bin/env python3
"""Validate and privately unpack an untrusted relay transfer token."""
import argparse
import base64
import ipaddress
import json
import os
from pathlib import Path
import re
import sys

import wg_hy2_generate as gen
import wg_hy2_install as installer


def object_no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate field')
        out[key] = value
    return out


def unpack(source, destination):
    if source.stat().st_size > 256 * 1024:
        raise ValueError('token too large')
    raw = re.sub(rb'\s+', b'', source.read_bytes())
    decoded = base64.b64decode(raw, altchars=b'-_', validate=True)
    token = json.loads(decoded, object_pairs_hook=object_no_duplicates)
    if not isinstance(token, dict) or set(token) != {'schema', 'relay', 'client'} or type(token['schema']) is not int or token['schema'] != 1:
        raise ValueError('unsupported token schema')
    relay, client = token['relay'], token['client']
    if not isinstance(relay, dict) or set(relay) != {'manifest', 'server'} or not isinstance(client, dict):
        raise ValueError('invalid token structure')
    manifest = installer.parse_manifest(relay['manifest'])
    if manifest['role'] != 'relay':
        raise ValueError('token is not a relay role')
    server = relay['server']
    installer.check_config_shape(server, manifest)
    hy = server['inbounds'][0]
    if len(hy['users']) != 1 or set(hy['users'][0]) != {'name', 'password'} or hy['users'][0]['name'] != 'client':
        raise ValueError('invalid HY2 user')
    password = hy['users'][0]['password']
    cert, key = hy['tls']['certificate'], hy['tls']['key']
    rule = server['route']['rules'][0]
    expected_server = gen.server_common()
    expected_server['inbounds'] = [gen.hy_in(manifest['hy2_port'], password, cert, key)]
    expected_server['route'] = {'rules': [rule, {'action': 'reject'}]}
    if server != expected_server:
        raise ValueError('unsupported relay settings')

    # Reconstruct the exact generator-produced client profile; reject added
    # logging paths, endpoints, route actions, and extra listeners.
    hy_out = client['outbounds'][0]
    ep = client['endpoints'][0]
    peer = ep['peers'][0]
    network_addr = str(ipaddress.IPv4Interface(ep['address'][0]).ip)
    if not re.fullmatch(r'10\.238\.(?:[0-9]{1,3})\.2', network_addr):
        raise ValueError('invalid WG client address')
    network = network_addr.rsplit('.', 1)[0]
    entry = gen.public_ipv4(hy_out['server'])
    dns_ip = client['dns']['servers'][0]['server']
    if dns_ip not in ('1.1.1.1', '9.9.9.9', '8.8.8.8'):
        raise ValueError('unsupported DNS server')
    expected = gen.client_config(entry, manifest['hy2_port'], rule['override_port'], 'dual', password,
                                 cert, network, {'PublicKey': peer['public_key']},
                                 {'PrivateKey': ep['private_key']}, peer['pre_shared_key'], dns_ip)
    if client != expected:
        raise ValueError('client settings do not match relay or expected format')
    for value in (ep['private_key'], peer['public_key'], peer['pre_shared_key']):
        if not isinstance(value, str) or len(base64.b64decode(value, validate=True)) != 32:
            raise ValueError('invalid WG key')
    role = destination / 'relay'
    role.mkdir(mode=0o700)
    for path, value in ((role / 'manifest.json', relay['manifest']), (role / 'server.json', server),
                        (destination / 'client.json', client)):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write('\n')


if __name__ == '__main__':
    try:
        if len(sys.argv) != 3:
            raise ValueError('usage: import_relay_token.py TOKEN_FILE OUTPUT_DIR')
        unpack(Path(sys.argv[1]), Path(sys.argv[2]))
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OSError, argparse.ArgumentTypeError, installer.InstallError):
        print('对接码无效或无法保存，请确认来源和完整性。', file=sys.stderr)
        sys.exit(1)
