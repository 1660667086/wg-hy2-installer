#!/usr/bin/env bash
# Run on the relay/control server. Generate both private roles there, install
# relay locally, and install landing over SSH. No local computer is needed.
# Both servers are checked before any credentials are generated, so a failed
# check (SSH, packages, ports) leaves nothing to clean up.
set -Eeuo pipefail

readonly VERSION="1.14.4"
readonly RAW_BASE="https://raw.githubusercontent.com/1660667086/wg-hy2-installer/main"
readonly RELEASE_BASE="https://github.com/SagerNet/sing-box/releases/download/v${VERSION}"

die() {
    printf '错误：%s\n' "$*" >&2
    exit 1
}

note() {
    printf '%s\n' "$*" >&2
}

usage() {
    cat >&2 <<'USAGE'
用法（在中转服务器上以 root 运行）：
  curl -fsSL https://raw.githubusercontent.com/1660667086/wg-hy2-installer/main/server_dual_install.sh \
    | sudo bash -s -- --relay-ip 中转机公网IPv4 \
      --landing-host 落地机SSH地址 --landing-ip 落地机公网IPv4 \
      [--landing-user root] [--ssh-port 22] [--identity SSH私钥] \
      [--client-output /root/wg-hy2-client.json] [--hy2-port 443] [--wg-port 51820] [--enable]

当前机器就是中转机。中转机须能用 SSH 密钥（或 agent）无密码登录落地机；
脚本不处理密码。落地机用户不是 root 时，须能免密码 sudo。
默认两台服务器的服务立即启动但不开机自启；加 --enable 才设置开机自启。
USAGE
}

[[ $EUID -eq 0 ]] || die "请以 root 运行（例如：sudo bash -s -- ...）"
for cmd in curl tar ssh scp python3; do
    command -v "$cmd" >/dev/null 2>&1 || die "缺少 $cmd，请先安装"
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' \
    || die "需要 Python 3.8 或更新版本（当前：$(python3 -V 2>&1)）"

relay_ip=""
landing_host=""
landing_ip=""
landing_user="root"
ssh_port="22"
identity=""
client_output="/root/wg-hy2-client.json"
enable=0
hy2_port=443
wg_port=51820

while (( $# > 0 )); do
    case "$1" in
        --relay-ip) (( $# >= 2 )) || die "--relay-ip 缺少参数"; relay_ip=$2; shift 2 ;;
        --landing-host) (( $# >= 2 )) || die "--landing-host 缺少参数"; landing_host=$2; shift 2 ;;
        --landing-ip) (( $# >= 2 )) || die "--landing-ip 缺少参数"; landing_ip=$2; shift 2 ;;
        --landing-user) (( $# >= 2 )) || die "--landing-user 缺少参数"; landing_user=$2; shift 2 ;;
        --ssh-port) (( $# >= 2 )) || die "--ssh-port 缺少参数"; ssh_port=$2; shift 2 ;;
        --identity) (( $# >= 2 )) || die "--identity 缺少路径"; identity=$2; shift 2 ;;
        --client-output) (( $# >= 2 )) || die "--client-output 缺少路径"; client_output=$2; shift 2 ;;
        --hy2-port) (( $# >= 2 )) || die "--hy2-port 缺少参数"; hy2_port=$2; shift 2 ;;
        --wg-port) (( $# >= 2 )) || die "--wg-port 缺少参数"; wg_port=$2; shift 2 ;;
        --enable) enable=1; shift ;;
        --no-enable) enable=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数：$1" ;;
    esac
done

[[ -n "$relay_ip" ]] || { usage; die "缺少 --relay-ip"; }
[[ -n "$landing_host" ]] || { usage; die "缺少 --landing-host"; }
[[ -n "$landing_ip" ]] || { usage; die "缺少 --landing-ip"; }
for value in "$ssh_port" "$hy2_port" "$wg_port"; do
    [[ "$value" =~ ^[0-9]{1,5}$ ]] && (( 10#$value >= 1 && 10#$value <= 65535 )) \
        || die "端口须为 1–65535：$value"
done
ssh_port=$((10#$ssh_port)) hy2_port=$((10#$hy2_port)) wg_port=$((10#$wg_port))
[[ "$landing_host" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || die "落地机 SSH 地址无效：$landing_host"
[[ "$landing_user" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]] || die "落地机 SSH 用户名无效：$landing_user"
[[ ! -e "$client_output" && ! -L "$client_output" ]] \
    || die "已存在 $client_output：可能已经安装过双机，或上次安装中途失败。确认不再需要后删除它（或用 --client-output 指定新路径）再重试"
client_parent=$(dirname -- "$client_output")
[[ -d "$client_parent" ]] || die "客户端配置所在目录不存在：$client_parent"

landing="$landing_user@$landing_host"
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=15 -p "$ssh_port")
scp_opts=(-o BatchMode=yes -o ConnectTimeout=15 -P "$ssh_port")
if [[ -n "$identity" ]]; then
    [[ -r "$identity" ]] || die "无法读取 SSH 私钥：$identity"
    ssh_opts+=(-i "$identity")
    scp_opts+=(-i "$identity")
fi
# A non-root landing user runs the privileged steps through passwordless sudo.
remote_sudo=()
[[ "$landing_user" == root ]] || remote_sudo=(sudo -n)

compatible_binary() {
    local candidate=$1 info
    [[ -x "$candidate" ]] || return 1
    info=$("$candidate" version 2>/dev/null || true)
    grep -Eq "^sing-box version ${VERSION}([[:space:]]|$)" <<<"$info" || return 1
    grep -q 'with_quic' <<<"$info" || return 1
    grep -q 'with_wireguard' <<<"$info" || return 1
    grep -q 'with_gvisor' <<<"$info" || return 1
}

tmp=$(mktemp -d "${TMPDIR:-/tmp}/wg-hy2-server-dual.XXXXXX")
chmod 700 "$tmp"
remote_stage=""
recovery=""
cleanup() {
    local status=$?
    set +e
    if [[ -n "$remote_stage" ]]; then
        ssh "${ssh_opts[@]}" "$landing" ${remote_sudo[@]+"${remote_sudo[@]}"} \
            rm -rf -- "$remote_stage" >/dev/null 2>&1 || true
    fi
    rm -rf -- "$tmp"
    if [[ -n "$recovery" ]]; then
        note "私有恢复包保留在：$recovery"
        if (( status != 0 )); then
            note "安装可能只完成了一部分；重试前请先检查两台服务器上的服务。"
        fi
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

fetch() {
    local name=$1
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
        --output "$tmp/$name" "$RAW_BASE/$name"
    chmod 700 "$tmp/$name"
}

fetch wg_hy2_generate.py
fetch wg_hy2_install.py
fetch one_click_install.sh

note "检查参数和本机（中转机）..."
python3 "$tmp/wg_hy2_generate.py" --mode dual --entry-ip "$relay_ip" --exit-ip "$landing_ip" \
    --hy2-port "$hy2_port" --wg-port "$wg_port" --check-only || die "参数有误，未做任何更改"
python3 "$tmp/wg_hy2_install.py" --preflight --name relay --hy2-port "$hy2_port" >/dev/null \
    || die "中转机安装前检查未通过（原因见上一行），未生成配置，也未修改系统"

note "检查落地机 $landing ..."
if ! remote_stage=$(ssh "${ssh_opts[@]}" "$landing" 'umask 077; mktemp -d /tmp/wg-hy2-server-dual.XXXXXXXX'); then
    remote_stage=""
    die "无法用 SSH 密钥登录 $landing（端口 $ssh_port）。请先在中转机上手动执行一次 ssh -p $ssh_port $landing ，核对并接受主机指纹，并确认无需输入密码即可登录"
fi
if [[ ! "$remote_stage" =~ ^/tmp/wg-hy2-server-dual\.[a-zA-Z0-9]{8}$ ]]; then
    remote_stage=""
    die "落地机返回了异常的临时目录"
fi
scp "${scp_opts[@]}" -q "$tmp/one_click_install.sh" "$tmp/wg_hy2_install.py" "$landing:$remote_stage/"
ssh "${ssh_opts[@]}" "$landing" ${remote_sudo[@]+"${remote_sudo[@]}"} \
    bash -s -- "$remote_stage" "$wg_port" <<'REMOTE' || die "落地机安装前检查未通过（原因见上方），未生成配置，也未修改系统"
set -eu
stage=$1
wg_port=$2
fail() { printf '落地机：%s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || fail '需要 root 权限'
[ "$(uname -s)" = Linux ] || fail '需要 Linux'
for cmd in python3 curl tar; do
    command -v "$cmd" >/dev/null 2>&1 || fail "缺少 $cmd，请先安装"
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' \
    || fail "需要 Python 3.8 或更新版本（当前：$(python3 -V 2>&1)）"
python3 "$stage/wg_hy2_install.py" --preflight --name landing --wg-port "$wg_port" >/dev/null
REMOTE

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
            die "中转机架构 $machine 无法自动下载 sing-box；请先安装带所需编译标签的 sing-box ${VERSION}"
            ;;
    esac
    cache_dir="/usr/local/libexec/wg-hy2"
    local_sing_box="$cache_dir/sing-box-${VERSION}-${arch}"
    if [[ -e "$local_sing_box" ]] && ! compatible_binary "$local_sing_box"; then
        die "已缓存的 sing-box 不兼容：$local_sing_box"
    fi
    if [[ ! -e "$local_sing_box" ]]; then
        archive="$tmp/$asset"
        note "下载并校验 sing-box ${VERSION}（${arch}）..."
        curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
            --output "$archive" "$RELEASE_BASE/$asset"
        if command -v sha256sum >/dev/null 2>&1; then
            actual=$(sha256sum "$archive" | cut -d ' ' -f 1)
        elif command -v shasum >/dev/null 2>&1; then
            actual=$(shasum -a 256 "$archive" | cut -d ' ' -f 1)
        else
            die "需要 sha256sum 或 shasum 来校验下载文件"
        fi
        [[ "$actual" == "$digest" ]] || die "sing-box 压缩包 SHA-256 校验失败"
        mkdir -p -m 0755 "$cache_dir"
        mkdir -p "$tmp/extract"
        tar -xzf "$archive" -C "$tmp/extract"
        extracted=$(find "$tmp/extract" -type f -name sing-box -print -quit)
        [[ -n "$extracted" ]] || die "sing-box 压缩包中没有可执行文件"
        install -m 0755 "$extracted" "$local_sing_box"
        compatible_binary "$local_sing_box" || die "下载的 sing-box 未通过版本/编译标签校验"
    fi
fi

recovery=$(mktemp -d /root/wg-hy2-recovery.XXXXXXXX)
chmod 700 "$recovery"
bundle="$recovery/bundle"
note "在中转机生成双机私有配置..."
if ! python3 "$tmp/wg_hy2_generate.py" --mode dual \
    --entry-ip "$relay_ip" --exit-ip "$landing_ip" \
    --hy2-port "$hy2_port" --wg-port "$wg_port" \
    --sing-box "$local_sing_box" --output "$bundle"; then
    # Nothing was generated, so there is nothing to recover.
    rmdir -- "$recovery" 2>/dev/null && recovery=""
    die "生成配置失败，未安装任何服务"
fi

# Save client credentials before installing either role. Exclusive creation
# prevents a concurrent run from replacing an existing user's configuration.
python3 - "$bundle/client/client.json" "$client_output" <<'PY'
import os, shutil, sys
fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'wb') as dst, open(sys.argv[1], 'rb') as src:
    shutil.copyfileobj(src, dst)
PY
note "客户端配置已保存：$client_output"

note "安装落地机..."
scp "${scp_opts[@]}" -q -r "$bundle/landing" "$landing:$remote_stage/"
remote_flags=()
(( enable == 1 )) || remote_flags+=(--no-enable)
ssh "${ssh_opts[@]}" "$landing" ${remote_sudo[@]+"${remote_sudo[@]}"} \
    bash -s -- "$remote_stage" ${remote_flags[@]+"${remote_flags[@]}"} <<'REMOTE'
set -eu
stage=$1
shift
cleanup() { rm -rf -- "$stage"; }
trap cleanup EXIT INT TERM
chmod 700 "$stage/landing" "$stage/one_click_install.sh" "$stage/wg_hy2_install.py"
"$stage/one_click_install.sh" "$stage/landing" "$@"
REMOTE

note "在本机安装中转..."
local_flags=()
(( enable == 1 )) || local_flags+=(--no-enable)
"$tmp/one_click_install.sh" "$bundle/relay" --sing-box "$local_sing_box" \
    ${local_flags[@]+"${local_flags[@]}"}

note "双机安装完成。客户端配置：$client_output"
if (( enable == 0 )); then
    note "两台服务器的服务均已启动，但未设置开机自启（--no-enable）。"
fi
