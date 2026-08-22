import asyncio

import httpx
import pytest

from bpc_fetch import discover
from bpc_fetch.source_policy import DiscoveryPolicy
from bpc_fetch.source_registry import CuratedSource, FeedSpec
from bpc_fetch.discover import (
    _extract_rss_links_from_html,
    _parse_rss,
    _parse_sitemap,
    _safe_get,
    discover_articles,
)

SAMPLE_RSS_2 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Sample Feed</title>
    <link>https://example.com</link>
    <item>
      <title>Article 1</title>
      <link>https://example.com/article-1</link>
      <pubDate>Mon, 17 Aug 2026 12:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Article 2</title>
      <link>https://example.com/article-2</link>
      <pubDate>Mon, 17 Aug 2026 13:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

SAMPLE_ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Sample Atom</title>
  <entry>
    <title>Atom Article 1</title>
    <link href="https://example.com/atom-1"/>
    <updated>2026-08-17T12:00:00Z</updated>
  </entry>
</feed>
"""

SAMPLE_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://example.com/sitemap-article-1</loc>
    <lastmod>2026-08-17</lastmod>
  </url>
</urlset>
"""

SAMPLE_HTML_WITH_RSS = """<!DOCTYPE html>
<html>
<head>
  <link rel="alternate" type="application/rss+xml" title="RSS" href="/feed.xml">
</head>
<body>Hello</body>
</html>
"""


def test_parse_rss_2():
    articles = _parse_rss(SAMPLE_RSS_2, limit=10)
    assert len(articles) == 2
    assert articles[0]["title"] == "Article 1"
    assert articles[0]["url"] == "https://example.com/article-1"
    assert articles[1]["title"] == "Article 2"


def test_parse_atom():
    articles = _parse_rss(SAMPLE_ATOM, limit=10)
    assert len(articles) == 1
    assert articles[0]["title"] == "Atom Article 1"
    assert articles[0]["url"] == "https://example.com/atom-1"


def test_parse_sitemap():
    articles = _parse_sitemap(SAMPLE_SITEMAP, limit=10)
    assert len(articles) == 1
    assert articles[0]["url"] == "https://example.com/sitemap-article-1"


def test_extract_rss_links():
    links = _extract_rss_links_from_html(SAMPLE_HTML_WITH_RSS, "https://example.com")
    assert links == ["https://example.com/feed.xml"]


def test_safe_get_rejects_private_redirect_before_second_request():
    requested = []

    async def handler(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(Exception, match="private_ip"):
                await _safe_get(client, "https://8.8.8.8/start")

    asyncio.run(exercise())
    assert requested == ["https://8.8.8.8/start"]


def test_scoped_bing_request_contains_encoded_domain_query(monkeypatch):
    requested = []
    bing_rss = """<?xml version=\"1.0\"?><rss><channel><title>Bing</title>
    <item><title>AI article - WSJ</title><link>https://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fwww.wsj.com%2Farticle</link></item>
    </channel></rss>"""

    async def fake_safe_get(client, url, **kwargs):
        requested.append(url)
        return httpx.Response(200, text=bing_rss)

    monkeypatch.setattr(discover, "_safe_get", fake_safe_get)
    result = asyncio.run(discover_articles("wsj.com", search_query="AI"))

    assert result["source_type"] == "bing_news_rss"
    assert requested[0].startswith("https://www.bing.com/news/search?")
    assert "q=site%3Awsj.com%20AI" in requested[0]
    assert "setmkt=en-US" in requested[0]


def test_verified_official_feeds_aggregate_and_dedupe_before_bing_and_google(monkeypatch):
    first_feed = "https://feeds.example.com/main"
    second_feed = "https://feeds.example.com/technology"
    requested = []
    second_rss = SAMPLE_RSS_2.replace(
        "https://example.com/article-2",
        "https://example.com/article-1?duplicate=1",
    ).replace("Article 2", "Article 1 duplicate")

    async def fake_safe_get(client, url, **kwargs):
        requested.append(url)
        if url == first_feed:
            return httpx.Response(200, text=SAMPLE_RSS_2)
        if url == second_feed:
            return httpx.Response(200, text=second_rss)
        raise AssertionError(f"unexpected fallback request: {url}")

    source = CuratedSource(
        domain="example.com",
        name="Example",
        focus=("technology",),
        feeds=(
            FeedSpec(first_feed, "general", "verified"),
            FeedSpec(second_feed, "technology", "verified"),
        ),
    )
    policy = DiscoveryPolicy(
        domain="example.com",
        discovery_mode="all_verified_feeds",
        feed_urls=(first_feed, second_feed),
        focus_feed_urls=(second_feed,),
        fallbacks=("bing_site",),
        date_quality="present",
        coverage="all_selected_outlet_news",
        reason="test",
    )
    monkeypatch.setattr(discover, "discovery_policy_for_domain", lambda domain: policy)
    monkeypatch.setattr(discover, "source_for_domain", lambda domain: source)
    monkeypatch.setattr(discover, "_safe_get", fake_safe_get)

    result = asyncio.run(discover_articles("example.com"))

    assert result["source_type"] == "official_feeds"
    assert result["source_urls"] == [first_feed, second_feed]
    assert result["count"] == 2
    assert requested == [first_feed, second_feed]
    assert all(article["discovery_source"] == "official_feed" for article in result["articles"])


def test_configured_subdomain_uses_its_own_feed_policy(monkeypatch):
    feed_url = "https://asia.nikkei.com/rss/feed/nar"
    source = CuratedSource(
        domain="asia.nikkei.com",
        name="Nikkei Asia",
        focus=("finance", "technology"),
        feeds=(FeedSpec(feed_url, "general", "verified"),),
    )
    policy = DiscoveryPolicy(
        domain="asia.nikkei.com",
        discovery_mode="all_verified_feeds",
        feed_urls=(feed_url,),
        focus_feed_urls=(),
        fallbacks=("bing_site",),
        date_quality="missing",
        coverage="all_selected_outlet_news",
        reason="test",
    )

    async def fake_safe_get(client, url, **kwargs):
        assert url == feed_url
        return httpx.Response(200, text=SAMPLE_RSS_2.replace("example.com", "asia.nikkei.com"))

    monkeypatch.setattr(discover, "source_for_domain", lambda domain: source if domain == source.domain else None)
    monkeypatch.setattr(discover, "discovery_policy_for_domain", lambda domain: policy if domain == source.domain else None)
    monkeypatch.setattr(discover, "_safe_get", fake_safe_get)

    result = asyncio.run(discover_articles("asia.nikkei.com"))

    assert result["domain"] == "asia.nikkei.com"
    assert result["source_type"] == "official_feeds"


def test_article_sitemap_discovery_excludes_the_information_section_root(monkeypatch):
    sitemap_url = "https://www.theinformation.com/sitemap-articles.xml"
    sitemap = """<?xml version=\"1.0\"?><urlset>
    <url><loc>https://www.theinformation.com/articles</loc><lastmod>2026-08-22T00:00:00Z</lastmod></url>
    <url><loc>https://www.theinformation.com/articles/example-story</loc><lastmod>2026-08-22T01:00:00Z</lastmod></url>
    </urlset>"""
    source = CuratedSource(
        domain="theinformation.com",
        name="The Information",
        focus=("technology",),
        feeds=(),
    )
    policy = DiscoveryPolicy(
        domain="theinformation.com",
        discovery_mode="sitemap_articles",
        feed_urls=(sitemap_url,),
        focus_feed_urls=(sitemap_url,),
        fallbacks=("bing_site",),
        date_quality="present",
        coverage="all_selected_outlet_news",
        reason="test",
    )

    async def fake_public_sitemap(url):
        assert url == sitemap_url
        return httpx.Response(200, text=sitemap)

    monkeypatch.setattr(discover, "source_for_domain", lambda domain: source if domain == source.domain else None)
    monkeypatch.setattr(discover, "discovery_policy_for_domain", lambda domain: policy if domain == source.domain else None)
    monkeypatch.setattr(discover, "_safe_get_public_sitemap", fake_public_sitemap)

    result = asyncio.run(discover_articles("theinformation.com"))

    assert result["source_type"] == "official_sitemap"
    assert [article["url"] for article in result["articles"]] == [
        "https://www.theinformation.com/articles/example-story"
    ]
    assert result["articles"][0]["published"] == "2026-08-22T01:00:00Z"


def test_ai_query_uses_matching_feed_scope_before_bing(monkeypatch):
    general_feed = "https://feeds.example.com/main"
    ai_feed = "https://feeds.example.com/ai"
    requested = []

    async def fake_safe_get(client, url, **kwargs):
        requested.append(url)
        if url == ai_feed:
            return httpx.Response(200, text=SAMPLE_RSS_2)
        raise AssertionError(f"unexpected feed or fallback request: {url}")

    source = CuratedSource(
        domain="example.com",
        name="Example",
        focus=("technology",),
        feeds=(
            FeedSpec(general_feed, "general", "verified"),
            FeedSpec(ai_feed, "artificial-intelligence", "verified"),
        ),
    )
    policy = DiscoveryPolicy(
        domain="example.com",
        discovery_mode="all_verified_feeds",
        feed_urls=(general_feed, ai_feed),
        focus_feed_urls=(ai_feed,),
        fallbacks=("bing_site",),
        date_quality="present",
        coverage="all_selected_outlet_news",
        reason="test",
    )
    monkeypatch.setattr(discover, "discovery_policy_for_domain", lambda domain: policy)
    monkeypatch.setattr(discover, "source_for_domain", lambda domain: source)
    monkeypatch.setattr(discover, "_safe_get", fake_safe_get)

    result = asyncio.run(discover_articles("example.com", search_query="AI"))

    assert result["source_type"] == "official_feeds"
    assert requested == [ai_feed]


def test_reuters_daily_sitemap_keeps_only_finance_and_technology_articles():
    links = [
        "https://www.reuters.com/technology/example-ai-story-2026-08-22/",
        "https://www.reuters.com/business/energy/example-energy-story-2026-08-22/",
        "https://www.reuters.com/world/example-world-story-2026-08-22/",
        "https://www.reuters.com/technology/",
        "https://example.com/technology/not-reuters-2026-08-22/",
    ]
    markdown = "[AI story](https://www.reuters.com/technology/example-ai-story-2026-08-22/)"

    articles = discover._reuters_article_links(markdown, links, "2026-08-22")

    assert [article["url"] for article in articles] == [
        "https://www.reuters.com/technology/example-ai-story-2026-08-22",
        "https://www.reuters.com/business/energy/example-energy-story-2026-08-22",
    ]
    assert articles[0]["title"] == "AI story"


def test_reuters_sitemap_discovery_wins_before_bing(monkeypatch):
    source = CuratedSource("reuters.com", "Reuters", ("finance", "technology"), ())
    policy = DiscoveryPolicy(
        domain="reuters.com",
        discovery_mode="firecrawl_daily_sitemap",
        feed_urls=(),
        focus_feed_urls=(),
        fallbacks=("bing_site",),
        date_quality="utc_sitemap_date",
        coverage="finance_technology_daily_sitemap",
        reason="test",
    )

    async def fake_sitemap(client):
        return [{
            "title": "Reuters tech story",
            "url": "https://www.reuters.com/technology/example-2026-08-22",
            "published": "2026-08-22",
            "discovery_source": "official_daily_sitemap",
        }]

    async def unexpected_http(*args, **kwargs):
        raise AssertionError("Bing/Google must not run after sitemap success")

    monkeypatch.setattr(discover, "source_for_domain", lambda domain: source if domain == source.domain else None)
    monkeypatch.setattr(discover, "discovery_policy_for_domain", lambda domain: policy if domain == source.domain else None)
    monkeypatch.setattr(discover, "_discover_reuters_daily_sitemap", fake_sitemap)
    monkeypatch.setattr(discover, "_safe_get", unexpected_http)

    result = asyncio.run(discover_articles("reuters.com"))

    assert result["source_type"] == "official_daily_sitemap"
    assert result["articles"][0]["url"] == "https://www.reuters.com/technology/example-2026-08-22"


def test_google_news_returns_title_signals_without_fetchable_urls(monkeypatch):
    google_rss = """<?xml version=\"1.0\"?><rss><channel><title>Google</title>
    <item><title>AI signal</title><link>https://news.google.com/rss/articles/token</link><pubDate>Fri, 22 Aug 2026 08:00:00 GMT</pubDate></item>
    </channel></rss>"""

    async def fake_safe_get(client, url, **kwargs):
        if "news.google.com" in url:
            return httpx.Response(200, text=google_rss)
        return httpx.Response(404, text="")

    monkeypatch.setattr(discover, "discovery_policy_for_domain", lambda domain: None)
    monkeypatch.setattr(discover, "source_for_domain", lambda domain: None)
    monkeypatch.setattr(discover, "_safe_get", fake_safe_get)

    result = asyncio.run(discover_articles("no-feed.example", search_query="AI"))

    assert result["articles"] == []
    assert result["title_signals"] == [{
        "title": "AI signal",
        "published": "Fri, 22 Aug 2026 08:00:00 GMT",
        "url": "",
        "source": "google_news_title_only",
        "kind": "title_signal",
    }]
    assert "news.google.com" not in result["next_command"]


def test_candidate_general_feed_is_not_used_for_an_ai_query():
    assert discover._feed_scope_matches_query("general", "AI") is False
    assert discover._feed_scope_matches_query("artificial-intelligence", "AI") is True
    assert discover._feed_scope_matches_query("markets", "AI") is False


def test_safe_get_rejects_oversized_body():
    async def handler(request):
        return httpx.Response(200, content=b"12345")

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(RuntimeError, match="too large"):
                await _safe_get(client, "https://8.8.8.8/feed", max_bytes=4)

    asyncio.run(exercise())
