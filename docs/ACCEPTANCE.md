# Phase 1 Acceptance Report

## Verdict

**PASS — environment-limited live evidence, with no Phase 1 critical blocker.**

The acceptance run used the same 48 historical article URLs and the same frozen
rules file for the baseline and current implementation. The current accepted
success rate is 2.08 percentage points below baseline, within the allowed 3 pp
regression budget. No domain lost two or more accepted articles. The WSJ
referer strategy was exercised on all three WSJ URLs; those requests ultimately
hit the site's live bot challenge, which is an allowed environment-limited
outcome under the Phase 1 DoD.

## Reproducibility record

| Item | Value |
|---|---|
| Baseline commit | `76e24f51f4bc7b1ed9bd91418796f3cf104be56b` |
| Release under test | staged `main` release candidate; planned tag `v0.2.1` |
| URL set | `tests/fixtures/eval_urls.yaml` |
| Frozen rules file | `/tmp/pac-eval-rules/sites.js` |
| Rules SHA-256 | `ad9fb6180640fb2b093a0d9196142bceed11a520c40d5fc274954cb92d672457` |
| Baseline report | `reports/phase1-baseline.json` |
| Current report | `reports/phase1-current.json` |
| Runner | `scripts/run_eval.py` |
| Per-URL timeout | 60 seconds |
| Concurrency | 2, with at least 2 seconds between requests to the same domain |

Both implementations used the frozen rules file explicitly. The URL sets in the
reports are identical: zero added and zero removed URLs.

## A-layer: deterministic acceptance

### A1 — Evaluation set

**PASS.** `tests/fixtures/eval_urls.yaml` contains:

- 48 historical article URLs;
- 16 domains;
- 13 hard/medium domains and 3 easy control domains;
- 4 canary URLs.

### A2 — Quality gate

**PASS.** The corpus contains 12 substantive public-domain U.S. government
prose excerpts and 10 public, unauthenticated teaser/paywall samples.
Provenance and reuse terms are documented in
`tests/fixtures/quality/README.md`.

Observed result from `pytest tests/test_quality.py -q`:

- 12/12 public-domain full-content samples accepted — 0% false positives;
- 10/10 teaser/paywall samples rejected as `PAYWALL_REMAINING` — 100% blocked;
- 9 quality test functions passed.

The gate additionally rejects known navigation/challenge shells as
`EXTRACT_FAILED`, including CNBC navigation-only output, FT Security
Verification, and Akamai Access Denied pages.

### A3 — Legacy regression suite and error taxonomy

**PASS.** `env -u PYTHONPATH .venv/bin/pytest tests/ -q` completed with
**56 passed**. Runtime is omitted because it varies slightly between runs.

The current live report contains explicit failure codes for every failed item:
18 `BOT_CHALLENGE`, 22 `HTTP_BLOCKED`, and 3 `NETWORK`. No current failure fell
back to an empty legacy classification.

### A4 — Clean-environment installation and smoke test

**PASS.** The 0.2.1 wheel and sdist were built with `python -m build` and both
passed `twine check`. The wheel was then installed into a new virtual
environment with `PYTHONPATH` removed. Playwright Chromium was installed using
the wheel environment's own CLI.

Observed smoke results:

- `pac doctor --compact`: `ok:true`, Chromium launched successfully, and 942
  sites loaded directly from the wheel's bundled `sites.js`; the expected
  `using_bundled_base` warning was present because the isolated rules cache was
  empty;
- `pac fetch https://example.com --compact`: exit 0, valid JSON, `ok:true`,
  191 content characters, `final_quality_pass`, and a Markdown output path;
- a Playwright browser fetch of `https://example.com` returned HTTP 200 while
  applying 28 effective WSJ general blockers;
- `pip check`: no broken requirements.

Additional deterministic release evidence:

- atomic ZIP rule sync and reload produced **927 sites**, with no temporary
  files left behind;
- the frozen rule audit found **29 active unique** general blockers and **28**
  effective blockers for `wsj.com`;
- a real Patchright fetch of `https://example.com` returned HTTP **200** while
  applying **28** general blockers.

## B-layer: controlled live comparison

### B1 — Success-rate and per-domain regression budget

**PASS.** Success means `ok:true` after applying the current quality gate to both
runs.

| Metric | Baseline | Current | Difference |
|---|---:|---:|---:|
| Accepted successes | 6/48 | 5/48 | -1 article |
| Accepted success rate | 12.5% | 10.4% | **-2.08 pp** |
| Raw successes | 11/48 | 5/48 | audit-only |
| Maximum item latency | 30.479s | 30.885s | +0.406s |

The accepted-rate difference is within the allowed `-3 pp` budget. WSJ moved
from 1 accepted item to 0, a one-article change; the blocking threshold is a
drop of at least 2 articles on one domain, so `domain_regressions` is empty.

The baseline raw rate is not used as the comparator because five raw baseline
"successes" failed the shared quality definition:

- 3 CNBC navigation shells -> `EXTRACT_FAILED`;
- 2 Barron's subscription teasers -> `PAYWALL_REMAINING`.

This normalization prevents baseline-only false positives from making the
comparison unfair.

### B2 — WSJ `referer_custom` path

**PASS with environment limitation.** All three WSJ evaluation URLs recorded:

- `http_referer_custom`;
- `http_googlebot_fallback`;
- `browser_cleanup`.

Their final outcomes were `BOT_CHALLENGE`, not a missing or skipped referer
strategy. This is evidence that the configured strategy path executed against
real URLs; the current live site denied extraction at a later stage. Under the
DoD's environment-limited policy, a strategy hit plus the recorded live failure
is acceptable evidence.

### B3 — Clean-environment usability

**PASS.** See A4. Installation, `doctor`, and an actual fetch all completed in
the isolated environment from the built wheel, without relying on the
repository's `.venv` or an inherited `PYTHONPATH`.

### B4 — Performance envelope

**PASS.** In the current 48-URL run:

- maximum item latency: **30.885s**, below the 120s limit;
- easy-domain P50: **1.787s**, below the 10s limit;
- no item timed out at the configured 60s per-URL limit.

## Environment-limited observations

The current run had 43 non-successes: 18 bot challenges, 22 HTTP blocks, and 3
network failures. These are retained in `reports/phase1-current.json` rather
than rewritten as successes. Canary outcomes were:

- Bloomberg: accepted in 1.416s;
- WSJ: bot challenge after the referer/fallback path;
- Barron's: bot challenge;
- The Register: HTTP blocked.

These observations affect the absolute live success rate but do not invalidate
the controlled comparison: both runs used the same URLs, rules source,
quality definition, timeout, and concurrency policy.

## Commands used

```bash
env -u PYTHONPATH .venv/bin/python scripts/run_eval.py \
  --urls tests/fixtures/eval_urls.yaml \
  --cmd '/tmp/pac-baseline/.venv/bin/bpc-fetch fetch --sites-js /tmp/pac-eval-rules/sites.js --out-dir /tmp/pac-eval-baseline-accepted2 --no-images --compact' \
  --out reports/phase1-baseline.json --timeout 60 --concurrency 2

env -u PYTHONPATH .venv/bin/python scripts/run_eval.py \
  --urls tests/fixtures/eval_urls.yaml \
  --cmd '.venv/bin/pac fetch --sites-js /tmp/pac-eval-rules/sites.js --out-dir /tmp/pac-eval-current-accepted2 --no-images --compact' \
  --out reports/phase1-current.json \
  --compare reports/phase1-baseline.json --timeout 60 --concurrency 2

env -u PYTHONPATH .venv/bin/pytest tests/ -q
env -u PYTHONPATH /tmp/pac-build-venv/bin/python -m build --outdir dist
env -u PYTHONPATH /tmp/pac-build-venv/bin/python -m twine check dist/*
env -u PYTHONPATH /tmp/pac-artifact-venv/bin/python -m pip install \
  dist/pac_cli-0.2.1-py3-none-any.whl
env -u PYTHONPATH /tmp/pac-artifact-venv/bin/python -m playwright install chromium
PAC_RULES_DIR=/tmp/pac-artifact-rules env -u PYTHONPATH \
  /tmp/pac-artifact-venv/bin/pac doctor --compact
PAC_RULES_DIR=/tmp/pac-artifact-rules env -u PYTHONPATH \
  /tmp/pac-artifact-venv/bin/pac fetch \
  https://example.com --out-dir /tmp/pac-artifact-out \
  --no-images --compact
```

## Scope note

No Phase 1 work changed `bpc_fetch/client.py` or added new channel code. This
report identifies the reviewed staged release candidate that will be annotated
with tag `v0.2.1` only after acceptance. Both release artifacts passed
metadata/archive checks, and the wheel passed isolated install, doctor, HTTP
fetch, browser fetch, and dependency verification.
