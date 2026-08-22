#!/usr/bin/env python3
"""Sequential P4 interactive eval. Concurrency is always 1.

Default backend is Ego lite. This is not ``pac batch``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

from bpc_fetch.interactive import fetch_interactive


async def _run(urls: list[str], profile: Path | None, cdp: str | None) -> dict:
    rows = []
    started = time.perf_counter()
    for url in urls:
        t0 = time.perf_counter()
        result = await fetch_interactive(
            url,
            profile=profile,
            cdp=cdp,
            cookie_header="",
            full_markdown=True,
        )
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        rows.append({
            "url": url,
            "ok": bool(result.get("ok")),
            "error_code": result.get("error_code") or "",
            "engine": result.get("engine") or "",
            "title": result.get("title") or "",
            "content_chars": result.get("content_chars") or 0,
            "paywall_suspected": bool(result.get("paywall_suspected")),
            "latency_ms": elapsed_ms,
            "cookie_copied": bool((result.get("interactive") or {}).get("cookie_copied")),
        })
    latencies = [row["latency_ms"] for row in rows]
    successes = [row for row in rows if row["ok"]]
    false_fulltext = [
        row for row in rows
        if row["ok"] and row["paywall_suspected"]
    ]
    return {
        "ok": True,
        "count": len(rows),
        "successes": len(successes),
        "false_fulltext": len(false_fulltext),
        "cookie_copied": any(row["cookie_copied"] for row in rows),
        "median_ms": int(statistics.median(latencies)) if latencies else 0,
        "p95_ms": int(sorted(latencies)[max(0, int(round(0.95 * (len(latencies) - 1))))]) if latencies else 0,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="P4 Ego lite interactive eval")
    parser.add_argument("--file", type=Path, required=True, help="URL list, one per line")
    parser.add_argument("--profile", type=Path, default=None, help="Only for PAC_INTERACTIVE_BACKEND=drissionpage")
    parser.add_argument("--cdp", default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    urls = [
        line.strip()
        for line in args.file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    report = asyncio.run(_run(urls, args.profile, args.cdp))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
