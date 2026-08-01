# pac-cli 架构说明（CLI + Skills）

**项目名（仓库目录）**: [`pac-cli`](../)  
**本地路径**: `~/Projects/pac-cli`  
**GitHub**: https://github.com/logicrw/pac-cli  
**简称**: PAC（仅文档内部缩写）  
**CLI / 包名（拟定）**: 命令与入口以 `pac` 为主（实现阶段以 `pyproject` 为准）  
**文档版本**: **0.4**  
**日期**: 2026-07-29  
**文档状态**: 供外部专家评审 / 指导实施  
**读者**: 外部 AI / 架构专家、以及后续实施 agent  
**文档位置**: 本文件在 **PAC 仓库内** `docs/ARCHITECTURE.md`（已从 `news-scraper-final` 迁出）

---

## ⚠️ 实施前请先审：代码怎么改

> **若你要审「具体改哪些文件、改成什么样的代码」——请先读：**  
> **[`IMPLEMENTATION.md`](./IMPLEMENTATION.md)**（实施说明 draft-1）  
>
> 那是实施方根据 **fork 真实源码行号** + 第二轮专家意见写的 **Phase 1 改码计划**（含改前/改后片段、plan、JSON envelope、DoD、不做什么）。  
> **专家批准 `IMPLEMENTATION.md` 之前，实施方不应合并 fork 业务代码或开工。**  
> 背景与产品目标仍以本文 ARCHITECTURE 为准；**冲突时 Phase 1 以 IMPLEMENTATION 为准。**

---

## 写给评审专家：请先读这一段

### 我们真正要做的事（主目标）

我们要做一个独立的、可安装的 **CLI 工具**（命令：`pac`），并配 **Skills**；需要时再暴露薄 Python API。目标是让多种 **AI Agent 工具**（Claude Code、Cursor、Grok、以及其他支持 Skill/CLI 的环境）可以稳定调用：

> **输入**: 一篇新闻/付费墙文章的 URL（或少量 URL）  
> **输出**: 干净的 Markdown 正文 + 结构化元数据（是否成功、用了什么策略、规则版本、错误码）

这是一个 **可复用的能力组件**，不是「再部署一套新闻爬虫系统」。

### 我们明确不把什么当主目标

| 不是主目标 | 说明 |
|------------|------|
| 只修 VPS 上现有 `news-scraper-final` | 现有系统继续跑；**与本项目独立开发、互不阻塞** |
| 替换/重构整个情报系统 | 发现、入库、Dashboard、聚类仍在 `news-scraper-final` |
| 第一天就 MCP / PyPI 大规模分发 | 可后置；先 CLI/`pac fetch` 能力正确，再 Skill 包装 |
| 完整 1:1 复刻 BPC 浏览器扩展 | 规则与关键字段对齐即可；contentScript 全量复刻不在 v1 |

### 与现有项目的关系（双轨，不冲突）

```
轨道 A（已有，VPS / 本地继续服役）
  ~/Projects/news-scraper-final
    glue.py + browser_fetch.py + PostgreSQL + sources.yaml
    用途：7×24 批处理抓取、入库、看板
    策略：本阶段 **默认不强制大改**；PAC 成熟后「可选」改为调用 PAC

轨道 B（本仓库，本文主角）—— 已建目录
  ~/Projects/pac-cli  →  https://github.com/logicrw/pac-cli
    用途：给 **其他 Agent** 临时/按需取全文（CLI + Skills；可选薄库 API）
    现状：GitHub 仓库当前为 bpc-fetch 的 fork；本地先定架构，改好后推送到该仓库
    策略：**独立开发**；汲取轨道 A 的优点与踩坑，但不绑定其 DB/调度
```

**请专家不要再建议「只在 glue 里打三天补丁就结束」。**  
那对 **仅优化轨道 A** 是合理捷径，但 **无法交付**「独立 CLI + Skills 给多 Agent 用」这一产品目标。

我们接受的专家意见是：

- 诊断（bpc-fetch 的洞）正确；
- 不要过度设计空中楼阁；
- 内核实质是 **规则同步 + 字段补全 + 质量门**（外加可复用的浏览器降级）；
- **包装（品牌/大而全框架）应后置**，但 **独立 CLI 仓库与稳定命令契约本身是交付物，不是浪费**。

### 请专家重点回答什么（有指导性的问题）

请直接给出 **可执行建议**，而不是重复「要不要做 Client」的哲学辩论：

1. **独立仓库的最小模块切分**是否合理？有无应砍/应加？  
2. 从 `news-scraper-final` **应移植哪些代码/思路**，哪些必须重写以免耦合？  
3. bpc-fetch：**vendor 改造 / fork / 只借鉴接口自研**，哪条路径最适合「独立产品」？  
4. 浏览器栈 v1 应多厚？（仅 Playwright vs 直接吸收 Camoufox 链）  
5. **BPC 扩展 load-extension** 作为 hard 站旁路，是否值得做 spike？排期？  
6. Skills 的形态、红线、与 CLI 的契约怎么写才利于多工具分发？  
7. 成功指标如何定（相对基线 vs 绝对阈值）？  
8. 合规表述与默认 batch 上限？

文末 **§12 Open Questions** 有完整列表与期望回复格式。

---

## 目录

1. [产品目标与非目标](#1-产品目标与非目标)  
2. [用户与使用场景](#2-用户与使用场景)  
3. [背景：为什么不直接用 bpc-fetch](#3-背景为什么不直接用-bpc-fetch)  
4. [现有资产盘点（必须汲取的优点）](#4-现有资产盘点必须汲取的优点)  
5. [对 0.1 稿与专家评审的回应](#5-对-01-稿与专家评审的回应)  
6. [目标架构（独立 CLI + Skills）](#6-目标架构独立-cli--skills)  
7. [对外契约：API / CLI / 结果模型](#7-对外契约api--cli--结果模型)  
8. [Skills 设计](#8-skills-设计)  
9. [与 news-scraper-final 的集成策略（可选、后置）](#9-与-news-scraper-final-的集成策略可选后置)  
10. [实施路线图](#10-实施路线图)  
11. [风险、指标、合规](#11-风险指标合规)  
12. [请专家回答的问题](#12-请专家回答的问题)  
13. [附录](#13-附录)

---

## 1. 产品目标与非目标

### 1.1 North Star（最终形态）

交付一个 **独立软件组件**：

| 交付物 | 说明 |
|--------|------|
| **CLI（主交付）** | `pac fetch|rules|doctor`，**stdout=JSON**，便于 Agent 解析 |
| **薄库 API（次要）** | 可选 `import pac`，与 CLI 同一实现 |
| **Skills** | 标准 `SKILL.md`（+ 必要 reference），教各 AI 工具何时调、怎么调、禁止做什么 |
| **规则同步** | 从 BPC 上游更新站点策略，可版本化、可钉扎 |
| **文档** | 安装、示例、合规边界、错误码 |

最终用户体验（Agent 侧）：

```text
用户: 帮我读这篇 WSJ / FT / Economist
Agent: （按 Skill）pac fetch "<url>" --compact
       → 得到 markdown 或明确的 error_code + 恢复建议
```

### 1.2 成功长什么样（产品语言）

1. **任意支持 Skill/CLI 的 Agent**，在装好依赖后，不读我们内部 scraper 代码也能取文。  
2. 对 **策略类付费墙**（UA / referer_custom / 拦脚本等），效果 **不低于** 当前 bpc-fetch，并尽量逼近 BPC 扩展在同类站上的可用性。  
3. 对 **假全文/teaser**，默认 **不算成功**（或明确 `paywall_suspected`，由调用方决定是否采用）。  
4. 规则可更新：不是永远卡在某次静态 `sites.js`。  
5. 与 VPS 爬虫 **可并行存在**；不要求先停机改造生产抓取。

### 1.3 非目标（v1 明确不做）

- 新闻发现（RSS / Google News / X 监控）—— 属于 `news-scraper-final`  
- 持久化数据库、去重调度、Dashboard  
- 无限制全网 crawl / 关键词跨站扫站（可提供有上限的 `batch`，默认很小）  
- MCP-first（需要时再薄封装同一 Client）  
- 完整移植 BPC `contentScript.js` 全部站点脚本  
- 对 BPC 列出的 900+ 站承诺同等 SLA  
- 公网 SaaS 化「付费墙破解 API」

### 1.4 设计原则（精简版）

1. **独立可安装**：不依赖 news-scraper 的 Postgres / `sources.yaml` / systemd。  
2. **能力在 CLI/库内核，Skill 只教用法**：禁止在 Skill 里复制绕过逻辑。  
3. **规则与执行分离**：BPC（及 override）是规则源；执行引擎我们控制。  
4. **质量门驱动成功判定**：HTTP 200 ≠ ok。  
5. **先做对再做厚**：v1 先 HTTP 策略 + 基础浏览器 + 质量门 + 规则同步；更强反 bot 栈可增量。  
6. **汲取现有项目，禁止重复造已经验证过的轮子**（见 §4）。  
7. **可观测**：`strategy_hit`、`rule_version`、`error_code`、`latency_ms` 必须返回。
8. **维护自动化保持无状态**：中央 GitHub Actions 每日检查并通过 PR 更新镜像；客户端只在首次使用且 TTL 过期时 lazy sync。禁止为此引入 daemon、数据库或绑定单一操作系统的调度器。

---

## 2. 用户与使用场景

### 2.1 主用户：AI Agent（通过 Skills）

| 场景 | 行为 | 要求 |
|------|------|------|
| 单篇精读 | 用户丢 URL → Agent fetch → 总结/翻译 | 低延迟、正文在 JSON 里或可截断 |
| 少量对比 | 2–10 个 URL batch | 硬上限，防误扫 |
| 排障 | doctor / rules show | 明确缺浏览器、缺规则、网络问题 |
| 研究工作流 | 与其他 Skill（搜索、笔记）编排 | 稳定 error_code，便于分支 |

### 2.2 次用户：人类开发者 / 脚本

- 在终端调试策略  
- 在评测脚本里跑 URL 金标集  
- （可选，后置）被 `news-scraper-final` 的 glue 当库调用  

### 2.3 非用户（v1 不服务）

- 「帮我监控 50 个媒体全量更新」→ 请用 news-scraper  
- 「帮我绕过登录墙去撞库」→ 拒绝；Skill 写明红线  

---

## 3. 背景：为什么不直接用 bpc-fetch

### 3.1 bpc-fetch 有价值的部分（要保留的思路）

- Agent 友好：JSON、`--compact`、子命令清晰  
- 有 discover/batch 等想法（我们 **可选择性吸收**，discover 不作为 v1 主路径）  
- 已解析 BPC `sites.js` 的一部分字段  

### 3.2 已核实的结构性缺陷（0.1 诊断 + 专家本地源码复核，均属实）

| 问题 | 影响 |
|------|------|
| `referer_custom` 未进入 `SiteStrategy` | **WSJ** 等依赖 Drudge referer 的站策略被静默丢掉 |
| `cs_dompurify` 被映射成 `archive` 类型 | 语义错误，降级路径跑偏 |
| 无 `ld_json` / AMP 等字段执行 | 跟不上 BPC 近年大量 fix |
| 回退主要是 archive.org，弱于 archive.is/today | Nature / 部分站表现差 |
| 无 `sites_updated.json` 热更 | 与 BPC 周更脱节 |
| 全局固定链 primary→googlebot→browser→archive.org | 不是站点特异 Plan |
| 浏览器能力弱于我们自研的 `browser_fetch.py` | hard/bot 站吃亏 |

因此：**不能把 bpc-fetch 当最终黑盒。**  
但它可以作为 **代码素材 / 反面教材 / 基线对照组**。

### 3.3 BPC 原版在解决什么

- 周更的 **站点规则与补丁**（changelog 以 Fix/Add/Remove 为主）  
- 浏览器内 **content script** 级修复（时序、CSS、ld_json…）  
- 可选 filters + userscript 旁路  

我们的 Client 要对齐的是 **规则新鲜度 + 关键字段语义 + 可批跑/可被 Agent 调用的执行环境**，  
不是把扩展 UI 搬进 Python。

---

## 4. 现有资产盘点（必须汲取的优点）

> 0.1 稿的主要失误：把 `news-scraper-final` 里 **已经落地** 的能力写成了「设想」。  
> 独立开发时，应 **移植思路与精简实现**，而不是再设计一个更弱的 BrowserFetcher。

### 4.1 仓库位置与职责

| 资产 | 路径（轨道 A：`~/Projects/news-scraper-final/`） | 对 PAC 的价值 |
|------|-----------------------------------------------------|---------------|
| 浏览器降级栈 | `browser_fetch.py` | **高**：Camoufox→Patchright→Playwright→FlareSolverr→打码钩子；paywall 脚本 host 拦截 |
| 抓取编排 | `glue.py` 的 `fetch_http` / `fetch_browser_upgrade` / `fetch_and_extract` | **高**：分层（easy HTTP 优先 / hard 浏览器优先）、统一结果结构 |
| 质量启发式 | `detect_paywall_suspected` + `MIN_CONTENT_LEN` | **高**：应上升为 Client 内建 QualityGate |
| Cookie | `ti_cookies.json` + cookies 参数 | **中**：Client 支持 `cookie_profile` 路径即可 |
| 探针 | `probe_sites.py` | **中**：可改编为 PAC 的 eval/基线工具 |
| 源列表 | `sources.yaml` | **低直接依赖**：PAC 不绑定 allowlist；可用作 **评测 URL 池** |
| 产品设计 | `DESIGN.md` | 背景；PAC 不实现其中的调度/DB |

### 4.2 glue 已有分层逻辑（应抽象进 Client，而不是丢掉）

当前（简化）：

```text
hard / paywall:
  先 browser_fetch 栈 → 失败再 bpc HTTP 策略链

easy / medium:
  先 bpc HTTP → 特定 error 再升级浏览器
```

PAC v1 建议的通用形态：

```text
resolve(domain) → 站点 Plan
  → HTTP 策略步骤（headers / cookie / …）
  → QualityGate
  → 浏览器步骤（可配置厚度）
  → QualityGate
  → Archive（is/today 优先，org 备选）
  → Extract → 最终 QualityGate → ArticleResult
```

具体是否「hard 先浏览器」可由 `FetchOptions.tier` 或启发式决定；**接口层要暴露 strategy_hit**。

### 4.3 明确不要搬进 PAC 的东西

- PostgreSQL schema、入库 SQL、健康检查与 cron  
- `sources.yaml` 作为硬编码扫描范围（Agent 场景是 **用户给 URL**）  
- grok-mcp-gateway / Dashboard / 导出 agent_news  
- 与 VPS 路径耦合的环境假设  

---

## 5. 对 0.1 稿与专家评审的回应

### 5.1 专家结论摘要（我们同意的部分）

1. **问题诊断准确**（字段丢失、无热更、固定降级链）。  
2. **实质工作量**主要是：规则同步 + 字段补全 + 质量门（外加用好浏览器栈）。  
3. **不要**为了修洞而发明过大的框架空壳。  
4. hard 站失败分两类：策略类 vs bot 类；规则同步 **只解决前者**。  
5. 指标宜 **相对基线**，不宜拍脑袋 hard≥60%。  
6. discover 不必塞进获取 Client。  
7. **BPC 扩展 load-extension** 作为 hard 站 **spike / 旁路** 值得认真考虑（0.1 否得过早）。  

### 5.2 我们 **不接受** 的结论（请专家按「独立产品」重评）

| 专家建议（在「只修 scraper」语境下） | 我们的产品立场 |
|--------------------------------------|----------------|
| 不必新建 Client，glue 原地改完即结束 | **否**。主交付是 **独立 CLI + Skills**（`pac-cli` 仓库） |
| Skills/CLI 包装可有可无 | **否**。Skills 是多 Agent 分发的一等交付物（可 **排期后置**，但不是取消） |
| 独立仓库 = 过度设计 | **否**。独立仓库是 **解耦与分发** 的需要，与「大框架」不是一回事 |
| 版本化 = 用 scraper 仓库 git commit 钉 sites.js | 对 scraper 够用；对 **多 Agent 安装的库**，需要 **自带规则缓存与 version 字段** |

### 5.3 0.1 → 0.2 修正清单

| 0.1 问题 | 0.2 修正 |
|----------|----------|
| 主叙事像「重建 scraper 获取层」 | 主叙事改为 **独立 Agent 向 Client** |
| 低估 browser_fetch / glue | §4 完整盘点，要求汲取 |
| Phase 混入包名/API 冻结仪式 | 路线图改为：先能力，后 Skill，再可选接入 scraper |
| 默认否决扩展加载 | 改为 hard 旁路 spike 候选项 |
| 绝对成功率指标 | 改为相对基线 + 抽检上限 |
| 与 VPS 强绑定替换 | 双轨：默认不阻塞生产；可选后置接入 |

---

## 6. 目标架构（独立 CLI + Skills）

### 6.1 仓库形态（轨道 B）—— 已创建

**仓库目录已建立**：`~/Projects/pac-cli`  
与 `~/Projects/news-scraper-final` **平级、独立**，不嵌套在 scraper 内。

当前状态：架构文档已就位；代码脚手架尚未实现（见 Phase 0/1）。

目标树：

```text
~/Projects/pac-cli/          # 本仓库（已创建）
  README.md
  docs/
    ARCHITECTURE.md                         # 本文（专家评审主材料）
  pyproject.toml                            # 待建
  LICENSE                                   # 待建
  src/pac/                                  # 待建；包名 pac
    __init__.py
    client.py                               # 可选薄库 API（与 CLI 同内核）
    cli.py                                  # 主入口：pac 命令
    rules/
      sync.py                               # 拉 BPC sites.js + sites_updated
      store.py                              # 本地缓存 + manifest（hash/version）
      model.py                              # NormalizedSiteRule
      overrides.example.yaml
    fetch/
      http.py                               # headers / referer_custom / cookies
      browser.py                            # 自 news-scraper browser_fetch 精简移植
      archive.py                            # archive.is/today/org
      plan.py                               # domain → steps
      quality.py                            # 长度 + teaser
      extract.py                            # trafilatura / DOM → markdown
    types.py                                # ArticleResult, error codes
  skills/pac-cli/                   # 待建
    SKILL.md
    references/error-codes.md
  tests/
  scripts/eval_urls.yaml                    # 金标/回归 URL（可从 scraper sources 抽样）
```

安装：

```bash
pip install -e .
playwright install chromium   # 及文档说明的可选引擎
pac doctor
pac rules sync
pac fetch "https://..." --compact
```

### 6.2 逻辑架构

```text
                    ┌──────────────────────┐
                    │ BPC 上游（周更规则）   │
                    │ sites.js             │
                    │ sites_updated.json   │
                    └──────────┬───────────┘
                               │ rules sync
                    ┌──────────▼───────────┐
                    │ Rule Store (本地)     │
                    │ + overrides          │
                    │ rules_version        │
                    └──────────┬───────────┘
                               │
┌──────────────┐    ┌──────────▼───────────┐    ┌─────────────────┐
│ PacClient /  │───▶│ Plan + Fetch Engine  │───▶│ ArticleResult   │
│ CLI          │    │ HTTP / Browser /     │    │ JSON to Agent   │
└──────┬───────┘    │ Archive + Quality    │    └─────────────────┘
       │            └──────────────────────┘
       │
┌──────▼───────┐
│ Skills       │  只描述何时调用 CLI/API、红线、示例
│ (多 AI 工具)  │
└──────────────┘
```

### 6.3 内核三件套（专家强调的「真工作」，放在独立包内）

| 模块 | 最小行为 | 完成定义 |
|------|----------|----------|
| **规则同步** | 拉取并 merge 上游规则；写 manifest（时间、hash、来源） | `pac rules version` 可查；失败保留旧版 |
| **字段补全与执行** | 至少：`useragent(_custom)`、`referer`、**`referer_custom`**、`block_regex`（尽力）、archive.is；修正 `cs_dompurify` 语义（≠ 盲目 archive） | WSJ 的 plan/strategy_hit 出现 referer_custom |
| **质量门** | 最短正文、teaser 文案、可选段落结构 | `ok=false` + `PAYWALL_REMAINING` 或 `paywall_suspected` 策略可配置 |

### 6.4 浏览器层策略（汲取轨道 A，分阶段变厚）

| 阶段 | 能力 |
|------|------|
| v1 默认 | Playwright（或 Patchright 若易装）+ 通用 paywall host 拦截 + 基础 unhide |
| v1 可选 | 环境变量打开 Camoufox / 代理 / FlareSolverr（API 对齐 `browser_fetch`） |
| v1 spike | 对补字段后仍失败的 hard 站：Playwright **load BPC 扩展** 对照成功率 |
| v2+ | 池化 Browser、更稳的并发、评测驱动加厚 |

**原则**：PAC 的 `browser.py` 从 `browser_fetch.py` **移植并去耦合**，不要重新设计一套更弱的。

### 6.5 关于「是否基于 bpc-fetch 源码」

**工程现状（已定）**：GitHub 仓库 https://github.com/logicrw/pac-cli **已经是** [Sophomoresty/bpc-fetch](https://github.com/Sophomoresty/bpc-fetch) 的 fork。  
因此默认实现路径是 **在 fork 上演进**（修规则同步、字段、质量门、CLI/Skills 叙事），而不是另起一个与 fork 无关的空壳仓再 vendor 一份。

仍请专家评估取舍：

| 路径 | 做法 | 现状 |
|------|------|------|
| **B. 在 logicrw/pac-cli（bpc-fetch fork）上改** | 修 `sites`/`strategy`、加 sync、质量门、对齐 CLI+Skills | **默认路径** |
| C. 大改/重写内部模块，仅保留 fork 历史 | 边界更干净，diff 大 | 若 fork 包袱过重可评估 |
| A. 继续 pip 依赖上游 bpc-fetch | — | **不推荐**（与 fork 策略矛盾） |

**对外主入口是 CLI `pac`**；库 API 可选、与 CLI 同内核。

---

## 7. 对外契约：API / CLI / 结果模型

> 目标是让 Agent **零内部知识** 也能用。字段可微调，但语义要稳定。

### 7.1 Python API（示意）

```python
class PacClient:  # 或模块级 API；对外主入口是 CLI `pac`
    async def doctor(self) -> dict: ...
    async def rules_sync(self) -> dict: ...
    async def rules_show(self, domain: str) -> dict: ...

    async def fetch(
        self,
        url: str,
        *,
        cookie_profile: str | None = None,
        include_images: bool = False,
        timeout_s: float | None = None,
        tier: str | None = None,  # easy|medium|hard|auto
    ) -> ArticleResult: ...

    async def batch(
        self,
        urls: list[str],
        *,
        max_urls: int = 10,
        concurrency: int = 2,
        **kwargs,
    ) -> BatchResult: ...
```

### 7.2 CLI

```text
pac doctor [--compact]
pac rules sync|version|show <domain>
pac fetch <url> [--cookie-profile PATH] [--tier auto|easy|medium|hard] [--compact]
pac batch --file urls.txt [--max 10] [--compact]
```

约定：

- **stdout**: 仅 JSON  
- **stderr**: 日志/进度  
- 失败：exit code ≠ 0，且 JSON 含 `ok: false`

### 7.3 ArticleResult（成功/失败统一 envelope）

```json
{
  "ok": true,
  "url": "https://www.wsj.com/...",
  "domain": "wsj.com",
  "title": "...",
  "markdown": "...",
  "content_chars": 4200,
  "paywall_suspected": false,
  "strategy_hit": ["http_referer_custom", "quality_pass"],
  "rule_version": "bpc-2026-07-26#a1b2c3d",
  "engine": "http",
  "latency_ms": 1800,
  "warnings": []
}
```

失败时增加：

```json
{
  "ok": false,
  "error_code": "PAYWALL_REMAINING",
  "error": "quality gate failed after browser",
  "recovery_hint": "optional: cookie_profile for subscription sites; or try later",
  "strategy_hit": ["http_headers", "browser", "archive_is"]
}
```

### 7.4 建议 error_code（供 Skill 分支）

| code | 含义 | Agent 可怎么做 |
|------|------|----------------|
| `RULE_MISSING` | 无规则 | 告知用户；可选裸浏览器碰运气（若我们实现 fallback） |
| `NO_STRATEGY` | 明确需订阅/Cookie | 提示 cookie_profile |
| `NETWORK` | 网络/超时 | 重试 |
| `HTTP_BLOCKED` / `BOT_CHALLENGE` | 403/挑战 | 换 tier/代理/引擎（若配置） |
| `PAYWALL_REMAINING` | 抓到但像 teaser | 不要把 markdown 当全文 |
| `EXTRACT_FAILED` | 有 HTML 无正文 | 记录 |
| `BROWSER_UNAVAILABLE` | 未装浏览器 | 跑 doctor / install |
| `ARCHIVE_FAILED` | 归档失败 | — |
| `LIMIT_EXCEEDED` | batch 超限 | 拆分请求 |
| `INTERNAL` | 未知 | 报错给用户 |

### 7.5 v1 不做的命令

- `crawl`（关键词跨站）—— 滥用面大  
- `search`（依赖外部搜索 Key）—— 交给别的工具  
- `discover`—— 留给 scraper；Agent 场景通常已有 URL  

---

## 8. Skills 设计

### 8.1 在产品中的位置

```text
Client 正确 ──▶ CLI 稳定 ──▶ 再写 Skill ──▶ 拷贝到各 AI 工具 skills 目录
```

Skills **不是**可有可无的文档附录，而是 **多 Agent 分发面**；  
但 **不应** 在 fetch 还不能用时堆砌 Skill 工程。

### 8.2 建议包内容

```text
skills/pac-cli/
  SKILL.md                      # 触发条件、流程、红线、示例
  references/error-codes.md     # 与 Client 同步
  references/compliance.md      # 用途限制
```

### 8.3 SKILL.md 必须写清的内容（评审可增删）

1. **名称与触发词**：付费墙、全文、WSJ/FT 读不了、fetch article…  
2. **前置**：已 `pip install`、已 `pac doctor` 通过  
3. **标准流程**：rules show（可选）→ fetch → 根据 error_code 分支  
4. **红线**：  
   - 禁止大批量无上限 crawl  
   - 禁止打印 Cookie/代理密钥  
   - 禁止把 teaser 当成全文交作业  
   - 用途：个人研究 / 本地 agent / 用户自有订阅增强  
5. **batch 默认上限**（建议 10）  
6. **与新闻爬虫系统的分工一句话**：持续监控用 scraper；单篇/少量用本 Skill  

### 8.4 多工具分发

维护 **一份权威 SKILL 源**（在 PAC 仓库内），复制到：

- Claude / agents skills 目录  
- Grok skills 目录  
- 其他工具各自约定路径  

MCP：仅当出现「只认 MCP、不认 Skill/CLI」的客户端时，再做 **薄适配**（调用同一 Client）。

---

## 9. 与 news-scraper-final 的集成策略（可选、后置）

| 阶段 | 动作 |
|------|------|
| 现在 | **零强制依赖**。VPS 继续现有 glue + bpc + browser_fetch |
| PAC v1 达标后 | 可选：glue 的 `fetch_and_extract` 改为调 PacClient（减少双栈） |
| 并行期 | 允许两套短暂共存；用同一 eval URL 集对比 |
| 永远可选 | 即使用户从不改 scraper，PAC 仍单独对 Agent 有价值 |

**集成不是成功标准；Agent 可调用才是。**

---

## 10. 实施路线图

### Phase 0 — 对齐与基线（0.5–1 天）

- [ ] 冻结仓库名、包名、CLI 名  
- [ ] 准备评测 URL 集（含 easy + hard，至少覆盖 WSJ / Economist / 1–2 个 easy）  
- [ ] 跑 **bpc-fetch 裸调基线** 与（可选）当前 glue 路径基线，记表  

### Phase 1 — 独立仓库 MVP（核心，约数天）

- [ ] 脚手架：`pyproject`、CLI、JSON 输出  
- [ ] **规则同步** + 本地 Rule Store + `rules_version`  
- [ ] **字段**：至少 `referer_custom` 等 P0 在 HTTP 路径生效；修正 dompurify/archive 语义  
- [ ] **质量门** + extract → ArticleResult  
- [ ] **浏览器**：从 `browser_fetch` 精简移植，默认 Playwright  
- [ ] `doctor`  
- [ ] 对照基线：策略类 hard 站应可见改善（尤其 WSJ referer）  

**本阶段完成定义（DoD）**：  
未装 news-scraper 的机器上，仅装 PAC，也能 `pac fetch` 出可判定的 JSON。

### Phase 2 — Agent 分发面

- [ ] `SKILL.md` + error-codes 文档  
- [ ] README 安装与合规  
- [ ] 在至少 **两种** AI 工具里实机走通「用户给 URL → 全文」  
- [ ] batch 上限与红线  

### Phase 3 — 加固与旁路

- [ ] archive.is/today  
- [ ] cookie_profile  
- [ ] （建议）BPC **load-extension** spike：仅对 P0 后仍失败的 hard 站  
- [ ] 可选 Camoufox/代理钩子与文档  
- [ ] eval 回归脚本  

### Phase 4 — 可选

- [ ] news-scraper glue 切换到 PAC  
- [ ] 薄 MCP  
- [ ] ld_json / AMP 等更高保真字段  

---

## 11. 风险、指标、合规

### 11.1 风险

| 风险 | 说明 | 缓解 |
|------|------|------|
| 规则有了但 contentScript 语义没有 | hard 成功率不涨 | 扩展 load spike；站级 override；质量门诚实失败 |
| bot 墙 | 与规则无关 | 浏览器栈 + 代理；不在 Skill 里假装能过 |
| 双轨维护成本 | scraper 与 PAC 两套 fetch | 达标后可选合并调用 |
| 合规 | 付费墙工具敏感 | Skill/README 边界；非 SaaS；Cookie 仅用户自有 |
| 过度设计复发 | 又做成大框架 | 以 Phase 1 DoD 约束范围 |

### 11.2 指标（请专家可改数字，但请保留「相对」思想）

| 指标 | 建议 |
|------|------|
| 相对 bpc-fetch 基线 | 同 URL 集成功率 **≥ 基线**，策略类站 **明显改善** |
| 质量 | teaser 误判为 ok 的比例可控；`PAYWALL_REMAINING` 可观测 |
| 规则 | sync 延迟目标 ≤ 7 天；失败保留旧版 |
| Agent | 按 Skill 首次集成 &lt; 30 分钟（有文档前提下） |
| 绝对 hard% | **先基线后定**，不在开干前写死 60% |

### 11.3 合规（必须进入 README + Skill）

- 个人研究、本地 agent、用户自有订阅会话增强  
- 不提供匿名公网破解服务  
- 不鼓励违反站点 ToS 的规模化抓取  
- 密钥与 Cookie 永不进入日志与模型上下文示例  

---

## 12. 请专家回答的问题

请按下面编号 **逐条给建议**（同意 / 修正方案 / 反对理由）。  
我们需要的是 **指导下一步怎么做**，不是再诊断一遍 bpc-fetch。

### A. 产品与边界

1. 在「独立 CLI + Skills 给多 Agent」前提下，§6 仓库切分是否合适？应砍掉或合并哪些目录？  
2. v1 是否应 **完全去掉** discover/crawl/search？有无 Agent 场景必须保留 discover？  
3. Skills 与 CLI 是否足够成为多工具标准接入？何种情况下必须上 MCP？  

### B. 实现路径

4. bpc 内核选 **vendor/fork（B）** 还是 **自研规则+fetch（C）**？为什么？  
5. 从 `browser_fetch.py` 移植时，v1 默认引擎最小集建议是什么？  
6. Rule Store：文件系统缓存是否够用？要不要 sqlite？  
7. `cs_dompurify` 在无完整 contentScript 时，执行层最小合理解释是什么？  

### C. Hard 站与保真度

8. **load-extension** spike 的优先级、环境约束（headless）、验收标准？  
9. 策略类 vs bot 类失败，在 API 上如何强制区分才有利 Agent？  
10. archive.is 优先是否有稳定性/合规顾虑？  

### D. 质量与指标

11. QualityGate：失败应 `ok=false`，还是 `ok=true` + `paywall_suspected`？默认策略？  
12. 基线 URL 集规模与更新频率建议？  
13. 哪些指标进入 v1 DoD 硬门槛？  

### E. 与 scraper 双轨

14. 是否同意「生产 scraper 暂不改造」？若不同意，最小并行策略是什么？  
15. 未来 glue 接入 PAC 时，最大兼容风险是什么？  

### F. 安全与分发

16. Skill 红线还需加什么？  
17. batch 默认上限建议？  
18. 开源 license 与「BPC 规则文件」再分发注意点？  

---

## 13. 附录

### A. 关联路径与上游

| 资源 | 位置 |
|------|------|
| **本项目 pac-cli** | 本地 `~/Projects/pac-cli`；远程 https://github.com/logicrw/pac-cli |
| 架构文档 | `docs/ARCHITECTURE.md`（本文件） |
| GitHub 现状 | `logicrw/pac-cli` 当前为 [Sophomoresty/bpc-fetch](https://github.com/Sophomoresty/bpc-fetch) 的 **fork**；本架构落地后推送到该仓库演进 |
| 情报系统（参考实现 / 双轨 A） | `~/Projects/news-scraper-final`（**兄弟目录，非 monorepo 子包**） |
| 系统设计 | `~/Projects/news-scraper-final/DESIGN.md` |
| 浏览器栈（移植源） | `~/Projects/news-scraper-final/browser_fetch.py` |
| 胶水编排（移植源） | `~/Projects/news-scraper-final/glue.py` |
| 源列表（评测可参考） | `~/Projects/news-scraper-final/sources.yaml` |
| bpc-fetch | https://github.com/Sophomoresty/bpc-fetch |
| BPC 扩展 | https://gitflic.ru/project/magnolia1234/bypass-paywalls-chrome-clean |
| BPC 热更 | https://gitflic.ru/project/magnolia1234/bpc_updates |

### B. 修订历史

| 版本 | 日期 | 说明 |
|------|------|------|
| 0.1 | 2026-07-29 | 初稿；偏「重建获取层」叙事；曾放在 news-scraper-final/docs |
| 0.2 | 2026-07-29 | 澄清主目标=独立 CLI+Skills；双轨；吸收专家三件套 |
| 0.3 | 2026-07-29 | 临时目录名 paywall-article-client；文档迁出 scraper |
| **0.4** | **2026-07-29** | **正式命名 `pac-cli`；GitHub https://github.com/logicrw/pac-cli（bpc-fetch fork）；叙事以 CLI+Skills 为主** |

### C. 给专家的回复模板

```text
总体结论: [ 方向正确可开干 / 需改边界后再开干 / 不建议独立仓 ]
对主目标（独立 CLI+Skills）的理解确认: [ 是 / 否，我认为你们实际需要的是… ]

A 产品边界 (1-3):
B 实现路径 (4-7):
C Hard 站 (8-10):
D 质量指标 (11-13):
E 双轨 (14-15):
F 安全分发 (16-18):

建议的下周具体任务列表（按天）:
建议立刻砍掉的范围:
建议立刻补上的范围:
```

### D. 一句话备忘（给任何后续 agent）

> **仓库：`pac-cli`（https://github.com/logicrw/pac-cli）。做独立的、给多 Agent 用的 fetch CLI + Skills；规则同步与字段保真是内核；浏览器与质量门抄 `news-scraper-final` 的实战经验；VPS 爬虫先别绑死改造；Skill 在 fetch 可用之后写。**

---

**文档结束（v0.4）**
