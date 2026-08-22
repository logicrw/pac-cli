#!/usr/bin/env python3
"""Import the final news-scraper source YAML into PAC's static registry.

This is a one-way migration helper, not a runtime dependency. It never reads
cookies or environment variables and writes only the explicitly requested JSON
output path.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml


FOCUS_BY_DOMAIN = {
    "bloomberg.com": ("finance", "technology"),
    "reuters.com": ("finance", "technology"),
    "wsj.com": ("finance", "technology"),
    "ft.com": ("finance", "technology"),
    "economist.com": ("finance", "technology"),
    "cnbc.com": ("finance",),
    "fortune.com": ("finance", "technology"),
    "forbes.com": ("finance", "technology"),
    "marketwatch.com": ("finance",),
    "barrons.com": ("finance",),
    "businessinsider.com": ("finance", "technology"),
    "techcrunch.com": ("technology",),
    "theverge.com": ("technology",),
    "wired.com": ("technology",),
    "arstechnica.com": ("technology",),
    "technologyreview.com": ("technology",),
    "theinformation.com": ("technology",),
    "venturebeat.com": ("technology",),
    "fastcompany.com": ("technology",),
    "politico.com": ("technology",),
    "asia.nikkei.com": ("finance", "technology"),
    "semafor.com": ("finance", "technology"),
    "nytimes.com": ("finance", "technology"),
    "washingtonpost.com": ("finance", "technology"),
    "axios.com": ("finance", "technology"),
    "scmp.com": ("finance", "technology"),
    "theregister.com": ("technology",),
    "404media.co": ("technology",),
    "sifted.eu": ("finance", "technology"),
}


def infer_scope(url: str) -> str:
    """Label a feed by URL semantics without claiming editorial completeness."""
    value = url.casefold()
    if "artificial-intelligence" in value or "/rss/ai" in value or "/tag/ai" in value:
        return "artificial-intelligence"
    if "fintech" in value:
        return "fintech"
    if "security" in value:
        return "security"
    if "technology" in value or "/tech" in value:
        return "technology"
    if "market" in value or "finance" in value or "econom" in value:
        return "finance"
    if "business" in value or "compan" in value or "venture" in value or "startup" in value:
        return "business"
    if "world" in value or "international" in value:
        return "world"
    return "general"


def feed_status(
    domain: str,
    url: str,
    valid_feed_urls: set[str],
    invalid_feed_urls: set[str],
) -> str:
    if url in valid_feed_urls:
        return "verified"
    if url in invalid_feed_urls:
        return "disabled"
    if domain == "ft.com" and url == "https://www.ft.com/artificial-intelligence?format=rss":
        return "verified"
    return "candidate"


def feed_urls(site: dict) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for url in list(site.get("rss_urls") or []) + ([site["rss_url"]] if site.get("rss_url") else []):
        normalized = str(url or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            values.append(normalized)
    return values


OFFICIAL_SUPPLEMENTAL_FEEDS = {
    "ft.com": (
        "https://www.ft.com/artificial-intelligence?format=rss",
    ),
}


def build_registry(
    source_path: Path,
    valid_feed_urls: set[str] | None = None,
    invalid_feed_urls: set[str] | None = None,
) -> dict:
    raw = yaml.safe_load(source_path.read_text(encoding="utf-8")) or {}
    valid_feed_urls = valid_feed_urls or set()
    invalid_feed_urls = invalid_feed_urls or set()
    sources = []
    for site in raw.get("sites") or []:
        domain = str(site.get("domain") or "").strip().casefold()
        if not domain:
            continue
        urls = feed_urls(site)
        for url in OFFICIAL_SUPPLEMENTAL_FEEDS.get(domain, ()):
            if url not in urls:
                urls.append(url)
        sources.append(
            {
                "domain": domain,
                "name": str(site.get("name") or domain),
                "focus": list(FOCUS_BY_DOMAIN.get(domain, ("general",))),
                "feeds": [
                    {
                        "url": url,
                        "scope": infer_scope(url),
                        "status": feed_status(
                            domain,
                            url,
                            valid_feed_urls,
                            invalid_feed_urls,
                        ),
                    }
                    for url in urls
                ],
            }
        )
    return {
        "schema_version": 1,
        "purpose": "Final 29-source configuration migrated from news-scraper-final current working registry; only verified feeds may be used automatically.",
        "sources": sources,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--health-report",
        type=Path,
        default=None,
        help="Optional pac feeds health JSON; valid Feed URLs become verified.",
    )
    args = parser.parse_args()

    valid_feed_urls: set[str] = set()
    invalid_feed_urls: set[str] = set()
    if args.health_report:
        health = json.loads(args.health_report.read_text(encoding="utf-8"))
        valid_feed_urls = {
            feed["feed_url"]
            for source in health.get("sources", [])
            for feed in source.get("feeds", [])
            if feed.get("kind") == "valid_feed"
        }
        invalid_feed_urls = {
            feed["feed_url"]
            for source in health.get("sources", [])
            for feed in source.get("feeds", [])
            if feed.get("kind") != "valid_feed"
        }
    registry = build_registry(args.input, valid_feed_urls, invalid_feed_urls)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"sources={len(registry['sources'])} feeds={sum(len(source['feeds']) for source in registry['sources'])}")


if __name__ == "__main__":
    main()
