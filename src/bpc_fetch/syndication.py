"""Pure validation for publisher-attributed syndicated representations.

A Yahoo Finance page that carries a ``(Bloomberg)`` attribution is a distinct
representation of a Bloomberg-reported story. This module deliberately does
not search, fetch, or extract pages: network access and shared quality checks
belong to the retrieval pipeline. The pure boundary below prevents a candidate
from being accepted without title, date, host, and attribution evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from urllib.parse import urlparse

_TITLE_STRIP_RE = re.compile(r"[^\w\s]")
_YAHOO_FINANCE_HOST = "finance.yahoo.com"
_BLOOMBERG_ATTRIBUTION = "(bloomberg)"
_MAX_PUBLICATION_DELTA_DAYS = 2


@dataclass(frozen=True)
class ArticleHint:
    """Verified metadata about the requested publisher article."""

    canonical_url: str
    title: str
    published_at: datetime | None


@dataclass(frozen=True)
class YahooSyndicationCandidate:
    """Evidence collected from a Yahoo Finance candidate page."""

    url: str
    title: str
    published_at: datetime | None
    attribution_text: str


@dataclass(frozen=True)
class SyndicatedRepresentation:
    """A validated Yahoo-hosted representation of a Bloomberg story."""

    canonical_request_url: str
    representation_url: str
    original_publisher: str
    syndicated: bool
    attribution: str
    text_identity: str


def normalize_title(title: str) -> str:
    """Case-, punctuation-, and whitespace-fold a title for exact comparison."""
    folded = _TITLE_STRIP_RE.sub(" ", title or "").casefold()
    return " ".join(folded.split())


def validate_yahoo_bloomberg_candidate(
    original: ArticleHint,
    candidate: YahooSyndicationCandidate,
) -> SyndicatedRepresentation | None:
    """Accept only a date-aligned Yahoo candidate attributed to Bloomberg.

    The caller must separately run the shared PAC quality gate on extracted
    candidate content. A successful result does not claim that Yahoo's text is
    byte-identical to Bloomberg's original article.
    """
    if not _has_required_original_metadata(original):
        return None
    if not _is_yahoo_finance_url(candidate.url):
        return None
    if normalize_title(candidate.title) != normalize_title(original.title):
        return None
    if not _published_within_window(original.published_at, candidate.published_at):
        return None
    if _BLOOMBERG_ATTRIBUTION not in candidate.attribution_text.casefold()[:4000]:
        return None
    return SyndicatedRepresentation(
        canonical_request_url=original.canonical_url,
        representation_url=candidate.url,
        original_publisher="bloomberg",
        syndicated=True,
        attribution="(Bloomberg)",
        text_identity="unknown",
    )


def _has_required_original_metadata(original: ArticleHint) -> bool:
    return bool(original.canonical_url and normalize_title(original.title) and original.published_at)


def _is_yahoo_finance_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold().rstrip(".")
    return host == _YAHOO_FINANCE_HOST or host.endswith(f".{_YAHOO_FINANCE_HOST}")


def _published_within_window(
    original_published_at: datetime | None,
    candidate_published_at: datetime | None,
) -> bool:
    if original_published_at is None or candidate_published_at is None:
        return False
    original_utc = _as_utc(original_published_at)
    candidate_utc = _as_utc(candidate_published_at)
    return abs((candidate_utc - original_utc).total_seconds()) <= _MAX_PUBLICATION_DELTA_DAYS * 86_400


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
