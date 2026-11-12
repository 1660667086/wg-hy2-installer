# WG + HY2 Installer

使用 sing-box 1.14.4 部署 WireGuard over Hysteria2。项目支持单服务器直出，以及经中转服务器连接落地服务器两种方式。配置在可信设备上生成，每次生成独立的密钥；不会使用项目作者的服务器或凭据。

## 两种部署方式

**单服务器**：客户端 → Hysteria2 → 同一台服务器上的 WireGuard → 互联网。只需在这一台服务器部署服务。

**双服务器**：客户端 → Hysteria2 → 中转服务器 → WireGuard → 落地服务器 → 互联网。中转机和落地机分别部署各自的服务。如果落地机已有兼容的 WireGuard 服务，需要另行配置匹配的 peer；本项目的流程是在落地机部署独立的 sing-box 服务。

双机模式下 WireGuard 从客户端加密到落地机；Hysteria2 包裹客户端到中转的那段流量。中转到落地通过 WireGuard 加密。客户端配置不直接写入落地公网 IP，但使用者仍可通过公网 IP 查询服务看到实际出口地址。

> **客户端兼容性：**客户端必须支持 sing-box 的 WireGuard endpoint 和 Hysteria2 detour；原版 WireGuard 客户端不能直接导入或运行此组合。

## 使用前准备

- 一台可信的配置生成设备，安装 Python 3、OpenSSL 和带 `with_gvisor`、`with_quic`、`with_wireguard` 的 sing-box 1.14.4。
- 单机模式需要一台有公网 IPv4 的 Linux 服务器；双机模式需要两台。服务器端需要 systemd、iptables/ip6tables、Python 3，以及与服务器架构匹配的 sing-box 1.14.4。
- 从 [sing-box 官方 v1.14.4 发布页](https://github.com/SagerNet/sing-box/releases/tag/v1.14.4)获取对应平台的二进制文件，并自行核验官方发布的校验值。生成配置的设备和服务器可以使用不同架构的二进制，但版本和所需编译标签必须匹配。
- 云防火墙或安全组允许入口服务器的 HY2 UDP 端口。双机模式还需让落地服务器的 WG UDP 端口接收来自**中转机实际公网出口 IPv4**的流量。不要对公网开放单机内部的 WG 端口。
- 每个客户端应使用独立的 WireGuard peer。不要让多台设备同时使用同一份客户端配置，否则 peer 地址可能互相抢占。

示例中的变量需要自行替换；请在可信环境运行，**不要把生成的部署包提交到 GitHub 或发送给不需要它的人**。

## 生成配置

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

默认 HY2 端口为 UDP 443，WG 端口为 UDP 51820，可分别用 `--hy2-port` 和 `--wg-port` 调整。输出目录必须不存在。生成器会创建只允许所有者访问的目录和配置文件。

单机部署包包含 `single/` 与 `client/`；双机部署包包含 `relay/`、`landing/` 与 `client/`。**只把对应角色的目录发送到对应服务器**；不要把 `client/` 或整个部署包传给中转机。配置文件和角色目录包含不同的密钥或连接凭据，应妥善保存。

## 安装服务器角色

将仓库里的 `wg_hy2_install.py`、对应角色目录，以及适合目标服务器架构并已校验的 sing-box 二进制，通过 SSH/SCP 等经过认证的安全通道复制到对应服务器。不要把本机 macOS 二进制直接用于 Linux 服务器。

在目标服务器上，以 root 运行：

```sh
python3 wg_hy2_install.py --bundle /安全路径/角色目录 \
  --sing-box /安全路径/sing-box --name mynode
```

双机模式先在落地机安装 `landing/`，再在中转机安装 `relay/`；单机安装 `single/`。`--name` 用于区分独立服务，安装器不会主动覆盖同名已有部署。可先加 `--dry-run` 检查配置、manifest 和二进制，但它不会验证云端口、系统防火墙或实际网络连通性；`--no-enable` 表示现在启动服务但不设置开机自启，具体见 `python3 wg_hy2_install.py --help`。

配置云安全组时：

| 模式与服务器 | 入站规则 |
| --- | --- |
| 单机 | 允许 HY2 UDP 端口；WG 端口无需向公网开放 |
| 双机中转 | 允许 HY2 UDP 端口 |
| 双机落地 | 仅允许中转机实际公网出口 IPv4 访问 WG UDP 端口 |

服务端默认拒绝客户端通过 WG 访问私网、环回、链路本地和云元数据地址。服务器防火墙仍须生效；云安全组和主机防火墙是两个不同的层次。注意中转机使用 NAT 时，落地机看到的源地址可能不同于机器内部地址。

## 客户端使用与限制

`client/client.json` 是 sing-box 客户端配置示例，需要导入支持这些配置项的 sing-box 客户端。它不适用于原版 WireGuard 客户端，也不是 OpenClash 的配置文件。示例仅配置 IPv4，并为设备本身生成 TUN 配置；IPv6 流量可能绕过隧道，需要自行处理。默认公网 IPv4 流量走隧道、私网流量直连；不自带国内外或 AI 分流规则，也不包含 L2TP 服务。

路由器透明代理、局域网 DNS、防火墙和已有 OpenClash 的切换需要按具体设备单独配置；请勿把通用客户端配置直接覆盖正在工作的路由器配置。项目不会自动修改路由器、OpenClash、系统 DNS 或用户已有服务。

## 安全与测试

项目生成带 IP 地址校验的自签名证书并在客户端固定信任该证书；它不等于受公网 CA 信任的网站证书，也不保证流量无法被识别。配置文件含密码、WireGuard 私钥和其他凭据；泄露后应更换对应配置。

建议先用隔离测试验证客户端出口及 DNS，再部署到实际网络；生产环境还要检查云防火墙、主机防火墙、断线后的恢复和 IPv6 行为。项目目录不应包含真实服务器地址、私人配置或密钥。

## 运行测试

不指定 `SING_BOX` 时运行 Python 单元测试；端到端测试会跳过。指定支持所需编译标签的 sing-box 1.14.4 可执行文件可运行完整测试：

```sh
python3 -m unittest discover -s tests -v
SING_BOX="$(command -v sing-box)" python3 -m unittest discover -s tests -v
```

端到端测试只在临时配置中将 TUN 换成本地 SOCKS，不修改系统路由或防火墙；它会短暂在本机所有网卡上监听随机 WireGuard UDP 端口，并使用临时随机密钥。系统服务和云端网络仍需要在自己的环境中验证。

## 许可证

本项目采用 [MIT License](LICENSE)。sing-box 及其他依赖使用各自的许可证。
