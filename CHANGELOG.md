# Changelog

All notable changes to PAC are documented here.

## Unreleased

- **On-demand interactive fetch**: `pac fetch URL --interactive` extracts an
  authorized article through Ego lite (Bypass Paywalls Clean stays in the
  browser). PAC may launch Ego lite if needed, uses a dedicated task space,
  concurrency 1, and never copies cookies into PAC. This path is not part of
  `pac batch`. Chrome/DrissionPage attach remains opt-in via
  `PAC_INTERACTIVE_BACKEND=drissionpage`.
- Docs and agent skill aligned with the 29-source registry, official-feed
  discovery, cookie isolation, and `--interactive`. Removed the dead
  `docs/IMPLEMENTATION.md` pointer and the stale “242 tests / Google News
  primary discovery / default paywall cleanup” claims.

## 0.2.2 (Permanent Architectural Freeze)

### Phase 5: Final Production Consolidation
- **Zero-Network Public Suffix List (PSL) Trie Resolver**: Built-in lazy reversed-label Trie (`public_suffix_list.dat`, MPL-2.0) accurately resolving global ccTLDs (`.com.cn`, `.co.uk`, `.com.au`, `.edu.tw`, etc.), wildcards, and exception rules with 77/77 official tests.
- **Opt-in Structured Diagnostics (`--diagnostics`)**: Supports `--diagnostics` across `fetch`, `batch`, and `discover`, returning unique `request_id`, engine execution timelines, attempt history, and quality score breakdowns without inflating default output.
- **Quality Package Modularization**: Refactored monolithic 1100-line `quality.py` into cohesive `quality/` subpackage (`access_control.py`, `paywall.py`, `metrics.py`, `__init__.py`) with 100% backward-compatible API.
- **Golden Quality Regression Suite**: Added `test_golden_quality.py` locking in 5 baseline layout evaluation behaviors (Full Article, Newsflash, 403 Challenge, Teaser, Navigation).
- **Production Hardening & Official Dockerfile**: Added multi-stage `Dockerfile` with pre-installed Playwright Chromium, Camoufox Firefox, and `curl_cffi`. SWR lock `fsync` crash durability and BrowserPool cancellation shielding.

### Phase 4: Offline-First SWR, Google News Decoder & Proxy Circuit Breaker
- **Stale-While-Revalidate (SWR) Rules Engine**: `pac fetch` / `batch` read local cached snapshot immediately (0ms blocking latency); detached background child process revalidates after 7-day TTL.
- **Pure Local Google News URL Decoder**: Reverse-decoded `CBMi...` Protobuf/Base64 URLs directly to canonical publisher URLs with zero network requests.
- **Proxy Pool & Circuit Breaker**: Shared failure cooldown and automatic candidate rotation across HTTP and Browser engines on 403/429/bot challenge/network errors.

### Phase 3: Protocol Impersonation & Multi-Gateway Resilience
- **TLS/JA3 Impersonation**: Integrated `curl_cffi` to simulate Chrome TLS and HTTP/2 handshake fingerprints with fail-open fallback to `httpx`.
- **Camoufox Anti-Detect Engine**: Integrated C++ Firefox-based Camoufox browser driver with main-world DOM cleaning.
- **Multi-Gateway Fallback Ladder**: `archive.today / archive.ph` (with 60s domain cooldown) -> `Wayback Machine API` -> `Reader Gateway` (`PAC_READER_GATEWAY`).

### Phase 2: Asynchronous Chain of Responsibility & Quality Gate
- **Pipeline Architecture**: Structured `DirectHttpHandler` -> `StealthBrowserHandler` -> `MultiGatewayArchiveHandler`.
- **Quality Gate**: DOM/text density scoring, multi-language teaser classification, and newsflash exemptions.
- **Ingress Discovery**: Added `pac discover` for RSS, Sitemap, and Google News discovery.

### Phase 1: Core Engine Normalization
- Streamlined CLI to `pac` with strict JSON envelope and machine-readable exit codes.
- Added daily GitHub Actions rule sync workflow and SSRF security guards.
