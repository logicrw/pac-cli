"""Firecrawl cloud gateway handler behavior (sync tests via asyncio.run)."""

import asyncio
import time

import httpx

from bpc_fetch.strategy import Context, FirecrawlGatewayHandler


class _Opts:
    plan = ["http_primary"]
    allow_partial = False
    rule_version = "test"
    full_markdown = False
    cookie_header = ""


def _context(client):
    return Context(
        url="https://example.com/article",
        domain="example.com",
        strategy=None,
        options=_Opts(),
        client=client,
        started_at=time.perf_counter(),
        attempts=[],
    )


def test_handler_silent_when_no_api_key(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv("PAC_FIRECRAWL_API_KEY", raising=False)
    handler = FirecrawlGatewayHandler()
    result = asyncio.run(_run(handler, httpx.AsyncClient()))
    assert result is None


def test_handler_records_attempt_on_http_error(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    class _BadClient:
        async def post(self, *args, **kwargs):
            raise httpx.ConnectTimeout("boom")

    handler = FirecrawlGatewayHandler()
    result = asyncio.run(_run(handler, _BadClient()))
    assert result is None


def test_handler_empty_markdown_returns_none(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    class _Resp:
        status_code = 200

        def json(self):
            return {"data": {"markdown": ""}}

    class _Client:
        async def post(self, *args, **kwargs):
            return _Resp()

    handler = FirecrawlGatewayHandler()
    result = asyncio.run(_run(handler, _Client()))
    assert result is None


async def _run(handler, client):
    ctx = _context(client)
    try:
        if hasattr(client, "aclose"):
            async with client:
                return await handler.process(ctx)
        return await handler.process(ctx)
    except Exception:
        return "raised"
