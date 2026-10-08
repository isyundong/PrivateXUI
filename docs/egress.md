# 后置出口 · SOCKS5

后置出口在服务器的 Xray 中转发业务流量。它不是 Clash 客户端的一个裸 SOCKS 代理，也不修改服务器系统代理。

启用后提供两类独立节点：

```text
[无后置]        客户端 → 原节点身份   → 3x-ui 原有路由
[后置 SOCKS5]   客户端 → 后置独立身份 → SOCKS5 → 目标
```

两类共用现有域名、WebSocket 路径与端口，不新增公网监听端口。原有节点凭据保留；后置身份使用新的 UUID 和各协议独立 email。专属规则同时匹配本项目入站标签和后置 email，其他项目、面板 API 和无后置身份继续原有行为。

## 配置

在已有部署的 VPS 上打开菜单，进入 **维护 → 后置出口 · SOCKS5**。

1. 选择 **配置 / 更改 SOCKS5**。
2. 输入 IP 或域名、端口；IPv4、IPv6、域名和本机 `127.0.0.1` 均可。
3. 选择无认证，或填写用户名和密码。密码隐藏输入；编辑同一用户名时，密码留空可保留现有值。
4. 核对确认摘要。程序校验配置，在同一 SQLite 事务中更新客户端与专属路由，重启 x-ui 并核实新核心加载的配置，之后更新 Worker。
5. 进入 **订阅**，选择所需类别并更新客户端配置。

需要本机 SQLite 版 3x-ui、Linux `/proc` 和 systemd。即使节点曾由面板 API 创建，此功能也使用本机数据库统一修改客户端和路由；不会对远端面板尝试不完整的多次 API 写入。

服务端配置保存在本机私有状态文件及 3x-ui 所需模板中；发布订阅需要你自己的 Cloudflare 权限。应用会短暂重启 x-ui，已有连接可能中断。仅查看状态或检查 SOCKS5 协商不请求 Cloudflare。

## 三种订阅

原完整订阅 URL 保持有效：

```text
https://sub.example.com/s/<令牌>/Private-XUI.yaml
```

| 参数 | 节点与默认选择 |
| --- | --- |
| 无参数，或 `?egress=all` | 同时包含两类；启用后置时 PROXY 默认选择“后置 SOCKS5” |
| `?egress=direct` | 仅 `[无后置]` 节点，沿用服务器原路由 |
| `?egress=socks` | 仅 `[后置 SOCKS5]` 节点 |

后置与无后置分别拥有自己的测速组。后置测速组不含无后置节点，因此上游故障不会自动回落到无后置路径。可以手动切换到无后置组。客户端可能保留此前的 PROXY 选择，更新订阅后请核对当前组选项；需要只允许后置时，导入 `?egress=socks`。

完整订阅显式定义 `GLOBAL → PROXY`，让客户端保留全局模式时也采用当前出口选择，避免内核自动生成的 GLOBAL 默认选择 DIRECT。UDP / IPv6 拒绝规则仍需使用规则模式才能执行；全局模式不会执行这些规则。

订阅标题和下载文件名带出口类别，支持同时导入两份配置。`protocol=vless|trojan|vmess` 与 `format=clash|raw|base64` 可以继续组合使用。未配置后置或已完成禁用并同步 Worker 时，`egress=socks` 返回不可用，不会替换为无后置列表。

SOCKS5 上游地址、用户名、密码不发送给 Worker，也不写入订阅；Worker 仅获得两类节点各自的入口 UUID。

## 命令行

```bash
private-xui egress configure   # 交互填写，确认后应用与发布
private-xui egress status      # 脱敏状态
private-xui egress check       # SOCKS5 协商与认证检查
private-xui egress retry       # 重试待完成的应用 / 订阅发布
private-xui egress disable     # 确认后撤销后置身份，保留无后置节点
```

自动化可使用权限为 `600` 的 JSON 文件，避免把密码放进命令行参数：

```json
{
  "address": "relay.example.net",
  "port": 1080,
  "username": "YOUR_USER",
  "password": "YOUR_PASSWORD"
}
```

无认证时省略 `username` 与 `password`。两个认证字段均区分大小写、保留首尾空格，长度限 1–255 个 UTF-8 字节。

```bash
chmod 600 /root/socks5.json
private-xui egress configure --config /root/socks5.json --yes
```

## 状态与恢复

- **待应用 / 待恢复：** 已有恢复记录，但尚未确认核心加载目标配置。旧运行配置可能仍在使用；选择重试，不把“数据库保存成功”当作“已生效”。
- **订阅待发布：** 本地身份与出口已应用，Worker 更新未完成。选择重试发布，或通过“更新订阅服务”继续。重试复用同一后置 UUID。
- **配置漂移：** 入站、专属客户端或路由被另一个操作改动。程序保留当前内容，不用旧整份模板覆盖用户设置。
- **运行配置已核实：** 核对了 Xray 进程及其加载文件中的专属路由和身份，不代表公网出口 IP 或所有目标网站已经实测。

禁用会删除本项目后置身份，并通过服务重启断开旧会话。旧缓存中的后置 UUID 认证失败，不会因为删除了 SOCKS 规则而转成无后置。即使 Worker 暂时不可用，本地撤销仍可完成，订阅保持待同步；客户端刷新前可能仍显示无法使用的后置节点。

卸载整个项目时，先确认本项目入站已删除，再清理专属出口。入站删除失败、额外客户端关联或用户手动修改专属对象时，会保留配置与恢复记录，不清理其他节点。

## DNS、UDP 与验证范围

后置规则覆盖该身份的 TCP / UDP，不配置直连 fallback。标准 SOCKS5 的 UDP 转发需要上游支持；本项目原有完整订阅仍拒绝普通 UDP，增加后置出口不会改写这项客户端策略。目的域名可交给 SOCKS5；SOCKS5 主机自身若使用域名，引导解析仍在 VPS 发生。依据：[Xray SOCKS 配置](https://xtls.github.io/config/outbounds/socks.html)、[路由规则](https://xtls.github.io/config/routing.html)、[SOCKS 客户端实现](https://github.com/XTLS/Xray-core/blob/main/proxy/socks/client.go)。

“检查 SOCKS5”仅检查连接、版本协商和可选认证，不探测外部网站、不验证公网 IP，也不声称 UDP 可用。SOCKS5 自身没有 TLS；本功能使用标准 SOCKS5 上游协议。

本地已使用官方 Xray 26.3.27 在回环网络验证三协议的双身份分流、域名传递、认证失败和上游失联不回退，以及撤销后置身份后旧节点失效。真实 VPS 的 3x-ui / systemd 生命周期、Cloudflare 发布及你的上游出口仍需在目标环境核实。
