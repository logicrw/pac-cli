# pac-cli 实施说明（代码怎么改）

**状态**: 待专家评审 — **评审通过前不写生产代码**  
**作者**: 实施方拟定计划（基于 fork 真实源码 + 架构 v0.4 + 专家第二轮意见）  
**对照源码基线**: `logicrw/pac-cli` = fork of `Sophomoresty/bpc-fetch`（本地曾 clone 到 `/tmp/pac-cli-upstream` 核对）  
**本地文档仓**: `~/Projects/pac-cli`（当前几乎只有 docs；代码在 GitHub fork）  
**远程**: https://github.com/logicrw/pac-cli  
**文档版本**: implementation-draft-2（**含自我审查修订**；draft-1 不可直接开工）

> **专家请先读 §S 自我审查，再读实施细节。**  
> 架构背景见同目录 [`ARCHITECTURE.md`](./ARCHITECTURE.md)。  
> 若本文与 ARCHITECTURE 冲突，**以本文「Phase 1 实施」为准**（更贴近可写代码）。

---

## S. 自我审查（实施方主动挑刺 — 2026-07-29）

### S.0 总评（人话）

| 问题 | 结论 |
|------|------|
| 方向对不对？ | **对。** fork 上补字段 + plan + 质量门 + CLI 面，能服务「多 Agent 用的 pac」 |
| draft-1 能否原样开工？ | **不能。** 有几处会在实践中直接翻车或过度承诺 |
| 能否达到产品目标？ | **能达到 Phase1 目标**（可安装 CLI、JSON、规则可版本化、策略类站明显好于裸 bpc）；**不能保证** hard/bot 站大捷或逼近 BPC 扩展 |
| 5 天是否够？ | **紧。** 若含真实 eval 网络抖动，应按「核心 5 天 + 缓冲 2 天」或砍 scope |

下面是 **必须在开工前写进计划的修正**（相对 draft-1）。

---

### S.1 会翻车的问题（高）

#### S.1.1 规则同步主 URL 当前不可用

**事实（今日探测）**：

```text
GET gitflic .../bypass-paywalls-chrome-clean/blob/raw?file=sites.js&branch=master
→ HTTP 404
```

`sites_updated.json`（bpc_updates）**可以**拉到。

**draft-1 问题**：把「远程拉完整 sites.js」写成主路径，DoD「rules sync」会经常红。

**修正（Phase1 规则源优先级）**：

1. **主**：仓库内 bundled `data/sites.js`（随 fork/release 更新；可用脚本从已下载 zip/crx 或 mirror 手工刷新）  
2. **增量**：拉 `sites_updated.json` merge 进 parse 结果（热修）  
3. **可选增强**：若未来 `PAC_SITES_JS_URL` 配通再覆盖全量  
4. sync **成功定义** = 写出 manifest + 可用 map；**不要求**每次全量远程 sites.js 200  

**`pac rules sync` 语义改为**：

- 尝试 fetch `sites_updated.json` 并 merge  
- 若配置了可访问的全量 URL 再更新 base  
- 任何网络失败 → 保留本地，返回 `ok: true, stale: true` 或 `ok: false` 但 doctor 可区分（建议：`ok: true` + `warnings: ["using_bundled_base"]` 以免 Agent 误判致命）

#### S.1.2 `sites_updated.json` merge 被写得太天真

**事实**：热修条目不只是扁平 domain 字段，还包含例如：

- `group: [...]` 多域名  
- `exception: [{ domain, block_regex, ...}]`  
- `block_js_inline`（**当前 SiteStrategy 与 browser 都不支持**）  
- `remove_cookies_select_drop`  
- `upd_version`

**draft-1 问题**：「对每个 entry 取 domain 覆盖」会 **漏 group/exception**，也 **静默丢** `block_js_inline`。

**修正**：

```text
merge 算法 Phase1：
  for each entry in sites_updated:
    if domain 以 ### 开头且有 group:
      for d in group: upsert(d, fields)
    if domain 正常: upsert(domain, fields)
    if exception 列表:
      for ex in exception: upsert(ex.domain, ex.fields)  # 覆盖 group 默认
    未知字段（block_js_inline 等）→ 写入 strategy.extra: dict 保留
    Phase1 执行层：能用的字段用；extra 仅 show/debug
```

`SiteStrategy` 增加：

```python
extra: dict = field(default_factory=dict)  # 未建模字段
```

**不宣称** Phase1 执行 `block_js_inline` / cookie 精细删除 —— 写进「已知缺口」。

#### S.1.3 `referer_custom` 很重要，但不是银弹

**事实**：bundled `sites.js` 里 `referer_custom` **仅约 2 处**（含 WSJ）。  
`cs_dompurify` ~159，`block_regex` ~361。

**draft-1 问题**：叙述容易让人以为「补字段 = hard 站大面积变好」。

**修正 — 成功预期写死**：

| 改动 | 预期收益 |
|------|----------|
| referer_custom + headers | **WSJ 等极少数站**策略类修复；必须单测 + 尽量实网验证 |
| cs_dompurify→浏览器清理 | 中等；**通用 unhide ≠ BPC contentScript**，部分站仍失败 |
| block_regex | 依赖现有 regex→glob，**仍脆弱**；Phase1 只保证「能挂 route」，不保证等价扩展 |
| archive.is | 部分站救命；有 429/验证码，**不能当稳定主路径** |
| 质量门 | **不提高成功率**，只减少「假成功」——对 Agent 目标同样关键 |

产品目标达成定义应是：

> **可分发的诚实 CLI** + **策略字段不再静默丢** + **假全文不 ok** + **不低于 bpc 基线**  
> 而不是「hard 站全面打通」。

#### S.1.4 实网 DoD 与 bot 墙混淆

**问题**：WSJ/Economist **实网**失败可能是 bot/IP，不是字段没补上。  
若 DoD 绑定「实网 WSJ 必过」，CI/家宽可能永久红。

**修正 — 双层验收**：

| 层 | 要求 |
|----|------|
| **A. 确定性（必须）** | 单测：parse WSJ → `referer_custom` 非空；`build_headers` 含该 Referer；`bypass_type`≠archive when only cs_dompurify |
| **B. 实网（尽力）** | eval 集对比 baseline；允许标注 `environment_limited`；**不得**因单一 bot 墙否决字段修复 PR |

`strategy_hit` 含 `http_referer_custom` 应在 **mock httpx** 或 headers 单测中验证，不单靠实网。

#### S.1.5 Markdown 塞满 stdout 会打爆 Agent 上下文

**draft-1** 默认 JSON 带全文 `markdown`。

**修正**：

```text
默认：
  markdown_max_chars = 100_000（或 50_000）
  超出则 markdown 截断 + truncated=true + content_chars=全文长度
  --full 才输出不截断
落盘：仅 --out-dir 时写完整文件
```

---

### S.2 中风险 / 计划过厚

| 项 | 问题 | 修正 |
|----|------|------|
| 5 工作日 | strategy 重写 + rules + eval + 删命令 + 实网，偏满 | 核心路径优先：字段+headers+quality+envelope+cli slim；rules sync **简化为 bundled+updated merge**；archive.is 可降为 P1.1 |
| googlebot fallback | 很多站已无效，白耗时 | 保留但 **短超时**；或仅当 primary 非 bot UA 时尝试 |
| regex→glob | 老问题未真正解决 | Phase1 文档承认；有空再加「整段 regex 用 page.route(callable) 匹配 URL」 |
| 删除 history/incremental | 有人依赖 | Phase1 删 CLI；文件可先移到 `src/bpc_fetch/_removed/` 一版再物理删 |
| pyproject name→pac-cli | 可能惊动依赖 bpc-fetch 的环境 | Phase1：**只加 script `pac=`**，`name` 可暂留 `bpc-fetch` 或双发布说明 |
| allow_cookies | 规则有、执行几乎无 | Phase1 不假装实现；`rules show` 展示字段即可 |
| SSRF | Agent 乱 fetch 内网 | Phase1：拒绝 `localhost`/`127.0.0.1`/`169.254.`/`10.` 等（可开关） |

---

### S.3 draft-1 里仍然正确、应保留的部分

- 在 fork 上演进，不重写  
- 包目录保持 `bpc_fetch`，入口 `pac`  
- `referer_custom` 进 dataclass + `build_headers`  
- `cs_dompurify` 语义改为浏览器清理而非 archive  
- 质量门默认 `ok=false`（teaser）  
- 统一 envelope：`error_code` + `failure_class` + `strategy_hit`  
- 不做 client.py / MCP / Camoufox 默认 / glue 接入  
- eval 基线表思想（但 URL 与环境预期要诚实）  

---

### S.4 修订后的 Phase1 范围（以这个为准）

**必须做（P0）**

1. 合并 fork 代码 + docs  
2. `SiteStrategy.referer_custom` + headers + 单测  
3. `cs_dompurify` → browser_cleanup 语义与 plan  
4. `quality` + 统一 fetch JSON envelope + 截断策略  
5. CLI：`pac` 入口；fetch/batch/doctor/rules；去掉 crawl/search/discover（history 按上表）  
6. rules：**bundled base + sites_updated merge + manifest**（不依赖 gitflic 全量 sites.js 200）  
7. eval harness + baseline 对比脚本  
8. NOTICE + README 安装  

**应该做但可缩（P0.5）**

- archive.is 条件回退  
- batch 共享 browser pool  
- Patchright 可选  

**明确不做（Phase1）**

- 完整 contentScript / ld_json / AMP 执行 / block_js_inline  
- load-extension  
- Skills 定稿（可周末草稿）  
- scraper 接入  

---

### S.5 对「能不能达到目标」的诚实答案

| 目标 | 能否达到 |
|------|----------|
| 独立 CLI 给多 Agent | **能**（P0 做完即具备） |
| Skills 分发 | **能**（文档工作，依赖 CLI 稳） |
| 比裸 bpc-fetch 更正确 | **能**（字段不再丢、假全文更少） |
| 实网成功率全面暴涨 | **不保证**；策略类局部改善，bot 类靠后 |
| 一周内完美 | **不保证**；按 S.4 砍 scope 更现实 |

---

## 0. 一句话思路（先审这个）

在 **不重写整个项目** 的前提下：

1. 把 GitHub fork 代码合并进本地 `~/Projects/pac-cli`（保留现有 `docs/`）。  
2. **内部包名仍叫 `bpc_fetch`**，只增加 CLI 入口 **`pac`**。  
3. **大改 3 处**：`sites.py`（字段）、`strategy.py`（按站 plan + 质量门）、新建 `rules/`（同步）。  
4. **改 `cli.py`**：删 discover/crawl/search/history；加 `rules`；`fetch` 输出统一 envelope。  
5. **小改** `browser.py` / `extract.py` / `pyproject.toml`；加 eval 夹具与基线脚本。  
6. **不做**：包重命名、`client.py` 库 API、MCP、Camoufox 默认、接 scraper glue。

---

## 1. 仓库落地（Day 0 / D1 前置）— 怎么把代码弄到本地

### 1.1 现状

| 位置 | 内容 |
|------|------|
| `~/Projects/pac-cli` | `.git` + `docs/` + `README.md` + `.gitignore`（**无 src/**） |
| `github.com/logicrw/pac-cli` | 完整 bpc-fetch fork（有 `src/bpc_fetch/*`） |

### 1.2 拟定操作（专家可改）

**方案 A（推荐）**：以 GitHub fork 为 main，把本地 docs 合进去。

```bash
# 备份当前文档仓
mv ~/Projects/pac-cli ~/Projects/pac-cli-docs-only

# 克隆 fork
gh repo clone logicrw/pac-cli ~/Projects/pac-cli
cd ~/Projects/pac-cli

# 拷回架构文档（覆盖/新增）
cp -R ~/Projects/pac-cli-docs-only/docs ./docs
# 用我们写好的 README 覆盖或 merge
# 保留 NOTICE 计划、.gitignore 合并

git status
pip install -e .
# 此时应能: bpc-fetch doctor --compact  （旧入口仍在）
```

**方案 B**：在现有 docs 仓 `git remote add` 后 `git fetch` + 允许 unrelated histories merge。更易冲突，次选。

### 1.3 合并后目标树（Phase 1 结束时应有）

```text
~/Projects/pac-cli/
  README.md
  NOTICE                          # 新增：上游署名
  pyproject.toml                  # 改：scripts 增加 pac=
  docs/
    IMPLEMENTATION.md             # 本文
    ARCHITECTURE.md
  data/
    sites.js                      # 仍可作 bundled 种子
    sites_cache.json              # 可生成
  src/bpc_fetch/
    __init__.py
    __main__.py                   # 可改为调 pac 或保留 bpc-fetch
    cli.py                        # 改
    sites.py                      # 大改字段
    strategy.py                   # 大改 plan
    browser.py                    # 小改
    extract.py                    # 小改质量
    rules/                        # 新增包
      __init__.py
      sync.py
      store.py
      paths.py
    quality.py                    # 新增（或放 extract；倾向独立）
    result.py                     # 新增：envelope / error codes
    # 删除或不再从 cli 引用：
    crawl.py, discover.py, search.py, history.py  → 删除文件（专家建议删而非藏）
  tests/
    fixtures/eval_urls.yaml       # 新增
    test_sites_referer_custom.py  # 新增
    test_quality_gate.py
  scripts/
    run_eval.py                   # 可选：跑基线
```

---

## 2. 命名与打包（最小改动）

### 2.1 `pyproject.toml` 改前 → 改后

**改前（fork 现状）:**

```toml
[project]
name = "bpc-fetch"
version = "0.1.0"
# ...
[project.scripts]
bpc-fetch = "bpc_fetch.cli:main"
```

**改后（Phase 1）:**

```toml
[project]
name = "pac-cli"                    # 产品名；若担心打断依赖可暂留 bpc-fetch，专家意见：产品面 entry 即可
version = "0.2.0"                   # 标记演进
description = "Fetch paywalled news articles as markdown (CLI for agents)"
requires-python = ">=3.11"
dependencies = [
    "httpx>=0.27",
    "trafilatura>=2.0",
    "markdownify>=0.14",
    "beautifulsoup4>=4.12",
    "playwright>=1.40",
    "platformdirs>=4.0",            # 新增：规则缓存目录
    # 可选 Phase1： "patchright>=1.0" 若采用 Patchright 优先
]

[project.scripts]
pac = "bpc_fetch.cli:main"          # 主入口
bpc-fetch = "bpc_fetch.cli:main"    # 兼容别名，Phase 2 可删

[tool.hatch.build.targets.wheel]
packages = ["src/bpc_fetch"]
```

**明确不做（Phase 1）:** 把包目录 `bpc_fetch` 重命名为 `pac`（机械重命名一天，零产品收益）。

---

## 3. `sites.py` — 字段补全（大改点 1）

### 3.1 问题（已核实）

`SiteStrategy` **没有** `referer_custom`：

```python
# 现状 sites.py:22-33
@dataclass
class SiteStrategy:
    domain: str
    name: str = ""
    useragent: str = ""
    useragent_custom: str = ""
    referer: str = ""
    random_ip: str = ""
    allow_cookies: bool = False
    block_regex: str = ""
    cs_dompurify: bool = False
    group: list[str] = field(default_factory=list)
```

`_build_strategy` **不读** `referer_custom`：

```python
# 现状 sites.py:81-93
def _build_strategy(...):
    return SiteStrategy(
        ...
        referer=props.get("referer", ""),
        # 缺失: referer_custom=props.get("referer_custom", ""),
        ...
    )
```

`bypass_type()` 把 `cs_dompurify` 标成 `"archive"`（错误语义）：

```python
# 现状 sites.py:42-43
if self.cs_dompurify:
    return "archive"
```

### 3.2 拟定改后：`SiteStrategy`

```python
@dataclass
class SiteStrategy:
    domain: str
    name: str = ""
    useragent: str = ""
    useragent_custom: str = ""
    referer: str = ""
    referer_custom: str = ""          # NEW
    random_ip: str = ""
    allow_cookies: bool = False
    block_regex: str = ""
    cs_dompurify: bool = False
    # Phase1 可选解析保留，未必全部执行：
    amp: bool = False                 # NEW optional，先存着
    # raw 未识别字段不进 dataclass；需要时另存 rules store
    group: list[str] = field(default_factory=list)

    def needs_browser_cleanup(self) -> bool:
        """cs_dompurify 语义：需要浏览器内 unhide/清理，不是 archive。"""
        return bool(self.cs_dompurify)

    def bypass_type(self) -> str:
        """人类可读主策略标签（用于展示，不单独驱动执行）。"""
        if self.useragent_custom:
            return "ua:custom"
        if self.useragent:
            return f"ua:{self.useragent}"
        if self.referer_custom:
            return "referer:custom"
        if self.referer:
            return f"referer:{self.referer}"
        if self.block_regex:
            return "block_js"
        if self.cs_dompurify:
            return "dom_cleanup"   # 不再是 archive
        return "cookies"
```

### 3.3 拟定改后：`_build_strategy`

```python
def _build_strategy(domain: str, name: str, props: dict) -> SiteStrategy:
    return SiteStrategy(
        domain=domain,
        name=name,
        useragent=str(props.get("useragent") or ""),
        useragent_custom=str(props.get("useragent_custom") or ""),
        referer=str(props.get("referer") or ""),
        referer_custom=str(props.get("referer_custom") or ""),  # NEW
        random_ip=str(props.get("random_ip") or ""),
        allow_cookies=bool(props.get("allow_cookies")),
        block_regex=str(props.get("block_regex_str") or props.get("block_regex") or ""),
        cs_dompurify=bool(props.get("cs_dompurify")),
        amp=bool(props.get("amp")),
        group=list(props.get("group") or []),
    )
```

### 3.4 缓存兼容

`get_sites_map` 从 `sites_cache.json` 反序列化时：

```python
# SiteStrategy(**v) 会因旧 cache 缺字段而炸
# 改法：用 asdict 默认值合并
def _strategy_from_dict(v: dict) -> SiteStrategy:
    known = {f.name for f in fields(SiteStrategy)}
    filtered = {k: v[k] for k in v if k in known}
    return SiteStrategy(**filtered)
```

规则同步启用后：**优先读 rules store**，bundled `data/sites.js` 仅作种子/离线回退。

### 3.5 单测（必须）

```python
# tests/test_sites_referer_custom.py
def test_wsj_has_referer_custom(tmp_path):
    # 最小 sites.js 片段含 WSJ + referer_custom
    # parse 后 strategy.referer_custom == "https://www.drudgereport.com/"
    # bypass_type() != "archive" 即使 cs_dompurify=1
```

---

## 4. `strategy.py` — 执行 plan（大改点 2）

### 4.1 问题（已核实）

`build_headers` **不用** `referer_custom`：

```python
# 现状 strategy.py:38-47 — 只处理 referer in {google,facebook,twitter}
ref = strategy.referer.lower() if strategy.referer else ""
if ref == "google":
    headers["Referer"] = REFERER_GOOGLE
# ... 无 referer_custom 分支
```

`fetch_with_retries` 固定链：primary → googlebot → browser(仅 block_js) → archive.org。

### 4.2 拟定改后：`build_headers`

```python
def build_headers(strategy: SiteStrategy) -> dict[str, str]:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    # UA（保持现有逻辑）
    if strategy.useragent_custom:
        headers["User-Agent"] = strategy.useragent_custom
    else:
        ...  # googlebot/bingbot/facebook/normal 不变

    # Referer：custom 优先于枚举
    if strategy.referer_custom:
        headers["Referer"] = strategy.referer_custom
    else:
        ref = (strategy.referer or "").lower()
        if ref == "google":
            headers["Referer"] = REFERER_GOOGLE
        elif ref == "facebook":
            headers["Referer"] = REFERER_FACEBOOK
        elif ref == "twitter":
            headers["Referer"] = REFERER_TWITTER
        elif not strategy.useragent and not strategy.useragent_custom:
            headers["Referer"] = REFERER_GOOGLE

    if strategy.random_ip:
        ...  # 保持

    return headers
```

### 4.3 新模块边界：`result.py` + `quality.py`

**`result.py`（新建）** — 统一 envelope 与错误码：

```python
# 错误码（Phase1）
ERROR_CODES = {
    "RULE_MISSING",
    "NO_STRATEGY",
    "NETWORK",
    "HTTP_BLOCKED",
    "BOT_CHALLENGE",
    "PAYWALL_REMAINING",
    "EXTRACT_FAILED",
    "BROWSER_UNAVAILABLE",
    "ARCHIVE_FAILED",
    "LIMIT_EXCEEDED",
    "INTERNAL",
}

FAILURE_CLASS = {
    "strategy",   # 规则/字段/paywall 语义类
    "bot",        # 403/挑战/反爬
    "network",
    "extract",
    "config",
    "none",       # 成功
}

def ok_article(...): -> dict: ...
def fail_article(..., error_code, failure_class, strategy_hit, ...): -> dict: ...
```

**成功 JSON（fetch stdout）拟定：**

```json
{
  "ok": true,
  "url": "...",
  "domain": "wsj.com",
  "title": "...",
  "markdown": "...",
  "content_chars": 4200,
  "paywall_suspected": false,
  "strategy_hit": ["http_referer_custom", "quality_pass"],
  "rule_version": "bpc-2026-07-26#abc123",
  "engine": "http",
  "latency_ms": 1800,
  "path": null,
  "warnings": []
}
```

**失败 JSON 拟定：**

```json
{
  "ok": false,
  "url": "...",
  "domain": "wsj.com",
  "error_code": "PAYWALL_REMAINING",
  "failure_class": "strategy",
  "error": "quality gate failed after browser_cleanup",
  "strategy_hit": ["http_referer_custom", "browser_cleanup", "archive_is"],
  "rule_version": "...",
  "recovery_hint": "...",
  "latency_ms": 90000
}
```

默认：`ok=false` 当 teaser；**不加** `--allow-partial` 时不返回「假成功」。  
Phase1 可实现 `--allow-partial` 开关（专家建议有）；默认关。

**`quality.py`（新建）** — 从 strategy 抽出并加强：

```python
MIN_CONTENT_CHARS = 100  # 与 scraper 对齐，可 env 覆盖

# teaser 词表 = strategy._is_paywalled markers ∪ glue._PAYWALL_CUES（合并去重）

def quality_check(html: str | None, text: str | None, title: str = "") -> QualityResult:
    """
    returns:
      ok: bool
      paywall_suspected: bool
      reason: str
    """
    # 1) text 长度 < MIN → fail EXTRACT 或 PAYWALL
    # 2) 标题+正文前 1200 字命中 teaser → paywall_suspected
    # 3) 默认：paywall_suspected → ok=False（严格）
```

HTML 阶段仍可用 `_has_content` / `_is_paywalled`（迁到 quality.py）。

### 4.4 拟定改后：核心函数 `fetch_article`（替换 `fetch_with_retries` 语义）

保留函数名 `fetch_with_retries` **或** 新名 `fetch_article` + 旧名薄包装。推荐：

```python
async def fetch_article(
    url: str,
    strategy: SiteStrategy | None,
    *,
    client: httpx.AsyncClient | None = None,
    use_browser: bool | None = None,
    allow_partial: bool = False,
    rule_version: str = "",
) -> dict:
    """返回 ArticleResult dict（不是 tuple）。"""
```

**Plan 生成（伪代码，即执行顺序）：**

```python
def build_plan(strategy: SiteStrategy | None, use_browser: bool | None) -> list[str]:
    steps = ["http_primary"]
    # 若主策略不是 googlebot，保留 googlebot 作为轻量 fallback（可选，专家未禁止）
    steps.append("http_googlebot_fallback")

    want_browser = use_browser
    if want_browser is None:
        want_browser = bool(
            strategy and (
                strategy.block_regex
                or strategy.needs_browser_cleanup()  # cs_dompurify
                or strategy.useragent_custom
            )
        )
    if want_browser:
        steps.append("browser_cleanup")

    # archive：优先 is/today 若规则或域名策略暗示；否则 org
    steps.append("archive_is")
    steps.append("archive_org")
    return steps
```

**每步成功判定：**

1. 拿到 html（或 dom_result）  
2. `extract_article` → text  
3. `quality_check` → 通过才 `return ok_article(...)`  
4. 记录 `strategy_hit.append(step_name)`  

**HTTP 步 `strategy_hit` 标签：**

- 若使用了 `referer_custom` → `"http_referer_custom"`  
- 若 ua custom → `"http_ua_custom"`  
- 否则 `"http_headers"`  

**浏览器步：**

- 调用重构后的 `browser.fetch_for_strategy(url, strategy)`  
- 设置 UA/referer headers（含 referer_custom）  
- route abort：`block_regex` 转 pattern + 通用 paywall host 黑名单  
- unhide JS（加强现有 evaluate）  
- `extract_article_dom`  
- `strategy_hit`: `"browser_cleanup"`  

**Archive 步：**

```python
# archive.is 优先（简单 URL 形式，失败再 org）
# https://archive.is/newest/{url} 或 https://archive.ph/newest/{url}
# 注意：可能 429/captcha → strategy_hit 记 archive_is_failed，failure 时 ARCHIVE_FAILED
# org: https://web.archive.org/web/2/{url} 保持
```

**浏览器何时跳过：** `use_browser=False`；或 `BROWSER_UNAVAILABLE` 记 hit 后跳过。

### 4.5 与旧 API 兼容

`cli._cmd_fetch` 现状：

```python
html, status, dom_result = await fetch_with_retries(...)
```

改为：

```python
result = await fetch_article(...)  # 已是 envelope
# 若仍要落盘 markdown，在 cli 层写文件，path 填进 result
return result
```

旧 `tuple` API 若 tests 依赖，可保留：

```python
async def fetch_with_retries(...) -> tuple[str, int, dict | None]:
    r = await fetch_article(...)
    if r["ok"]:
        return r.get("_html", ""), 200, r.get("_dom")
    return "", r.get("http_status") or 0, None
```

Phase1 内部尽量只走 `fetch_article`。

---

## 5. `browser.py` — 小改（不整包搬 scraper）

### 5.1 现状问题

- 每次 `fetch_with_retries` 内 **start/stop pool**（浪费）  
- 未设 `referer_custom`  
- unhide 较弱  

### 5.2 拟定改动

1. **对外函数**整理为：

```python
@dataclass
class BrowserResult:
    ok: bool
    html: str = ""
    status: int = 0
    engine: str = "playwright"  # or patchright
    dom_result: dict | None = None
    error_code: str = ""
    error_msg: str = ""

async def fetch_for_strategy(url: str, strategy: SiteStrategy, *, pool: BrowserPool | None = None) -> BrowserResult:
    ...
```

2. **引擎选择：**

```python
def _launch_browser(pw):
    # try patchright first if installed
    try:
        from patchright.async_api import async_playwright as ap
        engine = "patchright"
    except ImportError:
        from playwright.async_api import async_playwright as ap
        engine = "playwright"
```

Phase1：依赖仍可只声明 playwright；文档写可选 `pip install patchright`。

3. **headers：** `User-Agent` + `Referer`（含 referer_custom）经 `build_headers(strategy)` 注入 `new_context(extra_http_headers=...)`。

4. **route：** 保留 `_build_route_patterns` + 固定 host 黑名单（与现策略/scraper 列表对齐）。

5. **unhide JS：** 扩展现有片段（display/visibility/maxHeight/overflow + 常见 paywall class）。

6. **池化：** `fetch_article` 在单次 CLI 调用内可 short-lived pool；batch 时 **共享一个 pool**（改 `_cmd_batch` 传入 pool）。Phase1 不做跨进程常驻 daemon。

**明确不移植：** Camoufox / FlareSolverr / 打码（只预留注释或 env 空钩子）。

---

## 6. 新建 `rules/` — 规则同步（大改点 3）

> **以 §S.1.1 / S.1.2 为准。** 下文若仍写「主拉 gitflic sites.js」，视为 draft-1 残留，**作废**。

### 6.1 文件

```text
src/bpc_fetch/rules/
  paths.py    # 缓存目录
  store.py    # 读规则 + manifest
  sync.py     # 拉取 merge 写盘
```

### 6.2 路径

```python
# paths.py
from platformdirs import user_cache_dir
ROOT = Path(user_cache_dir("pac-cli", "pac-cli")) / "rules"
# ROOT / "sites.js"
# ROOT / "sites_updated.json"   # 上次 merge 用的热修原文（可选）
# ROOT / "rules_manifest.json"
# ROOT / "sites_cache.json"     # 解析后的 map 可仍用 sites.get_sites_map 逻辑
```

环境变量：

- `PAC_RULES_DIR` 覆盖根目录  
- `PAC_RULES_PIN` 若设置，sync 跳过网络，只用 pin 路径或已有文件  

### 6.3 上游 URL（可配置）

```python
# 全量 sites.js：可选；默认不依赖（gitflic 今日对 sites.js 返回 404）
SITES_JS_URL = os.environ.get("PAC_SITES_JS_URL", "")  # 空 = 跳过远程全量

# 热修：已验证可拉取
SITES_UPDATED_URL = os.environ.get(
    "PAC_SITES_UPDATED_URL",
    "https://gitflic.ru/project/magnolia1234/bpc_updates/blob/raw?file=sites_updated.json",
)
```

**Phase1 默认路径**：`bundled data/sites.js` + merge `sites_updated.json` → cache + manifest。  
doctor 报告：`rules_source=bundled+updated|bundled_only|remote_full|...`

### 6.4 merge 语义（sites_updated.json）

`sites_updated.json` 为 **按站名/对象的补丁 dict**（上游格式）。  
算法拟定：

1. 读 base `sites.js` 文本 → `parse_sites_js` → `dict[domain, SiteStrategy]`  
2. 读 updated JSON → 对每个 entry 取 domain，覆盖/写入对应 `SiteStrategy` 字段  
3. 写回：可写 **规范化 JSON map**（不必再写 JS），`get_sites_map` 优先读 JSON map  

更简单 Phase1：

- sync 下载最新 `sites.js` 若成功  
- 再下载 `sites_updated.json`，**二次覆盖** parse 结果里的字段  
- 序列化 `asdict` 到 `sites_cache.json`  
- manifest:

```json
{
  "rule_version": "2026-07-26T12:00:00Z#sha256:abcdef",
  "fetched_at": "...",
  "sources": ["sites.js@url", "sites_updated.json@url"],
  "site_count": 900,
  "content_hash": "sha256..."
}
```

### 6.5 CLI

```text
pac rules sync [--compact]
pac rules version [--compact]
pac rules show <domain> [--compact]
```

`get_sites_map()` 改为：

```python
def get_sites_map(...):
    store = load_store()  # 优先 PAC_RULES_DIR
    if store:
        return store.strategies, store.manifest.rule_version
    # fallback bundled data/sites.js
```

---

## 7. `cli.py` — 命令面

### 7.1 删除

从 argparse 与 `_dispatch` **删除**（并删除对应模块文件）：

| 删除命令 | 删除文件（Phase1） |
|----------|-------------------|
| search | `search.py` |
| discover | `discover.py` |
| crawl | `crawl.py` |
| history | `history.py`（incremental 若依赖 history，Phase1 fetch 可去掉 `--incremental` 或改为本地简单 path 存在检查） |

### 7.2 保留 / 修改

| 命令 | 动作 |
|------|------|
| `doctor` | 增加 rules 路径、rule_version、浏览器引擎探测 |
| `sites` | 保留为 `rules` 的别名或改为调用 `rules show` 列表；**倾向保留 `sites --filter` 薄封装** 方便人，内部读 store |
| `fetch` | 走 `fetch_article` envelope；默认 `--no-images` 可考虑默认 true 减负（专家未强制；拟定默认 `--no-images` 对 Agent 更友好，要图再开） |
| `batch` | 上限默认 10，硬顶 25；共享 browser pool；每条完整 envelope |
| `install-browser` | 保留 |
| `rules` | 新增 |

### 7.3 prog 名

```python
parser = argparse.ArgumentParser(prog="pac", description="...")
```

### 7.4 `_cmd_fetch` 改后逻辑（逐步）

```python
async def _cmd_fetch(args) -> dict:
    t0 = time.perf_counter()
    domain = domain_from_url(args.url)
    sites, rule_version = get_sites_map_with_version(args.sites_js)
    strategy = sites.get(domain)

    result = await fetch_article(
        args.url,
        strategy,
        allow_partial=getattr(args, "allow_partial", False),
        rule_version=rule_version,
    )
    result["latency_ms"] = int((time.perf_counter() - t0) * 1000)

    # 可选落盘
    if result.get("ok") and not args.no_save:  # 或始终可只 stdout markdown
        ...
    return result
```

**Agent 友好默认：** stdout 已含 `markdown` 字段，不强制写磁盘；`--out-dir` 时才写。

### 7.5 `_enrich_result`

去掉导向 discover/crawl 的 `next_command`；失败用 `recovery_hint` 字段。

---

## 8. `extract.py` — 小改

1. 抽文后调用 `quality.quality_check`（在 strategy/cli 层调也可，避免双处）。  
2. 扩展 `_clean_paywall_text` 词表与 quality 共用同一常量模块。  
3. 保持 trafilatura + dom_result 优先逻辑。

---

## 9. Eval 与基线（DoD 必备）

### 9.1 `tests/fixtures/eval_urls.yaml`

```yaml
# 40-60 条；示例结构
version: 1
items:
  - url: "https://www.wsj.com/..."
    domain: wsj.com
    tier: hard
    note: "stable paywall sample, published >7d"
  - url: "https://www.economist.com/..."
    domain: economist.com
    tier: hard
  # ... easy controls
```

URL 选取：从 `~/Projects/news-scraper-final/sources.yaml` 的 hard/medium 抽 domain，人工填可公开访问的旧文链接（实施时填真实 URL）。

### 9.2 `scripts/run_eval.py`

```text
python scripts/run_eval.py --out baseline.json
# 每条: url, ok, error_code, content_chars, strategy_hit, rule_version
```

对比：

```text
python scripts/run_eval.py --out after.json --compare baseline.json
```

### 9.3 Phase1 DoD 检查清单（代码完成定义）

| # | 条件 | 如何证明 |
|---|------|----------|
| 1 | rules sync 有 manifest，断网可回退 | 手测 + doctor |
| 2 | WSJ strategy_hit 含 `http_referer_custom` 或等价 | eval / 单测 parse+headers |
| 3 | eval 成功率 ≥ baseline 且无「故意变差」的单站崩盘 | compare 脚本 |
| 4 | teaser-as-ok = 0 | quality 单测 + eval |
| 5 | 干净 venv：`pip install -e .` → `pac doctor` → `pac fetch` JSON | README 步骤 |

---

## 10. NOTICE / 许可

新建 `NOTICE`：

```text
pac-cli is derived from bpc-fetch (MIT)
  https://github.com/Sophomoresty/bpc-fetch
  Copyright (c) Sophomoresty and contributors

Bypass Paywalls Clean site rules (MIT)
  https://gitflic.ru/project/magnolia1234/bypass-paywalls-chrome-clean
```

确认 `.gitignore` 含：`*.cookies.json`、`.env`、`ti_cookies.json`、cache 目录。

---

## 11. 明确 Phase1 不写的代码

| 项 | 原因 |
|----|------|
| `client.py` / 稳定 Python 库 API | 专家：CLI 唯一交付面 |
| 包目录改名 `bpc_fetch` → `pac` | 无收益 |
| MCP | 后置 |
| discover/crawl/search/history | 删 |
| Camoufox/Flare 默认栈 | 后置钩子即可 |
| load-extension | P2 spike |
| glue 接入 scraper | 双轨后置 |
| PyPI 发布流程 | 后置 |
| 完整 ld_json/AMP 执行 | 字段可先存，执行 Phase2 |
| 完整 BPC contentScript | 不做 |

---

## 12. 实施顺序（与专家 D1–D5 对齐，细化到提交点）

| 顺序 | 改动 | 提交信息示例 |
|------|------|----------------|
| 0 | 合并 fork 代码 + docs | `chore: bootstrap from bpc-fetch fork + docs` |
| 1 | eval fixture + run_eval + baseline | `test: add eval harness and baseline` |
| 2 | `result.py` + `quality.py` | `feat: result envelope and quality gate` |
| 3 | `sites.py` referer_custom + bypass_type | `fix(sites): map referer_custom; fix cs_dompurify label` |
| 4 | `build_headers` + `fetch_article` plan | `feat(strategy): plan-based fetch with quality` |
| 5 | `rules/*` + CLI rules | `feat(rules): sync/version/show` |
| 6 | `browser.py` headers/unhide/pool in batch | `fix(browser): headers and cleanup` |
| 7 | `cli.py` 删命令 + pac entry + fetch envelope | `feat(cli): pac entry; slim commands` |
| 8 | pyproject + NOTICE + README | `chore: package as pac-cli` |
| 9 | 全量 eval 对比 + 修回归 | `test: meet phase1 DoD` |

---

## 13. 关键代码对照速查（给专家扫）

| 文件 | 行号（fork 基线） | 现状 | 改后 |
|------|-------------------|------|------|
| `sites.py` | 22-33 | 无 `referer_custom` | 增加字段 |
| `sites.py` | 42-43 | cs_dompurify→archive | → `dom_cleanup` / needs_browser |
| `sites.py` | 81-93 | 不读 referer_custom | `props.get("referer_custom")` |
| `strategy.py` | 18-52 | headers 无 custom referer | custom 优先 |
| `strategy.py` | 79-173 | 固定四段链 | `fetch_article` plan + quality |
| `strategy.py` | 159-166 | 仅 archive.org | + archive.is 条件 |
| `cli.py` | 11-12 | prog bpc-fetch | prog pac |
| `cli.py` | 30-77 | search/discover/crawl/history | 删除 |
| `cli.py` | 299-345 | fetch 弱错误/无 envelope | 统一 result |
| `pyproject.toml` | 18-19 | 仅 bpc-fetch script | + `pac =` |
| 新 `rules/` | — | 无 | sync/store |
| 新 `quality.py` | — | 散落 | 集中 teaser+长度 |

---

## 14. 我（实施方）需要专家拍板的分歧点

请专家对下列 **Yes/No 或改写**（避免开工后返工）：

1. **包名** `name = "pac-cli"` 是否 Phase1 就改，还是保留 `bpc-fetch` 只加 script？  
2. **fetch 默认是否写磁盘**？我拟定 **默认不写**，只 stdout JSON（含 markdown）。  
3. **googlebot fallback** 是否保留在 plan 里？我拟定 **保留** 作为廉价第二步。  
4. **archive.is URL 模板** 是否接受 `https://archive.is/newest/{url}`，失败再 org？  
5. **gitflic 拉不到 sites.js** 时，仅靠 bundled + sites_updated 是否可接受为 sync「部分成功」？  
6. **删除** discover 等文件是否 Phase1 必须，还是先 CLI 不注册即可？（专家上次说删；我按删做。）  
7. **Patchright** 是否列为硬依赖，还是可选？我拟定 **可选**。  
8. **`--allow-partial`** Phase1 是否必须实现？我拟定 **实现，默认关**。  

---

## 15. 专家批复栏（已批复 — 2026-07-29，第三轮）

> 本栏为正式书面批复，自包含，是开工许可证。与本文其他章节冲突时，**以本栏为准**。

**总体：批准按修订版开工（修改后开工）。** draft-2 的方向、scope 切割、§S 自检质量合格；下列 §15.1 的 3 项强制修订并入后即具备开工条件。这些修订是文档级决断，不构成重新设计。

**批准后允许实施方直接编码直至 Phase 1 DoD：是。** 中途不必再问，除非触发 §15.6 红线。

### 15.1 强制修订（并入本文后方可写代码；均属「改文档描述」，非返工）

1. **merge 语义改为「按站名整条替换」**（取代 §S.1.2 与 §6.4 的字段级 upsert）。依据（评审实测 2026-07-29）：`sites_updated.json` 仅 11 条、key 为站名（含 `###_` group 条目）、语义为增量热修正条目替换。算法：按站名整条替换 → 展开 `###` group → 应用 `exception` 覆盖 → 重建 domain→strategy map。必须含单测：base 条目带 `block_regex` + updated 同名条目无该字段 → merge 后字段消失。另记录已知缺口：上游删站不经此通道传播。
2. **质量门验收改为标注夹具双侧指标**（取代「teaser-as-ok=0」表述）。建 `tests/fixtures/quality/`：≥10 完整文 + ≥10 teaser 保存样本；要求 full 误杀=0、teaser 拦截≥90%。长度（MIN_CONTENT_CHARS=100）只作 EXTRACT_FAILED 底线，不单独作 paywall 证据；teaser 判定=词表（限标题+正文前 1200 字符窗口）或结构信号。
3. **stdout 截断上限改为 20k 字符**（取代 §S.1.5 的 100k）。`--full` 放开；batch 默认每条只回摘要（无 markdown 或 ≤2k 字符），完整内容仅 `--out-dir` 落盘。

### 15.2 对 §14 八问的决断

1. pyproject `name`：**Phase 1 即改 `pac-cli`**；保留 `bpc-fetch` script 别名一个版本。
2. fetch 默认：**只 stdout JSON，不写磁盘**；`--out-dir` 时才写。
3. googlebot fallback：**保留**；条件=主策略非 bot UA 时才追加；该步超时 ≤10s。
4. archive.is：接受 `https://archive.is/newest/{url}` 模板；**条件触发**（HTTP+browser 均失败且规则暗示 archive 或 `--archive` 显式开启）；per-domain 冷却；失败 archive.org 兜底。
5. bundled + sites_updated 算 sync 部分成功：**可接受**。语义=`ok:true` + `warnings:["using_bundled_base"]` + manifest 记 stale 状态；前提=`--from-zip` base 刷新路径存在。
6. discover/crawl/search/history：**物理删文件**，并同步删除 fetch/batch 的 `--incremental` flag（依赖 history.is_fetched/mark_fetched，已查证无其他隐藏依赖）。
7. Patchright：**可选依赖**，不硬依赖；文档写明 `pip install patchright` 为可选项。
8. `--allow-partial`：**Phase 1 实现，默认关**。默认关时 teaser 一律 `ok=false`。

### 15.3 Phase 1 DoD（签字版）

**A 层（确定性，单测必过）**

- A1 parse：WSJ `referer_custom` 非空；`cs_dompurify` → `dom_cleanup` 语义；`###` group 展开 + `exception` 覆盖正确
- A2 merge：按站名整条替换（含 15.1.1 指定单测）；manifest 含 version/hash/sources；断网 sync → `ok:true` + stale warning
- A3 headers：`referer_custom` 优先于 referer 枚举
- A4 quality：标注夹具 full 误杀=0 且 teaser 拦截≥90%
- A5 envelope：所有失败路径必带 `error_code` + `failure_class` + `strategy_hit`；20k 截断行为正确
- A6 SSRF：拒绝 localhost/私网 IP/file:// 等；302 跳转目标逐跳重新校验

**B 层（实网尽力，允许 environment_limited 标注）**

- B1 eval（≥40 条，baseline 须在改代码前的 commit 冻结）：总成功率 ≥ baseline − 5pp，且无单站倒退 ≥2 篇
- B2 WSJ：`http_referer_custom` 出现于 strategy_hit（实网或 mock 至少其一，实网优先）
- B3 干净 venv：`pip install -e .` → `pac doctor` → `pac fetch` 输出合法 JSON
- B4 单 URL 总耗时 ≤120s（默认预算，可配）；easy P50 <10s

**明确：实网 WSJ 失败不否决 Phase 1**——只要 A1/A3 单测绿，实网失败可标 `environment_limited`（bot 墙/IP 问题不属于字段修复的验收范围）。

**移出 DoD**：teaser-as-ok=0 绝对表述、hard 站成功率绝对数字、archive.is 成功率、Skills、Camoufox、MCP。

### 15.4 文件白名单与禁止项

**允许修改**：`src/bpc_fetch/{sites.py, strategy.py, cli.py, browser.py, extract.py, __init__.py, __main__.py}`、`pyproject.toml`、`README.md`、`.gitignore`
**允许新增**：`src/bpc_fetch/rules/*`、`src/bpc_fetch/quality.py`、`src/bpc_fetch/result.py`、`tests/*`、`scripts/run_eval.py`、`NOTICE`、`docs/*`
**必须删除**：`src/bpc_fetch/{crawl.py, discover.py, search.py, history.py}` 及 cli 中对应注册与 `--incremental`
**禁止**：新建 `channels/` 目录、`client.py`；实现 jina/bpc_ext/archive_submit/common_crawl/iv_telegram 任何代码；MCP；Skills 定稿；glue 接入；PyPI 发布；包目录改名 `bpc_fetch`→`pac`

### 15.5 TARGET-ARCHITECTURE.md 定位

TARGET 是 **Phase 1.5+ 蓝图，非第一周施工图**。`bpc_ext` spike 最早开始时间=Phase 1 DoD 全绿之后；`jina_reader`/`archive_submit` 属 Phase 1.5 且默认关闭。`cs_code`（BPC 规则中的声明式 DOM 修复 JSON 串，形如 `[{"cond":"div.paywall","rm_attrib":"class|style"},{"hide_elem":"..."}]`，在 base sites.js 与 sites_updated 中均存在）：**Phase 1 不做不违约**，列为 P0.5——P0 全部完成且有富余才可在 browser_cleanup 步实现其解释器。冲突优先级：**Phase 1 以本文（draft-2 + 本批复）为准；Phase 1.5+ 以 TARGET 为准。**

### 15.6 红线（触发即停工再审）

1. eval 总成功率 < baseline − 5pp 且定位不到原因——禁止为凑绿改 URL 集、降 baseline 或放宽质量门
2. 范围蔓延：开始写 channels/、contentScript 完整复刻、ld_json 执行、MCP、常驻 browser daemon、glue 适配
3. 规则源全断（bpc_uploads 也不可达且本地无可用 base zip）——sync 降级为纯本地 pin，停工汇报
4. 质量夹具显示词表误杀完整文——只许调词表/窗口，禁止删样本或改阈值
5. merge 代码合入前缺 15.1.1 的整条替换单测

### 15.7 时间评估

一人真实工作量 **6–8 个工作日**（5 核心 + 2–3 缓冲）。eval URL 收集与质量夹具采样各占约 0.5–1 天，是最易被低估的两块。

**批复人**：外部技术评审（第三轮）
**日期**：2026-07-29

---

## 16. 给用户的说明

- 本文是 **「我打算怎么写代码」** 的详细稿，供专家审。  
- **§15 已批复（2026-07-29）：允许编码直至 Phase 1 DoD；仅触发 §15.6 红线时停工。**  
- 实施顺序：§12 + §15.1–15.4 + 专家 D1–D5（以 §15 为准覆盖草稿分歧）。  

**文档版本**: implementation-draft-2 + §15 批复  
**日期**: 2026-07-29
