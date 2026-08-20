# PAC-CLI 系统架构设计说明书

**项目名称**: `pac-cli` (Paywall Article Collector)  
**当前版本**: `0.2.2` (Permanent Architectural Freeze)  
**定位**: 专为 AI Agent（Claude Code、Cursor、Codex 等）设计的高鲁棒性、确定性新闻抓取与正文 Markdown 提取引擎。

---

## 1. 架构设计哲学

PAC 的核心设计遵循 **"Agent-First, Defense-in-Depth, Zero-Placebo"** 原则：

```
                    输入 URL / 关键词 (CLI / Python API)
                                    │
                                    ▼
                     ┌─────────────────────────────┐
                     │ 0. Ingress & 离线 PSL 解析器 │ (registrable_domain via PSL Trie)
                     └──────────────┬──────────────┘
                                    │
    ┌───────────────────────────────┴───────────────────────────────┐
    ▼                                                               ▼
[单一/批量抓取 (pac fetch/batch)]                             [文章发现 (pac discover)]
    │                                                               │
    ├─ 1. DirectHttp (curl_cffi TLS JA3/JA4 伪装)                  ├─ 1. Google News 本地 Protobuf 逆向解包
    ├─ 2. StealthBrowser (Camoufox 内核 / Playwright 渲染)         ├─ 2. 站点首页 HTML RSS 自动嗅探
    └─ 3. MultiGatewayArchive (archive.today / Wayback / Reader)    ├─ 3. 常见 RSS 路径探测
                                    │                               └─ 4. Sitemap 站点地图兜底
                                    ▼
                     ┌─────────────────────────────┐
                     │ 4. Quality Gate 质量门禁系统 │ (quality/ access_control, paywall, metrics)
                     └──────────────┬──────────────┘
                                    │
                                    ▼
                     ┌─────────────────────────────┐
                     │ 5. 标准化结果输出 Envelope  │ (JSON / Markdown / --diagnostics)
                     └─────────────────────────────┘
```

---

## 2. 核心子系统详解

### 2.1 异步职责链执行管道 (`src/bpc_fetch/strategy.py`)
整个获取流程被编排为严格按成本由低到高降级的责任链：
1. **`DirectHttpHandler`**：
   - 自动检测并优先使用 `curl_cffi` 模拟 Chrome 协议级 TLS/JA3/JA4 握手与 HTTP/2 指纹；
   - 动态应用站点特定策略（自定义 UA、Googlebot 爬虫、Referer 头、Cookie 规则）；
   - 在无可用规则时自动尝试 Googlebot 降级。
2. **`StealthBrowserHandler`**：
   - 自动调度 `Camoufox`（基于 Firefox C++ 内核的抗检测浏览器）或 `Playwright Chromium`；
   - 拦截并屏蔽 paywall 脚本与遥测追踪；
   - 执行主世界（Main World）DOM 清洗与遮罩隐藏。
3. **`MultiGatewayArchiveHandler`**：
   - 当原站直连和浏览器渲染均遭遇强阻断时触发；
   - 复合降级梯队：`archive.today / archive.ph` ➔ `Wayback Machine API` ➔ `Reader Gateway`（如 Jina Reader）。

### 2.2 离线 Public Suffix List (PSL) 前缀树 (`src/bpc_fetch/sites.py`)
- 内置 Mozilla 官方 Public Suffix List 离线数据文件（`public_suffix_list.dat`，MPL-2.0）；
- 基于反向 Label 构建惰性加载的内存 Trie 树；
- 零网络开销、微秒级精确解析全球所有复合国家域名（如 `.com.cn`、`.co.uk`、`.com.au`、`.edu.tw` 等）、通配符规则（`*.ck`）与异常规则（`!www.ck`）。

### 2.3 模块化质量门禁与反拦截壳 (`src/bpc_fetch/quality/`)
彻底杜绝“把 403 页面或登录弹窗误判为抓取成功”的假阳性脏数据：
- **`access_control.py`**：精确识别 Cloudflare Turnstile/Challenge、Akamai、DataDome、PerimeterX、Imperva、AWS WAF 拦截壳；
- **`paywall.py`**：多语言（10+ 语种）Teaser 与订阅提示特征识别，提供 `clean_paywall_text()` 正文清洗；
- **`metrics.py`**：正文-HTML 字符比、段落长度方差与离散系数（CV）、链接密度、DOM 深度分析，并对短快讯（300字以内短新闻）提供智能豁免。

### 2.4 SWR 零阻塞规则引擎 (`src/bpc_fetch/rules/sync.py`)
- **Stale-While-Revalidate（SWR）**：`pac fetch` 和 `pac batch` 主调用路径纯读本地缓存立即返回（0ms 阻塞）；
- **快照原子性（Crash-Durability）**：生成单文件 `rules_snapshot.json` 并执行 `fsync()` 刷盘，杜绝进程崩溃导致的混代状态；
- **防竞态锁**：SWR 锁注入 `{owner_pid, created_at, token}`，释放锁时强校验 128-bit Token，避免长耗时任务被误删锁；
- **后台静默刷新**：默认 7 天 TTL 超期后，通过 Detached 子进程在后台静默拉取更新，并配备长驻留进程 Reaper 线程回收子进程。

### 2.5 代理池与熔断器 (`src/bpc_fetch/browser.py`)
- 支持通过 `PAC_PROXIES="http://p1:8080,http://p2:8080"` 配置有序候选池；
- 遭遇 `403` / `429` / `BOT_CHALLENGE` / `NETWORK` 超时时，自动将故障节点置入冷却状态，并无缝轮换到下一个候选代理；
- 全节点冷却时放行最早恢复的节点做 half-open 探测，绝不私自降级为直连以防泄露真实 IP。

### 2.6 纯本地 Google News Protobuf 逆向解包 (`src/bpc_fetch/discover.py`)
- 针对 Google News RSS 发出的混淆链接（`CBMi...`），通过纯本地 URL-safe Base64 与 Protobuf 长度字段逆向解析算法，0 毫秒、0 网络请求瞬间还原原始媒体真实 URL；
- 带 `_safe_get()` 逐跳 SSRF 验证与 2MB payload 限制网络兜底。

---

## 3. 对外契约规范

### 3.1 退出码规范 (Exit Status)
- 成功 (`ok: true`)：退出码 `0`；
- 失败 (`ok: false`)：退出码 `1`（使下游 AI Agent 能够精准根据 exit code 判定执行状态）。

### 3.2 诊断与追踪 Envelope (`--diagnostics`)
当显式开启 `--diagnostics` 时，JSON 会附加 `diagnostics` 追踪块：
```json
{
  "ok": true,
  "url": "https://www.wsj.com/articles/...",
  "domain": "wsj.com",
  "title": "...",
  "markdown": "...",
  "diagnostics": {
    "request_id": "a83fd92c4f1e",
    "total_latency_ms": 1240,
    "engine_timings_ms": { "http": 180, "camoufox": 1060 },
    "attempts": [
      {
        "handler": "DirectHttpHandler",
        "label": "http_primary",
        "engine": "http",
        "status": 403,
        "elapsed_ms": 180,
        "error_code": "BOT_CHALLENGE"
      },
      {
        "handler": "StealthBrowserHandler",
        "label": "browser_cleanup",
        "engine": "camoufox",
        "status": 200,
        "elapsed_ms": 1060,
        "quality_reason": "pass"
      }
    ],
    "quality": {
      "evaluated": true,
      "ok": true,
      "score": 0.88,
      "reason": "pass",
      "components": { ... }
    }
  }
}
```

---

## 4. 测试与基准质量门禁

全量测试套件共包含 **242 个自动化单元与集成测试**：
- `test_psl_resolver.py`：77 个官方 Public Suffix List 参考用例；
- `test_golden_quality.py`：5 种黄金基准排版回归测试（完整文章、300字快讯、403挑战页、Teaser残页、导航页）；
- `test_phase5_consolidation.py`：SWR 锁原子性、BrowserPool 租约隔离、诊断输出契约测试。
