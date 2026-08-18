import json
import pytest

from bpc_fetch.browser import (
    ProxySettings,
    _domain_env_key,
    _parse_proxy_mapping,
    _proxy_from_mapping,
    _normalize_proxy,
    resolve_proxy,
)
from bpc_fetch.strategy import (
    _parse_wayback_snapshot_url,
    _format_reader_gateway_url,
    _reader_gateway_template,
    _AdaptiveHttpClient,
    _curl_cffi_enabled,
    MultiGatewayArchiveHandler,
)
from bpc_fetch.sites import SiteStrategy


def test_wayback_snapshot_url_parsing():
    payload = json.dumps({
        "archived_snapshots": {
            "closest": {
                "available": True,
                "url": "http://web.archive.org/web/20260817000000/https://example.com/news",
                "timestamp": "20260817000000",
                "status": "200"
            }
        }
    })
    url = _parse_wayback_snapshot_url(payload)
    assert url == "http://web.archive.org/web/20260817000000/https://example.com/news"


def test_wayback_snapshot_url_empty_when_unavailable():
    payload = json.dumps({"archived_snapshots": {}})
    assert _parse_wayback_snapshot_url(payload) == ""


def test_reader_gateway_url_formatting():
    template = "https://r.jina.ai/{url}"
    url = _format_reader_gateway_url(template, "https://example.com/article?id=1")
    assert url == "https://r.jina.ai/https://example.com/article?id=1"

    template_encoded = "https://reader.example.com/api?target={url_encoded}"
    url_encoded = _format_reader_gateway_url(template_encoded, "https://example.com/article?id=1")
    assert "https%3A%2F%2Fexample.com%2Farticle%3Fid%3D1" in url_encoded


def test_domain_proxy_resolution(monkeypatch):
    monkeypatch.setenv("PAC_PROXY_WSJ_COM", "http://wsj-proxy:8080")
    proxy = resolve_proxy("https://www.wsj.com/articles/test", None)
    assert proxy is not None
    assert proxy.server == "http://wsj-proxy:8080"


def test_proxy_mapping_parser():
    raw = "economist.com=http://proxy1:8080;ft.com=http://proxy2:8080"
    mapping = _parse_proxy_mapping(raw)
    assert mapping["economist.com"] == "http://proxy1:8080"
    assert mapping["ft.com"] == "http://proxy2:8080"


def test_curl_cffi_switch_env(monkeypatch):
    monkeypatch.setenv("PAC_CURL_CFFI", "off")
    assert _curl_cffi_enabled() is False

    monkeypatch.setenv("PAC_CURL_CFFI", "auto")
    assert _curl_cffi_enabled() is True


def test_http_proxy_pool_rotates_on_transport_error(monkeypatch):
    import asyncio
    from bpc_fetch import browser, strategy

    p1 = browser.ProxySettings("http://proxy1:8080")
    p2 = browser.ProxySettings("http://proxy2:8080")
    attempts = []

    monkeypatch.setattr(browser, "resolve_proxy_candidates", lambda *a, **k: [p1, p2])

    async def fake_request_once(client, url, *, headers, timeout, strategy, proxy_override):
        attempts.append(proxy_override.server)
        if proxy_override == p1:
            raise OSError("proxy connect failed")
        return type(
            "Response",
            (),
            {"status_code": 200, "text": "<html>ok</html>", "headers": {}, "url": url},
        )()

    monkeypatch.setattr(strategy, "_request_once", fake_request_once)
    monkeypatch.setattr(strategy, "assert_public_url", lambda url: None)

    async def exercise():
        client = strategy._AdaptiveHttpClient()
        try:
            return await strategy.fetch_page("https://example.com", client=client)
        finally:
            await client.aclose()

    body, status = asyncio.run(exercise())
    assert status == 200
    assert "ok" in body
    assert attempts == ["http://proxy1:8080", "http://proxy2:8080"]
