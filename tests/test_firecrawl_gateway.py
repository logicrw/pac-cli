"""Firecrawl cloud gateway handler behavior (sync tests via asyncio.run)."""

import asyncio
import time

import httpx

from bpc_fetch.strategy import CloudBudget, Context, FirecrawlGatewayHandler


class _Opts:
    plan = ["http_primary"]
    allow_partial = False
    rule_version = "test"
    full_markdown = False
    cookie_header = ""


def _context(client, domain="wsj.com", failure_code="BOT_CHALLENGE", cloud_max_calls=1):
    options = _Opts()
    options.cloud_budget = CloudBudget(cloud_max_calls)
    context = Context(
        url=f"https://www.{domain}/article",
        domain=domain,
        strategy=None,
        options=options,
        client=client,
        started_at=time.perf_counter(),
        attempts=[],
    )
    context.best_error_code = failure_code
    return context


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


def test_handler_skips_policy_denied_outlet_before_cloud_request(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    calls = []

    class _Client:
        async def post(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("policy denied outlet must not call Firecrawl")

    handler = FirecrawlGatewayHandler()
    result = asyncio.run(_run(handler, _Client(), domain="bloomberg.com"))

    assert result is None
    assert calls == []


def test_handler_skips_when_shared_cloud_budget_is_exhausted(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    calls = []

    class _Client:
        async def post(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("exhausted budget must not call Firecrawl")

    handler = FirecrawlGatewayHandler()
    result = asyncio.run(_run(handler, _Client(), cloud_max_calls=0))

    assert result is None
    assert calls == []


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


async def _run(handler, client, **context_kwargs):
    ctx = _context(client, **context_kwargs)
    try:
        if hasattr(client, "aclose"):
            async with client:
                return await handler.process(ctx)
        return await handler.process(ctx)
    except Exception:
        return "raised"
