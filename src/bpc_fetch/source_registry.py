"""Read-only curated source and feed registry for PAC discovery.

The registry is intentionally separate from retrieval policies. Entries start
as ``candidate`` and become ``verified`` only after outlet-specific validation;
loading this module never causes network I/O or enables a feed automatically.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Literal


SourceStatus = Literal["candidate", "verified", "disabled"]


@dataclass(frozen=True)
class FeedSpec:
    url: str
    scope: str
    status: SourceStatus


@dataclass(frozen=True)
class CuratedSource:
    domain: str
    name: str
    focus: tuple[str, ...]
    feeds: tuple[FeedSpec, ...]


@lru_cache(maxsize=1)
def load_curated_sources() -> tuple[CuratedSource, ...]:
    """Load the bundled source registry without contacting any media outlet."""
    path = files("bpc_fetch").joinpath("data/curated_sources.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    sources: list[CuratedSource] = []
    for raw_source in payload.get("sources", []):
        feeds = tuple(
            FeedSpec(
                url=str(raw_feed["url"]),
                scope=str(raw_feed.get("scope") or "general"),
                status=str(raw_feed.get("status") or "candidate"),
            )
            for raw_feed in raw_source.get("feeds", [])
            if raw_feed.get("url")
        )
        sources.append(
            CuratedSource(
                domain=str(raw_source["domain"]),
                name=str(raw_source["name"]),
                focus=tuple(str(value) for value in raw_source.get("focus", [])),
                feeds=feeds,
            )
        )
    return tuple(sources)


def source_for_domain(domain: str) -> CuratedSource | None:
    """Return one curated source by registrable domain."""
    normalized = domain.casefold().removeprefix("www.").rstrip(".")
    return next((source for source in load_curated_sources() if source.domain == normalized), None)


def verified_feeds_for_domain(domain: str) -> tuple[FeedSpec, ...]:
    """Return only feeds that passed outlet-specific validation."""
    source = source_for_domain(domain)
    if source is None:
        return ()
    return tuple(feed for feed in source.feeds if feed.status == "verified")
