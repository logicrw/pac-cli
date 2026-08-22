"""Credential-free health inspection for registered RSS and Atom feeds."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import xml.etree.ElementTree as ET
from typing import Awaitable, Callable, Iterable
from urllib.parse import urlparse

import httpx

from .source_registry import CuratedSource, load_curated_sources


@dataclass(frozen=True)
class FeedHealth:
    source_domain: str
    feed_url: str
    final_url: str
    status: int
    kind: str
    reason: str
    entry_count: int
    publisher_url_count: int
    dated_entry_count: int
    article_urls: tuple[str, ...]


@dataclass(frozen=True)
class SourceFeedHealth:
    domain: str
    feed_count: int
    valid_feed_count: int
    unique_publisher_url_count: int
    duplicate_publisher_url_count: int
    feeds: tuple[FeedHealth, ...]


def inspect_feed_document(
    source_domain: str,
    feed_url: str,
    status: int,
    final_url: str,
    content_type: str,
    body: str,
) -> FeedHealth:
    """Inspect one already-fetched feed response without network access."""
    if status < 200 or status >= 300:
        return _empty_health(source_domain, feed_url, final_url, status, "http_error", f"http_{status}")
    if not _looks_like_feed(content_type, body):
        return _empty_health(source_domain, feed_url, final_url, status, "non_feed", "not_xml_or_atom")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return _empty_health(source_domain, feed_url, final_url, status, "invalid_xml", "xml_parse_error")

    entries = [element for element in root.iter() if _local_name(element.tag) in {"item", "entry"}]
    urls: list[str] = []
    dated_entries = 0
    for entry in entries:
        url = _publisher_url_from_entry(entry, source_domain)
        if url:
            urls.append(url)
        if _entry_has_date(entry):
            dated_entries += 1

    unique_urls = tuple(dict.fromkeys(urls))
    return FeedHealth(
        source_domain=source_domain,
        feed_url=feed_url,
        final_url=final_url,
        status=status,
        kind="valid_feed",
        reason="",
        entry_count=len(entries),
        publisher_url_count=len(unique_urls),
        dated_entry_count=dated_entries,
        article_urls=unique_urls,
    )


def summarize_source_health(source: CuratedSource, feeds: tuple[FeedHealth, ...]) -> SourceFeedHealth:
    """Summarize valid feeds and overlap for one publisher."""
    valid_feeds = tuple(feed for feed in feeds if feed.kind == "valid_feed")
    url_occurrences = [url for feed in valid_feeds for url in feed.article_urls]
    unique_urls = set(url_occurrences)
    return SourceFeedHealth(
        domain=source.domain,
        feed_count=len(feeds),
        valid_feed_count=len(valid_feeds),
        unique_publisher_url_count=len(unique_urls),
        duplicate_publisher_url_count=len(url_occurrences) - len(unique_urls),
        feeds=feeds,
    )


FeedFetcher = Callable[[str], Awaitable[tuple[int, str, str, str]]]


async def validate_registered_feeds(
    domains: Iterable[str] | None = None,
    *,
    concurrency: int = 4,
    fetcher: FeedFetcher | None = None,
) -> tuple[SourceFeedHealth, ...]:
    """Probe registered feeds without credentials or article-page fetches.

    ``fetcher`` returns ``(status, final_url, content_type, body)`` and is
    injectable for deterministic tests. The default client sends no cookies,
    authorization headers, or browser identity.
    """
    requested = {domain.casefold().removeprefix("www.") for domain in domains or ()}
    sources = tuple(
        source for source in load_curated_sources()
        if not requested or source.domain in requested
    )
    if not sources:
        return ()
    if fetcher is not None:
        return tuple([await _validate_source(source, fetcher) for source in sources])

    limit = max(1, int(concurrency))
    semaphore = asyncio.Semaphore(limit)
    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": "PAC feed health/1.0 (+https://github.com/logicrw/pac-cli)"},
        follow_redirects=True,
    ) as client:
        async def fetch_public_feed(url: str) -> tuple[int, str, str, str]:
            async with semaphore:
                try:
                    response = await client.get(url)
                    return (
                        int(response.status_code),
                        str(response.url),
                        response.headers.get("content-type", ""),
                        response.text,
                    )
                except httpx.HTTPError:
                    return (0, url, "", "")

        return tuple([await _validate_source(source, fetch_public_feed) for source in sources])


async def _validate_source(source: CuratedSource, fetcher: FeedFetcher) -> SourceFeedHealth:
    async def inspect(feed_url: str) -> FeedHealth:
        status, final_url, content_type, body = await fetcher(feed_url)
        return inspect_feed_document(
            source.domain,
            feed_url,
            status,
            final_url,
            content_type,
            body,
        )

    health = tuple(await asyncio.gather(*(inspect(feed.url) for feed in source.feeds)))
    return summarize_source_health(source, health)


def health_report_as_dict(reports: Iterable[SourceFeedHealth]) -> dict:
    """Serialize a health report without article text or credential material."""
    values = tuple(reports)
    return {
        "source_count": len(values),
        "feed_count": sum(report.feed_count for report in values),
        "valid_feed_count": sum(report.valid_feed_count for report in values),
        "sources": [asdict(report) for report in values],
    }


def _empty_health(
    source_domain: str,
    feed_url: str,
    final_url: str,
    status: int,
    kind: str,
    reason: str,
) -> FeedHealth:
    return FeedHealth(
        source_domain=source_domain,
        feed_url=feed_url,
        final_url=final_url,
        status=status,
        kind=kind,
        reason=reason,
        entry_count=0,
        publisher_url_count=0,
        dated_entry_count=0,
        article_urls=(),
    )


def _looks_like_feed(content_type: str, body: str) -> bool:
    prefix = (body or "").lstrip()[:512].casefold()
    type_hint = (content_type or "").casefold()
    return (
        "xml" in type_hint
        or "rss" in type_hint
        or prefix.startswith("<?xml")
        or prefix.startswith("<rss")
        or prefix.startswith("<feed")
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _publisher_url_from_entry(entry: ET.Element, source_domain: str) -> str:
    for child in entry.iter():
        if _local_name(child.tag) != "link":
            continue
        value = (child.attrib.get("href") or child.text or "").strip()
        host = (urlparse(value).hostname or "").casefold().removeprefix("www.")
        if value.startswith("http") and (host == source_domain or host.endswith(f".{source_domain}")):
            return value
    return ""


def _entry_has_date(entry: ET.Element) -> bool:
    date_tags = {"pubdate", "published", "updated", "date", "lastmod"}
    return any(
        _local_name(child.tag) in date_tags and bool((child.text or "").strip())
        for child in entry.iter()
    )
