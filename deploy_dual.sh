#!/usr/bin/env bash
# Generate a private dual-server bundle locally and install each role over SSH.
# SSH key/agent authentication is intentional; passwords are never accepted or
# written to disk by this script.
set -Eeuo pipefail

readonly SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

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
[[ "$ssh_port" =~ ^[0-9]+$ ]] && (( ssh_port >= 1 && ssh_port <= 65535 )) \
    || die "--ssh-port must be 1..65535"
command -v python3 >/dev/null 2>&1 || die "python3 is required locally"
command -v ssh >/dev/null 2>&1 || die "ssh is required locally"
command -v scp >/dev/null 2>&1 || die "scp is required locally"

if [[ -z "$local_sing_box" ]]; then
    local_sing_box=$(command -v sing-box 2>/dev/null || true)
fi
[[ -n "$local_sing_box" ]] || die "pass --sing-box or put sing-box 1.14.4 in PATH"
[[ -x "$local_sing_box" ]] || die "local sing-box is not executable: $local_sing_box"

ssh_opts=(-o BatchMode=yes -p "$ssh_port")
scp_opts=(-P "$ssh_port")
if [[ -n "$identity" ]]; then
    [[ -r "$identity" ]] || die "SSH identity is not readable: $identity"
    ssh_opts+=(-i "$identity")
    scp_opts+=(-i "$identity")
fi

tmp=$(mktemp -d "${TMPDIR:-/tmp}/wg-hy2-dual-deploy.XXXXXX")
chmod 700 "$tmp"
cleanup_hosts=()
cleanup_stages=()
cleanup() {
    local i host stage
    set +e
    for ((i = 0; i < ${#cleanup_hosts[@]}; i++)); do
        host=${cleanup_hosts[i]}
        stage=${cleanup_stages[i]}
        ssh "${ssh_opts[@]}" "$ssh_user@$host" "rm -rf -- '$stage'" >/dev/null 2>&1 || true
    done
    rm -rf -- "$tmp"
}
trap cleanup EXIT INT TERM

bundle="$tmp/bundle"
echo "Generating a private dual-server bundle..." >&2
python3 "$SCRIPT_DIR/wg_hy2_generate.py" --mode dual \
    --entry-ip "$relay_ip" --exit-ip "$landing_ip" \
    --sing-box "$local_sing_box" --output "$bundle"

ssh_target() {
    printf '%s@%s' "$ssh_user" "$1"
}

remote_prepare() {
    local host=$1 stage=$2
    ssh "${ssh_opts[@]}" "$(ssh_target "$host")" \
        "umask 077; mkdir -p -- '$stage'; chmod 700 -- '$stage'"
}

remote_install() {
    local host=$1 role=$2
    local stage="/tmp/wg-hy2-deploy-${role}-$(date +%s)-$RANDOM"
    cleanup_hosts+=("$host")
    cleanup_stages+=("$stage")
    remote_prepare "$host" "$stage"
    scp "${scp_opts[@]}" -q -r \
        "$bundle/$role" "$SCRIPT_DIR/one_click_install.sh" \
        "$SCRIPT_DIR/wg_hy2_install.py" "$(ssh_target "$host"):$stage/"
    local remote_flags=()
    (( enable == 1 )) || remote_flags+=(--no-enable)
    echo "Installing $role on $host..." >&2
    ssh "${ssh_opts[@]}" "$(ssh_target "$host")" bash -s -- "$stage" "$role" "${remote_flags[@]}" <<'REMOTE'
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

# The landing role is installed first so the relay never points at an absent
# endpoint during the normal deployment order.
remote_install "$landing_host" landing
remote_install "$relay_host" relay

[[ ! -e "$client_output" && ! -L "$client_output" ]] \
    || die "refusing to overwrite existing client output: $client_output"
client_parent=$(dirname -- "$client_output")
[[ -d "$client_parent" ]] || die "client output directory does not exist: $client_parent"
install -m 600 "$bundle/client/client.json" "$client_output"
echo "Dual deployment complete. Client config: $client_output" >&2
if (( enable == 0 )); then
    echo "Services were started without boot autostart (--no-enable)." >&2
fi
