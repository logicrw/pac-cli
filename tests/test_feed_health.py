"""Feed health inspection contracts."""

import asyncio

from bpc_fetch.feed_health import (
    health_report_as_dict,
    inspect_feed_document,
    summarize_source_health,
    validate_registered_feeds,
)
from bpc_fetch.source_registry import CuratedSource, FeedSpec


RSS = """<?xml version="1.0"?>
<rss><channel><title>Example</title>
<item><title>One</title><link>https://www.example.com/articles/one</link><pubDate>Fri, 22 Aug 2026 08:00:00 GMT</pubDate></item>
<item><title>Two</title><link>https://www.example.com/articles/two</link><pubDate>Fri, 22 Aug 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_valid_rss_reports_entries_publisher_urls_and_dates():
    health = inspect_feed_document(
        source_domain="example.com",
        feed_url="https://feeds.example.com/rss.xml",
        status=200,
        final_url="https://feeds.example.com/rss.xml",
        content_type="application/rss+xml",
        body=RSS,
    )

    assert health.kind == "valid_feed"
    assert health.entry_count == 2
    assert health.publisher_url_count == 2
    assert health.dated_entry_count == 2
    assert health.article_urls == (
        "https://www.example.com/articles/one",
        "https://www.example.com/articles/two",
    )


def test_http_403_is_not_treated_as_a_feed():
    health = inspect_feed_document(
        source_domain="theinformation.com",
        feed_url="https://www.theinformation.com/feed",
        status=403,
        final_url="https://www.theinformation.com/feed",
        content_type="text/html",
        body="<html>Subscribe</html>",
    )

    assert health.kind == "http_error"
    assert health.reason == "http_403"
    assert health.entry_count == 0


def test_malformed_or_html_document_is_not_treated_as_feed():
    health = inspect_feed_document(
        source_domain="example.com",
        feed_url="https://example.com/feed",
        status=200,
        final_url="https://example.com/feed",
        content_type="text/html",
        body="<html><body>not a feed</body></html>",
    )

    assert health.kind == "non_feed"
    assert health.entry_count == 0


def test_source_summary_counts_duplicate_urls_across_feeds_once():
    source = CuratedSource(
        domain="example.com",
        name="Example",
        focus=("technology",),
        feeds=(
            FeedSpec("https://feeds.example.com/main", "general", "candidate"),
            FeedSpec("https://feeds.example.com/topic", "technology", "candidate"),
        ),
    )
    main = inspect_feed_document(
        source.domain, source.feeds[0].url, 200, source.feeds[0].url, "application/rss+xml", RSS
    )
    topic = inspect_feed_document(
        source.domain,
        source.feeds[1].url,
        200,
        source.feeds[1].url,
        "application/rss+xml",
        RSS.replace("articles/two", "articles/three"),
    )

    summary = summarize_source_health(source, (main, topic))

    assert summary.feed_count == 2
    assert summary.valid_feed_count == 2
    assert summary.unique_publisher_url_count == 3
    assert summary.duplicate_publisher_url_count == 1


def test_registered_feed_validation_uses_injected_public_fetcher(monkeypatch):
    source = CuratedSource(
        domain="example.com",
        name="Example",
        focus=("technology",),
        feeds=(FeedSpec("https://feeds.example.com/rss", "technology", "candidate"),),
    )

    async def fake_fetch(url):
        assert url == "https://feeds.example.com/rss"
        return (200, url, "application/rss+xml", RSS)

    monkeypatch.setattr("bpc_fetch.feed_health.load_curated_sources", lambda: (source,))
    reports = asyncio.run(validate_registered_feeds(fetcher=fake_fetch))
    report = health_report_as_dict(reports)

    assert report["source_count"] == 1
    assert report["valid_feed_count"] == 1
    assert report["sources"][0]["feeds"][0]["article_urls"] == (
        "https://www.example.com/articles/one",
        "https://www.example.com/articles/two",
    )
