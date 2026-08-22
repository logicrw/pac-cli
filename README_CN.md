# pac-cli

面向 AI Agent 的新闻抓取 CLI：把文章抽成 Markdown。发现范围是 **29 家精选金融/科技媒体**，规则库覆盖更多站点。

> URL → JSON（`markdown`、`strategy_hit`、`error_code`、`failure_class`）

## 安装

```bash
git clone https://github.com/logicrw/pac-cli.git
cd pac-cli
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"
playwright install chromium
camoufox fetch
pac doctor --compact
```

## 用法

```bash
pac feeds health --concurrency 4 --compact
pac discover ft.com --limit 8 --compact
pac fetch "https://www.wsj.com/articles/..." --compact
pac fetch "https://www.ft.com/content/..." --interactive --compact
pac batch --file urls.txt --out-dir articles --compact
pac rules show wsj.com --compact
```

- 发现走官方 Feed，不行再 Bing。Google News 只当标题信号，不能当正文 URL，也不能进 `batch`。
- teaser / 验证码页 / 导航壳 → `ok: false`，除非 `--allow-partial`。
- `--interactive` 用 Ego lite 里已装的 Bypass Paywalls Clean，给 FT/Bloomberg 等授权站按需全文。不进 `batch`，不要加 `--cookie`。
- 默认浏览器路径不删 paywall overlay；只有 `PAC_BROWSER_PAYWALL_CLEANUP=1` 才开启。

## 文档

- [架构](docs/ARCHITECTURE.md)
- [29 源迁移边界](docs/CURATED_SOURCE_MIGRATION.md)

## 测试

```bash
pip install -e ".[test]"
pytest -q
```

## 许可

MIT。内置 Public Suffix List 为 MPL-2.0，见 [NOTICE](NOTICE)。
