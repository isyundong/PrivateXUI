# Private XUI：自有域名的节点部署与订阅

一个项目、一个管理入口，在 VPS 上配置 3x-ui 节点，并把订阅 Worker 部署到**你自己的 Cloudflare 账号**，绑定你自己的订阅域名。

例如：

```text
客户端更新订阅 → https://sub.example.com/s/随机订阅令牌
                  ↓ 你账号下的 Worker 生成配置
客户端使用节点 → node.example.com:443 → Cloudflare → VPS 上的 3x-ui/Xray
```

运行时不访问 `yx-auto.pages.dev`、`url.v1.mk` 或原作者的优选地址服务。两个原项目的核心职责已整合；订阅部分重新实现，没有搬入原项目的第三方转换器及地址源。

## 这版包含什么

- VLESS、Trojan、VMess，WebSocket + 客户端 TLS，客户端入口统一为 443。
- 一个独立随机订阅令牌；URL 中没有节点 UUID、节点域名或 WebSocket 路径。
- 默认 Clash/Mihomo YAML、可选 Base64 订阅和原始节点链接，均在自己的 Worker 内生成。
- 自有优选 IP/域名列表，支持 IPv4、IPv6；不自动请求公共优选源。
- 安装、只读查看、订阅令牌轮换、Worker 更新、卸载。
- 显式配置 Worker Secret，关闭 Worker 日志采集和 Logpush；订阅响应 `no-store`，没有应用层请求日志、数据库或外部网络请求。
- 只添加/删除本项目的 Cloudflare 规则；已有 DNS 名称会拒绝覆盖。每个部署先写私有状态，失败自动尝试清理，清理失败保留状态以便重试。

这是同一个代码项目，运行在 VPS 和 Cloudflare Worker 两个位置，不需要原作者的任何在线服务。Cloudflare 平台仍是受信任的服务提供方，能处理 Worker 中的节点配置；不应把它理解为“任何第三方都看不到凭据”。持有完整订阅 URL 的人仍可下载节点凭据，独立令牌不会使订阅链接变成公开资料。

## 环境与域名

- Debian 11+ / Ubuntu 22.04+ VPS，x86_64 / ARM64，systemd，Python **3.9+**，使用 root/sudo；部署工具不需要 pip/npm。
- 本机 SQLite 版 3x-ui；优先使用自动检测到的面板 API Token，未检测到时使用数据库方式。
- 已托管到 Cloudflare 的域名、已启用 Workers 的 Cloudflare 账号。
- 两个空闲域名：如 `node.example.com` 和 `sub.example.com`。可以来自同一账号的不同 Zone；必须不同，避免 Worker 接管节点连接。
- VPS 回源端口必须允许 Cloudflare 访问。脚本会避开当前已监听端口，但不会改动系统/云防火墙。建议在防火墙中限制来源为 Cloudflare，而不是直接暴露所有节点端口。
- Worker 自定义域名的 DNS 和证书由 Cloudflare 管理；首次生效需要等待。

此版保留原部署方式中的 **Cloudflare → VPS 明文 WebSocket 回源**。SSL Flexible 只通过 Configuration Rule 应用于节点域名，**不会修改整个 Zone 的 SSL 模式**。如果要求全链路 TLS，需要进一步为 Xray/反向代理配置源站证书后使用 Full (strict)。

PostgreSQL、Docker 面板、自定义数据库路径、Surge/Sing-box 等专用配置格式未纳入本版支持范围；支持通用节点链接的客户端可使用通用订阅。Clash 侧请使用支持 VLESS 的 Mihomo 内核。

## Cloudflare API Token

使用 API Token，**不使用 Global API Key**。运行时隐藏输入，工具不会把 Cloudflare Token 写入状态文件或上传给 Worker。

请把资源限制到自己的账号和涉及的 Zone，并授予这些操作所需权限：

- 账号：Workers Scripts 编辑权限。
- Zone：Zone 读取、DNS 编辑、Workers Routes 编辑、Origin Rules 编辑、Config Settings 编辑。

控制台与 API 权限名称可能显示为 Edit 或 Write。配置规则/回源规则有套餐额度限制，额度不足会报错并清理本次部署，不会删除你的现有规则来腾额度。

权限依据：[Workers 权限](https://developers.cloudflare.com/workers/authorization/workers/)、[Token 权限列表](https://developers.cloudflare.com/fundamentals/api/reference/permissions/)、[Origin Rules API](https://developers.cloudflare.com/rules/origin-rules/create-api/)。

## 安装

先按 [README](../README.md) 下载启动程序。下面是适合脚本使用的命令行方式；常规使用直接运行 `sudo private-xui` 进入菜单。替换成自己的域名：

```bash
sudo private-xui install \
  --node-domain node.example.com \
  --sub-domain sub.example.com
```

按提示输入 Cloudflare API Token。默认创建三个协议；例如只创建 VLESS：

```bash
sudo private-xui install \
  --node-domain node.example.com \
  --sub-domain sub.example.com \
  --protocols vless \
  --ipv4 203.0.113.10
```

这里的 `203.0.113.10` 是文档示例，必须换成 VPS 的真实公网 IPv4。省略 `--ipv4` 时会使用公网 IP 查询服务；这些请求不携带订阅或节点凭据。

无 3x-ui 的裸机可追加 `--fresh`。这会执行经过固定 SHA-256 校验的官方安装器，并指定 3x-ui `v3.9.0`。安装器的非交互设置已调整为面板只监听 `127.0.0.1`，面板通过 SSH 隧道访问。安装器仍会下载上游发行包/系统依赖，本项目的测试不构成这些二进制或第三方依赖的安全审计。

新面板信息保存在 `/etc/x-ui/private-cf/panel.json` 和 `panel.txt`，权限均为 600。可以根据文件中的面板端口，在自己电脑执行：

```bash
ssh -L 2053:127.0.0.1:面板实际端口 root@VPS公网IP
```

然后访问 `http://127.0.0.1:2053/面板实际路径`。已有面板的监听设置不会被改变。

非交互场景支持进程环境变量 `CF_API_TOKEN`，以及 `XUI_API_TOKEN`、`XUI_PANEL_URL`、`XUI_BACKEND=db|api`。使用 sudo 时注意环境变量是否被保留，推荐交互输入 CF Token，避免把密钥写进命令历史。

## 查看与使用

```bash
sudo private-xui show
```

输出格式如下；令牌会由程序生成：

```text
Clash 订阅:   https://sub.example.com/s/随机令牌
其他客户端:   https://sub.example.com/s/随机令牌?format=base64
原始节点:     https://sub.example.com/s/随机令牌?format=raw
仅 VLESS:    https://sub.example.com/s/随机令牌?protocol=vless
```

`show` 只读取状态，不联网、不修复数据库、不重启服务。Worker 根路径及未授权请求返回 404，没有公开的配置输入页，也不能通过 URL 覆盖 UUID/domain/path。

支持自定义请求头的客户端也可请求 `https://sub.example.com/sub`，发送 `Authorization: Bearer 随机令牌`，避免令牌出现在 URL 路径中。

## 自有优选地址

将 `preferred.example.json` 复制为自己的列表，例如 `preferred.json`。填入经过你验证的 Cloudflare 接入地址；这些地址只是连接目标，TLS SNI 和 WebSocket Host 始终使用你的节点域名，证书校验保持开启。示例中的地址仅用于文档，不能直接作为可用节点。

安装时加 `--preferred preferred.json`；安装后更新：

```bash
sudo private-xui update-worker --preferred preferred.json
```

所有优选地址使用 443 端口，与 Flexible 回源兼容。节点自身域名始终保留为一个选项；列表为空时只生成自己的域名节点。工具不测速、不联网刷新优选列表。

## 令牌轮换与泄露处理

```bash
sudo private-xui rotate-token
```

部署成功后旧订阅 URL 失效，新 URL 会显示出来，需更新客户端订阅地址。**这只轮换订阅令牌，不轮换节点 UUID/密码**。别人以前已下载到的节点凭据仍然有效。若旧 `yx-auto` 订阅已向第三方披露，应迁移到本工具新生成的节点，并停用旧节点。

Worker 代码或列表更新不改变节点 UUID：

```bash
sudo private-xui update-worker
```

上传遇到超时、结果不确定时会保留 `update-pending` 状态及待生效令牌。排除错误后再次执行 `update-worker`，它会复用同一待更新配置，避免令牌丢失或反复变化。

## 卸载与失败恢复

```bash
sudo private-xui uninstall
```

只清理本次创建的 Worker/自定义域名、带本项目标记的节点 DNS 和规则、匹配标签/端口/凭据的入站。不会卸载 3x-ui 面板，也不会批量还原整个规则列表。若本项目的入站已被人工改动，会保留恢复状态并提示人工核对，避免误删。

状态文件默认是 `/etc/x-ui/private-cf/state.json`，权限 600，含节点和订阅凭据，请勿公开。所有资源变更前写入恢复信息；接口失败自动尝试清理。清理失败时保留状态，恢复网络/权限后运行 `uninstall` 重试。空的 Cloudflare phase ruleset 可能保留，里面不再包含本项目的规则。

不要直接删除状态文件后重装，否则会丢失资源归属和恢复信息。可以用 `private-xui --state /你的私有目录/state.json ...` 指定其他状态路径。

## 从原项目迁移

本版使用独立状态文件，不自动读取旧订阅快照，不会继续输出作者域名的订阅，也不尝试修改原部署记录。建议使用一对新的空闲子域名部署，验证新节点后停用旧入站，再核对并清理原 Cloudflare 资源。新安装会生成新的节点 UUID 和订阅令牌。

## 本地测试与验证范围

Python 运行测试不需要第三方依赖；Worker 测试需要 Node.js 20+：

```bash
python3 -m unittest discover -s tests -v
npm ci --ignore-scripts
npm test
```

测试覆盖临时 SQLite 上的创建/删除、业务规则保留、部署中断与清理重试、订阅鉴权和令牌轮换、三协议格式、Unicode/IPv6、文件权限及禁止 Worker 外发请求。

交付时没有使用真实 Cloudflare 账号或 VPS 部署。API 写入、域名证书签发、3x-ui/Xray 真实启动及客户端连接仍需在目标环境验收。语法和隔离测试通过不等于线上连通性已验证。

## 代码来源与接口依据

- `xui_backend.py` 从 [byJoey/xui-cf-deployer](https://github.com/byJoey/xui-cf-deployer/tree/c7c3d9a976819a8c300d62c5408c9330b3b23b02) 提取并修改必要的面板/数据库适配，去掉旧订阅服务、全局 SSL 改写、批量规则替换、自动修复其他客户端及菜单汉化逻辑。
- `worker.mjs` 根据 [yx-auto](https://github.com/byJoey/yx-auto/tree/17bb2f6c8fcb8848e31230ffa4c9cb8f7a32659a) 的“节点配置 → 多协议订阅”功能重新实现；无原作者的线上服务、公共地址列表或转换器依赖。
- 新安装器来源固定到 [3x-ui e897b095](https://github.com/MHSanaei/3x-ui/blob/e897b0957a12c3a106f505e0551c18887b0528d6/install.sh)，本机绑定补丁在执行前应用并检查匹配。
- [Worker 上传与 Secret](https://developers.cloudflare.com/workers/configuration/multipart-upload-metadata/)、[自定义域名 API](https://developers.cloudflare.com/api/resources/workers/subresources/domains/methods/update/)、[按域名设置 SSL](https://developers.cloudflare.com/rules/configuration-rules/settings/)。
