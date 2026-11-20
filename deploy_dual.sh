#!/usr/bin/env bash
# Generate a private dual-server bundle locally and install each role over SSH.
# SSH key/agent authentication is intentional; passwords are never accepted or
# written to disk by this script. Both servers are checked before either one
# is changed, and the client config is saved before anything is installed.
set -Eeuo pipefail

readonly SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
readonly HY2_PORT=443
readonly WG_PORT=51820

die() {
    echo "error: $*" >&2
    exit 1
}

usage() {
    cat >&2 <<'USAGE'
Usage:
  ./deploy_dual.sh --relay-host HOST --landing-host HOST \
    [--relay-ip PUBLIC_IPV4] [--landing-ip PUBLIC_IPV4] \
    [--ssh-user root] [--ssh-port 22] [--identity SSH_KEY] \
    [--sing-box LOCAL_SING_BOX] [--client-output PATH] [--enable]

The SSH hosts default to their public IPv4 addresses. Use --relay-ip and
--landing-ip when SSH uses hostnames or a separate management address.
SSH key/agent authentication is required; the script never handles passwords.
A non-root --ssh-user must be able to run sudo without a password.
By default services start now without boot autostart. Pass --enable to enable it.
USAGE
}

relay_host=""
landing_host=""
relay_ip=""
landing_ip=""
ssh_user="root"
ssh_port="22"
identity=""
local_sing_box=""
client_output="./client.json"
enable=0

while (( $# > 0 )); do
    case "$1" in
        --relay-host) (( $# >= 2 )) || die "--relay-host needs a value"; relay_host=$2; shift 2 ;;
        --landing-host) (( $# >= 2 )) || die "--landing-host needs a value"; landing_host=$2; shift 2 ;;
        --relay-ip) (( $# >= 2 )) || die "--relay-ip needs a value"; relay_ip=$2; shift 2 ;;
        --landing-ip) (( $# >= 2 )) || die "--landing-ip needs a value"; landing_ip=$2; shift 2 ;;
        --ssh-user) (( $# >= 2 )) || die "--ssh-user needs a value"; ssh_user=$2; shift 2 ;;
        --ssh-port) (( $# >= 2 )) || die "--ssh-port needs a value"; ssh_port=$2; shift 2 ;;
        --identity) (( $# >= 2 )) || die "--identity needs a path"; identity=$2; shift 2 ;;
        --sing-box) (( $# >= 2 )) || die "--sing-box needs a path"; local_sing_box=$2; shift 2 ;;
        --client-output) (( $# >= 2 )) || die "--client-output needs a path"; client_output=$2; shift 2 ;;
        --enable) enable=1; shift ;;
        --no-enable) enable=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

[[ -n "$relay_host" ]] || { usage; die "--relay-host is required"; }
[[ -n "$landing_host" ]] || { usage; die "--landing-host is required"; }
[[ -n "$relay_ip" ]] || relay_ip=$relay_host
[[ -n "$landing_ip" ]] || landing_ip=$landing_host
for host in "$relay_host" "$landing_host"; do
    [[ "$host" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || die "invalid SSH host: $host"
done
[[ "$ssh_user" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]] || die "invalid SSH user: $ssh_user"
[[ "$ssh_port" =~ ^[0-9]{1,5}$ ]] && (( 10#$ssh_port >= 1 && 10#$ssh_port <= 65535 )) \
    || die "--ssh-port must be 1..65535"
ssh_port=$((10#$ssh_port))
command -v python3 >/dev/null 2>&1 || die "python3 is required locally"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' \
    || die "Python 3.8 or newer is required locally (found: $(python3 -V 2>&1))"
command -v ssh >/dev/null 2>&1 || die "ssh is required locally"
command -v scp >/dev/null 2>&1 || die "scp is required locally"

if [[ -z "$local_sing_box" ]]; then
    local_sing_box=$(command -v sing-box 2>/dev/null || true)
fi
[[ -n "$local_sing_box" ]] || die "pass --sing-box or put sing-box 1.14.4 in PATH"
[[ -x "$local_sing_box" ]] || die "local sing-box is not executable: $local_sing_box"

# Check the output path first: the client config is written before any
# server changes, so a bad path must not be discovered after installation.
[[ ! -e "$client_output" && ! -L "$client_output" ]] \
    || die "refusing to overwrite existing client output: $client_output"
client_parent=$(dirname -- "$client_output")
[[ -d "$client_parent" ]] || die "client output directory does not exist: $client_parent"

ssh_opts=(-o BatchMode=yes -o ConnectTimeout=15 -p "$ssh_port")
scp_opts=(-o BatchMode=yes -o ConnectTimeout=15 -P "$ssh_port")
if [[ -n "$identity" ]]; then
    [[ -r "$identity" ]] || die "SSH identity is not readable: $identity"
    ssh_opts+=(-i "$identity")
    scp_opts+=(-i "$identity")
fi
# A non-root SSH user runs the privileged steps through passwordless sudo.
remote_sudo=()
[[ "$ssh_user" == root ]] || remote_sudo=(sudo -n)

python3 "$SCRIPT_DIR/wg_hy2_generate.py" --mode dual --entry-ip "$relay_ip" --exit-ip "$landing_ip" \
    --hy2-port "$HY2_PORT" --wg-port "$WG_PORT" --check-only

tmp=$(mktemp -d "${TMPDIR:-/tmp}/wg-hy2-dual-deploy.XXXXXX")
chmod 700 "$tmp"
stage_hosts=()
stage_dirs=()
cleanup() {
    local i
    set +e
    for ((i = 0; i < ${#stage_hosts[@]}; i++)); do
        ssh "${ssh_opts[@]}" "$ssh_user@${stage_hosts[i]}" ${remote_sudo[@]+"${remote_sudo[@]}"} \
            rm -rf -- "${stage_dirs[i]}" >/dev/null 2>&1 || true
    done
    rm -rf -- "$tmp"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

ssh_target() {
    printf '%s@%s' "$ssh_user" "$1"
}

# Create a private remote stage with mktemp (never a guessable /tmp name),
# upload the installer tools and run the bundle-free preflight there.
prepared_stage=""
prepare_host() {
    local host=$1 role=$2 stage
    local -a port_flag
    if ! stage=$(ssh "${ssh_opts[@]}" "$(ssh_target "$host")" 'umask 077; mktemp -d /tmp/wg-hy2-deploy.XXXXXXXX'); then
        die "cannot log in to $(ssh_target "$host") with an SSH key; run 'ssh -p $ssh_port $(ssh_target "$host")' once to verify the host key, and make sure no password is needed"
    fi
    [[ "$stage" =~ ^/tmp/wg-hy2-deploy\.[a-zA-Z0-9]{8}$ ]] || die "unexpected remote temporary directory on $host"
    stage_hosts+=("$host")
    stage_dirs+=("$stage")
    scp "${scp_opts[@]}" -q "$SCRIPT_DIR/one_click_install.sh" "$SCRIPT_DIR/wg_hy2_install.py" \
        "$(ssh_target "$host"):$stage/"
    if [[ "$role" == landing ]]; then
        port_flag=(--wg-port "$WG_PORT")
    else
        port_flag=(--hy2-port "$HY2_PORT")
    fi
    echo "Checking $role server $host..." >&2
    ssh "${ssh_opts[@]}" "$(ssh_target "$host")" ${remote_sudo[@]+"${remote_sudo[@]}"} \
        bash -s -- "$stage" "$role" "${port_flag[@]}" <<'REMOTE' \
        || die "$role server $host failed its pre-install check (see above); nothing was installed"
set -eu
stage=$1
role=$2
shift 2
fail() { printf '%s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || fail 'root privileges are required'
[ "$(uname -s)" = Linux ] || fail 'Linux is required'
for cmd in python3 curl tar; do
    command -v "$cmd" >/dev/null 2>&1 || fail "$cmd is required; install it first"
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' \
    || fail "Python 3.8 or newer is required (found: $(python3 -V 2>&1))"
python3 "$stage/wg_hy2_install.py" --preflight --name "$role" "$@" >/dev/null
REMOTE
    prepared_stage=$stage
}

remote_install() {
    local host=$1 role=$2 stage=$3
    local -a remote_flags=()
    (( enable == 1 )) || remote_flags+=(--no-enable)
    scp "${scp_opts[@]}" -q -r "$bundle/$role" "$(ssh_target "$host"):$stage/"
    echo "Installing $role on $host..." >&2
    ssh "${ssh_opts[@]}" "$(ssh_target "$host")" ${remote_sudo[@]+"${remote_sudo[@]}"} \
        bash -s -- "$stage" "$role" ${remote_flags[@]+"${remote_flags[@]}"} <<'REMOTE'
set -eu
stage=$1
role=$2
shift 2
cleanup() { rm -rf -- "$stage"; }
trap cleanup EXIT INT TERM
chmod 700 "$stage/$role" "$stage/one_click_install.sh" "$stage/wg_hy2_install.py"
"$stage/one_click_install.sh" "$stage/$role" "$@"
REMOTE
}

prepare_host "$landing_host" landing
landing_stage=$prepared_stage
prepare_host "$relay_host" relay
relay_stage=$prepared_stage

bundle="$tmp/bundle"
echo "Generating a private dual-server bundle..." >&2
python3 "$SCRIPT_DIR/wg_hy2_generate.py" --mode dual \
    --entry-ip "$relay_ip" --exit-ip "$landing_ip" \
    --hy2-port "$HY2_PORT" --wg-port "$WG_PORT" \
    --sing-box "$local_sing_box" --output "$bundle"

# Exclusive creation: never follow or replace a file that appeared meanwhile.
python3 - "$bundle/client/client.json" "$client_output" <<'PY'
import os, shutil, sys
fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'wb') as dst, open(sys.argv[1], 'rb') as src:
    shutil.copyfileobj(src, dst)
PY
echo "Client config saved at: $client_output" >&2

# The landing role is installed first so the relay never points at an absent
# endpoint during the normal deployment order.
remote_install "$landing_host" landing "$landing_stage"
remote_install "$relay_host" relay "$relay_stage"

echo "Dual deployment complete. Client config: $client_output" >&2
if (( enable == 0 )); then
    echo "Services were started without boot autostart (--no-enable)." >&2
fi
