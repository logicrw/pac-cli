"""Bloomberg Yahoo representation handler contracts."""

import asyncio
from datetime import datetime, timezone
import time

from bpc_fetch import strategy
from bpc_fetch.syndication import ArticleHint, SyndicatedRepresentation
from bpc_fetch.strategy import Context, FetchOptions, YahooSyndicationHandler


class _Client:
    async def post(self, *args, **kwargs):
        raise AssertionError("Yahoo representation must not use cloud POST")


def _context():
    options = FetchOptions(
        use_browser=False,
        allow_partial=False,
        rule_version="test",
        force_archive=False,
        full_markdown=False,
        plan=("http_primary",),
    )
    context = Context(
        url="https://www.bloomberg.com/news/articles/2026-08-20/example",
        domain="bloomberg.com",
        strategy=None,
        options=options,
        client=_Client(),
        started_at=time.perf_counter(),
    )
    context.best_error_code = "BOT_CHALLENGE"
    context.best_failure_class = "bot"
    return context


def test_handler_returns_explicit_syndicated_provenance_after_quality_pass(monkeypatch):
    representation = SyndicatedRepresentation(
        canonical_request_url="https://www.bloomberg.com/news/articles/2026-08-20/example",
        representation_url="https://finance.yahoo.com/article",
        original_publisher="bloomberg",
        syndicated=True,
        attribution="(Bloomberg)",
        text_identity="unknown",
    )

    async def fake_resolve(context):
        return representation, "<article>Yahoo content</article>"

    async def fake_evaluate(*args, **kwargs):
        return strategy.CandidateEvaluation(
            result={"ok": True, "url": "https://finance.yahoo.com/article", "strategy_hit": []},
            quality=object(),
            article={},
            markdown="Yahoo content",
        )

    monkeypatch.setattr(strategy, "_resolve_yahoo_bloomberg_document", fake_resolve)
    monkeypatch.setattr(strategy, "_evaluate_candidate", fake_evaluate)

    result = asyncio.run(YahooSyndicationHandler().process(_context()))

    assert result["ok"] is True
    assert result["url"] == "https://finance.yahoo.com/article"
    assert result["canonical_request_url"] == "https://www.bloomberg.com/news/articles/2026-08-20/example"
    assert result["representation_url"] == "https://finance.yahoo.com/article"
    assert result["syndicated"] is True
    assert result["original_publisher"] == "bloomberg"
    assert result["text_identity"] == "unknown"


def test_resolver_uses_discovery_hint_and_never_forwards_publisher_cookie(monkeypatch):
    context = _context()
    context.options = FetchOptions(
        use_browser=False,
        allow_partial=False,
        rule_version="test",
        force_archive=False,
        full_markdown=False,
        plan=("http_primary",),
        article_title="Broadcom Seeks More Than $60 Billion in Latest AI Debt Deal",
        article_published_at="Wed, 20 Aug 2026 12:00:00 GMT",
        cookie_header="session=publisher-only",
    )
    candidate_url = "https://finance.yahoo.com/technology/ai/articles/broadcom-debt.html"
    calls = []

    async def fake_discover(*args, **kwargs):
        return {"articles": [{"url": candidate_url, "published": "Wed, 20 Aug 2026 14:00:00 GMT"}]}

    async def fake_fetch(url, strategy_arg, client, *, timeout, cookie_header):
        calls.append((url, cookie_header))
        return "<html>candidate</html>", 200

    def fake_extract(html, url):
        return {
            "title": "Broadcom Seeks More Than $60 Billion in Latest AI Debt Deal",
            "date": "",
            "text": "(Bloomberg) -- Broadcom is in talks with lenders.",
        }

    monkeypatch.setattr("bpc_fetch.discover.discover_articles", fake_discover)
    monkeypatch.setattr(strategy, "fetch_page", fake_fetch)
    monkeypatch.setattr("bpc_fetch.extract.extract_article", fake_extract)

    resolved = asyncio.run(strategy._resolve_yahoo_bloomberg_document(context))

    assert resolved is not None
    representation, html = resolved
    assert html == "<html>candidate</html>"
    assert representation.representation_url == candidate_url
    assert representation.text_identity == "unknown"
    assert calls == [(candidate_url, "")]


def test_policy_denial_preserves_original_failure_without_resolver_call(monkeypatch):
    calls = []

    async def fake_resolve(context):
        calls.append(context)
        raise AssertionError("policy denial must not resolve Yahoo")

    monkeypatch.setattr(strategy, "may_use_yahoo_bloomberg_representation", lambda domain: False)
    monkeypatch.setattr(strategy, "_resolve_yahoo_bloomberg_document", fake_resolve)
    context = _context()

    result = asyncio.run(YahooSyndicationHandler().process(context))

    assert result is None
    assert context.best_error_code == "BOT_CHALLENGE"
    assert calls == []


def test_handler_preserves_original_failure_when_no_valid_candidate(monkeypatch):
    async def fake_resolve(context):
        return None

    monkeypatch.setattr(strategy, "_resolve_yahoo_bloomberg_document", fake_resolve)
    context = _context()
    result = asyncio.run(YahooSyndicationHandler().process(context))

    assert result is None
    assert context.best_error_code == "BOT_CHALLENGE"
    assert "syndication_yahoo_miss" in context.strategy_hit
