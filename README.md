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

### 3. 跟着菜单完成配置

选择 **1**，按提示确认服务器公网 IP，填写节点域名、订阅域名、协议和 Cloudflare Token。未安装 3x-ui 时会引导全新安装，已安装时复用现有面板。

VPS 需放行 **TCP `22`（SSH）、`17001`（VLESS）、`17002`（Trojan）、`17003`（VMess）**。只安装部分协议时，只需放行相应端口；SSH 改过端口则以实际设置为准。面板通过 SSH 隧道访问，不额外开放公网端口。系统防火墙和云安全组都需要检查，脚本不会自动修改它们。

默认端口被占用时会报错停止，不会换成随机端口。节点回源端口建议仅允许 Cloudflare IP 段访问。

```text
1. 全新安装 / 安装节点与订阅（自动识别）
2. 卸载本项目配置
3. 查看 Clash 订阅
4. 查看面板访问信息
5. 更新订阅服务 / 优选列表
6. 更换订阅令牌
7. 重新检查环境和 IP
8. 查看 x-ui 管理命令
0. 退出
```

### 4. 导入 Clash

复制安装完成后显示的订阅地址：

```text
https://sub.example.com/s/你的随机订阅令牌
```

直接添加到客户端的「订阅 / 配置」。**默认返回 Clash YAML，不是 Base64，也不需要第三方转换。** VLESS 请使用 Mihomo 内核客户端，例如 Clash Verge Rev。首次绑定域名时，证书和 DNS 可能需要等待生效。

以后输入下面命令即可重新打开菜单：

```bash
private-xui
```

非 root 用户使用 `sudo private-xui`。订阅地址包含访问权限，请勿公开分享。新面板默认仅监听服务器本机，菜单 **4** 会显示 SSH 隧道访问方法。节点回源方式、Token 权限、迁移与故障处理见 [详细说明](docs/advanced.md)。

开发者：[测试与构建](docs/development.md) · [上游来源](docs/advanced.md#代码来源与接口依据)
