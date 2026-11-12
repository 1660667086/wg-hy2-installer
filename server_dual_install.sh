#!/usr/bin/env bash
# Run on the relay/control server. Generate both private roles there, install
# relay locally, and install landing over SSH. No local computer is needed.
set -Eeuo pipefail

readonly VERSION="1.14.4"
readonly RAW_BASE="https://raw.githubusercontent.com/1660667086/wg-hy2-installer/main"
readonly RELEASE_BASE="https://github.com/SagerNet/sing-box/releases/download/v${VERSION}"

die() {
    echo "error: $*" >&2
    exit 1
}

usage() {
    cat >&2 <<'USAGE'
Usage (run as root on the relay/control server):
  curl -fsSL https://raw.githubusercontent.com/1660667086/wg-hy2-installer/main/server_dual_install.sh \
    | sudo bash -s -- --relay-ip RELAY_PUBLIC_IPV4 \
      --landing-host LANDING_SSH_HOST --landing-ip LANDING_PUBLIC_IPV4 \
      [--landing-user root] [--ssh-port 22] [--identity SSH_KEY] \
      [--client-output /root/wg-hy2-client.json] [--enable]

The relay server is the current machine. SSH key/agent authentication from
this machine to the landing server is required; passwords are not handled.
By default both services start now without boot autostart. Pass --enable to
enable boot autostart.
USAGE
}

[[ $EUID -eq 0 ]] || die "run as root (for example: sudo bash -s -- ...)"
command -v curl >/dev/null 2>&1 || die "curl is required"
command -v tar >/dev/null 2>&1 || die "tar is required"
command -v ssh >/dev/null 2>&1 || die "ssh is required"
command -v scp >/dev/null 2>&1 || die "scp is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"

relay_ip=""
landing_host=""
landing_ip=""
landing_user="root"
ssh_port="22"
identity=""
client_output="/root/wg-hy2-client.json"
enable=0

while (( $# > 0 )); do
    case "$1" in
        --relay-ip) (( $# >= 2 )) || die "--relay-ip needs a value"; relay_ip=$2; shift 2 ;;
        --landing-host) (( $# >= 2 )) || die "--landing-host needs a value"; landing_host=$2; shift 2 ;;
        --landing-ip) (( $# >= 2 )) || die "--landing-ip needs a value"; landing_ip=$2; shift 2 ;;
        --landing-user) (( $# >= 2 )) || die "--landing-user needs a value"; landing_user=$2; shift 2 ;;
        --ssh-port) (( $# >= 2 )) || die "--ssh-port needs a value"; ssh_port=$2; shift 2 ;;
        --identity) (( $# >= 2 )) || die "--identity needs a path"; identity=$2; shift 2 ;;
        --client-output) (( $# >= 2 )) || die "--client-output needs a path"; client_output=$2; shift 2 ;;
        --enable) enable=1; shift ;;
        --no-enable) enable=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

[[ -n "$relay_ip" ]] || { usage; die "--relay-ip is required"; }
[[ -n "$landing_host" ]] || { usage; die "--landing-host is required"; }
[[ -n "$landing_ip" ]] || { usage; die "--landing-ip is required"; }
[[ "$ssh_port" =~ ^[0-9]+$ ]] && (( ssh_port >= 1 && ssh_port <= 65535 )) \
    || die "--ssh-port must be 1..65535"
[[ ! -e "$client_output" && ! -L "$client_output" ]] \
    || die "refusing to overwrite existing client output: $client_output"
client_parent=$(dirname -- "$client_output")
[[ -d "$client_parent" ]] || die "client output directory does not exist: $client_parent"

ssh_opts=(-o BatchMode=yes -p "$ssh_port")
scp_opts=(-P "$ssh_port")
if [[ -n "$identity" ]]; then
    [[ -r "$identity" ]] || die "SSH identity is not readable: $identity"
    ssh_opts+=(-i "$identity")
    scp_opts+=(-i "$identity")
fi

compatible_binary() {
    local candidate=$1 info
    [[ -x "$candidate" ]] || return 1
    info=$("$candidate" version 2>/dev/null || true)
    grep -Eq "^sing-box version ${VERSION}([[:space:]]|$)" <<<"$info" || return 1
    grep -q 'with_quic' <<<"$info" || return 1
    grep -q 'with_wireguard' <<<"$info" || return 1
    grep -q 'with_gvisor' <<<"$info" || return 1
}

local_sing_box=""
if candidate=$(command -v sing-box 2>/dev/null) && compatible_binary "$candidate"; then
    local_sing_box=$candidate
else
    machine=$(uname -m)
    case "$machine" in
        x86_64|amd64)
            asset="sing-box-${VERSION}-linux-amd64.tar.gz"
            digest="699a1b510aa4396b2176d32d1181920399ebf1e267835da6f3b598d0f7c24968"
            arch="amd64" ;;
        aarch64|arm64)
            asset="sing-box-${VERSION}-linux-arm64.tar.gz"
            digest="436441f76ee2f7fe21b5b2d426d3cb261a480455a704865789ad533e4273ec26"
            arch="arm64" ;;
        *)
            die "unsupported control-server architecture: $machine; install sing-box ${VERSION} with required tags first"
            ;;
    esac
    cache_dir="/usr/local/libexec/wg-hy2"
    local_sing_box="$cache_dir/sing-box-${VERSION}-${arch}"
    if ! compatible_binary "$local_sing_box"; then
        tmp_download=$(mktemp -d "${TMPDIR:-/tmp}/wg-hy2-sing-box.XXXXXX")
        trap 'rm -rf -- "${tmp_download:-}"' EXIT INT TERM
        archive="$tmp_download/$asset"
        echo "Downloading verified sing-box ${VERSION} for ${arch}..." >&2
        curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
            --output "$archive" "$RELEASE_BASE/$asset"
        if command -v sha256sum >/dev/null 2>&1; then
            actual=$(sha256sum "$archive" | cut -d ' ' -f 1)
        elif command -v shasum >/dev/null 2>&1; then
            actual=$(shasum -a 256 "$archive" | cut -d ' ' -f 1)
        else
            die "sha256sum or shasum is required"
        fi
        [[ "$actual" == "$digest" ]] || die "sing-box archive SHA-256 mismatch"
        mkdir -p -m 0755 "$cache_dir" "$tmp_download/extract"
        tar -xzf "$archive" -C "$tmp_download/extract"
        extracted=$(find "$tmp_download/extract" -type f -name sing-box -print -quit)
        [[ -n "$extracted" ]] || die "sing-box archive did not contain its executable"
        install -m 0755 "$extracted" "$local_sing_box"
        compatible_binary "$local_sing_box" || die "downloaded sing-box failed validation"
        rm -rf -- "$tmp_download"
        tmp_download=""
        trap - EXIT INT TERM
    fi
fi

tmp=$(mktemp -d "${TMPDIR:-/tmp}/wg-hy2-server-dual.XXXXXX")
chmod 700 "$tmp"
remote_stage="/tmp/wg-hy2-server-dual-$(date +%s)-$RANDOM"
cleanup() {
    set +e
    ssh "${ssh_opts[@]}" "$landing_user@$landing_host" "rm -rf -- '$remote_stage'" >/dev/null 2>&1 || true
    rm -rf -- "$tmp"
}
trap cleanup EXIT INT TERM

fetch() {
    local name=$1
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
        --output "$tmp/$name" "$RAW_BASE/$name"
    chmod 700 "$tmp/$name"
}

fetch wg_hy2_generate.py
fetch wg_hy2_install.py
fetch one_click_install.sh

bundle="$tmp/bundle"
echo "Generating private dual-server credentials on this server..." >&2
python3 "$tmp/wg_hy2_generate.py" --mode dual \
    --entry-ip "$relay_ip" --exit-ip "$landing_ip" \
    --sing-box "$local_sing_box" --output "$bundle"

echo "Preparing landing server..." >&2
ssh "${ssh_opts[@]}" "$landing_user@$landing_host" \
    "umask 077; mkdir -p -- '$remote_stage'; chmod 700 -- '$remote_stage'"
scp "${scp_opts[@]}" -q -r \
    "$bundle/landing" "$tmp/one_click_install.sh" "$tmp/wg_hy2_install.py" \
    "$landing_user@$landing_host:$remote_stage/"
remote_flags=()
(( enable == 1 )) || remote_flags+=(--no-enable)
ssh "${ssh_opts[@]}" "$landing_user@$landing_host" bash -s -- \
    "$remote_stage" "${remote_flags[@]}" <<'REMOTE'
set -eu
stage=$1
shift
cleanup() { rm -rf -- "$stage"; }
trap cleanup EXIT INT TERM
chmod 700 "$stage/landing" "$stage/one_click_install.sh" "$stage/wg_hy2_install.py"
"$stage/one_click_install.sh" "$stage/landing" "$@"
REMOTE

echo "Installing relay locally..." >&2
local_flags=()
(( enable == 1 )) || local_flags+=(--no-enable)
"$tmp/one_click_install.sh" "$bundle/relay" --sing-box "$local_sing_box" "${local_flags[@]}"

install -m 600 "$bundle/client/client.json" "$client_output"
echo "Dual server installation complete. Client config: $client_output" >&2
if (( enable == 0 )); then
    echo "Both services started without boot autostart (--no-enable)." >&2
fi
