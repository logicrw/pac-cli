# pac-cli 交接说明（给下一位实施工程师）

**日期**: 2026-07-30
**写给**: 接替实施工作的 AI（Kimi K3）或人类工程师
**当前状态**: Phase 1 代码已完成并通过 A 层单测 + 部分实网验证；剩 B 层验收项
**你的任务**: 完成 Phase 1 剩余验收，拿 DoD 检查表回来交验。**不是**开始新功能。

---

## 0. 你是谁、边界在哪

你是**实施方**。另有评审方（Claude）负责架构决断与验收。分工纪律：

- 开工许可 = `docs/IMPLEMENTATION.md` **§15**（专家批复，最高优先级文档）
- Phase 1 以 `IMPLEMENTATION.md`（draft-2 + §15）为准；`TARGET-ARCHITECTURE.md` 是 Phase 1.5+ 蓝图，**现在不许做**
- 任何对话里的口头承诺都不算数，以落盘文档为准
- 触发 `IMPLEMENTATION.md` §15.6 五条红线之一 → 停工，找评审

## 1. 必读文档（按序，都在本仓库 docs/）

1. `IMPLEMENTATION.md` —— §S 自我审查 + **§15 批复**（逐条读，含 DoD、白名单、红线）
2. `TARGET-ARCHITECTURE.md` —— 只需读 §0 设计公理和 §3 衔接约束，知道为什么现在不做通道化
3. `ARCHITECTURE.md` —— 产品背景（独立 CLI + Skills 给多 Agent 用；与 news-scraper-final 双轨）

## 2. 仓库现状（评审方已验证的事实，2026-07-29/30）

- 代码基线：fork 自 `Sophomoresty/bpc-fetch` + Phase 1 实现（commit `ece3c1a`）
- **25 单测全绿**：`cd ~/Projects/pac-cli && source .venv/bin/activate && python -m pytest tests/ -q`
- **工作区有评审侧的未提交修复**（你的第一件事，见 §3）：
  - `src/bpc_fetch/ssrf.py`：PAC_SSRF 开关 + fake-ip（198.18/15）+ 代理 env 适配
  - `src/bpc_fetch/strategy.py`：archive 条件触发 + 冷却；200 通过质量门的 `final_quality_pass` 出口；终判块仅 status==200 走抽取
  - `src/bpc_fetch/result.py`：`classify_http_failure` 对 200 归 `EXTRACT_FAILED/extract`
  - `tests/test_ssrf.py`（+4 测试）、`tests/test_plan_archive.py`（新增 4 测试）
- 端到端已验证（评审方实测）：
  - `pac fetch https://example.com` → ok / 191 字符 / EXIT=0
  - AP 真实新闻 → ok / 5702 字符 / http_primary 一步命中
  - Wikipedia → 诚实的 `HTTP_BLOCKED/bot/403`（Wikimedia 封本机代理出口，**环境问题不是 bug，不要修**）
  - 失败 fetch `EXIT=1`（契约正确）

## 3. 环境陷阱（这台 Mac，踩过的坑，别再踩）

1. **Surge fake-ip**：本机所有域名 DNS 解析到 198.18.0.0/15。ssrf.py 已适配（hostname 全 fake-ip 时跳过本地 DNS 检查）。**不要把这段适配当 bug 修掉。**
2. **macOS 无 `timeout` 命令**；需要时用 `gtimeout`（若装了 coreutils）或直接跑。
3. **管道陷阱**：`pac fetch ... | head; echo $?` 的 `$?` 是 `head` 的状态。测 exit code 必须 `pac fetch ... > /tmp/x.json; echo $?`。
4. **历史说明（已被 0.2.2 维护路径取代）**：GitFlic project raw blob 端点不可靠；不要恢复手工 ZIP 流程。当前中央 workflow 使用已验证的 `bpc_uploads` master ZIP 与 `bpc_updates` shallow clone，客户端只同步 PAC GitHub 镜像。
5. **GitHub 一次性设置**：在 `Settings > Actions > General` 开启 **Allow GitHub Actions to create and approve pull requests**，否则同步 workflow 会明确失败且不会直推 `main`。同时建议为 `main` 开启 required-PR branch protection；workflow 本身只开 PR，但当前仓库设置不会阻止其他写入者绕过该约定。
6. `.venv` 已装好依赖；激活方式 `source .venv/bin/activate`。

## 4. 剩余任务（按序执行，验收前只做这些）

### T1 提交评审侧修复（0.5h）
`git diff` 逐文件审查 §2 列出的未提交改动 → 无疑问则提交（message 建议：`fix: ssrf env adaptation, archive gating, failure classification (review round)`）→ 跑一遍 25 测试确认仍绿。**有疑问不要改回去，先问评审。**

### T2 填充 eval URL 集（0.5–1 天，最易低估）
- 从 `~/Projects/news-scraper-final/sources.yaml` 的 hard/medium 档抽 10–15 个 domain，每域 3–5 篇**发布 >7 天**的文章 URL（付费墙状态稳定），加 3 个 easy 对照域，总数 ≥40
- 填进 `tests/fixtures/eval_urls.yaml`（当前是 example.com 占位）
- 其中标 3–5 条 `canary: true`（给 TARGET §9 的 sync 金丝雀用）
- 注意：选**旧文**，不要选今天的新闻（archive 快照和付费墙状态都不稳）

### T3 冻结并跑 baseline（0.5 天）
```bash
cd ~/Projects/pac-cli
git worktree add /tmp/pac-baseline 76e24f5   # = tests/fixtures/BASELINE_NOTE.txt 里的 BASELINE_SHA
cd /tmp/pac-baseline && python -m venv .venv && source .venv/bin/activate && pip install -e .
python scripts/run_eval.py --out /tmp/baseline.json   # 若 baseline 版无此脚本，用主仓脚本指向 baseline 的 pac
```
**同机同时段**再跑主仓 after：`python scripts/run_eval.py --out /tmp/after.json --compare /tmp/baseline.json`

### T4 B1 判定
总成功率 ≥ baseline − 5pp，且无单站倒退 ≥2 篇。失败可标 `environment_limited`（须写明证据，如 403 截图/状态码）。**不达标不许改 URL 集、不许降 baseline、不许放宽质量门——那是红线。**

### T5 真实质量夹具（0.5 天）
当前 `tests/fixtures/quality/` 是合成句子，证据强度不足：
- `full/`：从 news-scraper 的 PostgreSQL 历史库导 ≥10 篇真实已入库正文（`content` 字段）
- `teaser/`：实网抓 ≥10 个付费墙预告页正文（WSJ/FT/Economist 等未绕过状态下的 HTML→文本）
- 替换后保持 A4 双侧指标：full 误杀=0，teaser 拦截≥90%。**误杀只许调词表/窗口，不许删样本。**

### T6 `--from-zip` 端到端（历史验收项，已完成）
该手工流程仅保留为恢复入口；常规维护已由中央 workflow + 客户端 lazy sync 取代。

### T7 交验
按 `IMPLEMENTATION.md` §15.3 的 DoD 表逐项打勾，每项附证据（命令输出/文件路径），交回评审。

## 5. 禁止事项（§15.4/§15.6 摘要，违反即返工）

- 禁止新建 `channels/`、`client.py`；禁止实现 jina/bpc_ext/archive_submit/common_crawl 任何代码
- 禁止 Skills 定稿、MCP、glue 接入、PyPI 发布、包目录改名
- 禁止为凑 eval 绿改 URL 集 / 降 baseline / 放宽质量门
- 禁止在 merge 无整条替换单测的情况下改 rules 代码
- 禁止「看起来能跑就宣布完成」——每个完成项要附命令输出证据

## 6. 工作方式约定

- 小步提交，commit message 参照仓库已有风格（`fix:` / `feat:` / `test:` / `chore:`）
- 发现代码与文档冲突：以 §15 为准，在 commit message 里说明依据
- 不确定的事：写下「不确定 + 需要什么证据」，不要用乐观填坑
- 完成定义 = §15.3 DoD 全绿，不是「我觉得做完了」
