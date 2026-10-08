# Private XUI

用你自己的域名和 Cloudflare 账号，交互式部署 **3x-ui 节点 + Clash 订阅**。支持 VLESS、Trojan、VMess，不使用原作者的订阅或转换服务。

## 快速开始

### 1. 登录服务器

在电脑终端登录你的 VPS：

```bash
ssh root@你的服务器IP
```

支持 **Debian 11+ / Ubuntu 22.04+、x86_64 / ARM64、systemd、Python 3.9+**。准备两个托管到同一 Cloudflare 账号的空闲域名，如 `node.example.com` 和 `sub.example.com`，以及 [Cloudflare API Token](docs/advanced.md#cloudflare-api-token)。

### 2. 下载并运行

在服务器终端执行：

```bash
curl -fsSL https://raw.githubusercontent.com/isyundong/private-xui/main/install.sh -o private-xui.sh
bash private-xui.sh
```

以上命令在 root 登录后运行；非 root 用户请使用 `sudo bash private-xui.sh`。脚本会下载并校验单文件程序，检查系统、Python、权限、systemd 和公网 IP。不支持时会给出原因和处理说明，不会继续部署。

若提示缺少 Python 或 curl，Debian/Ubuntu 可先执行：

```bash
apt-get update && apt-get install -y python3 curl ca-certificates
```

### 3. 使用终端界面

支持方向键选择、Enter 确认、Esc 返回。界面采用 Neutral / Blue、直角细边框和灰底蓝字焦点；状态同时用文字表达，支持无颜色终端。主页保留四个入口：

```text
PRIVATE XUI

3x-ui    已安装 · 运行中
本项目   已部署（本机记录）
订阅     sub.example.com · 动态优选

  部署       订阅       维护       退出
```

[查看实际界面截图](docs/interface.md)。旧配置尚未启用自动优选时，首页显示黄色提示，可按 **A** 直达设置。

首次选择 **部署**，填写 IP、两个域名、协议和订阅名称，再输入 Cloudflare Token。界面显示安装步骤和耗时；不支持全屏的终端自动使用简洁文字模式。

VPS 回源端口固定为 TCP `17001`（VLESS）、`17002`（Trojan）、`17003`（VMess），另保留实际 SSH 端口（默认 `22`）。新面板通过 SSH 隧道访问。部署后会检查经过 Cloudflare 的回源握手，通过后不再重复提醒开端口；脚本不会自动修改防火墙。

默认使用 **优选域名池 + [cf.090227.xyz](https://cf.090227.xyz/#/api) 电信、联通、移动 IP 池**，由你自己的 Worker 生成订阅，Clash 客户端自动测延迟。来源数量会变化；域名和多条 IP 是同一 VPS 的不同入口。

### 4. 导入 Clash

进入 **订阅**，复制完整地址：

```text
https://sub.example.com/s/你的随机订阅令牌/Private-XUI.yaml
```

直接添加到客户端的「订阅 / 配置」。**默认返回 Clash YAML，不是 Base64，也不需要第三方转换。** VLESS 请使用 Mihomo 内核客户端，例如 Clash Verge Rev。首次绑定域名时，证书和 DNS 可能需要等待生效。

以后输入下面命令即可重新打开菜单：

```bash
private-xui
```

非 root 用户使用 `sudo private-xui`。订阅地址包含访问权限，请勿公开分享。新面板默认仅监听服务器本机，“维护 → 查看面板访问”会显示 SSH 隧道访问方法。节点回源方式、Token 权限、迁移与故障处理见 [详细说明](docs/advanced.md)。

遇到 Cloudflare `403 / 9109` 时，先在“维护 → 连接与凭据检查”做只读检查，再按 [Token 排查说明](docs/advanced.md#cloudflare-403--9109) 检查凭据、资源权限和 IP 限制。首次读取域名列表失败时，还未部署节点，无需卸载。

## 已经部署旧版

重新执行上面的下载命令即可更新工具，**不要卸载节点**。进入 **维护 → 订阅设置**，填写名称并选择 **自动优选**，确认后会更新自己的 Worker。再从首页 **订阅** 复制新的完整地址导入 Clash。

旧客户端配置可能保留之前的随机显示名，需手动改名或用新地址重新导入。不要导入带 `?protocol=vless` 的旧单协议链接，除非只需要 VLESS。

开发者：[测试与构建](docs/development.md) · [上游来源](docs/advanced.md#代码来源与接口依据)

## 客户端 TUN 与 DNS

完整订阅已包含偏隐私的 TUN、DNS 和路由配置：网站 DNS 经代理加密查询，捕获的 IPv6 目标流量和非 DNS 的普通 UDP 被拒绝，可能影响视频通话、游戏和 QUIC。Clash Verge Rev 仍需启用 TUN 并检查配置覆盖；Linux Mihomo 需要对应权限。更新时刷新完整配置，不能只更新节点。详见 [多设备设置与保护边界](docs/privacy.md)。

## TUI Dashboard

在 SSH 终端查看流量趋势、热门域名和连接历史，无网页、无新增端口。

```bash
private-xui dashboard install  # 首次启用后台采集
private-xui dashboard          # 打开终端 Dashboard
```

也可从 **维护 → Dashboard · 流量与历史** 进入。历史从启用后开始记录，默认保留 30 天；若需开启访问日志，会提示重启 x-ui。详见 [使用与统计边界](docs/dashboard.md)。
