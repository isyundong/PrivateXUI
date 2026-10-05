# 测试与构建

部署运行时只需要 Python 3.9+，不需要 pip 或 npm。Node.js 20+ 和 npm 仅用于开发测试，其中 `yaml` 是验证真实 YAML 解析的开发依赖。

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

2026-10-06 交付验证：80 项 Python 测试、23 项 Worker 测试通过。实际 PTY 验证了首页、维护、表单取消、单条订阅查看和终端恢复。186 条模拟三协议配置及自动选择组通过官方 Mihomo v1.19.32 `-t` 检查；这验证配置可解析，不代表已连接真实节点。

仅用公开来源请求复核原作数量：接口样本为五组各十条 IP，原作另有 11 个域名和原生入口。Worker 单元测试验证可构成 62 条单协议或 186 条三协议配置，但线上源变化、去重/过滤或超时回退会改变数量。源请求测试只验证应用构造的 URL/请求头/正文不含节点凭据，Cloudflare 自行添加的平台来源元数据另见高级说明。
