"""Lightweight article discovery via RSS, Sitemap, and Google News RSS."""
from __future__ import annotations

import re
import urllib.parse
from typing import Any
import xml.etree.ElementTree as ET

import httpx

from .sites import domain_from_url

COMMON_RSS_PATHS = (
    "/feed",
    "/rss",
    "/rss.xml",
    "/feed.xml",
    "/index.xml",
    "/news/feed",
    "/rss/news.xml",
)

COMMON_SITEMAP_PATHS = (
    "/sitemap.xml",
    "/news-sitemap.xml",
    "/sitemap_news.xml",
    "/sitemap-news.xml",
)

TIMEOUT = 15.0
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
}


async def discover_articles(
    target: str,
    *,
    limit: int = 20,
    search_query: str | None = None,
) -> dict[str, Any]:
    """Discover articles from a domain, RSS feed URL, or Google News search query."""
    articles: list[dict[str, str]] = []
    source_type = "unknown"
    source_url = target

    async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
        # Case 1: Search query across or within domain via Google News RSS
        if search_query or not target.startswith("http"):
            domain = target.replace("https://", "").replace("http://", "").split("/")[0]
            query = f"site:{domain} {search_query}".strip() if search_query else f"site:{domain}"
            encoded_query = urllib.parse.quote(query)
            gnews_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
            source_url = gnews_url
            try:
                resp = await client.get(gnews_url)
                if resp.status_code == 200:
                    articles = _parse_rss(resp.text, limit=limit)
                    if articles:
                        source_type = "google_news_rss"
            except Exception:
                pass

        # Case 2: Direct RSS URL or probing domain for RSS
        if not articles:
            if target.startswith("http://") or target.startswith("https://"):
                base_url = target.rstrip("/")
            else:
                base_url = f"https://{target}".rstrip("/")

            domain = domain_from_url(base_url)

            # Try direct target first
            try:
                resp = await client.get(base_url)
                if resp.status_code == 200:
                    content_type = resp.headers.get("content-type", "").lower()
                    if "xml" in content_type or "rss" in content_type or resp.text.strip().startswith("<?xml"):
                        articles = _parse_rss(resp.text, limit=limit) or _parse_sitemap(resp.text, limit=limit)
                        if articles:
                            source_type = "rss_feed"
                            source_url = base_url
                    else:
                        # Parse HTML for <link rel="alternate" type="application/rss+xml" ...>
                        rss_links = _extract_rss_links_from_html(resp.text, base_url)
                        for rss_url in rss_links:
                            try:
                                r_rss = await client.get(rss_url)
                                if r_rss.status_code == 200:
                                    parsed = _parse_rss(r_rss.text, limit=limit)
                                    if parsed:
                                        articles = parsed
                                        source_type = "rss_link"
                                        source_url = rss_url
                                        break
                            except Exception:
                                continue
            except Exception:
                pass

            # Probe common RSS paths if still empty
            if not articles:
                for path in COMMON_RSS_PATHS:
                    probe_url = urllib.parse.urljoin(base_url, path)
                    try:
                        resp = await client.get(probe_url)
                        if resp.status_code == 200:
                            parsed = _parse_rss(resp.text, limit=limit)
                            if parsed:
                                articles = parsed
                                source_type = "rss_probe"
                                source_url = probe_url
                                break
                    except Exception:
                        continue

            # Probe Sitemaps if still empty
            if not articles:
                for path in COMMON_SITEMAP_PATHS:
                    probe_url = urllib.parse.urljoin(base_url, path)
                    try:
                        resp = await client.get(probe_url)
                        if resp.status_code == 200:
                            parsed = _parse_sitemap(resp.text, limit=limit)
                            if parsed:
                                articles = parsed
                                source_type = "sitemap"
                                source_url = probe_url
                                break
                    except Exception:
                        continue

    domain = domain_from_url(target if target.startswith("http") else f"https://{target}")
    urls = [a["url"] for a in articles if a.get("url")]
    next_cmd = f"pac batch {' '.join(urls[:3])} --compact" if urls else "pac fetch <url>"

    return {
        "ok": len(articles) > 0,
        "domain": domain,
        "source_type": source_type,
        "source_url": source_url,
        "count": len(articles),
        "articles": articles[:limit],
        "next_command": next_cmd,
    }


def _parse_rss(xml_text: str, limit: int = 20) -> list[dict[str, str]]:
    """Parse RSS 2.0 or Atom feed."""
    articles = []
    try:
        clean_xml = re.sub(r' xmlns(:[a-zA-Z0-9]+)?="[^"]+"', '', xml_text, count=5)
        root = ET.fromstring(clean_xml)

        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            if link and link.startswith("http"):
                articles.append({"title": title, "url": link, "published": pub_date})
            if len(articles) >= limit:
                break

        if not articles:
            for entry in root.findall(".//entry"):
                title = (entry.findtext("title") or "").strip()
                link_elem = entry.find("link")
                link = link_elem.attrib.get("href", "") if link_elem is not None else ""
                published = (entry.findtext("published") or entry.findtext("updated") or "").strip()
                if link and link.startswith("http"):
                    articles.append({"title": title, "url": link, "published": published})
                if len(articles) >= limit:
                    break
    except Exception:
        pass
    return articles


def _parse_sitemap(xml_text: str, limit: int = 20) -> list[dict[str, str]]:
    """Parse XML sitemap for url locs."""
    articles = []
    try:
        clean_xml = re.sub(r' xmlns(:[a-zA-Z0-9]+)?="[^"]+"', '', xml_text, count=5)
        root = ET.fromstring(clean_xml)
        for url_elem in root.findall(".//url"):
            loc = (url_elem.findtext("loc") or "").strip()
            lastmod = (url_elem.findtext("lastmod") or "").strip()
            if loc and loc.startswith("http"):
                articles.append({"title": "", "url": loc, "published": lastmod})
            if len(articles) >= limit:
                break
    except Exception:
        pass
    return articles


def _extract_rss_links_from_html(html_text: str, base_url: str) -> list[str]:
    """Find RSS/Atom link tags inside HTML."""
    links = []
    matches = re.findall(
        r'<link[^>]+type=["\']application/(?:rss\+xml|atom\+xml)["\'][^>]*>',
        html_text,
        re.IGNORECASE,
    )
    for m in matches:
        href_match = re.search(r'href=["\']([^"\']+)["\']', m, re.IGNORECASE)
        if href_match:
            links.append(urllib.parse.urljoin(base_url, href_match.group(1)))
    return links
