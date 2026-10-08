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

## 当前 TUI 与客户端配置验证

新版终端界面保留 Neutral / Blue 配色、焦点、确认摘要滚动和窄屏布局，覆盖 52×20、80×24、120×36。Dashboard 的 C 与 ? 快捷键及统计功能保持可用。设计归档见 [设计说明](../design/tui-shadcn/README.md)，现有 Dashboard 截图见 [界面说明](interface.md)。

客户端配置保留 GLOBAL 默认选择 PROXY 并允许手动选节点、自动组优先使用已有 IPv4 入口、TUN 使用核心的平台自动路由范围。网站 DNS 仍经 PROXY；完整 UDP / IPv6 拒绝策略需要规则模式。

可选真实 Mihomo 回归使用 Worker 生成的普通节点分组与回环模拟出口，不访问外部服务，也不改变宿主 TUN 或系统路由：

```bash
PRIVATE_XUI_MIHOMO=/你已核验的/mihomo python3 -m unittest discover -s tests -p 'test_mihomo_runtime.py' -v
```

该测试需本地 Node.js 与已安装的开发依赖；未设置 PRIVATE_XUI_MIHOMO 时默认跳过。它验证全局节点选择、缓存恢复和无需域名入口即可启动，不代替用户设备上的 DNS、TUN 与端到端验收。

当前验证：Python 常规测试 113 项通过，3 项可选 Mihomo 用例在普通运行中跳过并单独实核验证通过；Worker / workerd 测试 29 项通过。构建一致性、SHA-256、安装脚本语法与差异检查均通过。
