"""Paywall and teaser detection plus extracted-text cleanup."""
from __future__ import annotations

import re
from typing import Mapping

from bs4 import BeautifulSoup

from .metrics import (
    MAX_HTML_ANALYSIS_CHARS, TEASER_WINDOW, _analyze_html, _extract_title_from_soup,
    _has_terminal_ending, _meaningful_length, _normalize_space, _paragraph_metrics,
    _repeated_line_ratio, _split_paragraphs,
)

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
    "continue reading this article",
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
    "abonnez-vous pour continuer",
    "connectez-vous pour continuer",
    "suscríbete para continuar",
    "inicia sesión para continuar",
    "abonnieren sie, um weiterzulesen",
    "melden sie sich an, um weiterzulesen",
    "assine para continuar lendo",
    "accedi per continuare",
    "abbonati per continuare",
    "请登录后继续",
    "登录后继续阅读",
    "订阅后继续阅读",
    "本文为付费内容",
    "會員限定",
    "会員限定",
    "続きを読むには",
    "구독 후 계속",
    "로그인 후 계속",
    "اشترك لمتابعة القراءة",
    "سجل الدخول للمتابعة",
    "подпишитесь, чтобы продолжить",
    "войдите, чтобы продолжить",
)

_CLEAN_MARKERS = (
    "Enjoying our latest content?",
    "Log in or create an account to continue",
    "Subscribe to continue reading",
    "Already a subscriber?",
    "Sign in to continue",
    "Create a free account to continue",
    "Register for free to continue reading",
    "Want to read more?",
    "Access the most recent journalism",
    "Explore the latest features & opinion",
    "请登录后继续",
    "订阅后继续阅读",
    "本文为付费内容",
)

_NAVIGATION_SIGNALS = (
    "skip navigation",
    "pre-markets",
    "search quotes, news & videos",
    "investing club",
    "latest video",
)

def _window(title: str, text: str) -> str:
    head = (text or "")[:TEASER_WINDOW]
    return f"{title or ''}\n{head}".casefold()

def is_teaser(title: str, text: str) -> bool:
    """Return ``True`` for explicit subscription/login teaser language."""

    window = _window(title, text)
    if not window.strip():
        return False
    return any(marker.casefold() in window for marker in _TEASER_MARKERS)

def is_navigation_shell(title: str, text: str) -> bool:
    """Detect publisher navigation mistakenly extracted as article content."""

    window = _window(title, text)
    if "do not delete" in (title or "").casefold():
        if sum(signal in window for signal in _NAVIGATION_SIGNALS) >= 3:
            return True

    paragraphs = _split_paragraphs(text)
    if len(paragraphs) >= 8:
        lengths = [_meaningful_length(paragraph) for paragraph in paragraphs]
        short_ratio = sum(length <= 35 for length in lengths) / max(len(lengths), 1)
        if short_ratio >= 0.75 and _repeated_line_ratio(text) >= 0.2:
            return True
    return False

def _structural_teaser(
    text: str,
    title: str,
    metrics: Mapping[str, Any],
) -> tuple[bool, str]:
    if is_teaser(title, text):
        return True, "teaser_markers"

    content_chars = _meaningful_length(text)
    paragraph_count = int(metrics.get("paragraph_count_dom") or 0)
    if not paragraph_count:
        paragraph_count = int(_paragraph_metrics(text).get("paragraph_count") or 0)
    link_density = float(metrics.get("link_density") or 0.0)
    form_count = int(metrics.get("form_count") or 0)
    password_inputs = int(metrics.get("password_input_count") or 0)
    modal_count = int(metrics.get("modal_count") or 0)
    paywall_attributes = int(metrics.get("paywall_attribute_count") or 0)
    hidden_prose = int(metrics.get("hidden_prose_count") or 0)
    jsonld_chars = int(metrics.get("jsonld_article_body_chars") or 0)
    cta_count = int(metrics.get("cta_count") or 0)

    schema_mismatch = bool(
        jsonld_chars >= 800
        and content_chars < 1600
        and jsonld_chars >= max(content_chars * 2.2, content_chars + 500)
    )
    access_ui = bool(
        password_inputs
        or (modal_count and form_count)
        or hidden_prose
        or (paywall_attributes >= 1 and schema_mismatch)
    )
    sparse_shell = bool(content_chars < 1200 and paragraph_count <= 3)
    abrupt = bool(
        content_chars < 1200
        and (
            (text or "").rstrip().endswith("…") or bool(re.search(r"\.{3}$", (text or "").rstrip()))
            or (not _has_terminal_ending(text) and content_chars >= 180)
        )
    )

    if schema_mismatch and (access_ui or hidden_prose or abrupt):
        return True, "teaser_schema_mismatch"
    if sparse_shell and access_ui and (cta_count >= 1 or link_density >= 0.2):
        return True, "teaser_access_shell"
    if sparse_shell and paywall_attributes >= 1 and hidden_prose >= 1:
        return True, "teaser_clipped_content"
    if content_chars < 700 and form_count >= 1 and cta_count >= 2 and link_density >= 0.25:
        return True, "teaser_login_shell"
    return False, ""

def html_looks_paywalled(html: str) -> bool:
    """Conservatively detect a paywall/login shell in raw HTML."""

    if not html or len(html) < 200:
        return True
    metrics = _analyze_html(html)
    try:
        soup = BeautifulSoup(html[:MAX_HTML_ANALYSIS_CHARS], "html.parser")
        for removable in soup.find_all(["script", "style", "noscript", "template", "svg"]):
            removable.decompose()
        text = _normalize_space(soup.get_text(" ", strip=True))
        title = _extract_title_from_soup(soup)
    except Exception:
        text = _normalize_space(re.sub(r"<[^>]+>", " ", html))
        title = ""
    teaser, _ = _structural_teaser(text, title, metrics)
    return teaser

def clean_paywall_text(text: str) -> str:
    """Trim trailing paywall/login prompts while preserving the article prefix."""

    if not text:
        return ""
    folded = text.casefold()
    earliest: int | None = None
    for marker in _CLEAN_MARKERS:
        index = folded.find(marker.casefold())
        if index > 0 and (earliest is None or index < earliest):
            earliest = index
    if earliest is not None:
        return text[:earliest].rstrip()
    return text
