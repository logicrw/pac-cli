# pac-cli

专为 AI Agent（Claude Code、Cursor、Codex 等）设计的确定性新闻文章抓取与正文 Markdown 提取命令行工具与 Python 库。

> URL → 标准结构化 JSON (`markdown`, `strategy_hit`, `rule_version`, `error_code`, `failure_class`, `diagnostics`)

---

## 核心能力

1. **协议层 TLS 伪装与内核防指纹**：
   - 自动识别并使用 `curl_cffi` 模拟 Chrome JA3/JA4 握手；
   - 自动接入 `Camoufox`（基于 Firefox C++ 内核的抗检测浏览器），拒绝生硬的 JS CDP 注入；
2. **多层阶梯式降级容灾管道**：
   - `DirectHttp` (TLS 直连) ➔ `Browser` (Playwright / Camoufox 浏览器) ➔ `MultiGateway` (Archive.today 镜像 / Wayback Machine 快照 / Jina Reader 兜底)；
3. **高严苛度质量门禁与反拦截壳**：
   - 100% 杜绝虚假成功：精确识别 Cloudflare、Akamai、DataDome 等 403 拦截壳与多语言订阅提示；
4. **SWR（Stale-While-Revalidate）零阻塞规则引擎**：
   - 本地规则缓存优先，7 天 TTL 周期后台异步静默刷新，单次请求响应稳定在 200~300ms 级别；
5. **离线 Public Suffix List (PSL) Trie 域名解析**：
   - 内置官方 PSL 前缀树，零网络开销精确识别 `.com.cn`、`.co.uk`、`.com.au` 等复合域名；
6. **代理池与熔断器（Proxy Circuit Breaker）**：
   - 支持 `PAC_PROXIES` 代理池轮换与故障自动冷却。

---

## 快速安装

### 本地开发安装
```bash
git clone https://github.com/logicrw/pac-cli.git
cd pac-cli
python3 -m venv .venv && source .venv/bin/activate

# 安装完整能力包（含 stealth 与 eval 依赖）
pip install -e ".[all]"
playwright install chromium
camoufox fetch

# 检查环境健康度
pac doctor --compact
```

### Docker 容器化运行
```bash
# 构建镜像
docker build -t logicrw/pac-cli .

# 单篇抓取
docker run --rm logicrw/pac-cli fetch "https://www.economist.com/..." --compact

# 链路诊断抓取
docker run --rm logicrw/pac-cli fetch "https://www.wsj.com/..." --diagnostics --compact
```

---

## 使用指南

```bash
# 1. 抓取文章（默认精简 JSON）
pac fetch "https://www.wsj.com/articles/..." --compact

# 2. 抓取并输出链路耗时与质量诊断
pac fetch "https://www.wsj.com/articles/..." --diagnostics --compact

# 3. 批量抓取（自动去重、带 URL 哈希防文件名覆盖）
pac batch --file urls.txt --out-dir articles --compact

# 4. 文章发现（支持纯本地零网络 Google News 解码与 RSS/Sitemap 探测）
pac discover economist.com --limit 5 --compact

# 5. 规则同步与检查
pac rules show wsj.com --compact
pac rules sync --compact
```

---

## 测试

```bash
pip install -e ".[test]"
pytest -q
```

全量 242 个自动化单元与集成测试（包含 77 个 PSL 官方参考用例与 5 个黄金质量门禁测试）。

---

## 开源许可

本项目遵循 MIT 协议，内置的 Public Suffix List 遵循 MPL-2.0 协议，详情见 [NOTICE](NOTICE)。
