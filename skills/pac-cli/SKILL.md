---
name: pac-cli
description: Fetch paywalled news articles, discover recent publisher articles, or batch extract structured Markdown using PAC. Use when the user asks to fetch or discover news (WSJ, FT, Bloomberg, Economist, Reuters, etc.), bypass paywalls for personal research, or extract article Markdown.
version: 0.2.2
---

# PAC CLI

PAC 面向 **29 家精选金融/科技媒体** 做发现与诚实提取。规则库覆盖更多站点，但 curated discovery 名单不要自行扩大。

## 命令

| 任务 | 命令 |
|---|---|
| 环境 | `pac doctor --compact` |
| 公开 Feed 健康 | `pac feeds health --concurrency 4 --compact` |
| 发现最近文章 | `pac discover ft.com --limit 8 --compact` |
| 抓取 | `pac fetch "<URL>" --compact` |
| 授权站按需全文 | `pac fetch "<URL>" --interactive --compact` |
| 批量（不要加 `--interactive`） | `pac batch --file urls.txt --out-dir ./articles --compact` |
| 规则 | `pac rules show wsj.com --compact` |

始终加 `--compact`。发现用官方 Feed / Bing；Google News 只是标题信号，不能当 canonical URL，也不能直接丢进 `batch`。

查某站新闻的标准流程：`pac discover <domain>` → 需要全文时 `pac fetch`；FT/Bloomberg 等默认管道只剩 teaser 时再 `--interactive`。

## 结果

- `ok: true`：过 quality gate 的 Markdown。不是金标对照全文。
- `PAYWALL_REMAINING`：teaser，不要当成功。
- `BOT_CHALLENGE` / `EXTRACT_FAILED`：停下来报告，不要当正文。
- `--interactive` 成功时 `engine` 为 `interactive_cdp`，`interactive.cookie_copied` 必须为 false。

## 约束

- 不要把 `--cookie` / `PAC_COOKIE` 和 `--interactive` 一起用。BPC 登录留在 Ego lite。
- `--interactive` 不进 `batch`。PAC 可按需打开 Ego lite，抽完不退出。
- Firecrawl 仅按 retrieval policy 使用。Bloomberg Yahoo 转载必须带 provenance，不得声称逐字原文。
- 第三方 archive/reader/cloud 不得带 publisher cookie。

## 环境变量

- `PAC_PROXIES`：代理池
- `PAC_COOKIE` / `pac cookies`：按注册域的授权 session（仅 publisher）
- `PAC_FIRECRAWL_API_KEY`：按 policy 的云端兜底
- `PAC_BROWSER_PAYWALL_CLEANUP=1`：才允许浏览器拦 paywall / 揭 overlay
- `PAC_INTERACTIVE_BACKEND=drissionpage`：改走专用 Chrome profile attach
- `PAC_INTERACTIVE_EGO_AUTOSTART=0`：禁止 PAC 自动打开 Ego lite
