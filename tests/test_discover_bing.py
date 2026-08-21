"""Bing News RSS discovery and publisher URL extraction."""

import pytest

from bpc_fetch.discover import _extract_bing_publisher_urls


def _item(link: str) -> dict[str, str]:
    return {"title": "Some article", "url": link}


def test_bing_apiclick_link_is_rewritten_to_publisher_url():
    encoded = "https%3a%2f%2farstechnica.com%2ftech-policy%2f2026%2f08%2farticle%2f"
    items = [
        _item(
            "http://www.bing.com/news/apiclick.aspx?ref=FexRss&aid=&tid=abc"
            f"&url={encoded}&p=&delv=3"
        )
    ]
    out = _extract_bing_publisher_urls(items)
    assert len(out) == 1
    assert out[0]["url"] == "https://arstechnica.com/tech-policy/2026/08/article/"


def test_bing_internal_links_are_dropped():
    items = [
        _item("https://www.bing.com/news/search?q=spirit&format=rss"),
        _item("https://www.msn.com/en-us/money/general/story/ar-AA1"),
    ]
    out = _extract_bing_publisher_urls(items)
    # msn.com is a publisher, not a bing internal page; only bing.com is dropped
    assert all(not u["url"].startswith("https://www.bing.com/") for u in out)


def test_bing_items_without_url_param_are_dropped():
    items = [
        _item("http://www.bing.com/news/apiclick.aspx?ref=FexRss&aid=&tid=xyz&p=")
    ]
    assert _extract_bing_publisher_urls(items) == []


def test_empty_input_passes_through():
    assert _extract_bing_publisher_urls([]) == []
