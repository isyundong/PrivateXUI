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

覆盖范围：环境与公网 IP 检查、不支持时停止、交互输入校验与取消、单文件资源读取、固定端口映射与 IPv4/IPv6 占用检查、数据库操作、Cloudflare 增量规则、失败清理与重试、令牌轮换、Clash YAML 解析、三协议、IPv6 和 Unicode。

Python 测试使用临时数据库和模拟云接口；不会修改开发电脑的服务或真实 Cloudflare 资源。真正的域名证书签发、3x-ui/Xray 运行及客户端连接仍需在目标 VPS 验收。

2026-10-06 交付验证：36 项 Python 测试、9 项 Worker 测试通过。含三协议和 IPv6 的生成配置另经官方 Mihomo v1.19.32 `-t` 检查通过；这验证配置可解析，不代表已连接真实节点。
