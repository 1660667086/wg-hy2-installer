#!/usr/bin/env bash
# Interactive entry point; /dev/tty keeps prompts working when piped from curl.
set -Eeuo pipefail

readonly BASE_URL='https://raw.githubusercontent.com/1660667086/wg-hy2-installer/main'

fail() { printf '错误：%s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || fail '请使用 root 运行，或者通过 sudo bash 启动。'
[[ $(uname -s) == Linux ]] || fail '此安装菜单需要 Linux。'
command -v curl >/dev/null 2>&1 || fail '请先安装 curl。'
if ! { exec 3<>/dev/tty; } 2>/dev/null; then
    fail '需要交互式终端；请通过 SSH 登录后运行命令。'
fi

ask() {
    local variable=$1 prompt=$2 default=${3-} __menu_input
    if [[ -n $default ]]; then
        printf '%s [%s]: ' "$prompt" "$default" >&3
    else
        printf '%s: ' "$prompt" >&3
    fi
    IFS= read -r __menu_input <&3 || fail '终端输入结束。'
    [[ -n $__menu_input ]] || __menu_input=$default
    printf -v "$variable" '%s' "$__menu_input"
}

ask_required() {
    local var=$1 prompt=$2
    while :; do
        ask "$var" "$prompt"
        [[ -n ${!var} ]] && return
        printf '此项不能为空。\n' >&3
    done
}

ask_port() {
    local var=$1 prompt=$2 default=$3 value
    while :; do
        ask value "$prompt" "$default"
        if [[ $value =~ ^[0-9]{1,5}$ ]] && (( 10#$value >= 1 && 10#$value <= 65535 )); then
            printf -v "$var" '%s' "$((10#$value))"
            return
        fi
        printf '请输入 1 到 65535 之间的端口。\n' >&3
    done
}

ask_enable() {
    local answer
    while :; do
        ask answer '开机自动启动？输入 y 或 n' n
        case $answer in
            y|Y) enable_arg=(--enable); return ;;
            n|N) enable_arg=(); return ;;
            *) printf '请输入 y 或 n。\n' >&3 ;;
        esac
    done
}

run_install() {
    local file=$1
    shift
    local tmp status
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/wg-hy2-menu.XXXXXXXX") || fail '无法创建临时目录。'
    chmod 700 "$tmp"
    if ! curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
        --output "$tmp/$file" "$BASE_URL/$file"; then
        rm -rf -- "$tmp"
        printf '下载失败，未运行安装脚本。\n' >&3
        return 1
    fi
    printf '\n开始安装，现有同名服务或端口被占用时会停止。\n' >&3
    status=0
    bash "$tmp/$file" "$@" <&3 || status=$?
    rm -rf -- "$tmp"
    if (( status != 0 )); then
        printf '\n安装未完成（退出码 %s）；请检查上面的提示及可能已安装的服务。\n' "$status" >&3
    fi
    return "$status"
}

install_single() {
    local server_ip hy2_port wg_port
    local -a enable_arg=()
    ask_required server_ip '本机公网 IPv4'
    ask_port hy2_port 'HY2 UDP 端口' 443
    ask_port wg_port 'WireGuard UDP 端口' 51820
    ask_enable
    printf '\n将在本机安装单机出口；云安全组需允许 UDP %s。\n' "$hy2_port" >&3
    run_install server_single_install.sh --server-ip "$server_ip" \
        --hy2-port "$hy2_port" --wg-port "$wg_port" ${enable_arg[@]+"${enable_arg[@]}"} || true
}

install_dual() {
    local relay_ip landing_ip landing_host landing_user ssh_port identity hy2_port wg_port
    local -a enable_arg=() identity_arg=()
    printf '\n请在中转服务器运行此项。中转机需预先通过 SSH 密钥登录落地机。\n' >&3
    ask_required relay_ip '中转机公网 IPv4'
    ask_required landing_ip '落地机公网 IPv4'
    ask landing_host '落地机 SSH 地址' "$landing_ip"
    ask landing_user '落地机 SSH 用户（必须具有 root 权限）' root
    ask_port ssh_port '落地机 SSH 端口' 22
    ask identity 'SSH 私钥路径（留空使用 SSH 默认密钥或 agent）'
    [[ -z $identity ]] || identity_arg=(--identity "$identity")
    ask_port hy2_port 'HY2 UDP 端口' 443
    ask_port wg_port 'WireGuard UDP 端口' 51820
    ask_enable
    printf '\n中转机云安全组需允许 UDP %s；落地机需允许中转机访问 UDP %s。\n' "$hy2_port" "$wg_port" >&3
    run_install server_dual_install.sh --relay-ip "$relay_ip" --landing-ip "$landing_ip" \
        --landing-host "$landing_host" --landing-user "$landing_user" --ssh-port "$ssh_port" \
        --hy2-port "$hy2_port" --wg-port "$wg_port" ${identity_arg[@]+"${identity_arg[@]}"} ${enable_arg[@]+"${enable_arg[@]}"} || true
}

choose_service() {
    local choice
    while :; do
        printf '\n选择本机服务：1) 单机  2) 中转  3) 落地  0) 返回\n' >&3
        ask choice '请输入选项'
        case $choice in
            1) selected_service=wg-hy2-single.service; return 0 ;;
            2) selected_service=wg-hy2-relay.service; return 0 ;;
            3) selected_service=wg-hy2-landing.service; return 0 ;;
            0) return 1 ;;
            *) printf '请输入有效选项。\n' >&3 ;;
        esac
    done
}

manage_service() {
    local choice selected_service
    command -v systemctl >/dev/null 2>&1 || { printf '本机未找到 systemctl。\n' >&3; return; }
    choose_service || return
    if [[ $(systemctl show "$selected_service" -p LoadState --value 2>/dev/null) != loaded ]]; then
        printf '本机没有找到 %s。\n' "$selected_service" >&3
        return
    fi
    printf '\n1) 查看状态  2) 启动  3) 停止  4) 启用开机启动  5) 关闭开机启动  0) 返回\n' >&3
    ask choice '请输入操作'
    case $choice in
        1) systemctl --no-pager --full status "$selected_service" >&3 2>&3 || true ;;
        2) systemctl start "$selected_service" >&3 2>&3 || true ;;
        3) systemctl stop "$selected_service" >&3 2>&3 || true ;;
        4) systemctl enable "$selected_service" >&3 2>&3 || true ;;
        5) systemctl disable "$selected_service" >&3 2>&3 || true ;;
        0) return ;;
        *) printf '无效选项。\n' >&3 ;;
    esac
}

while :; do
    printf '\n=== WG + HY2 安装管理 ===\n1) 安装单机出口\n2) 安装双机（在中转机执行）\n3) 管理本机服务\n0) 退出\n' >&3
    ask choice '请选择'
    case $choice in
        1) install_single ;;
        2) install_dual ;;
        3) manage_service ;;
        0) printf '已退出。\n' >&3; exit 0 ;;
        *) printf '请输入有效选项。\n' >&3 ;;
    esac
done
