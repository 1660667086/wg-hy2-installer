"""Fake commands for exercising the shell installers without servers.

ssh runs the "remote" command locally, scp copies locally, sudo just runs
the command, and sing-box answers version/keypair/check like the real one.
The installer stages record their arguments instead of touching systemd.
Every invocation is appended to the log file named by FAKE_LOG.
"""
import os
from pathlib import Path
import shutil
import stat

ROOT = Path(__file__).resolve().parents[1]

FAKE_SING_BOX = r'''#!/usr/bin/env python3
import base64, os, sys
args = sys.argv[1:]
if args == ['version']:
    print('sing-box version 1.14.4\n\nEnvironment: go1.24.0 linux/amd64\n'
          'Tags: with_gvisor,with_quic,with_wireguard,with_utls\nCGO: disabled')
elif args[:2] == ['generate', 'wg-keypair']:
    key = lambda: base64.b64encode(os.urandom(32)).decode()
    print('PrivateKey: ' + key())
    print('PublicKey: ' + key())
elif args[:1] != ['check']:
    sys.exit(2)
'''

FAKE_SSH = r'''#!/usr/bin/env bash
set -u
while (( $# )); do
    case $1 in
        -o|-p|-i) shift 2 ;;
        -*) shift ;;
        *) break ;;
    esac
done
target=$1
shift
printf 'ssh\t%s\t%s\n' "$target" "$*" >> "$FAKE_LOG"
if [[ -n ${FAKE_SSH_FAIL:-} ]]; then
    echo 'Host key verification failed.' >&2
    exit 255
fi
exec bash -c "$*"
'''

FAKE_SCP = r'''#!/usr/bin/env bash
set -eu
while (( $# )); do
    case $1 in
        -o|-P|-i) shift 2 ;;
        -*) shift ;;
        *) break ;;
    esac
done
destination=${*: -1}
sources=("${@:1:$#-1}")
printf 'scp\t%s\t%s\n' "${destination%%:*}" "${sources[*]##*/}" >> "$FAKE_LOG"
cp -R -- "${sources[@]}" "${destination#*:}"
'''

FAKE_SUDO = r'''#!/usr/bin/env bash
[[ ${1-} == -n ]] && shift
printf 'sudo\t%s\n' "$*" >> "$FAKE_LOG"
exec "$@"
'''

# Serves raw.githubusercontent.com downloads from FAKE_RAW_DIR.
FAKE_CURL = r'''#!/usr/bin/env bash
out= url=
while (( $# )); do
    case $1 in
        --output|-o) out=$2; shift 2 ;;
        --proto) shift 2 ;;
        -*) shift ;;
        *) url=$1; shift ;;
    esac
done
source="$FAKE_RAW_DIR/${url##*/}"
[[ -f $source && -n $out ]] || { echo "fake curl: refusing $url" >&2; exit 22; }
cp -- "$source" "$out"
'''

# Also records whether FAKE_EXPECT_FILE (the client config) already existed
# when this role was installed.
STUB_ONE_CLICK = r'''#!/usr/bin/env bash
saved=client-missing
[[ -e ${FAKE_EXPECT_FILE:-/nonexistent} ]] && saved=client-saved
printf 'install\t%s\t%s\t%s\n' "${1##*/}" "${*:2}" "$saved" >> "$FAKE_LOG"
exit "${FAKE_INSTALL_STATUS:-0}"
'''

STUB_INSTALLER = r'''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
with open(os.environ['FAKE_LOG'], 'a') as log:
    log.write('installer\t' + ' '.join(args) + '\n')
if '--preflight' in args:
    name = args[args.index('--name') + 1]
    if name in os.environ.get('FAKE_PREFLIGHT_FAIL', '').split(','):
        print('error: injected preflight failure for ' + name, file=sys.stderr)
        sys.exit(1)
'''


def write_executable(path, text):
    path.write_text(text, encoding='utf-8')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def fake_bin(directory):
    """Create the fake commands and return the directory to prepend to PATH."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in (('sing-box', FAKE_SING_BOX), ('ssh', FAKE_SSH), ('scp', FAKE_SCP),
                       ('sudo', FAKE_SUDO), ('curl', FAKE_CURL)):
        write_executable(directory / name, text)
    return directory


def stub_tools(directory):
    """Real generator plus recording stand-ins for the system installer."""
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / 'wg_hy2_generate.py', directory / 'wg_hy2_generate.py')
    write_executable(directory / 'one_click_install.sh', STUB_ONE_CLICK)
    write_executable(directory / 'wg_hy2_install.py', STUB_INSTALLER)
    return directory


def patched_script(name, destination, replacements=()):
    """Copy a project script, applying exact, must-exist test substitutions."""
    text = (ROOT / name).read_text(encoding='utf-8')
    for old, new in replacements:
        if old not in text:
            raise AssertionError(f'{name} no longer contains {old!r}')
        text = text.replace(old, new)
    return write_executable(destination / name, text)


def environment(bin_dir, log, raw_dir=None, **extra):
    env = dict(os.environ, PATH=f'{bin_dir}{os.pathsep}{os.environ.get("PATH", "")}',
               FAKE_LOG=str(log), LC_ALL='C.UTF-8')
    if raw_dir is not None:
        env['FAKE_RAW_DIR'] = str(raw_dir)
    env.update({key: str(value) for key, value in extra.items()})
    return env


def log_lines(log):
    if not log.exists():
        return []
    return [line.split('\t') for line in log.read_text(encoding='utf-8').splitlines()]
