#!/usr/bin/env bash
# Install a landing endpoint and prepare a confidential relay/client handoff.
set -Eeuo pipefail
umask 077

VERSION=1.14.4
RAW_BASE=https://raw.githubusercontent.com/1660667086/wg-hy2-installer/main
RELEASE_BASE=https://github.com/SagerNet/sing-box/releases/download/v${VERSION}
die() { printf '错误：%s\n' "$*" >&2; exit 1; }
usage() { printf 'Usage: %s --relay-ip IPv4 --landing-ip IPv4 [--hy2-port 443] [--wg-port 51820] [--enable] [--sing-box PATH]\n' "$0"; }

relay_ip= landing_ip= hy2_port=443 wg_port=51820 enabled=0 binary=
while (( $# )); do
    case "$1" in
        --relay-ip) (( $# >= 2 )) || die '--relay-ip 缺少参数'; relay_ip=$2; shift 2 ;;
        --landing-ip) (( $# >= 2 )) || die '--landing-ip 缺少参数'; landing_ip=$2; shift 2 ;;
        --hy2-port) (( $# >= 2 )) || die '--hy2-port 缺少参数'; hy2_port=$2; shift 2 ;;
        --wg-port) (( $# >= 2 )) || die '--wg-port 缺少参数'; wg_port=$2; shift 2 ;;
        --sing-box) (( $# >= 2 )) || die '--sing-box 缺少参数'; binary=$2; shift 2 ;;
        --enable) enabled=1; shift ;;
        --no-enable) enabled=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数：$1" ;;
    esac
done
[[ $EUID -eq 0 ]] || die '请以 root 运行'
[[ $(uname -s) == Linux ]] || die '仅支持 Linux 服务器'
[[ -n $relay_ip && -n $landing_ip ]] || die '请提供中转机和落地机公网 IPv4'
for value in "$hy2_port" "$wg_port"; do
    [[ $value =~ ^[0-9]{1,5}$ ]] && (( 10#$value >= 1 && 10#$value <= 65535 )) || die '端口须为 1–65535'
done
for cmd in python3 openssl curl; do command -v "$cmd" >/dev/null || die "缺少 $cmd"; done

compatible() {
    local info
    [[ -x $1 ]] || return 1
    info=$("$1" version 2>/dev/null) || return 1
    [[ $info == *"sing-box version $VERSION"* && $info == *with_gvisor* && $info == *with_quic* && $info == *with_wireguard* ]]
}

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
temp_dir=$(mktemp -d /tmp/wg-hy2-landing-tools.XXXXXXXX)
cleanup() { rm -rf -- "$temp_dir"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -n $binary ]]; then
    compatible "$binary" || die '指定的 sing-box 版本或编译标签不匹配'
elif command -v sing-box >/dev/null && compatible "$(command -v sing-box)"; then
    binary=$(command -v sing-box)
else
    case $(uname -m) in
        x86_64|amd64) arch=amd64; digest=699a1b510aa4396b2176d32d1181920399ebf1e267835da6f3b598d0f7c24968 ;;
        aarch64|arm64) arch=arm64; digest=436441f76ee2f7fe21b5b2d426d3cb261a480455a704865789ad533e4273ec26 ;;
        *) die '此架构请自行提供带所需编译标签的 sing-box 1.14.4（--sing-box）' ;;
    esac
    binary="/usr/local/libexec/wg-hy2/sing-box-${VERSION}-${arch}"
    if [[ -e $binary || -L $binary ]]; then
        compatible "$binary" || die "已有 $binary 不兼容，请检查后重试"
    else
        command -v tar >/dev/null || die '缺少 tar'
        command -v sha256sum >/dev/null || die '缺少 sha256sum'
        asset="sing-box-${VERSION}-linux-${arch}.tar.gz"
        curl -fsSL --proto '=https' --tlsv1.2 "$RELEASE_BASE/$asset" -o "$temp_dir/$asset"
        printf '%s  %s\n' "$digest" "$temp_dir/$asset" | sha256sum -c - >/dev/null || die '下载文件校验失败'
        mkdir "$temp_dir/extract"
        tar -xzf "$temp_dir/$asset" -C "$temp_dir/extract"
        extracted=$(find "$temp_dir/extract" -type f -name sing-box -print -quit)
        [[ -n $extracted ]] && compatible "$extracted" || die '下载的 sing-box 无法运行或不兼容'
        mkdir -p -m 0755 /usr/local/libexec/wg-hy2
        install -m 0755 "$extracted" "$binary"
    fi
fi

for file in wg_hy2_generate.py wg_hy2_install.py one_click_install.sh; do
    if [[ -f $script_dir/$file ]]; then
        cp -- "$script_dir/$file" "$temp_dir/$file"
    else
        curl -fsSL --proto '=https' --tlsv1.2 "$RAW_BASE/$file" -o "$temp_dir/$file"
    fi
    chmod 700 "$temp_dir/$file"
done

# Keep the complete private deployment package even if installation fails.
state_dir=/root/wg-hy2-landing-handoff
mkdir -m 700 -- "$state_dir" || die "无法创建 $state_dir（可能已经存在）；请检查已有部署"
printf '私有恢复包保存在 %s；安装失败时也会保留。\n' "$state_dir" >&2
python3 "$temp_dir/wg_hy2_generate.py" --mode dual --entry-ip "$relay_ip" --exit-ip "$landing_ip" \
    --hy2-port "$hy2_port" --wg-port "$wg_port" --sing-box "$binary" --output "$state_dir/bundle"

# Token holds relay credentials and client secrets, but not the landing WG key.
python3 - "$state_dir/bundle" "$state_dir/relay-token.txt" <<'PY'
import base64, json, os, pathlib, sys
bundle = pathlib.Path(sys.argv[1])
def load(path):
    return json.loads((bundle / path).read_text(encoding='utf-8'))
payload = {'schema': 1, 'relay': {'manifest': load('relay/manifest.json'),
                                  'server': load('relay/server.json')},
           'client': load('client/client.json')}
encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode()
fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w', encoding='ascii') as out:
    for offset in range(0, len(encoded), 76):
        out.write(encoded[offset:offset + 76] + '\n')
PY

flags=(--name landing --sing-box "$binary")
(( enabled )) || flags+=(--no-enable)
if ! "$temp_dir/one_click_install.sh" "$state_dir/bundle/landing" "${flags[@]}"; then
    die "落地机安装未完成；配置保留在 $state_dir，请检查错误，不要使用对接信息"
fi
: > "$state_dir/installed"
printf '落地机安装完成。私密对接信息：%s/relay-token.txt\n' "$state_dir"
printf '请安全传给中转机；文件包含客户端私钥和中转认证信息，不要公开。\n' >&2
