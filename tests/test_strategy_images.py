import asyncio
from types import SimpleNamespace
from typing import Any, cast

from bpc_fetch import strategy


ARTICLE = {
    "title": "Image Article",
    "text": "complete article body",
    "images": ["https://example.com/image.jpg"],
}


def _patch_successful_extraction(monkeypatch):
    monkeypatch.setattr("bpc_fetch.extract.extract_article", lambda *args, **kwargs: ARTICLE)
    monkeypatch.setattr("bpc_fetch.extract.article_to_markdown", lambda *args, **kwargs: "body")
    monkeypatch.setattr(
        strategy,
        "quality_check",
        lambda *args, **kwargs: SimpleNamespace(ok=True, paywall_suspected=False),
    )


def test_try_extract_ok_carries_private_image_urls(monkeypatch):
    _patch_successful_extraction(monkeypatch)

    result = asyncio.run(
        strategy._try_extract_ok(
            "<html></html>",
            "https://example.com/article",
            "example.com",
            dom_result=None,
            allow_partial=False,
            strategy_hit=["http_primary"],
            rule_version="test-version",
            engine="http",
            t0=0,
            full_markdown=True,
        )
    )

    assert result is not None
    assert result["_image_urls"] == ARTICLE["images"]


def test_final_quality_pass_carries_private_image_urls(monkeypatch):
    _patch_successful_extraction(monkeypatch)

    async def fake_fetch_page(*args, **kwargs):
        return "<html>fallback body</html>", 200

    monkeypatch.setattr(strategy, "assert_public_url", lambda url: None)
    monkeypatch.setattr(strategy, "build_plan", lambda *args, **kwargs: ["http_primary"])
    monkeypatch.setattr(strategy, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(strategy, "html_has_content", lambda html: False)

    result = asyncio.run(
        strategy.fetch_article(
            "https://example.com/article",
            client=cast(Any, object()),
            domain="example.com",
            full_markdown=True,
        )
    )

    assert result["ok"] is True
    assert result["strategy_hit"][-1] == "final_quality_pass"
    assert result["_image_urls"] == ARTICLE["images"]
