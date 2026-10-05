# Private XUI：自有域名的节点部署与订阅

一个项目、一个管理入口，在 VPS 上配置 3x-ui 节点，并把订阅 Worker 部署到**你自己的 Cloudflare 账号**，绑定你自己的订阅域名。

例如：

```text
客户端更新订阅 → https://sub.example.com/s/随机订阅令牌
                  ↓ 你账号下的 Worker 生成配置
客户端使用节点 → node.example.com:443 → Cloudflare → VPS 上的 3x-ui/Xray
```

订阅不访问 `yx-auto.pages.dev`、`url.v1.mk`，由你自己的 Worker 生成。自动优选模式使用 cf.090227.xyz 的公开 IP 数据及原作域名池；代码不把 UUID、节点路径或订阅令牌作为来源请求参数。

## 这版包含什么

- VLESS、Trojan、VMess，WebSocket + 客户端 TLS，客户端入口统一为 443。
- 一个独立随机订阅令牌；URL 中没有节点 UUID、节点域名或 WebSocket 路径。
- 默认 Clash/Mihomo YAML、可选 Base64 订阅和原始节点链接，均在自己的 Worker 内生成。
- 默认自动优选：公开动态 IP + 原作 11 个域名；也可选静态候选、仅域名入口或自有列表。支持 IPv4、IPv6。
- TUI 首页四入口：部署、订阅、维护、退出；高级操作收纳到维护。安装/更新有阶段进度与耗时。
- 显式配置 Worker Secret，关闭 Worker 日志采集和 Logpush；订阅响应 `no-store`，没有应用层请求日志或数据库；自动优选模式只访问固定的公开地址源。
- 只添加/删除本项目的 Cloudflare 规则；已有 DNS 名称会拒绝覆盖。每个部署先写私有状态，失败自动尝试清理，清理失败保留状态以便重试。

这是同一个代码项目，运行在 VPS 和 Cloudflare Worker 两个位置，不需要原作者的在线订阅或转换服务。Cloudflare 平台仍是受信任的服务提供方，能处理 Worker 中的节点配置；不应把它理解为“任何第三方都看不到凭据”。持有完整订阅 URL 的人仍可下载节点凭据，独立令牌不会使订阅链接变成公开资料。

## 环境与域名

- Debian 11+ / Ubuntu 22.04+ VPS，x86_64 / ARM64，systemd，Python **3.9+**，使用 root/sudo；部署工具不需要 pip/npm。
- 本机 SQLite 版 3x-ui；优先使用自动检测到的面板 API Token，未检测到时使用数据库方式。
- 已托管到 Cloudflare 的域名、已启用 Workers 的 Cloudflare 账号。
- 两个空闲域名：如 `node.example.com` 和 `sub.example.com`。可以来自同一账号的不同 Zone；必须不同，避免 Worker 接管节点连接。
- VPS 回源 TCP 端口固定为 VLESS `17001`、Trojan `17002`、VMess `17003`，只使用选中的协议端口，与选择顺序无关。端口必须允许 Cloudflare 访问；已有入站配置或系统监听占用时会停止，不会自动随机换端口。工具不会改动系统/云防火墙，建议限制节点端口来源为 Cloudflare IP 段。
- Worker 自定义域名的 DNS 和证书由 Cloudflare 管理；首次生效需要等待。

此版保留原部署方式中的 **Cloudflare → VPS 明文 WebSocket 回源**。SSL Flexible 只通过 Configuration Rule 应用于节点域名，**不会修改整个 Zone 的 SSL 模式**。如果要求全链路 TLS，需要进一步为 Xray/反向代理配置源站证书后使用 Full (strict)。

PostgreSQL、Docker 面板、自定义数据库路径、Surge/Sing-box 等专用配置格式未纳入本版支持范围；支持通用节点链接的客户端可使用通用订阅。Clash 侧请使用支持 VLESS 的 Mihomo 内核。

## Cloudflare API Token

使用 API Token，**不使用 Global API Key**。运行时隐藏输入；Token 仅在当前进程内存中复用，认证失败后清空以便重新输入。退出不保存，工具不会把它写入状态文件或上传给 Worker。

在 [My Profile → API Tokens](https://dash.cloudflare.com/profile/api-tokens) 创建 Custom Token，只复制创建完成后显示的 Token 本身，不要复制整段 curl 命令、`Bearer`、Token ID 或 Global API Key。已有令牌也可以核对并调整授权策略。

请把资源限制到自己的账号和涉及的 Zone，并配置以下权限：

| 类别 | 权限 | 级别 |
| --- | --- | --- |
| Account | Workers | 管理员（Admin） |
| Zone | Zone | Read |
| Zone | DNS | Edit |
| Zone | Workers Routes | Edit |
| Zone | Origin Rules | Edit |
| Zone | Config Rules | Edit |

Account Resources 选择域名所在的账号；Zone Resources 选择节点与订阅域名所在的主域名，例如 `example.com`。仅 DNS 编辑权限并不能代替 `Zone → Zone → Read`。

新版 Workers 权限需要产品级 `Workers → Admin` 才能创建/删除 Worker，不能只选 Editor。旧版 `Workers Scripts` 名称可能仍出现在权限列表中；新建令牌请按新版选择。配置规则在当前控制台显示为 `Config Rules`，API 文档也可能称为 `Config Settings`。依据：[Workers 角色与创建权限](https://developers.cloudflare.com/workers/authorization/workers/)。

控制台与 API 权限名称可能显示为 Edit 或 Write。配置规则/回源规则有套餐额度限制，额度不足会报错并清理本次部署，不会删除你的现有规则来腾额度。

权限依据：[Workers 权限](https://developers.cloudflare.com/workers/authorization/workers/)、[Token 权限列表](https://developers.cloudflare.com/fundamentals/api/reference/permissions/)、[Origin Rules API](https://developers.cloudflare.com/rules/origin-rules/create-api/)。

### Cloudflare 403 / 9109

这表示 Cloudflare 拒绝了认证或授权，仅凭错误码不能确定具体原因。新版会保留识别到的安全认证说明，例如 `Invalid access token` 或客户端 IP 限制拒绝；不会原样打印可能包含密钥的完整 API 错误响应。

1. 确认输入的是正确的 API Token，本工具不接受 Global API Key。确认令牌未撤销、未过期且已到生效时间。
2. 若在 Client IP Address Filtering 中设置了限制，需要允许 **VPS 实际向 Cloudflare 请求时使用的出口 IP**。只允许你电脑的 IP 会导致 VPS 请求被拒绝；VPS 的 IPv6 或代理出口也可能不同于界面检测的 IPv4。应按真实出口调整允许范围。
3. 检查 `Zone → Zone → Read` 以及 Zone Resources 是否包含目标主域名，再核对上表的部署权限。
4. 若设置了 `CF_API_TOKEN` 环境变量，工具会优先使用它。要重新粘贴令牌，先在当前 shell 执行 `unset CF_API_TOKEN`。

可以先进行只读检查，不必重复填写整套安装参数：

```bash
private-xui check-cloudflare --domain node.example.com
```

或进入 TUI 的“维护 → 连接与凭据检查 → Cloudflare 凭据”。它只读取域名列表和检查目标 Zone 是否可见，不创建状态文件，也不修改面板、节点或 Cloudflare 资源。读取成功仅说明这一步的认证和读取权限可用，不能保证所有后续写入权限都具备。

如果失败发生在首次 `GET /zones`，还未安装面板或创建节点，不需要卸载。只有状态标记为未完成部署时才应执行清理；已完成的部署不会因一次凭据错误而需要卸载。

官方说明：[创建 Token、资源与 IP/TTL 限制](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/)、[认证排查](https://developers.cloudflare.com/fundamentals/api/troubleshooting/)、[List Zones 所需权限](https://developers.cloudflare.com/api/resources/zones/methods/list/)。

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

默认只展示一条完整 Clash 链接。以下是高级可选格式，令牌由程序生成：

```text
Clash 订阅:   https://sub.example.com/s/随机令牌/Private-XUI.yaml
其他客户端:   https://sub.example.com/s/随机令牌?format=base64
原始节点:     https://sub.example.com/s/随机令牌?format=raw
仅 VLESS:    https://sub.example.com/s/随机令牌?protocol=vless
```

`show` 只读取状态，不联网、不修复数据库、不重启服务。Worker 根路径及未授权请求返回 404，没有公开的配置输入页，也不能通过 URL 覆盖 UUID/domain/path。

支持自定义请求头的客户端也可请求 `https://sub.example.com/sub`，发送 `Authorization: Bearer 随机令牌`，避免令牌出现在 URL 路径中。

## 自动优选与自有地址

入口模式有四种，在 **维护 → 订阅设置** 中选择：

- `auto`：默认模式。自己的 Worker 并行请求 `https://cf.090227.xyz/ct?ips=12`、`/cu?ips=12`、`/cmcc?ips=12`，分别读取电信、联通、移动的纯文本 IP 列表；无需来源 API 密钥，也不使用 Cloudflare Token 取数。合并原作 11 个域名和自己的节点域名。按三组各 12 条计算，每协议去重前为 48 个入口（过滤后可能更少，实际以来源响应为准），这些仍共用一台 VPS。
- `builtin`：六个 Cloudflare 静态候选 + 自己的域名，不访问公开地址源。
- `direct`：每个已安装协议只生成自己的域名入口，不访问公开地址源。
- `custom`：导入自有 JSON 地址列表，不自动联网更新。

动态 IP 只接受 Cloudflare 官方 IPv4/IPv6 网段，按地址去重，所有客户端入口统一 443。每个源的请求和读取总限时 5 秒，最多 64 KiB，总入口最多 128 个。单个运营商接口失败不影响其他来源；三个接口都无有效结果时，保留原域名和域名池，并用六个静态候选兜底。来源可能包含非 Cloudflare 官方网段的中转地址，这些地址会被过滤，数量不保证等于网站展示数量。默认不开启原作的 GitHub 来源，与原部署器 `egi=no` 一致。

代码仅构造公开列表请求，不转发订阅 URL、UUID、密码、路径、Cookie 或 Authorization。Cloudflare 平台可能自行加入 `CF-Worker` 等来源标识，源站可能据此知道请求来自哪个 Zone；这不是匿名取数服务。若不希望接触公共来源，选择 `builtin`、`direct` 或 `custom`。参考 [Cloudflare 子请求请求头](https://developers.cloudflare.com/fundamentals/reference/http-headers/#cf-worker)。

本项目在客户端每次拉取订阅时重新请求来源；请在客户端设置订阅自动更新。没有独立的服务器 Cron/KV 定时池，和 Cloudflare SOCKS5 项目的每 30 分钟定时池不同。优选域名本身保留为域名，由客户端按 DNS TTL 重新解析；无法保证来源运营者按特定频率更新。

多个入口时，Clash 配置包含“自动选择”组，由客户端通过代理访问 `https://www.gstatic.com/generate_204`，每 300 秒检测可达性和延迟。它不是下载带宽测试，也不把公共源的结果当作你所在网络的实测结果。证书校验保持开启，TLS SNI/WS Host 仍为自己的节点域名。

旧版空列表不会自动被覆盖。升级工具后，可通过 TUI 显式切换自动优选，或运行：

```bash
sudo private-xui update-worker --preferred-mode auto --subscription-name "Private XUI"
```

自有文件的示例仍是 `preferred.example.json`，其中 IP 是文档示例，需替换为自己验证的 Cloudflare 接入地址：

```bash
sudo private-xui update-worker --preferred-mode custom --preferred preferred.json
```

订阅名称通过友好路径 `/s/<token>/Private-XUI.yaml`、`Content-Disposition` 和 `Profile-Title` 传递；旧 URL 继续有效。客户端可能保留旧条目的显示名，需要手动改名或重新导入。

## 安装状态、进度和连通性

TUI 显示 3x-ui 是否安装及 systemd 运行状态；本项目是否部署来自私有状态文件，明确标注本机记录，不假装实时核验了云端资源。未部署、部署未完成、待清理、更新待恢复分别显示。

下载时，有响应总大小则显示真实字节比例，否则显示已下载字节。安装器阶段显示完成步骤、活动提示和经过时间，不伪造安装百分比。安装器原始输出不混入进度，失败日志保存在权限 600 的文件中。

部署结束自动检查经 Cloudflare 的 WebSocket 回源；也可在 **维护 → 连接与凭据检查 → 节点连接** 或下面命令手动检查：

```bash
sudo private-xui check-connectivity
```

检查只请求节点域名的 443 和已配置路径，不发送节点 UUID/密码；验证 101 升级响应、WebSocket 校验值和 Cloudflare 响应标记，之后立即关闭连接。通过表示当时的回源传输链路可达，不等于已验证代理认证或所有优选 IP。

通过后不再显示放行端口提醒。结果按当前域名/路径配置缓存 5 分钟；过期显示未验证。失败会区分 DNS、TLS、HTTP 响应和超时，不能据此直接判定防火墙关闭。工具不读取云厂商安全组，也不自动修改防火墙。首次 DNS/证书尚未生效时可稍后重试，检查失败不撤销已经完成的部署。

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

此前已用本工具部署的随机端口不会被自动改动。查看订阅时会显示实际端口；仅更新工具或 Worker 仍保留旧端口。若要切换到 `17001/17002/17003`，先卸载本项目配置，再重新安装；新安装会更换节点凭据和订阅链接，需要重新导入客户端。

本版使用独立状态文件，不自动读取旧订阅快照，不会继续输出作者域名的订阅，也不尝试修改原部署记录。建议使用一对新的空闲子域名部署，验证新节点后停用旧入站，再核对并清理原 Cloudflare 资源。新安装会生成新的节点 UUID 和订阅令牌。

## 本地测试与验证范围

Python 运行测试不需要第三方依赖；Worker 测试需要 Node.js 20+：

```bash
python3 -m unittest discover -s tests -v
npm ci --ignore-scripts
npm test
```

测试覆盖临时 SQLite 操作、规则保留、恢复重试、订阅鉴权、三协议、动态地址源的隐私隔离/超时/限额、TUI 导航和取消、终端恢复、进度线程结束、回源握手及失败原因。

交付时没有使用真实 Cloudflare 账号或 VPS 部署。API 写入、域名证书签发、3x-ui/Xray 真实启动及客户端连接仍需在目标环境验收。语法和隔离测试通过不等于线上连通性已验证。

## 代码来源与接口依据

- `xui_backend.py` 从 [byJoey/xui-cf-deployer](https://github.com/byJoey/xui-cf-deployer/tree/c7c3d9a976819a8c300d62c5408c9330b3b23b02) 提取并修改必要的面板/数据库适配，去掉旧订阅服务、全局 SSL 改写、批量规则替换、自动修复其他客户端及菜单汉化逻辑。
- `worker.mjs` 根据 [yx-auto](https://github.com/byJoey/yx-auto/tree/17bb2f6c8fcb8848e31230ffa4c9cb8f7a32659a) 的“节点配置 → 多协议订阅”功能重新实现；保留动态地址与域名池机制，公开 IP 源改为 [cf.090227.xyz API](https://cf.090227.xyz/#/api)，替换订阅生成和格式转换为自己的 Worker；不使用原作者的订阅服务或第三方转换器。
- 新安装器来源固定到 [3x-ui e897b095](https://github.com/MHSanaei/3x-ui/blob/e897b0957a12c3a106f505e0551c18887b0528d6/install.sh)，本机绑定补丁在执行前应用并检查匹配。
- [Worker 上传与 Secret](https://developers.cloudflare.com/workers/configuration/multipart-upload-metadata/)、[自定义域名 API](https://developers.cloudflare.com/api/resources/workers/subresources/domains/methods/update/)、[按域名设置 SSL](https://developers.cloudflare.com/rules/configuration-rules/settings/)。
