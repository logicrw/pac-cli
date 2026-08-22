# PAC 架构

当前产品范围：金融/科技新闻的 **29 家精选媒体** 发现 + 诚实全文提取。版本号仍为 `0.2.2`；29 源策略与 `--interactive` 在后续提交中落地。

## 管道

```
URL / 域名
  ├─ discover: 已验证官方 Feed → Bing site: → Google News 仅标题信号
  └─ fetch/batch:
        Direct HTTP (curl_cffi)
        → 隐身浏览器 (Camoufox / Playwright；paywall cleanup 默认关闭)
        → 按 retrieval policy 的 archive / Firecrawl / Yahoo 转载
        → quality gate（teaser / challenge / 导航壳不得当全文）
  └─ fetch --interactive（不进 batch）:
        Ego lite 专用 task space，BPC 会话留在浏览器
```

## 发现

权威配置：

- `src/bpc_fetch/data/curated_sources.json`：29 源与 Feed
- `src/bpc_fetch/data/source_discovery_policies.json`：发现策略

`news-scraper-final` 只作只读参考。不要把历史 48 源或「约 50 源」写回 PAC。Google News 解码器仅为兼容保留，生产 URL 不得用它冒充 canonical。

特例：Reuters 走有上限的官方 UTC sitemap（失败再 Bing）；The Information 公开 `/feed` 为 403，发现靠 sitemap，正文要授权 session。

## 检索与出处

权威配置：`src/bpc_fetch/data/source_retrieval_policies.json`。

- Firecrawl 只按 policy + failure code + budget 使用，不是全局兜底。
- Bloomberg 原站失败时，policy 允许 Yahoo Finance `(Bloomberg)` 转载候选：双 URL、`syndicated=true`、`original_publisher=bloomberg`、`text_identity=unknown`，不得声称逐字原文。
- 第三方 archive / reader / cloud / Yahoo **不得**收到 publisher cookie。跨注册域 redirect 同时去掉 header 与 strategy cookie。

## 浏览器

- 默认浏览器路径：**不**拦截 paywall provider、**不**删 overlay。仅 `PAC_BROWSER_PAYWALL_CLEANUP=1` 才 opt-in。
- Cookie 注入必须绑在目标 URL 上，禁止 context-wide `Cookie` header。
- `--interactive`：默认 Ego lite。PAC 可按需打开应用，只开关自己的 tab，并发 1。Chrome/DrissionPage attach 需 `PAC_INTERACTIVE_BACKEND=drissionpage` 与专用 profile，禁止日常 Chrome Default。

## 质量门禁

`src/bpc_fetch/quality/`：`access_control`（challenge 壳）、`paywall`（teaser）、`metrics`（结构分）。`ok: true` 表示通过门禁，不是「已对照金标全文」。

## 延期

可选的 10–15 分钟轮询、已见 URL 库、后台全文、本地资料库、主动通知——未实现，除非以后明确启动。
