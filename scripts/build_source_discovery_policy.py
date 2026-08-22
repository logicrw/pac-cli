#!/usr/bin/env python3
"""Build static per-source discovery policies from PAC feed health output."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def build_policy(registry_path: Path, health_path: Path) -> dict:
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    health = json.loads(health_path.read_text(encoding="utf-8"))
    health_by_domain = {source["domain"]: source for source in health.get("sources", [])}
    policies = []

    for source in registry.get("sources", []):
        domain = source["domain"]
        report = health_by_domain.get(domain, {})
        valid_feeds = [
            feed["feed_url"]
            for feed in report.get("feeds", [])
            if feed.get("kind") == "valid_feed"
        ]
        dated_entries = sum(
            int(feed.get("dated_entry_count") or 0)
            for feed in report.get("feeds", [])
            if feed.get("kind") == "valid_feed"
        )
        entry_count = sum(
            int(feed.get("entry_count") or 0)
            for feed in report.get("feeds", [])
            if feed.get("kind") == "valid_feed"
        )

        scope_by_url = {
            feed["url"]: feed.get("scope", "general")
            for feed in source.get("feeds", [])
        }
        focus_scopes = {
            "finance",
            "business",
            "technology",
            "technology-policy",
            "artificial-intelligence",
            "fintech",
            "security",
        }
        focus_feed_urls = [
            url for url in valid_feeds
            if scope_by_url.get(url, "general") in focus_scopes
        ]

        if valid_feeds:
            mode = "all_verified_feeds"
            reason = "public_feed_health_pass"
        elif domain == "theinformation.com":
            mode = "sitemap_articles"
            valid_feeds = ["https://www.theinformation.com/sitemap-articles.xml"]
            focus_feed_urls = list(valid_feeds)
            dated_entries = 1
            entry_count = 1
            reason = "public_article_sitemap_feed_http_403"
        else:
            mode = "bing_site"
            reason = "no_public_feed_configured"

        policies.append(
            {
                "domain": domain,
                "discovery_mode": mode,
                "feed_urls": valid_feeds,
                "focus_feed_urls": focus_feed_urls,
                "fallbacks": ["bing_site"],
                "date_quality": "present" if dated_entries else ("missing" if entry_count else "not_applicable"),
                "coverage": "all_selected_outlet_news" if valid_feeds else "search_index_fallback",
                "reason": reason,
            }
        )

    return {
        "schema_version": 1,
        "purpose": "Per-source discovery policy for PAC final 29 selected outlets.",
        "policies": policies,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--health", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    policy = build_policy(args.registry, args.health)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"policies={len(policy['policies'])}")


if __name__ == "__main__":
    main()
