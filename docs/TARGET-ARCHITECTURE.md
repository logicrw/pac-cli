# pac-cli 目标架构：通道编排（Channel Orchestration）

**文档版本**: 1.0
**日期**: 2026-07-29
**状态**: Phase 1.5+ 蓝图（方向认可，细节待各 spike 验证）—— 与 `IMPLEMENTATION.md`（Phase 1）是 **叠加关系，不是替代关系**；**本文不是第一周施工图**
**读者**: 实施 agent（人类或 AI）。按本文编码前必须先读 `IMPLEMENTATION.md` §S 与 **§15（专家批复，已落盘）**

> **一句话**：Phase 1 交付的「确定性脊柱」（字段补全 / 质量门 / envelope / rules sync）**照旧执行、不变**；
> 本文定义 Phase 1.5+ 的扩展：fetch 从「写死的降级链」进化为「通道编排器」。
> 冲突优先级：**Phase 1 以 `IMPLEMENTATION.md`（draft-2 + §15 批复）为准；Phase 1.5+ 以本文为准。**
> 硬约束：**Phase 1 DoD（§15.3）未全绿前，除 §4.3 规定的 spike 时机外，禁止开始本文任何实现（含 `channels/` 目录）。**

---

## 0. 设计公理（改动前先读，违反任一即返工）

1. **规则的终态是执行上游产物，不是翻译上游产物。** BPC 的价值是活人周更的 900 站 JS 修复；Python 翻译必失真。能直接跑扩展就不重实现。
2. **墙挡的是我方 IP/指纹，不是「文章」本身。** 第三方服务器代取（archive 提交、reader 代理）是合法一等通道，env-gated、可关。
3. **没有单点。** 任一通道失效只导致降级，不导致失能。新通道必须是可独立关闭的插件。
4. **「什么对这个站有效」让数据说话。** BPC 规则给先验排序，本地统计给后验排序；不许拍脑袋写死第三条以上的固定顺序。
5. **不确定的不承诺。** 任何通道转正前必须过 spike 验收（§4.3 / §5.5），文档里标 `未验证` 的不得写进对外成功率宣传。

---

## 1. 通道抽象（Phase 1.5 核心重构）

### 1.1 Channel 接口（Python Protocol）

```python
# src/bpc_fetch/channels/base.py
from dataclasses import dataclass, field
from typing import Protocol

@dataclass
class ChannelMeta:
    name: str                 # "http_rules" | "browser_rules" | "bpc_ext" | ...
    cost: int                 # 相对成本 1-5（排序用）
    avg_latency_s: float      # 冷启动先验延迟
    needs: list[str] = field(default_factory=list)  # 依赖: "playwright" | "ext_zip" | env var 名
    server_side: bool = False # True = URL 会发给第三方（隐私门控）

@dataclass
class ChannelOutcome:
    ok: bool
    html: str = ""
    markdown: str = ""
    title: str = ""
    status: int = 0
    error_code: str = ""      # 复用 result.py 枚举
    error_msg: str = ""
    extra: dict = field(default_factory=dict)

class Channel(Protocol):
    meta: ChannelMeta
    def available(self) -> bool: ...                      # 依赖是否就绪（浏览器装了？env 开了？）
    async def run(self, url: str, strategy, *, timeout_s: float) -> ChannelOutcome: ...
```

### 1.2 Resolver（替换 build_plan 的内部实现，签名不变）

```python
# src/bpc_fetch/channels/resolver.py
def resolve(domain: str, strategy, stats: StatsStore, opts) -> list[Channel]:
    """返回有序通道列表：
    1. 过滤 available() == False 的
    2. 先验排序：BPC 规则暗示（referer_custom→http 在前；cs_dompurify→browser 在前；archive 暗示→lookup 提前）
    3. 后验调整：stats 里该 domain 成功率高的通道前移（Phase 2 才自动；Phase 1.5 只记录）
    4. server_side 通道仅在 opts.allow_external 时纳入
    """
```

**约束**：`fetch_article(url, strategy, ...)` 的对外签名与返回 envelope **不变**；
`strategy_hit` 记录实际走过的 channel name 序列。

### 1.3 统计存储（Phase 1.5 只写不读，Phase 2 读）

```jsonl
// ~/.cache/pac-cli/stats.jsonl  （PAC_STATS_DIR 可覆盖）
{"ts":"2026-07-29T12:00:00Z","domain":"wsj.com","channel":"bpc_ext","ok":true,"latency_ms":4200,"error_code":""}
{"ts":"...","domain":"ft.com","channel":"http_rules","ok":false,"latency_ms":1800,"error_code":"PAYWALL_REMAINING"}
```

- 只 append，损坏行读取时跳过；
- 文件不可读 → 后验缺失 → 纯先验排序，**功能不减**（红线，必须有单测）。

---

## 2. 通道清单与优先级

| 通道 | 类型 | 阶段 | 状态 | 说明 |
|------|------|------|------|------|
| `http_rules` | 正面 | **P0（已批准）** | 已定 | headers/UA/referer_custom/cookies；见 IMPLEMENTATION.md |
| `browser_rules` | 正面 | **P0（已批准）** | 已定 | Playwright + block_regex + 通用 unhide + cs_code（P0.5） |
| `archive_lookup` | 侧翼 | **P0（已批准）** | 已定 | archive.is/org 查已有快照，条件触发 |
| `bpc_ext` | 正面 | **P1.5** | **未验证，需 spike** | 原生执行 BPC 扩展（§4） |
| `archive_submit` | 侧翼 | **P1.5** | 部分验证 | 主动提交快照（§5.1） |
| `jina_reader` | 侧翼 | **P1.5** | 已验证可用（2026-07-29 实测） | r.jina.ai 代理（§5.2） |
| 学习排序 | 机制 | **P2** | — | stats 后验自动排序（§1.2 第 3 步启用） |
| `common_crawl` | 侧翼 | **P2** | 已验证索引可达 | 老文章（>1 月）快照源 |
| `iv_telegram` | 侧翼 | **P2** | **未验证** | t.me/iv 服务器侧渲染，真付费墙效果未知 |
| 多抽取器投票 | 质量 | **P2** | — | trafilatura + Readability.js 一致性信号（§6） |

---

## 3. 与 Phase 1 的衔接（不许破坏的东西）

1. `IMPLEMENTATION.md` §S.4 的 P0 清单与 §15.3 批复 DoD（A1–A6 / B1–B4）**全部照旧**，先做完。
2. Phase 1 的 `build_plan` 内部实现改为查 resolver，**对外行为在 P0 通道集上必须与批复版 plan 等价**（回归：A 层单测全绿 + eval 对比不劣化）。
3. envelope / error_code / 质量门 / rules sync / CLI 面 —— 零改动。
4. 通道重构与 Phase 1 同仓同分支演进；Phase 1 DoD 未全绿前，**禁止**开始 §4/§5 的通道实现（spike 例外，见 §4.3 时机）。

---

## 4. 通道：`bpc_ext`（原生执行 BPC 扩展）

### 4.1 思路

不翻译 BPC 规则为 Python——在 Playwright persistent context 里 `--load-extension` 加载官方扩展，
让 content script 在它原生环境运行，对修好的 DOM 做 trafilatura 抽取。
规则同步对本通道 = **每周下载 release zip**（bpc_uploads），不需要 merge 算法。

### 4.2 实现规格

```python
# src/bpc_fetch/channels/bpc_ext.py
# - 扩展目录: PAC_BPC_EXT_DIR（默认 ~/.cache/pac-cli/bpc-ext/，由 `pac rules sync-ext` 从 zip 解压更新）
# - 启动: playwright chromium.launch_persistent_context(
#     user_data_dir=..., headless=False,   # 或 headless=new（chromium channel），spike 验证两者
#     args=[f"--disable-extensions-except={ext_dir}", f"--load-extension={ext_dir}"])
# - 每 URL: 新 page → goto → wait_until="domcontentloaded" + 定页 settle（默认 3s，可配）
#   → page.content() → trafilatura → ChannelOutcome
# - 池化: 一个 batch 内复用同一 context；CLI 单次调用 short-lived
# - 超时: 默认 45s/页，可配
```

### 4.3 Spike 验收（转正标准，timebox 1 天）

**时机**：Phase 1 DoD 全绿后立即做；若 P0 阶段 WSJ/FT 实网已稳定通过，可推迟但不取消。

| # | 检查 | 通过标准 |
|---|------|----------|
| S1 | headless（headless=new 或 xvfb headed）下扩展加载、content script 生效 | 对 3 个已知依赖 contentScript 的站（WSJ + 2 个 cs_dompurify 站），DOM 中出现扩展修复痕迹（paywall 元素被移除/正文展开） |
| S2 | 与 `browser_rules` 对照 eval 子集（≥10 条 hard URL） | 成功率 +≥20pp 或救回 ≥2 个 domain |
| S3 | 开销 | 每页额外开销 ≤5s，连续 20 页无 crash/泄漏 |
| S4 | 可关闭 | `PAC_BPC_EXT_DIR` 未设置时 `available()==False`，resolver 自动跳过 |

**S1–S4 全过 → 转正为 hard 站前置通道；任一不过 → 保留代码、默认关闭、记录原因，不阻塞其他工作。**

---

## 5. 通道：服务器侧代取（全部 env-gated，默认关）

**隐私铁律**：`server_side=True` 的通道只在 `--allow-external` 或 `PAC_ALLOW_EXTERNAL=1` 时进入 resolver；
Skill/README 必须声明「开启后 URL 会发送给第三方服务」。

### 5.1 `archive_submit`（主动存档）

- Wayback SPN2：`POST https://web.archive.org/save/<url>`；支持可选 `PAC_ARCHIVE_S3KEY`（免费额度）；
  无 key 时预期 429（2026-07-29 实测）→ 单 domain 冷却 60s，429 后本轮不再试。
- archive.today submit：`GET https://archive.ph/submit/?url=<url>` → 解析返回的快照地址 → 轮询（≤3 次，间隔 10s）。
- 成功 = 拿到快照页且过 QualityGate；失败归 `ARCHIVE_FAILED`。
- **这是新文章（无既有快照）的唯一侧翼通道**，优先级高于 lookup 对 <24h 文章。

### 5.2 `jina_reader`

- `GET https://r.jina.ai/<url>`（2026-07-29 实测：无 key 返回干净 markdown；注意其响应可能带 "cached snapshot" 警告）。
- 可选 `PAC_JINA_API_KEY` 提额。直接以返回的 markdown 进 QualityGate（跳过 trafilatura）。
- 速率受限 → 只做 fallback，禁止主路径。

### 5.3 Phase 2 候选（只登记，不实现）

- `common_crawl`：CDX API 查 → WARC 记录取 HTML（限发布 >30 天文章）；
- `iv_telegram`：`https://t.me/iv?url=...` 未验证，spike 后决定。

---

## 6. Phase 2：多抽取器投票（质量门升级，登记不实施）

- 浏览器通道内：trafilatura（Python）+ Readability.js（页面内执行，Mozilla reader mode）双抽；
- 一致性信号进 QualityGate：两家字数比 >3× 或一家命中 teaser → 降信度；
- 目标：替代「调词表阈值」的脆弱平衡。实施前需在标注夹具上证明优于单词表。

---

## 7. 分阶段 DoD

### Phase 1.5 DoD（在 Phase 1 DoD 之上叠加）

- [ ] Channel Protocol + resolver 落地；P0 三通道以插件形式注册，行为与批复版 plan 等价（A 层单测全绿）
- [ ] stats.jsonl 每次 fetch 落行；文件损坏/不可读时功能不减（单测）
- [ ] `bpc_ext` spike 按 §4.3 执行并记录结论（转正或关闭都有据）
- [ ] `archive_submit` + `jina_reader` 实现，env 关闭时 resolver 中不可见（单测）
- [ ] envelope 新增字段 `channel_path`（=strategy_hit 的通道名序列），无其他对外变更
- [ ] README 增补：外部通道隐私声明 + 各 env 开关表

### Phase 2 DoD（登记）

- [ ] 后验排序启用 + 冷启动回退单测
- [ ] 多抽取器投票在标注夹具上 ≥ 单词表基线
- [ ] `common_crawl` / `iv_telegram` spike 结论

---

## 8. 红线（与 IMPLEMENTATION.md §15.6 批复红线叠加，不替代）

1. **禁止**在 spike 验收前把 `bpc_ext` 或任何 `server_side` 通道设为默认开启。
2. **禁止**为提升 eval 数字把 `server_side` 通道计入 baseline 对比（baseline 与 after 必须同通道集）。
3. **禁止**引入代理池管理、打码、账号/Cookie 共享、分布式队列（超产品边界）。
4. resolver 重构后 P0 通道行为出现任何回归（eval 劣化超批复阈值）→ 停工回滚，不许带病前进。
5. 任何通道「看起来有效」但无 spike/统计证据 → 文档标 `未验证`，不许写进 README 成功率表述。

---

## 9. 上游自动更新机制（细化 IMPLEMENTATION.md §6 的 sync）

### 9.0 上游通道实测事实（2026-07-29，先于设计）

| 信号 | 状态 | 设计结论 |
|------|------|----------|
| `sites_updated.json` raw | ✅ 200；每条带 `upd_version` | 热修全自动的唯一可靠通道 |
| gitflic 文件/提交列表页 | SPA，静态 HTML 无文件数据 | 不做 HTML 抓取 |
| gitflic API | 403（需认证） | 不依赖 |
| release zip 直链 | raw blob 端点整体 404 | 不做 URL 猜测；保留 `--from-zip` 人工通道 |
| X `@Magnolia1234B` 发布通告 | 存在；调用方已有 x_retrieve 能力 | 基线版本 watcher 信号 |

**推论：禁止把任何自动化建立在 gitflic HTML/API 上。** 后续若上游恢复 raw 端点或提供稳定 release URL，经 spike 验证后可升级为全自动。

### 9.1 两层更新模型

**Tier 1 — 热修（全自动，Phase 1 即具备）**

- 调度：systemd timer（VPS，对齐 news-scraper.timer 模式）或 launchd（macOS），每 12–24h 执行 `pac rules sync`。
- 变更检测：`sites_updated.json` content_hash 与本地 manifest 不同 → 重新 merge，`rule_version` 递增。
- 金丝雀校验（必须实现）：sync 成功后自动对 `tests/fixtures/eval_urls.yaml` 中 **canary: true 的 3–5 条钉死 URL** 跑一遍；
  - 全部不劣化 → 新 manifest 生效；
  - 任一劣化 → **自动回滚**到上一份 manifest（本地保留最近 3 份），输出 `rules_rollback` 警告，不中断后续抓取。
- 钉扎：`PAC_RULES_PIN` 设置时一切 sync 短路（评测/复现场景）。

**Tier 2 — 基线大版本（自动发现 + 半自动应用）**

- 信号 A（免费、已在数据里）：sync 时比较 `max(upd_version)` 与本地 base 版本；
  上游发了新 release 后热修会带更高 `upd_version` → 触发 `base_stale` 警告（doctor/rules version 可见 + 可选 webhook/ntfy 告警）。
- 信号 B（发布通告）：X watcher 监听 @Magnolia1234B 的版本帖（复用现有 grok-mcp-gateway），正则提 `v\d+\.\d+\.\d+\.\d+` → 告警。
- 应用：告警后人工浏览器下载 zip → `pac rules sync --from-zip <path>`（已设计）；
  对 `bpc_ext` 通道同一 zip 同时是扩展来源：`pac rules sync-ext --from-zip`。
- 频率预期：周更 → 人工介入约每周 5 分钟；base 滞后一周对命中率的实际影响有限（热修仍在 Tier 1 自动流进来）。

### 9.2 可选增强：GitHub Action 周检（Phase 2 登记）

仓库内 cron workflow：每周拉 `sites_updated.json` + 重 merge → 有 diff 则自动开 PR（含 hash/版本变化摘要）。
效果：bundled 规则随仓库演进，用户 `git pull` / 重装即更新；为将来 pip 分发铺路。不阻塞 Phase 1.5。

### 9.3 验收

- [ ] 单测：content_hash 变化 → merge + version 递增；无变化 → no-op
- [ ] 单测：canary 劣化 → 回滚到上一 manifest 且告警字段正确
- [ ] 单测：`PAC_RULES_PIN` 设置时 sync 短路
- [ ] 单测：`upd_version` 超过 base 版本 → `base_stale` 警告出现
- [ ] 实网尽力：timer 连续运行 7 天无人工干预，manifest 历史完整

---

## 10. 诚实声明（写进 README 的口径）

- 本工具是「规则的忠诚执行者 + 通道的聪明编排者」，不生产规则；上限受制于 BPC 上游与 archive 服务的存续。
- `bpc_ext` / `iv_telegram` 在各自 spike 完成前为 **未验证** 能力。
- `server_side` 通道会将 URL 暴露给第三方，默认关闭，开启即视为知情同意。
