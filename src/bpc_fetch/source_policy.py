"""Read-only per-outlet discovery policies for PAC's final source list."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files


@dataclass(frozen=True)
class DiscoveryPolicy:
    domain: str
    discovery_mode: str
    feed_urls: tuple[str, ...]
    focus_feed_urls: tuple[str, ...]
    fallbacks: tuple[str, ...]
    date_quality: str
    coverage: str
    reason: str


@lru_cache(maxsize=1)
def load_discovery_policies() -> tuple[DiscoveryPolicy, ...]:
    path = files("bpc_fetch").joinpath("data/source_discovery_policies.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        DiscoveryPolicy(
            domain=str(raw["domain"]),
            discovery_mode=str(raw["discovery_mode"]),
            feed_urls=tuple(str(url) for url in raw.get("feed_urls", [])),
            focus_feed_urls=tuple(str(url) for url in raw.get("focus_feed_urls", [])),
            fallbacks=tuple(str(value) for value in raw.get("fallbacks", [])),
            date_quality=str(raw["date_quality"]),
            coverage=str(raw["coverage"]),
            reason=str(raw["reason"]),
        )
        for raw in payload.get("policies", [])
    )


def discovery_policy_for_domain(domain: str) -> DiscoveryPolicy | None:
    normalized = domain.casefold().removeprefix("www.").rstrip(".")
    return next((policy for policy in load_discovery_policies() if policy.domain == normalized), None)
