---
name: pac-cli
description: Fetch paywalled news articles, discover recent publisher articles, or batch extract structured Markdown using PAC (Bypass Paywalls Clean engine) with TLS impersonation, Camoufox stealth browser, and multi-gateway fallbacks. Use whenever the user asks to fetch news articles, read paywalled articles, bypass paywalls, extract article content as Markdown, or discover recent news from major publishers (WSJ, Economist, FT, Bloomberg, NYT, etc.).
version: 0.2.2
author: pac-cli contributors
license: MIT
metadata:
  hermes:
    tags: [articles, news, paywall, research, cli, scraping]
    related_skills: []
---

# PAC CLI Skill

## Overview

`pac-cli` is an agent-facing, deterministic engine to retrieve and extract structured Markdown articles from 930+ global news publishers. It incorporates TLS JA3/JA4 impersonation (`curl_cffi`), anti-detect browser execution (`Camoufox` / `Playwright`), Stale-While-Revalidate (SWR) rules caching, and multi-gateway fallback (`archive.today` / `Wayback` / `Reader Gateway`).

---

## Quick Reference Commands

| Task | Command | Description |
|---|---|---|
| **Healthcheck** | `pac doctor --compact` | Verify Python environment, rule caches, Chromium, and Camoufox status. |
| **Fetch Article** | `pac fetch "<URL>" --compact` | Fetch single article as structured JSON with full/truncated Markdown. |
| **Fetch with Diagnostics** | `pac fetch "<URL>" --diagnostics --compact` | Output `request_id`, execution timeline, engine breakdown, and quality metrics. |
| **Batch Fetch** | `pac batch --file urls.txt --out-dir ./articles --compact` | Concurrently download articles with auto-deduplication and collision safety. |
| **Discover Articles** | `pac discover economist.com --limit 5 --compact` | Find latest articles via zero-network Google News decoding or RSS/Sitemap. |
| **Rules Inspection** | `pac rules show wsj.com --compact` | Inspect the active bypass strategy and CSS selectors for a domain. |
| **Rules Manual Sync** | `pac rules sync --compact` | Force an immediate upstream rule refresh from GitFlic/GitHub mirror. |

---

## Standard Agent Execution Workflow

### 1. Pre-flight Verification
Before running intensive batch or critical fetches, ensure the engine is healthy:
```bash
pac doctor --compact
```
Verify `"ok": true` and `"chromium_installed": true`.

### 2. Fetching Single Articles
Always use `--compact` to produce single-line JSON without terminal formatting overhead:
```bash
pac fetch "https://www.economist.com/finance-and-economics/2026/08/17/example-article" --compact
```

#### Parsing Result Envelope:
- **Success (`"ok": true`)**:
  - `result["markdown"]`: The clean, structured Markdown article body.
  - `result["title"]`: Extracted article title.
  - `result["strategy_hit"]`: Ordered strategies that succeeded (e.g. `["http_primary", "final_quality_pass"]`).
  - `result["engine"]`: The engine that fulfilled the request (`http`, `browser`, `archive_today`, etc.).
- **Failure (`"ok": false`)**:
  - `result["error_code"]`: Standardized code (see Error Semantics below).
  - `result["failure_class"]`: High-level failure category (`bot`, `network`, `strategy`, `extract`, `config`).
  - `result["recovery_hint"]`: Actionable recommendation.

### 3. Discovering & Batch Fetching News
When asked to "find and read the latest news from X":
```bash
# Step 1: Discover articles
pac discover wsj.com --limit 3 --compact

# Step 2: Extract the "next_command" from output JSON and run it to batch download
pac batch https://www.wsj.com/... https://www.wsj.com/... --out-dir ./articles --compact
```

---

## Error Semantics & Agent Branching

| `error_code` | `failure_class` | Meaning / Agent Action |
|---|---|---|
| `BOT_CHALLENGE` | `bot` | Hard CAPTCHA/Turnstile challenge encountered. Report to user or retry via residential proxy (`PAC_PROXIES`). |
| `PAYWALL_REMAINING` | `strategy` | Extraction was stopped because only a teaser/login barrier was found (never treat teaser as full article). |
| `HTTP_BLOCKED` | `bot` / `strategy` | Server returned 403/429. The proxy circuit breaker will automatically cooldown this node. |
| `NETWORK` | `network` | Connect error or timeout. Safe to retry once. |
| `EXTRACT_FAILED` | `extract` | Page was retrieved but no acceptable prose content could be extracted. |
| `SSRF_BLOCKED` | `config` | Target URL resolved to a private/link-local IP. Do NOT bypass. |
| `BROWSER_UNAVAILABLE` | `config` | Playwright or Chromium binary missing. Run `playwright install chromium`. |

---

## Environment Variables for Advanced Scenarios

- `PAC_PROXIES="http://user:pass@p1:8080,http://user:pass@p2:8080"`: Comma-separated residential proxy pool with auto-rotation on 403/429/bot challenge.
- `PAC_READER_GATEWAY="https://r.jina.ai/{url}"`: Custom Reader Gateway fallback URL template.
- `PAC_RULES_AUTO_SYNC=0`: Force pure offline rule mode (0 network checks).
