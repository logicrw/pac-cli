"""Quality gate: length + teaser heuristics (§15.1.2 / A4)."""
from __future__ import annotations

import re
from dataclasses import dataclass

MIN_CONTENT_CHARS = 100  # EXTRACT_FAILED floor only
TEASER_WINDOW = 1200

# Combined from strategy._is_paywalled + news-scraper glue cues
_TEASER_MARKERS = (
    "log in or create an account to continue",
    "subscribe to continue reading",
    "subscribe to continue",
    "subscribe to read",
    "subscription required",
    "sign in to continue",
    "sign in to read",
    "log in to continue",
    "login to continue",
    "create a free account to continue",
    "create a free account",
    "this article is for subscribers",
    "to read the full story",
    "register for free to continue reading",
    "register to continue",
    "already a subscriber? sign in",
    "already a subscriber",
    "become a subscriber",
    "want to read more?",
    "unlock this article",
    "premium content",
    "members only",
    "member-only",
    "subscribers only",
    "subscriber-only",
    "remaining free articles",
    "you've reached your limit",
    "you have reached your article limit",
    "to continue reading",
    "请登录后继续",
    "订阅后继续阅读",
    "本文为付费内容",
)


@dataclass
class QualityResult:
    ok: bool
    paywall_suspected: bool
    reason: str = ""
    error_code: str = ""  # EXTRACT_FAILED | PAYWALL_REMAINING | ""


def _window(title: str, text: str) -> str:
    head = (text or "")[:TEASER_WINDOW]
    return f"{title or ''}\n{head}".lower()


def is_teaser(title: str, text: str) -> bool:
    w = _window(title, text)
    if not w.strip():
        return False
    return any(m in w for m in _TEASER_MARKERS)


def html_looks_paywalled(html: str) -> bool:
    if not html or len(html) < 200:
        return True
    return is_teaser("", html)


def html_has_content(html: str) -> bool:
    if not html or len(html) < 500:
        return False
    lower = html.lower()
    if "<article" in lower or "articlebody" in lower or "article-body" in lower:
        return True
    if lower.count("<p") > 3:
        return True
    return len(html) > 5000


def quality_check(
    text: str,
    title: str = "",
    *,
    allow_partial: bool = False,
) -> QualityResult:
    """Strict by default: teaser => not ok unless allow_partial."""
    t = (text or "").strip()
    if len(t) < MIN_CONTENT_CHARS:
        return QualityResult(
            ok=False,
            paywall_suspected=False,
            reason=f"content_chars={len(t)} < {MIN_CONTENT_CHARS}",
            error_code="EXTRACT_FAILED",
        )
    teaser = is_teaser(title, t)
    if teaser and not allow_partial:
        return QualityResult(
            ok=False,
            paywall_suspected=True,
            reason="teaser_markers",
            error_code="PAYWALL_REMAINING",
        )
    if teaser and allow_partial:
        return QualityResult(
            ok=True,
            paywall_suspected=True,
            reason="teaser_allowed_partial",
            error_code="",
        )
    return QualityResult(ok=True, paywall_suspected=False, reason="pass", error_code="")


def clean_paywall_text(text: str) -> str:
    if not text:
        return ""
    for marker in (
        "Enjoying our latest content?",
        "Log in or create an account to continue",
        "Subscribe to continue reading",
        "Already a subscriber?",
    ):
        idx = text.find(marker)
        if idx > 0:
            text = text[:idx].rstrip()
    return text
