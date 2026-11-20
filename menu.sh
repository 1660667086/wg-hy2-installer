#!/usr/bin/env bash
# Interactive entry point; /dev/tty keeps prompts working when piped from curl.
set -Eeuo pipefail

readonly BASE_URL='https://raw.githubusercontent.com/1660667086/wg-hy2-installer/main'

fail() { printf '错误：%s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || fail '请使用 root 运行，或者通过 sudo bash 启动。'
[[ $(uname -s) == Linux ]] || fail '此安装菜单需要 Linux。'
command -v curl >/dev/null 2>&1 || fail '请先安装 curl。'
command -v python3 >/dev/null 2>&1 || fail '请先安装 Python 3.8 或更新版本（python3）。'
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' \
    || fail "需要 Python 3.8 或更新版本，当前是 $(python3 -V 2>&1)。"
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
    # Drop surrounding spaces or a pasted CR, e.g. from an IP copied out of a console.
    __menu_input=${__menu_input#"${__menu_input%%[![:space:]]*}"}
    __menu_input=${__menu_input%"${__menu_input##*[![:space:]]}"}
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

prepare_landing() {
    local relay_ip landing_ip hy2_port wg_port
    local -a enable_arg=()
    printf '\n请在落地服务器上运行。中转机出口 IP 必须是落地机实际看到的公网源地址。\n' >&3
    ask_required relay_ip '中转机实际公网出口 IPv4'
    ask_required landing_ip '本落地机公网 IPv4'
    ask_port hy2_port '中转机将使用的 HY2 UDP 端口' 443
    ask_port wg_port '本落地机 WireGuard UDP 端口' 51820
    ask_enable
    printf '请在落地机安全组允许来自中转机的 UDP %s。\n' "$wg_port" >&3
    run_install server_landing_prepare.sh --relay-ip "$relay_ip" --landing-ip "$landing_ip" \
        --hy2-port "$hy2_port" --wg-port "$wg_port" ${enable_arg[@]+"${enable_arg[@]}"} || true
}

show_handoff() {
    local token=/root/wg-hy2-landing-handoff/relay-token.txt
    if [[ ! -f $token || ! -f /root/wg-hy2-landing-handoff/installed ]]; then
        printf '没有找到已完成安装的对接信息，请先在本机完成落地安装。\n' >&3
        return
    fi
    printf '\n对接信息包含私钥，请只发送给可信的中转机，切勿公开。复制下面全部内容：\n' >&3
    cat "$token" >&3
    printf '\n复制完成后，在中转机的导入界面粘贴，并单独输入 END。\n' >&3
}

import_relay() {
    local dir line status=0 count=0 ended=0
    local -a enable_arg=()
    ask_enable
    dir=$(mktemp -d /tmp/wg-hy2-import.XXXXXXXX) || fail '无法创建临时目录。'
    chmod 700 "$dir"
    : > "$dir/token"
    chmod 600 "$dir/token"
    trap 'rm -f -- "$dir/token"; rmdir -- "$dir"; exit 130' INT TERM
    printf '\n请粘贴落地机生成的全部对接信息，最后另起一行输入 END。对接信息包含私钥。\n' >&3
    while IFS= read -r line <&3; do
        # Accept "END" with stray spaces or a CR from a Windows clipboard.
        if [[ ${line//[[:space:]]/} == END ]]; then
            ended=1
            break
        fi
        count=$((count + ${#line} + 1))
        if (( count > 131072 )); then
            printf '对接信息过长，已取消。\n' >&3
            rm -f -- "$dir/token"
            rmdir -- "$dir"
            trap - INT TERM
            return 1
        fi
        printf '%s\n' "$line" >> "$dir/token"
    done
    if (( ! ended )); then
        printf '输入中断，未安装。\n' >&3
        rm -f -- "$dir/token"
        rmdir -- "$dir"
        trap - INT TERM
        return
    fi
    run_install server_relay_import.sh --token-file "$dir/token" ${enable_arg[@]+"${enable_arg[@]}"} || status=$?
    rm -f -- "$dir/token"
    rmdir -- "$dir"
    trap - INT TERM
    return "$status"
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

install_panel() {
    local mode public_ip port template answer
    local -a template_arg=() autostart_arg=()
    printf '\n在单机服务器或双机落地机安装面板。用户变更会短暂重启正在运行的服务。\n' >&3
    while :; do
        ask mode '请选择管理对象：1 单机，2 落地机，0 返回'
        case $mode in
            1) mode=single; break ;;
            2) mode=landing; break ;;
            0) return ;;
            *) printf '请输入有效选项。\n' >&3 ;;
        esac
    done
    ask_required public_ip '本面板服务器公网 IPv4'
    ask_port port '面板 HTTPS TCP 端口' 8443
    ask template '客户端模板绝对路径（默认使用本机安装时生成的模板）'
    [[ -z $template ]] || template_arg=(--template "$template")
    while :; do
        ask answer '面板开机自动启动？输入 y 或 n' n
        case $answer in
            y|Y) autostart_arg=(--enable-panel); break ;;
            n|N) break ;;
            *) printf '请输入 y 或 n。\n' >&3 ;;
        esac
    done
    printf '请在云安全组放行 TCP %s，建议限制管理员来源 IP。到期检查任务将开机自启。\n' "$port" >&3
    run_install panel_install.sh --role "$mode" --public-ip "$public_ip" --port "$port" \
        ${template_arg[@]+"${template_arg[@]}"} ${autostart_arg[@]+"${autostart_arg[@]}"} || true
}

while :; do
    printf '\n=== WG + HY2 安装管理 ===\n1) 安装单机出口\n2) 安装双机（中转机通过 SSH 安装落地机）\n3) 管理本机服务\n4) 安装落地机并生成对接信息\n5) 在中转机导入对接信息并安装\n6) 显示落地机对接信息\n7) 安装可视化管理面板\n0) 退出\n' >&3
    ask choice '请选择'
    case $choice in
        1) install_single ;;
        2) install_dual ;;
        3) manage_service ;;
        4) prepare_landing ;;
        5) import_relay || true ;;
        6) show_handoff ;;
        7) install_panel ;;
        0) printf '已退出。\n' >&3; exit 0 ;;
        *) printf '请输入有效选项。\n' >&3 ;;
    esac
done
