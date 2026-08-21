import asyncio
import base64
import json
import pytest

from bpc_fetch.discover import (
    is_google_news_url,
    decode_google_news_url,
    decode_google_news_url_local,
    _urlsafe_b64decode,
    _url_candidates_from_payload,
)
from bpc_fetch.browser import (
    resolve_proxy,
    resolve_proxy_candidates,
    get_proxy_circuit_breaker,
    _normalize_proxy,
)
from bpc_fetch.rules.sync import (
    swr_nonblocking_mode,
    _SWR_NONBLOCKING,
)
from bpc_fetch.sites import SiteStrategy


def test_is_google_news_article_url():
    assert is_google_news_url("https://news.google.com/rss/articles/CBMi12345?oc=5") is True
    assert is_google_news_url("https://example.com/article/123") is False


def test_decode_google_news_payload_plaintext_fallback():
    raw = b"\x08\x01\x12'https://www.economist.com/finance/123"
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    fake_url = f"https://news.google.com/rss/articles/{encoded}"
    decoded = decode_google_news_url_local(fake_url)
    assert decoded == "https://www.economist.com/finance/123"


def test_swr_nonblocking_context():
    assert _SWR_NONBLOCKING.get() is False
    with swr_nonblocking_mode():
        assert _SWR_NONBLOCKING.get() is True
    assert _SWR_NONBLOCKING.get() is False


def test_proxy_pool_and_circuit_breaker(monkeypatch):
    monkeypatch.setenv("PAC_PROXIES", "http://proxy1:8080,http://proxy2:8080")
    cb = get_proxy_circuit_breaker()
    candidates = resolve_proxy_candidates("https://www.wsj.com/article", None)
    assert len(candidates) >= 2
    assert candidates[0] is not None and candidates[0].server == "http://proxy1:8080"
    assert candidates[1] is not None and candidates[1].server == "http://proxy2:8080"

    # Mark proxy1 as failed
    cb.mark_failure(candidates[0], "BOT_CHALLENGE")
    active_proxy = resolve_proxy("https://www.wsj.com/article", None)
    assert active_proxy is not None
    assert active_proxy.server == "http://proxy2:8080"

    # Mark proxy1 as restored
    cb.mark_success(candidates[0])
    active_proxy2 = resolve_proxy("https://www.wsj.com/article", None)
    assert active_proxy2 is not None
    assert active_proxy2.server == "http://proxy1:8080"


def test_malformed_proxy_port_is_ignored():
    from bpc_fetch.browser import _normalize_proxy

    assert _normalize_proxy("http://proxy.example:not-a-port") is None


def test_browser_proxy_pool_rotates_on_network_failure(monkeypatch):
    from bpc_fetch import browser

    p1 = browser.ProxySettings("http://proxy1:8080")
    p2 = browser.ProxySettings("http://proxy2:8080")
    calls = []

    monkeypatch.setattr(browser, "resolve_proxy_candidates", lambda *a, **k: [p1, p2])

    async def fake_single(url, strategy, pool=None, *, proxy_override, cookie_header=""):
        calls.append(proxy_override.server)
        if proxy_override == p1:
            return browser.BrowserResult(ok=False, error_code="NETWORK", error_msg="connect failed")
        return browser.BrowserResult(ok=True, html="ok", status=200)

    monkeypatch.setattr(browser, "_fetch_for_strategy_single", fake_single)
    result = asyncio.run(browser.fetch_for_strategy("https://example.com", None))

    assert result.ok is True
    assert calls == ["http://proxy1:8080", "http://proxy2:8080"]
    assert result.proxy_attempts == 2
