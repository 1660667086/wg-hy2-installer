#!/usr/bin/env bash
# Install the relay role from a handoff token produced on the landing server.
set -Eeuo pipefail
umask 077
BASE_URL=https://raw.githubusercontent.com/1660667086/wg-hy2-installer/main
die() { printf '错误：%s\n' "$*" >&2; exit 1; }
usage() { printf 'Usage: %s --token-file PATH [--enable]\n' "$0"; }
token_file= enabled=0
while (($#)); do
    case $1 in
        --token-file) (($# >= 2)) || die '--token-file 缺少路径'; token_file=$2; shift 2 ;;
        --enable) enabled=1; shift ;;
        --no-enable) enabled=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数：$1" ;;
    esac
done
[[ $EUID -eq 0 ]] || die '请以 root 运行'
[[ $(uname -s) == Linux ]] || die '需要 Linux 服务器'
[[ -f $token_file && -r $token_file ]] || die '请提供可读的对接码文件'
command -v python3 >/dev/null || die '缺少 python3'
command -v curl >/dev/null || die '缺少 curl'
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' \
    || die "需要 Python 3.8 或更新版本（当前：$(python3 -V 2>&1)）"
tools_dir=$(mktemp -d /tmp/wg-hy2-import-tools.XXXXXXXX)
trap 'rm -rf -- "$tools_dir"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
for file in import_relay_token.py wg_hy2_generate.py wg_hy2_install.py one_click_install.sh; do
    if [[ -f $script_dir/$file ]]; then
        cp -- "$script_dir/$file" "$tools_dir/$file"
    else
        curl -fsSL --proto '=https' --tlsv1.2 "$BASE_URL/$file" -o "$tools_dir/$file"
    fi
    chmod 700 "$tools_dir/$file"
done
state_dir=$(mktemp -d /root/wg-hy2-relay-import.XXXXXXXX)
chmod 700 "$state_dir"
if ! python3 "$tools_dir/import_relay_token.py" "$token_file" "$state_dir"; then
    # The token is fully validated before anything is written, so the
    # directory is still empty here; rmdir never removes saved credentials.
    rmdir -- "$state_dir" 2>/dev/null || true
    exit 1
fi
printf '私有配置保存在 %s，安装失败时保留。\n' "$state_dir" >&2
printf '客户端配置：%s/client.json\n' "$state_dir" >&2
if (( enabled )); then
    install_status=0
    bash "$tools_dir/one_click_install.sh" "$state_dir/relay" || install_status=$?
else
    install_status=0
    bash "$tools_dir/one_click_install.sh" "$state_dir/relay" --no-enable || install_status=$?
fi
(( install_status == 0 )) || die "中转机安装未完成；私有配置保留在 $state_dir，请检查上面的错误"
if ! /opt/wg-hy2-relay/sing-box check -c "$state_dir/client.json" >/dev/null 2>&1; then
    die '客户端配置校验失败；中转服务可能已安装，请检查服务及保留的配置'
fi
printf '安装完成。客户端配置：%s/client.json\n' "$state_dir"
