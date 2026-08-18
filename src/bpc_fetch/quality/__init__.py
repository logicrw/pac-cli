"""Public quality-gate API with backward-compatible exports."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .access_control import AccessControlResult, classify_access_control_page, is_challenge_shell
from .metrics import (
    MAX_HTML_ANALYSIS_CHARS, MIN_CONTENT_CHARS, MIN_NEWSFLASH_CHARS, QUALITY_PASS_SCORE,
    SHORT_NEWSFLASH_MAX_CHARS, TEASER_WINDOW, _analyze_html,
    _is_short_newsflash, _meaningful_length, _merge_dom_metrics, _normalize_space,
    _paragraph_metrics, _quality_score, _structural_navigation, _repeated_line_ratio,
    _terminal_count, _letter_number_ratio,
)
from .paywall import (
    _structural_teaser, clean_paywall_text, html_looks_paywalled,
    is_navigation_shell, is_teaser,
)

@dataclass
class QualityResult:
    """Result returned by :func:`quality_check`.

    The first four fields preserve the original public contract.  ``score`` and
    ``metrics`` are additive diagnostics and therefore do not break existing
    callers that construct or inspect the legacy fields.
    """

    ok: bool
    paywall_suspected: bool
    reason: str = ""
    error_code: str = ""
    score: float = 0.0
    metrics: dict[str, Any] = field(default_factory=dict)

def html_has_content(html: str) -> bool:
    """Return whether HTML contains enough article-like material to attempt extraction."""

    if not html or len(html) < 300:
        return False
    access = classify_access_control_page(html)
    if access.detected:
        return False
    lower = html.casefold()
    if "<article" in lower or "articlebody" in lower or "article-body" in lower:
        return True
    paragraph_count = len(re.findall(r"<p\b", html, re.I))
    if paragraph_count >= 2:
        text = _normalize_space(re.sub(r"<[^>]+>", " ", html[:MAX_HTML_ANALYSIS_CHARS]))
        return _meaningful_length(text) >= MIN_CONTENT_CHARS
    metrics = _analyze_html(html)
    return bool(
        int(metrics.get("container_text_chars") or 0) >= 250
        and float(metrics.get("link_density") or 0.0) < 0.45
    )

def quality_check(
    text: str,
    title: str = "",
    *,
    allow_partial: bool = False,
    html: str = "",
    dom_metrics: Mapping[str, Any] | None = None,
) -> QualityResult:
    """Evaluate extracted content using language-agnostic structural signals.

    ``text`` and ``title`` retain their original positional semantics.  ``html``
    and ``dom_metrics`` are optional additive inputs used for DOM density,
    text-to-HTML ratio, paragraph variance, and link-density analysis.
    """

    candidate = (text or "").strip()
    metrics = _paragraph_metrics(candidate)
    html_metrics = _analyze_html(html) if html else {}
    metrics.update(html_metrics)
    _merge_dom_metrics(metrics, dom_metrics)
    metrics["content_chars"] = _meaningful_length(candidate)
    metrics["title_chars"] = _meaningful_length(title)
    metrics["repeated_line_ratio"] = _repeated_line_ratio(candidate)
    metrics["terminal_count"] = _terminal_count(candidate)
    metrics["letter_number_ratio"] = _letter_number_ratio(candidate)

    access_source = html if html else candidate
    access = classify_access_control_page(access_source, title=title)
    if access.detected:
        metrics["access_control_provider"] = access.provider
        metrics["access_control_challenge"] = access.challenge
        metrics["access_control_score"] = access.score
        return QualityResult(
            ok=False,
            paywall_suspected=False,
            reason="challenge_shell",
            error_code="EXTRACT_FAILED",
            score=0.0,
            metrics=metrics,
        )

    if is_navigation_shell(title, candidate) or _structural_navigation(metrics, candidate):
        return QualityResult(
            ok=False,
            paywall_suspected=False,
            reason="navigation_shell",
            error_code="EXTRACT_FAILED",
            score=0.0,
            metrics=metrics,
        )

    teaser, teaser_reason = _structural_teaser(candidate, title, metrics)
    content_chars = int(metrics["content_chars"])
    if content_chars < MIN_CONTENT_CHARS:
        if teaser:
            return QualityResult(
                ok=False,
                paywall_suspected=True,
                reason=teaser_reason or "teaser_markers",
                error_code="PAYWALL_REMAINING",
                score=0.0,
                metrics=metrics,
            )
        provisional_score = _quality_score(candidate, title, metrics)
        metrics["quality_score"] = provisional_score
        if _is_short_newsflash(candidate, title, metrics, provisional_score):
            return QualityResult(
                ok=True,
                paywall_suspected=False,
                reason="pass",
                error_code="",
                score=provisional_score,
                metrics=metrics,
            )
        return QualityResult(
            ok=False,
            paywall_suspected=False,
            reason=f"content_chars={content_chars} < {MIN_CONTENT_CHARS}",
            error_code="EXTRACT_FAILED",
            score=provisional_score,
            metrics=metrics,
        )

    if teaser and not allow_partial:
        return QualityResult(
            ok=False,
            paywall_suspected=True,
            reason=teaser_reason or "teaser_markers",
            error_code="PAYWALL_REMAINING",
            score=0.0,
            metrics=metrics,
        )
    if teaser and allow_partial:
        score = _quality_score(candidate, title, metrics)
        return QualityResult(
            ok=True,
            paywall_suspected=True,
            reason="teaser_allowed_partial",
            error_code="",
            score=score,
            metrics=metrics,
        )

    score = _quality_score(candidate, title, metrics)
    metrics["quality_score"] = score
    if score >= QUALITY_PASS_SCORE or _is_short_newsflash(candidate, title, metrics, score):
        return QualityResult(
            ok=True,
            paywall_suspected=False,
            reason="pass",
            error_code="",
            score=score,
            metrics=metrics,
        )

    return QualityResult(
        ok=False,
        paywall_suspected=False,
        reason=f"structural_score={score:.3f} < {QUALITY_PASS_SCORE:.3f}",
        error_code="EXTRACT_FAILED",
        score=score,
        metrics=metrics,
    )

__all__ = [
    "AccessControlResult", "QualityResult", "MAX_HTML_ANALYSIS_CHARS", "MIN_CONTENT_CHARS",
    "MIN_NEWSFLASH_CHARS", "QUALITY_PASS_SCORE", "SHORT_NEWSFLASH_MAX_CHARS", "TEASER_WINDOW",
    "classify_access_control_page",
    "clean_paywall_text", "html_has_content", "html_looks_paywalled",
    "is_challenge_shell", "is_navigation_shell", "is_teaser", "quality_check",
]
