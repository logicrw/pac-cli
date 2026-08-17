import pytest
from bpc_fetch.discover import _parse_rss, _parse_sitemap, _extract_rss_links_from_html

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
