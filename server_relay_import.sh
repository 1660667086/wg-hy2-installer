#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
BASE_URL=https://raw.githubusercontent.com/1660667086/wg-hy2-installer/main
die() { printf '错误：%s\n' "$*" >&2; exit 1; }
token_file= enabled=0
while (($#)); do
    case $1 in
        --token-file) (($# >= 2)) || die '--token-file 缺少路径'; token_file=$2; shift 2 ;;
        --enable) enabled=1; shift ;;
        --no-enable) enabled=0; shift ;;
        *) die "未知参数：$1" ;;
    esac
done
[[ $EUID -eq 0 ]] || die '请以 root 运行'
[[ $(uname -s) == Linux ]] || die '需要 Linux 服务器'
[[ -f $token_file && -r $token_file ]] || die '请提供可读的对接码文件'
command -v python3 >/dev/null || die '缺少 python3'
command -v curl >/dev/null || die '缺少 curl'
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
printf '私有配置保存在 %s，安装失败时保留。\n' "$state_dir" >&2
python3 "$tools_dir/import_relay_token.py" "$token_file" "$state_dir"
printf '客户端配置：%s/client.json\n' "$state_dir" >&2
if (( enabled )); then
    bash "$tools_dir/one_click_install.sh" "$state_dir/relay"
else
    bash "$tools_dir/one_click_install.sh" "$state_dir/relay" --no-enable
fi
if ! /opt/wg-hy2-relay/sing-box check -c "$state_dir/client.json" >/dev/null 2>&1; then
    die '客户端配置校验失败；中转服务可能已安装，请检查服务及保留的配置'
fi
printf '安装完成。客户端配置：%s/client.json\n' "$state_dir"
