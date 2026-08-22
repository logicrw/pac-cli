#!/usr/bin/env python3
"""Build explicit per-source retrieval policies for PAC's final 29 outlets."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


CLOUD_DISABLED = {"bloomberg.com", "ft.com", "theinformation.com"}
PAYWALL_EXPECTED = {"bloomberg.com", "ft.com", "theinformation.com", "barrons.com", "economist.com"}


def policy_for_domain(domain: str) -> dict:
    firecrawl_mode = "never" if domain in CLOUD_DISABLED else "on_bot_or_http_block"
    credential_mode = "vault_required_for_fulltext" if domain == "theinformation.com" else "vault_optional"
    representation = "yahoo_bloomberg_candidate" if domain == "bloomberg.com" else "publisher_page"
    failure_mode = "paywall_remaining" if domain in PAYWALL_EXPECTED else "honest_failure"
    return {
        "domain": domain,
        "browser_mode": "render_only",
        "credential_mode": credential_mode,
        "firecrawl_mode": firecrawl_mode,
        "firecrawl_failure_codes": ["BOT_CHALLENGE", "HTTP_BLOCKED"],
        "representation_mode": representation,
        "failure_mode": failure_mode,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    policy = {
        "schema_version": 1,
        "purpose": "Explicit retrieval policy for PAC final 29 selected outlets.",
        "policies": [policy_for_domain(source["domain"]) for source in registry.get("sources", [])],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"policies={len(policy['policies'])}")


if __name__ == "__main__":
    main()
