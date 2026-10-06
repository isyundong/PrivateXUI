# TUN、DNS 与多设备隐私检查

生成的完整 Clash/Mihomo 配置默认启用 TUN、自动路由、严格路由和 TCP/UDP 53 的 DNS 接管。网站 DNS 使用经过 PROXY 的加密 DoH，普通 UDP（包括 STUN/QUIC）和进入内核的 IPv6 目标连接会被拒绝，其他业务流量经过 PROXY。

这会影响部分视频通话、游戏和其他 UDP 应用；QUIC 通常会回退 TCP，但并非每个应用都支持回退。不要为修复这些应用随意添加 DIRECT 兜底。

顶层 `ipv6: true` 是为了允许 TUN 建立 IPv6 路由并捕获流量；DNS 的 `ipv6: false` 不返回 AAAA，`IP-CIDR6,::/0,REJECT,no-resolve` 阻止捕获的 IPv6 出站。只写顶层 `ipv6: false` 并不等于关闭操作系统的 IPv6。

## Clash Verge Rev

1. 更新自己的 Worker，再刷新并重新激活**完整订阅配置**。仅更新代理集合中的节点，不会更新顶层 TUN/DNS/rules。
2. 安装/启用客户端服务模式，打开 TUN。根据操作系统授权创建虚拟网卡和设置路由。
3. 关闭会覆盖订阅的自定义 DNS 覆写，或确保覆写内容与本配置一致。检查全局扩展、订阅扩展和 TUN 设置；GUI 保存的部分设置优先级可能更高。
4. 查看客户端最终生效配置，确认 `tun.enable`、`auto-route`、`strict-route`、两条 `dns-hijack`、IPv6 捕获路由、DNS 的 `#PROXY` 以及 UDP/IPv6 拒绝规则仍在。选择**规则模式**；切换“直连模式”或“全局模式”会改变规则行为。
5. 检查内核日志，不能存在创建 TUN、配置路由或监听 DNS 失败的错误。

参考：[Clash Verge Rev 配置覆盖顺序](https://www.clashverge.dev/guide/extend.html)。不同版本的菜单名称可能不同，最终配置和运行日志才是验证依据。

## Linux 原生 Mihomo

使用完整配置启动，而不是只加载 `proxies.yaml`。运行身份需要访问 `/dev/net/tun` 并具备设置路由的权限（常见是正确配置的系统服务或 root）。本工具不会自动给你的电脑提权或修改防火墙。

先用 `mihomo -t -f /你的配置路径/config.yaml` 检查配置，再由你的服务管理方式加载。检查 TUN 网卡、IPv4/IPv6 路由和内核日志。systemd-resolved、NetworkManager、多网卡及其他 VPN 可能改变 DNS/路由，不能仅凭配置文件判断已经接管。

## 保护边界

- 这些规则只控制进入 Mihomo 的流量。局域网 DNS、其他 VPN、特殊网卡绑定或客户端排除路由可能绕开 TUN。官方说明 macOS/Windows 不能自动劫持所有发往局域网的 DNS；Android 私人 DNS 也有独立行为。遇到此类情况，需要按设备核对系统 DNS 和路由，不能盲目套一条通用系统命令。
- 代理节点域名需要先解析才能建立代理。配置为这类引导查询保留**直连加密 DoH**；Cloudflare DNS 仍能看到你的来源 IP 和被查询的节点域名。新项目的订阅下载也直接访问自己的域名。网站查询才走 PROXY，不承诺所有连接都不暴露来源 IP 给基础设施运营商。
- 阻断 UDP 可降低 STUN 探测公网地址的风险，但不能保证浏览器不枚举本地地址或通过其他机制暴露信息。支持的浏览器可另外限制 WebRTC 的非代理 UDP，例如 Chromium 的 `disable_non_proxied_udp` 策略。它不能由 Clash YAML 设置，浏览器品牌、版本及管理策略的入口不同；不需要 WebRTC 的浏览器可考虑禁用该功能。
- TUN 不是退出后的断网保护。内核退出、系统撤销 VPN 或路由被其他软件替换后，系统可能恢复直连。若要求代理停止即断网，需要在每台设备上另设系统级阻断策略；订阅不能替代它。
- 所有节点失效时，本配置没有业务流量 DIRECT 兜底。但这不等于已经验证你每台设备的系统断网保护。

## 每台设备都应验证

1. 启用 TUN 后检查浏览器和命令行看到的公网 IPv4；应是预期代理出口。
2. 检查公网 IPv6；按本配置应不可达，而不是显示本机公网 IPv6。
3. 检查 DNS 查询与 WebRTC/ICE 候选，区分本地私有地址、mDNS 名称、代理出口与真实公网地址。仅看到某个 DNS 服务商名称，不足以判定泄漏。
4. 切换 Wi-Fi/有线网络、休眠恢复后复查。需要断网保护时，再单独测试代理中断场景。

本仓库验证了配置字段、规则顺序和 Mihomo 解析；没有替你在每台设备上抓包或完成泄漏测试。

依据：[Mihomo TUN](https://wiki.metacubex.one/config/inbound/tun/)、[DNS](https://wiki.metacubex.one/config/dns/)、[Chromium WebRTC 隐私策略](https://developer.chrome.com/docs/extensions/reference/api/privacy#property-network-webRTCIPHandlingPolicy)。
