# PAC 约定

面向金融/科技新闻的个人研究 CLI。精选名单是 **29 源**，以 `src/bpc_fetch/data/curated_sources.json` 为准。

## 红线

- 不要把历史 48 源或「约 50 源」加回来；增删媒体必须用户明确决定。
- `news-scraper-final` 只读参考，不要复用它的抓取代码（短 teaser 会被算成功）。
- 不要把 teaser / nav / challenge 当正文。
- 不要把 publisher cookie 送给 archive、reader、cloud、Yahoo。
- 不要把 Google News 解码结果当 production canonical URL，也不要把它接进 `pac batch`。
- `--interactive` 不进默认 `batch`，并发恒为 1。不要复制日常 Chrome Default，不要 `import` Chrome 资料进 Ego。
- 不要实现后台轮询服务，除非用户以后明确要求。

## 日常

```bash
pac feeds health --concurrency 4 --compact
pac discover ft.com --limit 20 --compact
pac fetch "<URL>" --compact
pac fetch "<URL>" --interactive --compact   # FT/Bloomberg 等授权站按需
```

Firecrawl、cookie vault、Yahoo 转载都要看 `source_retrieval_policies.json`，不要当全局开关。
