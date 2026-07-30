# 给 Grok 的任务简报（pac-cli Phase 1 收尾）

**日期**: 2026-07-30
**来自**: 技术负责人（Kimi K3，已核实仓库现状）
**你的角色**: 实施方。负责执行，不负责架构决断。完成后把证据交回来，我逐项验收。
**最高优先级文档**: `docs/IMPLEMENTATION.md` §15（开工许可证 + DoD + 红线）。与本简报冲突时以 §15 为准。

---

## 0. 开工前先读

1. `docs/HANDOFF.md`（交接说明，含环境陷阱）
2. `docs/IMPLEMENTATION.md` **§15 全文**（§15.1–15.7 逐条读）
3. `docs/TARGET-ARCHITECTURE.md` 只读 §0 和 §3 —— Phase 1.5+ 蓝图，**现在不许做**

## 1. 已核实的现状（不要重复验证，直接用）

- `7aa498b` 已提交评审侧修复；工作区干净（仅 `docs/HANDOFF.md` 未跟踪）
- `pytest tests/ -q` → **25 passed**（2026-07-30 实测）
- A 层 DoD 已过关；**B 层（B1–B4）一项未验**
- 三个缺口：
  1. `scripts/run_eval.py` **不存在**（§15.4 白名单允许新增）
  2. `tests/fixtures/eval_urls.yaml` 只有 1 条 example.com 占位
  3. `tests/fixtures/quality/{full,teaser}/` 各 10 个文件全是**同一段合成句子的拷贝**，证据强度为零

## 2. 任务顺序（严格按此执行，每步完成后小步提交）

### Step 0 — 提交 HANDOFF.md（5 分钟）
`git add docs/HANDOFF.md docs/GROK-BRIEF.md && git commit -m "docs: handoff + grok brief"`。交接文档不能留在 untracked。

### Step 1 — 写 `scripts/run_eval.py`（0.5 天，阻塞项，最先做）
用途：T3 baseline 对比的载体。要求：
- 读 `tests/fixtures/eval_urls.yaml`（`items[]` 含 url/domain/tier/note/canary）
- 逐条调用 `pac fetch <url>`（**子进程方式**，测真实 CLI 行为和 exit code），记录：ok、error_code、failure_class、strategy_hit、耗时、内容长度
- 并发 ≤2，per-domain 间隔 ≥2s（别打爆目标站，也别触发本机代理限流）
- 输出 JSON：`--out <path>`，含每条结果 + 按 domain 聚合 + 总成功率
- `--compare <baseline.json>`：输出总成功率差值（pp）和单站倒退清单（某站成功数下降 ≥2 即列出）
- 支持 `--urls <path>` 覆盖默认 URL 集、 `--timeout`（默认 150s/条，覆盖 B4 的 120s 预算+余量）
- 注意：**这个脚本必须能在 baseline commit `76e24f5` 的 worktree 里也能跑**。该 commit 的 CLI 叫 `bpc-fetch`（或无 pac entry point）——用 `--cmd` 参数指定可执行命令，默认 `pac fetch`。
- 跑一次自检：`python scripts/run_eval.py --urls tests/fixtures/eval_urls.yaml --out /tmp/smoke.json`（当前 1 条 URL 也要能跑通）

### Step 2 — 填充 eval URL 集（0.5–1 天，最易低估）
- 源：`~/Projects/news-scraper-final/sources.yaml` 的 hard/medium 档，抽 10–15 个 domain，每域 3–5 篇
- **只选发布 >7 天的旧文**（付费墙状态和 archive 快照才稳定），加 3 个 easy 对照域，总数 **≥40 条**
- 标 3–5 条 `canary: true`
- 每条先手动 `curl -sI` 或浏览器确认 URL 还活着（200 或已知付费墙 403 均可，死链不要）
- 填进 `tests/fixtures/eval_urls.yaml`，删掉占位

### Step 3 — 换真实质量夹具（0.5 天，隐性风险最大，提到前面做）
当前 A4 的绿是假绿（10 份相同合成文本）。替换：
- `full/`：从 news-scraper 的 PostgreSQL 历史库导出 ≥10 篇真实已入库正文（`content` 字段，纯文本）
- `teaser/`：实网抓 ≥10 个付费墙预告页正文（WSJ/FT/Economist/Bloomberg 等，未绕过状态下 HTML→文本）
- 替换后跑 `pytest tests/test_quality.py -v`，必须保持 **full 误杀=0、teaser 拦截≥90%**
- **红线 4**：如果真实样本挂了，只许调词表/窗口（`src/bpc_fetch/quality.py`），**禁止删样本、禁止改阈值**。调了就把 diff 和理由写进 commit message。

### Step 4 — baseline 冻结 + 双跑（0.5 天）
```bash
cd ~/Projects/pac-cli
git worktree add /tmp/pac-baseline 76e24f5
cd /tmp/pac-baseline && python -m venv .venv && source .venv/bin/activate && pip install -e .
# 用主仓的 run_eval.py 指向 baseline 的 CLI：
python ~/Projects/pac-cli/scripts/run_eval.py --cmd ".venv/bin/bpc-fetch fetch" --out /tmp/baseline.json
```
- **同机同时段**跑 after：`cd ~/Projects/pac-cli && source .venv/bin/activate && python scripts/run_eval.py --out /tmp/after.json --compare /tmp/baseline.json`
- 判定：总成功率 ≥ baseline − 5pp，且无单站倒退 ≥2 篇
- **红线 1**：不达标 → 标 `environment_limited` 并附证据（状态码/错误信息），**禁止改 URL 集、禁止降 baseline、禁止放宽质量门**
- 同时记录每条耗时，验 B4：单 URL ≤120s、easy 档 P50 <10s
- B2：确认 WSJ 条目的 `strategy_hit` 含 `http_referer_custom`（实网失败可标 environment_limited，不否决）

### Step 5 — `--from-zip` 端到端（1 小时）
浏览器打开 https://gitflic.ru/project/magnolia1234/bpc_uploads 手工下载最新 zip → `pac rules sync --from-zip <path>` → `pac rules version` 确认 base 更新、stale 警告消失。贴完整命令输出。

### Step 6 — 交验
按 `IMPLEMENTATION.md` §15.3 的 DoD 表逐项打勾，每项附证据（命令 + 输出 + 文件路径），写成 `docs/ACCEPTANCE.md` 交回给我。

## 3. 环境陷阱（这台 Mac，别踩）

1. **Surge fake-ip**：所有域名 DNS 解析到 198.18.0.0/15。`ssrf.py` 的适配**不是 bug，不许删**
2. Wikipedia 的 `HTTP_BLOCKED/bot/403` 是代理出口被 Wikimedia 封，**环境问题，不修**
3. 测 exit code：`pac fetch ... > /tmp/x.json; echo $?`，**不要** `| head; echo $?`（那是 head 的状态）
4. gitflic raw blob 端点全 404，正常现象；base 刷新只走 `--from-zip`
5. 无 `timeout` 命令，用 `gtimeout` 或直接跑
6. venv 激活：`source .venv/bin/activate`

## 4. 红线（§15.6 摘要，触发即停工，回来找我）

1. eval 成功率不达标且定位不到原因 → 停工，不许凑绿
2. 范围蔓延：channels/、client.py、jina/bpc_ext/archive_submit/common_crawl、MCP、Skills → 一律不写
3. 规则源全断且本地无 base zip → 停工汇报
4. 真实夹具暴露词表误杀 → 只调词表/窗口，不删样本不改阈值
5. merge 代码改动前缺整条替换单测 → 不许动

## 5. 工作纪律

- 小步提交，message 用 `fix:` / `feat:` / `test:` / `chore:` 前缀（参照 git log 风格）
- 每完成一个 Step，贴：跑了什么命令、输出是什么、文件在哪。**没有证据 = 没做**
- 不确定的事写「不确定 + 需要什么证据」，不要乐观填坑
- 完成定义 = §15.3 DoD 全绿，不是「看起来能跑」
