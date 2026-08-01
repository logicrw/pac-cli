#!/usr/bin/env python3
"""Audit upstream BPC rule fields against PAC's current coverage."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bpc_fetch import sites
from bpc_fetch.rules import paths


STRUCTURAL = {"domain", "group", "exception", "name"}
EXECUTED = {
    "useragent",
    "useragent_custom",
    "referer",
    "referer_custom",
    "random_ip",
    "block_regex",
    "block_regex_str",
    "block_regex_general",
    "excluded_domains",
    "cs_dompurify",
}
MODELED_NOT_EXECUTED = {"allow_cookies", "amp"}


def status_for(field: str) -> str:
    if field in STRUCTURAL:
        return "structural"
    if field in EXECUTED:
        return "executed"
    if field in MODELED_NOT_EXECUTED:
        return "modeled_not_executed"
    return "unmodeled"


def load_entries(path: Path) -> dict[str, dict]:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^var defaultSites\s*=\s*", "", text.strip())
    text = re.sub(r";\s*$", "", text)
    text = re.sub(
        r"^var grouped_sites\s*=\s*\{.*?\};\s*", "", text, flags=re.DOTALL
    )
    entries = sites._extract_entries(text)
    if not entries:
        raise ValueError("no site entries found")
    return entries


def build_report(path: Path) -> dict:
    entries = load_entries(path)
    domain_map = sites.entries_to_domain_map(entries)
    counts: Counter[str] = Counter()
    for props in entries.values():
        if not isinstance(props, dict):
            continue
        counts.update(key for key in props if not key.startswith("_"))
        for exception in props.get("exception") or []:
            if isinstance(exception, dict):
                counts.update(key for key in exception if not key.startswith("_"))

    field_counts = [
        {"field": field, "count": count, "status": status_for(field)}
        for field, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {
        "ok": True,
        "sites_js": str(path),
        "entry_count": len(entries),
        "domain_count": len(domain_map),
        "field_counts": field_counts,
        "unmodeled_fields": [
            item for item in field_counts if item["status"] == "unmodeled"
        ],
    }


def default_sites_js() -> Path:
    cached = paths.sites_js_path()
    return cached if cached.exists() else sites.SITES_JS_DEFAULT


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sites-js", type=Path)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    path = args.sites_js or default_sites_js()
    try:
        report = build_report(path)
        exit_code = 0
    except Exception as exc:
        report = {"ok": False, "sites_js": str(path), "error": str(exc)}
        exit_code = 1
    print(json.dumps(report, indent=None if args.compact else 2, sort_keys=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
