"""Unified ArticleResult envelope and error codes (Phase 1 / §15)."""
from __future__ import annotations

from typing import Any

# §15.3 A5
ERROR_CODES = frozenset({
    "RULE_MISSING",
    "NO_STRATEGY",
    "NETWORK",
    "HTTP_BLOCKED",
    "BOT_CHALLENGE",
    "PAYWALL_REMAINING",
    "EXTRACT_FAILED",
    "BROWSER_UNAVAILABLE",
    "ARCHIVE_FAILED",
    "LIMIT_EXCEEDED",
    "SSRF_BLOCKED",
    "INTERNAL",
})

FAILURE_CLASS = frozenset({
    "strategy",
    "bot",
    "network",
    "extract",
    "config",
    "none",
})

MARKDOWN_MAX_CHARS = 20_000  # §15.1.3
BATCH_SUMMARY_CHARS = 2_000


def truncate_markdown(text: str, max_chars: int = MARKDOWN_MAX_CHARS) -> tuple[str, bool]:
    if not text:
        return "", False
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def ok_result(
    *,
    url: str,
    domain: str,
    title: str = "",
    markdown: str = "",
    strategy_hit: list[str] | None = None,
    rule_version: str = "",
    engine: str = "http",
    latency_ms: int = 0,
    path: str | None = None,
    warnings: list[str] | None = None,
    paywall_suspected: bool = False,
    full_markdown: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    content_chars = len(markdown or "")
    md_out, truncated = (markdown, False) if full_markdown else truncate_markdown(markdown or "")
    out: dict[str, Any] = {
        "ok": True,
        "url": url,
        "domain": domain,
        "title": title or "",
        "markdown": md_out,
        "content_chars": content_chars,
        "truncated": truncated,
        "paywall_suspected": paywall_suspected,
        "strategy_hit": list(strategy_hit or []),
        "rule_version": rule_version,
        "engine": engine,
        "latency_ms": latency_ms,
        "path": path,
        "warnings": list(warnings or []),
        "error_code": "",
        "failure_class": "none",
    }
    if extra:
        out.update(extra)
    return out


def fail_result(
    *,
    url: str,
    domain: str,
    error_code: str,
    failure_class: str,
    error: str = "",
    strategy_hit: list[str] | None = None,
    rule_version: str = "",
    recovery_hint: str = "",
    latency_ms: int = 0,
    engine: str = "",
    http_status: int | None = None,
    warnings: list[str] | None = None,
    markdown: str = "",
    full_markdown: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if error_code not in ERROR_CODES:
        error_code = "INTERNAL"
    if failure_class not in FAILURE_CLASS:
        failure_class = "config"
    md_out, truncated = (markdown, False) if full_markdown else truncate_markdown(markdown or "")
    out: dict[str, Any] = {
        "ok": False,
        "url": url,
        "domain": domain,
        "title": "",
        "markdown": md_out,
        "content_chars": len(markdown or ""),
        "truncated": truncated,
        "paywall_suspected": error_code == "PAYWALL_REMAINING",
        "strategy_hit": list(strategy_hit or []),
        "rule_version": rule_version,
        "engine": engine,
        "latency_ms": latency_ms,
        "path": None,
        "warnings": list(warnings or []),
        "error_code": error_code,
        "failure_class": failure_class,
        "error": error or error_code,
        "recovery_hint": recovery_hint,
    }
    if http_status is not None:
        out["http_status"] = http_status
    if extra:
        out.update(extra)
    return out


def classify_http_failure(status: int, body: str = "") -> tuple[str, str]:
    """Return (error_code, failure_class)."""
    if status in (401, 403):
        lower = (body or "")[:2000].lower()
        if any(x in lower for x in ("captcha", "cf-browser", "challenge", "turnstile", "cloudflare")):
            return "BOT_CHALLENGE", "bot"
        return "HTTP_BLOCKED", "bot"
    if status == 429:
        return "HTTP_BLOCKED", "bot"
    if status >= 500 or status == 0:
        return "NETWORK", "network"
    if status == 200:
        # 拿到 200 却走到失败分类：内容不可用（无正文/预筛不过），不是封锁
        return "EXTRACT_FAILED", "extract"
    return "HTTP_BLOCKED", "strategy"
