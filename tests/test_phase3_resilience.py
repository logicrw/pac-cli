import asyncio
import json
import time

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
    Context,
    FetchOptions,
    MultiGatewayArchiveHandler,
    fetch_page,
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

    async def fake_request_once(client, url, *, headers, timeout, strategy, proxy_override, **kwargs):
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


def test_fetch_page_keeps_cookie_for_same_registrable_domain_redirect(monkeypatch):
    from bpc_fetch import strategy

    requests = []

    async def fake_request_once(client, url, *, headers, **kwargs):
        requests.append((url, dict(headers), kwargs["allow_strategy_cookies"]))
        if len(requests) == 1:
            return type("Response", (), {
                "status_code": 302,
                "text": "",
                "headers": {"location": "https://www.example.com/article"},
                "url": url,
            })()
        return type("Response", (), {
            "status_code": 200,
            "text": "<html>ok</html>",
            "headers": {},
            "url": url,
        })()

    monkeypatch.setattr(strategy, "_request_once", fake_request_once)
    monkeypatch.setattr(strategy, "assert_public_url", lambda url: None)

    assert asyncio.run(fetch_page(
        "https://example.com/start",
        client=object(),
        cookie_header="session=publisher-only",
    ))[1] == 200
    assert requests[0][1]["Cookie"] == "session=publisher-only"
    assert requests[1][1]["Cookie"] == "session=publisher-only"
    assert requests[0][2] is True
    assert requests[1][2] is True


def test_fetch_page_strips_cookie_on_cross_domain_redirect(monkeypatch):
    from bpc_fetch import strategy

    requests = []

    async def fake_request_once(client, url, *, headers, **kwargs):
        requests.append((url, dict(headers), kwargs["allow_strategy_cookies"]))
        if len(requests) == 1:
            return type("Response", (), {
                "status_code": 302,
                "text": "",
                "headers": {"location": "https://third-party.test/article"},
                "url": url,
            })()
        return type("Response", (), {
            "status_code": 200,
            "text": "<html>ok</html>",
            "headers": {},
            "url": url,
        })()

    monkeypatch.setattr(strategy, "_request_once", fake_request_once)
    monkeypatch.setattr(strategy, "assert_public_url", lambda url: None)

    assert asyncio.run(fetch_page(
        "https://publisher.example/start",
        client=object(),
        cookie_header="session=publisher-only",
    ))[1] == 200
    assert requests[0][1]["Cookie"] == "session=publisher-only"
    assert "Cookie" not in requests[1][1]
    assert requests[0][2] is True
    assert requests[1][2] is False


def test_archive_gateway_never_forwards_publisher_cookie(monkeypatch):
    captured = []

    async def fake_limited_fetch(self, context, url, strategy, *, timeout, cookie_header=""):
        captured.append(cookie_header)
        return "", 404

    monkeypatch.setattr(MultiGatewayArchiveHandler, "_limited_fetch", fake_limited_fetch)
    options = FetchOptions(
        use_browser=False,
        allow_partial=False,
        rule_version="test",
        force_archive=True,
        full_markdown=False,
        plan=("archive_is",),
        cookie_header="session=publisher-only",
    )
    context = Context(
        url="https://publisher.example/article",
        domain="publisher.example",
        strategy=None,
        options=options,
        client=object(),
        started_at=time.perf_counter(),
    )

    asyncio.run(MultiGatewayArchiveHandler()._attempt_html_gateway(
        context,
        "https://archive.today/example",
        label="archive_is",
        engine="archive_is",
        failure_code="ARCHIVE_FAILED",
    ))
    assert captured == [""]
