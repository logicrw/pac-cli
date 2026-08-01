"""Eval runner for pac-cli Phase 1 B-layer acceptance.

Runs `pac fetch <url>` over tests/fixtures/eval_urls.yaml, records per-item
envelope metrics, and writes an aggregate JSON report. Optionally compares
against a baseline report.

Usage:
    python scripts/run_eval.py --out report.json [--compare baseline.json]
"""
from __future__ import annotations

import argparse
import json
import shlex
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml

DEFAULT_URLS = "tests/fixtures/eval_urls.yaml"
DEFAULT_CMD = "pac fetch"
DEFAULT_TIMEOUT = 150
DEFAULT_CONCURRENCY = 2
MAX_CONCURRENCY = 2
DOMAIN_MIN_INTERVAL_SEC = 2.0
REGRESSION_DROP_THRESHOLD = 2


def load_items(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = data.get("items") or []
    out = []
    for it in items:
        url = (it.get("url") or "").strip()
        if not url:
            continue
        domain = (it.get("domain") or "").strip() or _domain_from_url(url)
        out.append({
            "url": url,
            "domain": domain,
            "tier": it.get("tier") or "",
            "note": it.get("note") or "",
            "canary": bool(it.get("canary", False)),
        })
    return out


def _domain_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


class DomainThrottle:
    """Enforce >= DOMAIN_MIN_INTERVAL_SEC between requests to the same domain."""

    def __init__(self, min_interval: float = DOMAIN_MIN_INTERVAL_SEC):
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def wait(self, domain: str) -> None:
        with self._lock:
            now = time.monotonic()
            last = self._last.get(domain)
            if last is not None:
                delay = self._min_interval - (now - last)
                if delay > 0:
                    time.sleep(delay)
            self._last[domain] = time.monotonic()


def run_one(item: dict, cmd_prefix: list[str], timeout: int, throttle: DomainThrottle) -> dict:
    url = item["url"]
    rec = {
        "url": url,
        "domain": item["domain"],
        "tier": item["tier"],
        "canary": item["canary"],
        "raw_ok": False,
        "ok": False,
        "quality_checked": False,
        "quality_reason": "",
        "error_code": "",
        "failure_class": "",
        "strategy_hit": [],
        "elapsed_sec": 0.0,
        "content_chars": 0,
        "exit_code": None,
        "error": "",
    }
    throttle.wait(item["domain"])
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd_prefix + [url],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        rec["exit_code"] = proc.returncode
    except subprocess.TimeoutExpired:
        rec["elapsed_sec"] = round(time.monotonic() - t0, 3)
        rec["error"] = f"timeout after {timeout}s"
        return rec
    except Exception as e:  # noqa: BLE001 - eval runner must not crash on one item
        rec["elapsed_sec"] = round(time.monotonic() - t0, 3)
        rec["error"] = f"subprocess error: {e}"
        return rec
    rec["elapsed_sec"] = round(time.monotonic() - t0, 3)

    stdout = (proc.stdout or "").strip()
    try:
        env = json.loads(stdout)
    except json.JSONDecodeError:
        rec["ok"] = False
        rec["error"] = f"json parse failed (exit={proc.returncode}): {stdout[:200]!r} {proc.stderr[-200:]!r}".strip()
        return rec

    rec["raw_ok"] = bool(env.get("ok", False))
    rec["ok"] = rec["raw_ok"]
    rec["error_code"] = env.get("error_code") or ""
    rec["failure_class"] = env.get("failure_class") or ""
    rec["strategy_hit"] = env.get("strategy_hit") or []
    rec["content_chars"] = int(env.get("content_chars") or env.get("text_length") or 0)

    # Baseline commits predate the strict quality envelope and can report a
    # paywall teaser as ok.  Re-apply the current quality gate to any content
    # returned inline or written by the subprocess so baseline/current share
    # the same success definition.  Preserve raw_ok for auditability.
    if rec["raw_ok"]:
        content = env.get("markdown") or ""
        content_path = env.get("path") or ""
        if not content and content_path:
            try:
                content = Path(content_path).read_text(encoding="utf-8")
            except OSError as e:
                rec["quality_reason"] = f"content read failed: {e}"
        if content:
            try:
                from bpc_fetch.quality import quality_check

                quality = quality_check(content, env.get("title") or "")
                rec["quality_checked"] = True
                rec["quality_reason"] = quality.reason
                rec["ok"] = quality.ok
                if not quality.ok:
                    rec["error_code"] = quality.error_code
                    rec["failure_class"] = "paywall" if quality.paywall_suspected else "extract"
            except ImportError:
                # Keeps the script runnable from the historical baseline
                # worktree, where bpc_fetch.quality does not exist.
                rec["quality_reason"] = "quality module unavailable"
    if not rec["raw_ok"] and not rec["error_code"]:
        rec["error"] = env.get("error") or (
            f"non-zero exit {proc.returncode}" if proc.returncode != 0 else "reported ok=false"
        )
    elif proc.returncode != 0 and not rec["error_code"]:
        rec["error"] = env.get("error") or f"non-zero exit {proc.returncode}"
    return rec


def aggregate(items: list[dict]) -> dict:
    by_domain: dict[str, dict] = {}
    for it in items:
        d = by_domain.setdefault(it["domain"], {"total": 0, "ok": 0})
        d["total"] += 1
        d["ok"] += 1 if it["ok"] else 0
    for d in by_domain.values():
        d["rate"] = round(d["ok"] / d["total"], 4) if d["total"] else 0.0
    return by_domain


def compare_runs(current: dict, baseline: dict) -> dict:
    cur_meta = current["meta"]
    base_meta = baseline.get("meta", {})
    rate_diff = round(cur_meta["success_rate"] * 100 - base_meta.get("success_rate", 0) * 100, 2)

    cur_dom = current.get("by_domain", {})
    base_dom = baseline.get("by_domain", {})
    regressions = []
    for domain, cur in sorted(cur_dom.items()):
        base = base_dom.get(domain)
        if not base:
            continue
        drop = base.get("ok", 0) - cur.get("ok", 0)
        if drop >= REGRESSION_DROP_THRESHOLD:
            regressions.append({
                "domain": domain,
                "baseline_ok": base.get("ok", 0),
                "current_ok": cur.get("ok", 0),
                "drop": drop,
            })

    cur_urls = {it["url"] for it in current.get("items", [])}
    base_urls = {it.get("url") for it in baseline.get("items", [])}
    added = sorted(cur_urls - base_urls)
    removed = sorted(base_urls - cur_urls)

    return {
        "baseline_generated_at": base_meta.get("generated_at", ""),
        "baseline_success_rate": base_meta.get("success_rate", 0),
        "current_success_rate": cur_meta["success_rate"],
        "success_rate_diff_pp": rate_diff,
        "domain_regressions": regressions,
        "added_urls": added,
        "removed_urls": removed,
    }


def print_compare_summary(cmp: dict) -> None:
    print("=== compare with baseline ===")
    print(f"baseline rate : {cmp['baseline_success_rate'] * 100:.1f}% ({cmp['baseline_generated_at'] or 'unknown time'})")
    print(f"current rate  : {cmp['current_success_rate'] * 100:.1f}%")
    print(f"diff          : {cmp['success_rate_diff_pp']:+.2f} pp")
    if cmp["domain_regressions"]:
        print("domain regressions (ok drop >= 2):")
        for r in cmp["domain_regressions"]:
            print(f"  - {r['domain']}: {r['baseline_ok']} -> {r['current_ok']} (drop {r['drop']})")
    else:
        print("domain regressions: none")
    print(f"added urls   : {len(cmp['added_urls'])}")
    for u in cmp["added_urls"]:
        print(f"  + {u}")
    print(f"removed urls : {len(cmp['removed_urls'])}")
    for u in cmp["removed_urls"]:
        print(f"  - {u}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run pac fetch over eval URL set and report metrics")
    parser.add_argument("--urls", type=Path, default=Path(DEFAULT_URLS), help=f"URL set YAML (default {DEFAULT_URLS})")
    parser.add_argument("--cmd", type=str, default=DEFAULT_CMD, help=f"Command prefix (default '{DEFAULT_CMD}')")
    parser.add_argument("--out", type=Path, required=True, help="Output report JSON path")
    parser.add_argument("--compare", type=Path, default=None, help="Baseline report JSON to compare against")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"Per-URL timeout seconds (default {DEFAULT_TIMEOUT})")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help=f"Max parallel fetches, capped at {MAX_CONCURRENCY} (default {DEFAULT_CONCURRENCY})")
    args = parser.parse_args()

    if not args.urls.exists():
        print(f"error: urls file not found: {args.urls}", file=sys.stderr)
        return 2

    items = load_items(args.urls)
    if not items:
        print(f"error: no URLs in {args.urls}", file=sys.stderr)
        return 2

    cmd_prefix = shlex.split(args.cmd)
    if not cmd_prefix:
        print("error: empty --cmd", file=sys.stderr)
        return 2

    workers = max(1, min(int(args.concurrency or 1), MAX_CONCURRENCY))
    throttle = DomainThrottle()
    print(f"running {len(items)} URLs with '{args.cmd}' (concurrency={workers}, timeout={args.timeout}s)")

    t0 = time.monotonic()
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(run_one, item, cmd_prefix, args.timeout, throttle) for item in items]
        for i, fut in enumerate(futs, 1):
            rec = fut.result()
            results.append(rec)
            mark = "ok " if rec["ok"] else "FAIL"
            print(f"[{i}/{len(items)}] {mark} {rec['url']} ({rec['elapsed_sec']:.1f}s"
                  + (f", {rec['error_code'] or rec['error']}" if not rec["ok"] else f", {rec['content_chars']} chars")
                  + ")")
    total_elapsed = time.monotonic() - t0

    raw_ok_count = sum(1 for r in results if r["raw_ok"])
    ok_count = sum(1 for r in results if r["ok"])
    easy_elapsed = [r["elapsed_sec"] for r in results if r["tier"] == "easy"]
    report = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cmd": args.cmd,
            "total": len(results),
            "raw_ok_count": raw_ok_count,
            "raw_success_rate": round(raw_ok_count / len(results), 4) if results else 0.0,
            "ok_count": ok_count,
            "success_rate": round(ok_count / len(results), 4) if results else 0.0,
            "quality_checked_count": sum(1 for r in results if r["quality_checked"]),
            "elapsed_sec": round(total_elapsed, 3),
            "max_elapsed_sec": max((r["elapsed_sec"] for r in results), default=0.0),
            "easy_p50_sec": round(statistics.median(easy_elapsed), 3) if easy_elapsed else None,
        },
        "by_domain": aggregate(results),
        "items": results,
    }

    if args.compare:
        if not args.compare.exists():
            print(f"error: baseline file not found: {args.compare}", file=sys.stderr)
            return 2
        try:
            baseline = json.loads(args.compare.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"error: baseline is not valid JSON: {e}", file=sys.stderr)
            return 2
        cmp_result = compare_runs(report, baseline)
        report["compare"] = cmp_result
        print_compare_summary(cmp_result)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"report written to {args.out} "
          f"({ok_count}/{len(results)} ok, {report['meta']['success_rate'] * 100:.1f}%, {total_elapsed:.1f}s total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
