# WG + HY2 Installer

使用 sing-box 1.14.4 部署 WireGuard over Hysteria2。项目支持单服务器直出，以及经中转服务器连接落地服务器两种方式。配置在可信设备上生成，每次生成独立的密钥；不会使用项目作者的服务器或凭据。

## 快速开始

1. 准备一台满足[系统要求](#系统要求)的 Linux 服务器，先装好依赖：

   ```sh
   sudo apt update && sudo apt install -y curl python3 openssl iptables tar   # Debian/Ubuntu
   sudo dnf install -y python3 openssl iptables-nft tar                       # Rocky/Alma/RHEL 9
   ```

   Rocky/Alma/RHEL 9 通常已自带 `curl-minimal`，不要再单独安装 `curl`，否则 dnf 会报软件包冲突。

2. 通过 SSH 登录服务器，执行以下命令打开中文菜单：

   ```sh
   curl -fsSL https://raw.githubusercontent.com/1660667086/wg-hy2-installer/main/install.sh | sudo bash
   ```

3. 按下表选择菜单项，并在云安全组放行对应端口（见[云安全组与主机防火墙](#云安全组与主机防火墙)）。

| 菜单 | 用途 | 在哪台服务器运行 |
| --- | --- | --- |
| 1 | 安装单机出口 | 单机服务器 |
| 2 | 安装双机：中转机通过 SSH 安装落地机 | 中转机 |
| 3 | 查看状态、启动/停止、开关开机自启 | 任意已安装的服务器 |
| 4 | 安装落地机并生成对接信息（不用 SSH 的双机流程） | 落地机 |
| 5 | 粘贴对接信息，安装中转机 | 中转机 |
| 6 | 再次显示落地机的对接信息 | 落地机 |
| 7 | 安装可视化多用户管理面板 | 单机服务器或落地机 |

安装完成后，客户端配置路径会显示在屏幕上（单机默认在 `/root/wg-hy2-single/bundle/client/client.json`）。它包含私钥，只能通过可信的加密方式传给自己的设备。

安装开始前，脚本会先检查参数、端口占用、systemd 和 iptables 等条件；检查不通过时不会生成密钥，也不会改动系统，按提示处理后直接重试即可。

## 两种部署方式

**单服务器**：客户端 → Hysteria2 → 同一台服务器上的 WireGuard → 互联网。只需在这一台服务器部署服务。

**双服务器**：客户端 → Hysteria2 → 中转服务器 → WireGuard → 落地服务器 → 互联网。中转机和落地机分别部署各自的服务。如果落地机已有兼容的 WireGuard 服务，需要另行配置匹配的 peer；本项目的流程是在落地机部署独立的 sing-box 服务。

双机模式下 WireGuard 从客户端加密到落地机；Hysteria2 包裹客户端到中转的那段流量。中转到落地通过 WireGuard 加密。客户端配置不直接写入落地公网 IP，但使用者仍可通过公网 IP 查询服务看到实际出口地址。

> **客户端兼容性：**客户端必须支持 sing-box 的 WireGuard endpoint 和 Hysteria2 detour；原版 WireGuard 客户端不能直接导入或运行此组合。

## 系统要求

服务器需要：

- 使用 systemd 的 Linux，并以 root 运行（或通过 `sudo`）。
- **Python 3.8 或更新版本**。例如 Debian 11+、Ubuntu 20.04+、Rocky/Alma/RHEL 9 自带的版本都满足；CentOS 7、Ubuntu 18.04、Debian 10 以及 Rocky/Alma/RHEL 8 自带的 `python3` 版本过低，需先安装更新的 Python 并让 `python3` 指向它。
- OpenSSL 1.1.1 或更新版本、curl、tar、sha256sum（或 shasum）。
- iptables；内核启用了 IPv6 时还需要 ip6tables（通常与 iptables 同一个软件包）。精简系统可能默认没有 iptables，可用 `apt install -y iptables`（Debian/Ubuntu）或 `dnf install -y iptables-nft`（Rocky/Alma/RHEL 9）安装。
- 缺少匹配的 sing-box 时，脚本会自动下载 sing-box 1.14.4 官方发布包并校验固定的 SHA-256。菜单 1、2、4 的自动下载只支持 x86_64（amd64）和 aarch64（arm64）；`one_click_install.sh`（菜单 5 及进阶用法使用）还支持 386、armv5/v6/v7、loong64、ppc64le、riscv64 和 s390x。其他情况请把带 `with_gvisor`、`with_quic`、`with_wireguard` 编译标签的 sing-box 1.14.4 放进 PATH，脚本会自动使用它。

当前客户端配置是 sing-box 格式，默认所有公网 IPv4 走隧道，并不包含国内外或 AI 分流。

## 服务器交互式安装

菜单可选择单机出口、双机部署或管理本机服务，并可设置 HY2 和 WireGuard 端口及开机启动。输入时首尾多余的空格会被忽略。

**菜单 2（经 SSH 的双机）**在中转机执行。中转机必须能以 SSH 密钥无交互登录落地机：

- 首次连接前，先在中转机上手动执行一次 `ssh -p 端口 用户@落地机`，核对并接受 SSH 主机指纹。
- 落地机用户默认是 root；如果云服务器禁止 root 登录（例如只能用 `ubuntu`、`ec2-user` 等用户），可填写该用户，但它必须能**免密码 sudo**。
- 脚本会先检查落地机的 SSH、权限、Python 版本、端口和依赖，全部通过后才生成密钥并安装。

`install.sh` 将菜单和项目安装代码打包在同一个脚本内；若服务器缺少匹配的 sing-box，安装时还需要联网下载并校验官方二进制。修改项目源码后，可在仓库内运行 `python3 build_single_file.py` 重新生成该脚本（`python3 build_single_file.py --check` 可检查是否需要重新生成）。

也可不使用两台服务器之间的 SSH：

1. 在落地机运行同一条菜单命令，选 **4**，填写中转机实际公网出口 IPv4、落地机公网 IPv4 和端口，完成落地机安装。
2. 在落地机菜单选 **6**，显示并复制对接信息。它包含客户端私钥和认证信息，应只通过可信的加密连接传递。
3. 在中转机运行同一条菜单命令，选 **5**，粘贴全部对接信息，再另起一行输入 `END`。脚本会安装中转服务，并提示中转机上的客户端配置路径。

落地机安全组须允许中转机的实际公网出口访问 WireGuard UDP 端口；中转机安全组须允许客户端访问 HY2 UDP 端口。此流程的两台服务器都能读取客户端私钥，应确保都可信。对接信息不具备加密或来源认证，只能导入可信落地机生成的内容。

单机配置保存在 `/root/wg-hy2-single/bundle/`；双机客户端配置默认保存在中转机 `/root/wg-hy2-client.json`，完整恢复包位于 `/root/wg-hy2-recovery.*`。这些目录含有私钥和连接凭据；双机流程在中转机生成并保存完整配置，因此中转机必须可信。安装中途失败时请查看屏幕上的恢复路径和两端服务状态，不要直接重复安装。

## 云安全组与主机防火墙

| 模式与服务器 | 入站规则 |
| --- | --- |
| 单机 | 允许 HY2 UDP 端口（默认 443）；WG 端口无需向公网开放 |
| 双机中转 | 允许 HY2 UDP 端口（默认 443） |
| 双机落地 | 仅允许中转机实际公网出口 IPv4 访问 WG UDP 端口（默认 51820） |
| 管理面板 | 允许面板 HTTPS TCP 端口（默认 8443），建议只放行管理员 IP |

服务启动时会自动添加所需的 iptables 规则，停止时自动移除；它们不会修改云安全组。服务端默认拒绝客户端通过 WG 访问私网、环回、链路本地和云元数据地址。注意中转机使用 NAT 时，落地机看到的源地址可能不同于机器内部地址。

如果服务器启用了 **firewalld**（Rocky/Alma/RHEL 等常见），它会另外拦截这些端口，需要额外放行；安装器检测到 firewalld 时会打印对应命令，例如：

```sh
sudo firewall-cmd --permanent --add-port=443/udp && sudo firewall-cmd --reload
```

ufw 一般不影响 sing-box 服务的端口（服务启动时把规则插在 INPUT 链最前面），但会拦截管理面板端口，需要 `sudo ufw allow 8443/tcp`。

## 常见问题

**“安装前检查未通过”**：上一行的 `error:` 就是原因，此时没有生成任何配置，处理后直接重试：

- `iptables ... are required`：安装 iptables（见[系统要求](#系统要求)）。
- `UDP port 443 is not available`：端口已被其他程序占用（例如另一个 Hysteria2），可用 `sudo ss -ulpn | grep ':443 '` 查看，换一个 HY2 端口或停掉占用它的程序。
- `systemd unit already exists` / `Installation directory or unit already exists`：本机已经装过同名服务；如需重装，先按[卸载](#卸载)删除旧服务。

**重试时提示目录已存在**：单机（菜单 1）和落地机（菜单 4）在确认服务未安装后，会把上次未完成安装留下的目录自动改名为 `*.old-日期时间` 再继续。双机（菜单 2）若提示 `/root/wg-hy2-client.json` 已存在，说明之前安装过或中途失败过；确认不再需要后删除该文件再重试。

**服务装好但启动失败**：安装器会显示服务的最近日志并自动回滚；也可以执行 `sudo journalctl -u wg-hy2-single -n 50`（把 `single` 换成 `relay` 或 `landing`）查看。

**客户端连不上**：依次检查云安全组是否放行 UDP 端口、是否启用了 firewalld（见上文）、服务状态（菜单 3）、客户端是否支持 sing-box 的 WireGuard endpoint，以及客户端配置是否来自本次安装。

## 卸载

以单机服务 `single` 为例；中转机和落地机把 `single` 换成 `relay` 或 `landing`：

```sh
sudo systemctl disable --now wg-hy2-single.service   # 停止时会移除该服务添加的防火墙规则
sudo rm -f /etc/systemd/system/wg-hy2-single.service
sudo systemctl daemon-reload
sudo rm -rf /opt/wg-hy2-single
```

客户端配置和私钥另外保存在 `/root/wg-hy2-single`、`/root/wg-hy2-landing-handoff`、`/root/wg-hy2-recovery.*`、`/root/wg-hy2-relay-import.*` 或 `/root/wg-hy2-client.json` 中；确认不再需要后再删除。自动下载的 sing-box 位于 `/usr/local/libexec/wg-hy2/`，没有其他实例使用时可以删除。

卸载管理面板：

```sh
sudo systemctl disable --now wg-hy2-panel.service wg-hy2-panel-expire.timer
sudo rm -f /etc/systemd/system/wg-hy2-panel.service \
  /etc/systemd/system/wg-hy2-panel-expire.service /etc/systemd/system/wg-hy2-panel-expire.timer
sudo systemctl daemon-reload
sudo rm -rf /opt/wg-hy2-panel
```

面板添加的用户已写入服务配置，卸载面板后这些用户仍可连接，且不再自动到期；请先在面板中停用不再需要的用户，再卸载面板。

## 在可信电脑上生成配置

以下是不使用服务器菜单的进阶方式。需要一台可信的配置生成设备，安装 Python 3.8+、OpenSSL 和带 `with_gvisor`、`with_quic`、`with_wireguard` 的 sing-box 1.14.4。从 [sing-box 官方 v1.14.4 发布页](https://github.com/SagerNet/sing-box/releases/tag/v1.14.4)获取对应平台的二进制文件，并自行核验官方发布的校验值。生成配置的设备和服务器可以使用不同架构的二进制，但版本和所需编译标签必须匹配。

每个客户端应使用独立的 WireGuard peer。不要让多台设备同时使用同一份客户端配置，否则 peer 地址可能互相抢占。示例中的变量需要自行替换；请在可信环境运行，**不要把生成的部署包提交到 GitHub 或发送给不需要它的人**。

在可信设备上克隆仓库，准备指向本机 sing-box 的路径，并为部署包选择一个不存在的目录：

```sh
git clone https://github.com/1660667086/wg-hy2-installer.git
cd wg-hy2-installer
LOCAL_SING_BOX="$(command -v sing-box)"
BUNDLE="$HOME/wg-hy2-private-bundle"
```

单机：

```sh
python3 wg_hy2_generate.py --mode single \
  --entry-ip '<单机服务器公网IPv4>' \
  --sing-box "$LOCAL_SING_BOX" --output "$BUNDLE"
```

双机：

```sh
python3 wg_hy2_generate.py --mode dual \
  --entry-ip '<中转机公网IPv4>' --exit-ip '<落地机公网IPv4>' \
  --sing-box "$LOCAL_SING_BOX" --output "$BUNDLE"
```

默认 HY2 端口为 UDP 443，WG 端口为 UDP 51820，可分别用 `--hy2-port` 和 `--wg-port` 调整。输出目录必须不存在。生成器会创建只允许所有者访问的目录和配置文件。只想检查地址和端口参数时，可追加 `--check-only`（不需要 sing-box，也不生成文件）。

单机部署包包含 `single/` 与 `client/`；双机部署包包含 `relay/`、`landing/` 与 `client/`。若在可信电脑上生成，应只把对应角色的目录发送到对应服务器，不要把客户端配置或整个部署包传给中转机。服务器交互菜单则在中转机生成并保留完整恢复包，需要信任中转机。配置文件和角色目录包含不同的密钥或连接凭据，应妥善保存。

## 安装服务器角色

将仓库里的 `wg_hy2_install.py`、`one_click_install.sh`、对应角色目录，以及适合目标服务器架构并已校验的 sing-box 二进制，通过 SSH/SCP 等经过认证的安全通道复制到对应服务器。不要把本机 macOS 二进制直接用于 Linux 服务器。

最简安装方式是在每台服务器上运行一条命令。双机模式先在落地机执行，再在中转机执行：

```sh
sudo ./one_click_install.sh /安全路径/landing
sudo ./one_click_install.sh /安全路径/relay
```

一键脚本会优先使用 PATH 中且版本/编译标签匹配的 `sing-box`；找不到时，只在支持的 Linux 架构上下载 sing-box 1.14.4 官方发布包，并校验脚本内固定的 SHA-256 后再安装到 `/usr/local/libexec/wg-hy2/`。也可以显式指定已核验的二进制：

```sh
sudo ./one_click_install.sh /安全路径/landing \
  --sing-box /安全路径/sing-box
```

脚本会从 `manifest.json` 自动识别 `landing`、`relay` 或 `single` 角色，并以该角色作为服务名；如需多个同角色实例，可追加 `--name NAME`。安装器不会主动覆盖同名已有部署。`--no-enable` 表示启动服务但不设置开机自启，`--dry-run` 表示只检查不修改系统：

```sh
sudo ./one_click_install.sh /安全路径/landing --no-enable
sudo ./one_click_install.sh /安全路径/relay --dry-run \
  --sing-box /安全路径/sing-box
```

`--dry-run` 不会下载二进制，因此需要已有匹配的 `sing-box`。如果不希望脚本下载，可以直接使用不下载的轻量包装器 `install_role.sh`，或直接调用 `wg_hy2_install.py`；`--sing-box` 默认使用 PATH 中的 `sing-box`，`--name` 默认使用 manifest 中的角色名。双机模式仍须分别安装 `landing/` 和 `relay/`，不能把同一角色目录同时放到两台服务器。

不需要部署包、只想检查某台服务器能否安装时，可以运行 `sudo python3 wg_hy2_install.py --preflight --name single --hy2-port 443 --wg-port 51820`，它检查 root、systemd、iptables、同名服务和端口占用，不做任何修改。

## 从本机一键部署双机

如果希望从生成配置的可信电脑一次拉起两台服务器，可以使用 `deploy_dual.sh`。它会先检查两台服务器（SSH 登录、权限、Python、端口和依赖），都通过后才在本机生成临时私有包，并**在改动任何服务器之前**把客户端配置保存到本机；之后通过 SSH 密钥/agent 分别上传 `landing` 和 `relay`，按“先落地、后中转”的顺序安装，远端临时目录由 `mktemp` 创建并在结束后清理。脚本不接受或保存服务器密码，也不会自动修改云安全组：

```sh
./deploy_dual.sh \
  --relay-host '<中转机SSH地址>' \
  --landing-host '<落地机SSH地址>' \
  --relay-ip '<中转机公网IPv4>' \
  --landing-ip '<落地机公网IPv4>' \
  --identity ~/.ssh/id_ed25519 \
  --sing-box /本机路径/sing-box \
  --client-output ./client.json
```

如果 SSH 地址本身就是公网 IPv4，可省略 `--relay-ip` 和 `--landing-ip`。默认远端服务启动但不设置开机自启；追加 `--enable` 才会启用开机自启。`--ssh-user` 默认是 root；使用其他用户时，该用户必须能免密码 sudo。两台服务器必须允许对应 SSH 登录，且本机已能使用 `ssh`/`scp` 密钥无交互登录；落地机仍需在云安全组放行来自中转机实际出口地址的 WG UDP 端口，中转机放行 HY2 UDP 端口。该脚本使用默认端口（HY2 443、WG 51820）。

## 在服务器上从 GitHub 一键拉取

如果你已经把私有的 `landing/` 或 `relay/` 角色目录上传到服务器，可以不复制整个仓库，直接在服务器执行：

```sh
curl -fsSL https://raw.githubusercontent.com/1660667086/wg-hy2-installer/main/bootstrap.sh \
  | sudo bash -s -- /安全路径/landing --no-enable
```

中转机把路径换成 `/安全路径/relay`，同样执行一次。`bootstrap.sh` 会从 GitHub 拉取安装器和一键脚本到临时目录，安装结束后自动清理。若服务器没有匹配的 sing-box，它会继续从官方发布地址下载并校验固定 SHA-256。**角色目录含有私钥和密码，不能从公开 GitHub 拉取；必须通过 SSH/SCP 等安全方式单独上传。**

如果连角色目录也不想从本机上传，可以直接在**中转服务器**执行下面这一条命令（与菜单 2 相同）。它会先检查中转机和落地机，然后在中转机生成私有配置，通过 SSH 把 `landing` 角色发送到落地服务器并安装，最后在中转机本地安装 `relay`：

```sh
curl -fsSL https://raw.githubusercontent.com/1660667086/wg-hy2-installer/main/server_dual_install.sh \
  | sudo bash -s -- \
      --relay-ip '<中转机公网IPv4>' \
      --landing-host '<落地机SSH地址>' \
      --landing-ip '<落地机公网IPv4>' \
      --identity /root/.ssh/id_ed25519
```

这条命令不依赖本机电脑，但要求中转服务器能用 SSH 密钥无交互登录落地服务器（默认 root；用 `--landing-user` 指定其他用户时须能免密码 sudo），并且已安装 `curl`、`scp`、`ssh`、`python3`、`tar`。客户端配置会保存在中转机的 `/root/wg-hy2-client.json`；默认不设置开机自启，追加 `--enable` 才启用。

## 客户端使用与限制

`client/client.json` 是 sing-box 客户端配置示例，需要导入支持这些配置项的 sing-box 客户端。它不适用于原版 WireGuard 客户端，也不是 OpenClash 的配置文件。示例仅配置 IPv4，并为设备本身生成 TUN 配置；IPv6 流量可能绕过隧道，需要自行处理。默认公网 IPv4 流量走隧道、私网流量直连；不自带国内外或 AI 分流规则，也不包含 L2TP 服务。

路由器透明代理、局域网 DNS、防火墙和已有 OpenClash 的切换需要按具体设备单独配置；请勿把通用客户端配置直接覆盖正在工作的路由器配置。项目不会自动修改路由器、OpenClash、系统 DNS 或用户已有服务。

## 可视化用户面板

在单机服务器或双机**落地机**运行上面的 `install.sh`，选择菜单 **7**。面板需要已安装的对应服务和匹配的原始客户端模板；通过中转机 SSH 安装的双机模式，客户端模板可能在中转机的私有恢复包中，须安全复制到落地机，并在安装面板时填写其绝对路径。支持 OpenSSL X25519 的 Linux 服务器才可使用；安装前会检查密钥与模板是否匹配。

按提示设置公网 IPv4、HTTPS 端口和至少 12 位的管理密码（输错可重新输入）。默认 HTTPS TCP 端口 8443，云安全组需放行，建议限制来源为管理员 IP；启用了 firewalld 或 ufw 时，安装结束会提示放行命令。安装会显示自签证书 SHA-256 指纹：浏览器会警告证书不受信任，须通过可信 SSH 通道核对指纹；自签证书不能自动证明服务器身份，证书有效期为一年。

面板可添加独立 WireGuard 用户、下载客户端配置、停用/启用并修改到期时间。双机沿用既有 HY2 口令；到期会撤销对应 WireGuard peer，但不会单独撤销共享 HY2 认证。修改用户会重启正在运行的对应 sing-box 服务，造成短暂断线；已有原始用户保留，不会自动纳入面板列表。

到期检查通过独立 systemd 定时器约每 30 秒运行，服务器断电、定时器故障或系统时间不准确时可能延迟；重启后约 10 秒进行首次检查。安装时定时器会设置开机自启，面板默认不自启（可在安装时选择）；这不改变 sing-box 的开机自启设置。用户到期停用后续费，需要更新到期时间再点“启用”。

```sh
sudo systemctl start wg-hy2-panel.service    # 手动启动网页面板
sudo systemctl stop wg-hy2-panel.service     # 手动停止网页面板
sudo systemctl status wg-hy2-panel-expire.timer  # 查看独立到期检查
```

## 安全

项目生成带 IP 地址校验的自签名证书并在客户端固定信任该证书；它不等于受公网 CA 信任的网站证书，也不保证流量无法被识别。配置文件含密码、WireGuard 私钥和其他凭据；泄露后应更换对应配置。

建议先用隔离测试验证客户端出口及 DNS，再部署到实际网络；生产环境还要检查云防火墙、主机防火墙、断线后的恢复和 IPv6 行为。项目目录不应包含真实服务器地址、私人配置或密钥。

## 运行测试

不指定 `SING_BOX` 时运行 Python 单元测试和 shell 流程测试；端到端测试会跳过。指定支持所需编译标签的 sing-box 1.14.4 可执行文件可运行完整测试：

```sh
python3 -m unittest discover -s tests -v
SING_BOX="$(command -v sing-box)" python3 -m unittest discover -s tests -v
```

shell 流程测试用假的 `ssh`、`scp`、`sudo`、`curl` 和 `sing-box` 在本机运行各安装脚本，不连接服务器，也不修改 systemd、防火墙或 `/root`。端到端测试只在临时配置中将 TUN 换成本地 SOCKS，不修改系统路由或防火墙；它会短暂在本机所有网卡上监听随机 WireGuard UDP 端口，并使用临时随机密钥。系统服务和云端网络仍需要在自己的环境中验证。

## 许可证

本项目采用 [MIT License](LICENSE)。sing-box 及其他依赖使用各自的许可证。
