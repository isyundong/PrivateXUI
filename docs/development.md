# 测试与构建

部署运行时只需要 Python 3.9+，不需要 pip 或 npm。Node.js 22+ 和 npm 仅用于开发测试，其中 `yaml` 是验证真实 YAML 解析的开发依赖。

```bash
python3 tools/build.py
python3 -m unittest discover -s tests -v
npm ci --ignore-scripts
npm test
bash -n install.sh
python3 tools/build.py --check
```

`tools/build.py` 生成可复现的 `dist/private-xui.pyz` 和 SHA-256 校验文件。修改运行时代码后，需要重新构建并把生成文件一并提交。下载脚本先确定同一个 Git commit，再下载该版本的程序和校验文件，避免更新期间混用版本。

覆盖范围：环境检查、TUI 键盘导航/取消/缩放/终端恢复、安装状态与秘密不出现在首页、步骤进度及动画停止、回源握手/错误分类、会话凭据缓存、固定端口映射、数据库恢复、订阅鉴权与命名、三协议、动态来源隐私隔离/超时/限额、IPv6 和 Unicode。

Python 测试使用临时数据库和模拟云接口；不会修改开发电脑的服务或真实 Cloudflare 资源。真正的域名证书签发、3x-ui/Xray 运行及客户端连接仍需在目标 VPS 验收。

2026-10-06 交付验证：102 项 Python 测试、26 项 Worker 测试通过。实际 PTY 验证了首页、维护、表单取消、单条订阅查看和终端恢复。144 条模拟三协议配置及自动选择组通过官方 Mihomo v1.19.32 `-t` 检查；这验证配置可解析，不代表已连接真实节点。

当前改用 cf.090227.xyz 的三家运营商文本接口；测试每组 12 条独立 IP，连同 11 个优选域名与自有入口，构成 48 条单协议或 144 条三协议配置。线上数量会因来源、过滤、去重和超时回退变化。另覆盖单运营商失败时保留其他来源。源请求测试只验证应用构造的 URL/请求头/正文不含节点凭据，Cloudflare 自行添加的平台来源元数据另见高级说明。

首页新版分区、高亮和 A 快捷入口另覆盖 52×20、80×24、120×32 布局。界面截图来自真实 curses PTY 输出，用演示数据在 xterm 标准渲染器中呈现；不连接真实服务。

新增 workerd 运行时回归测试：真实 fetch 选项解析、三家来源取数、重定向拒绝。修复 Worker 不支持 redirect:error 而导致动态来源全部回退的问题；不只依赖 Node fetch 模拟。

完整订阅包含 TUN、代理 DoH、IPv6 捕获/拒绝和 UDP 拒绝策略。新增配置策略测试，两个项目的完整配置通过 Mihomo v1.19.32 -t；未改变开发机器路由，也未声称完成用户设备的实际泄漏测试。

TUI Dashboard 新增本地采集、时间增量/重置/缺口、日志游标与轮转、保留上限、安装中断恢复、字段级还原和终端键盘/布局测试。纯 TUI，无 HTTP 监听。102 项 Python 测试在 3.14 与 3.9 通过；截图来自真实 curses PTY 的演示数据。systemd 生命周期使用隔离模拟，未在用户 VPS 实装验收。

## 2026-10-07：新版 TUI、后置出口与双身份订阅

本轮本地验证：**140 项 Python 测试、38 项 Worker 测试通过**。其中 Python 包含两个可选真实 Xray 测试；Worker 包含实际 workerd / Miniflare 验证。构建一致性、SHA-256、`bash -n install.sh` 与 `git diff --check` 均通过。构建文件已更新；以上为本地验证，未在用户 VPS 实装验收。

新增测试覆盖：

- 原客户端与后置客户端身份隔离，legacy / v3 规范化表及客户端关联保留。
- 专属出站与身份同事务变更，写前恢复记录、并发模板修改、重试身份稳定、Worker 发布失败后的本地撤销。
- 仅在本项目入站删除后清理出口；兼容面板 API 自动裁剪 `inboundTag`，保护其他存活入站的 JSON 与规范化表关联。
- Go 非主线程启动的 Xray 进程发现、加载身份与路由检查、旧凭据撤销检查。
- 凭据不进入 Worker、状态或订阅；用户名 / 密码空格保留、密码隐藏与错误消息遮蔽。
- 三种订阅筛选、独立测速组、默认后置组、后置不可用时明确报错、公开来源请求不包含任一入口 UUID。

真实核心测试使用官方下载并核验 SHA-256 的 **Xray 26.3.27**，测试只监听回环地址，使用本地模拟 HTTP 目标与 SOCKS5 上游，不访问外部目标网站。验证 VLESS、Trojan、VMess 各自的无后置 / 后置路径、目的域名传递，以及认证失败、上游失联和身份撤销时不回退。

复现可选真实核心测试：

```bash
PRIVATE_XUI_XRAY=/你已核验的/xray python3 -m unittest discover -s tests -p 'test_egress_runtime.py' -v
```

没有设置此变量时，常规测试跳过这两个真实核心用例。测试不会下载或安装 Xray，不改变系统路由，也不操作真实 systemd / Cloudflare。

独立审核发现并修复了发布失败后禁止本地撤销、API 删除标签后的清理冲突、v3 额外关联遗漏、非主线程进程发现及用户名空格裁剪问题。六张生产 `TerminalUI` 的真实 PTY 截图见 [界面说明](interface.md)，覆盖首页、八项维护、三类订阅、后置设置与两种 Dashboard 尺寸。截图使用演示数据。

仍需在目标 VPS 验证真实 3x-ui / systemd 生命周期、Cloudflare 发布和用户自己的 SOCKS5 服务。运行配置核实不等同公网出口 IP 或 UDP 连通性验收。

## 2026-10-08：全局模式使用当前出口选择

完整订阅现在显式定义 `GLOBAL → PROXY`，避免客户端保留全局模式时使用内核自动生成的 DIRECT 默认项。39 项 Worker / workerd 测试通过；新增一个可选的真实 Mihomo v1.19.29 回环测试，覆盖旧 DIRECT 缓存、配置热重载、同缓存重启及实际 HTTP 转发路径。测试使用真实 Worker 生成的分组和本地模拟出口，不访问外部服务，也不改变宿主 TUN 或系统路由。

```bash
PRIVATE_XUI_MIHOMO=/你已核验的/mihomo python3 -m unittest discover -s tests -p 'test_mihomo_runtime.py' -v
```

该测试需本地 Node.js 与已安装的开发依赖；未设置 `PRIVATE_XUI_MIHOMO` 时默认跳过。全局模式仍不执行 UDP / IPv6 拒绝规则，完整策略需要规则模式。这个回归只验证出口选择，不代替用户设备上的 DNS、TUN 与端到端连接验收。
