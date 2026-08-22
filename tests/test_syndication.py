"""Bloomberg → Yahoo syndicated-representation resolver contracts."""

from datetime import datetime, timezone

from bpc_fetch.syndication import (
    ArticleHint,
    YahooSyndicationCandidate,
    validate_yahoo_bloomberg_candidate,
)


ORIGINAL = ArticleHint(
    canonical_url="https://www.bloomberg.com/news/articles/2026-08-20/example",
    title="Broadcom Seeks More Than $60 Billion in Latest AI Debt Deal",
    published_at=datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
)


def candidate(**changes):
    values = {
        "url": "https://finance.yahoo.com/technology/ai/articles/broadcom-seeks-more-60bn-debt.html",
        "title": "Broadcom Seeks More Than $60 Billion in Latest AI Debt Deal",
        "published_at": datetime(2026, 8, 20, 14, tzinfo=timezone.utc),
        "attribution_text": "(Bloomberg) -- Broadcom Inc. is in talks with lenders.",
    }
    values.update(changes)
    return YahooSyndicationCandidate(**values)


def test_accepts_exact_title_yahoo_bloomberg_candidate_within_date_window():
    representation = validate_yahoo_bloomberg_candidate(ORIGINAL, candidate())

    assert representation is not None
    assert representation.original_publisher == "bloomberg"
    assert representation.syndicated is True
    assert representation.text_identity == "unknown"
    assert representation.canonical_request_url == ORIGINAL.canonical_url


def test_rejects_candidate_without_original_publication_date():
    original = ArticleHint(
        canonical_url=ORIGINAL.canonical_url,
        title=ORIGINAL.title,
        published_at=None,
    )
    assert validate_yahoo_bloomberg_candidate(original, candidate()) is None


def test_rejects_non_yahoo_host():
    assert validate_yahoo_bloomberg_candidate(
        ORIGINAL,
        candidate(url="https://news.yahoo.com/article"),
    ) is None


def test_rejects_normalized_title_mismatch():
    assert validate_yahoo_bloomberg_candidate(
        ORIGINAL,
        candidate(title="Broadcom Raises $60 Billion for Different Deal"),
    ) is None


def test_rejects_candidate_outside_two_day_window():
    assert validate_yahoo_bloomberg_candidate(
        ORIGINAL,
        candidate(published_at=datetime(2026, 8, 23, 12, tzinfo=timezone.utc)),
    ) is None


def test_rejects_candidate_without_bloomberg_attribution():
    assert validate_yahoo_bloomberg_candidate(
        ORIGINAL,
        candidate(attribution_text="Proactive reports Broadcom is in talks with lenders."),
    ) is None
