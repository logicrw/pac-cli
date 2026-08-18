"""Unified ArticleResult envelope and error codes (Phase 1 / §15)."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Iterable, Mapping
import uuid

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

def new_request_id() -> str:
    """Return a collision-resistant request identifier for opt-in diagnostics."""

    return uuid.uuid4().hex


def _attempt_mapping(attempt: Any) -> dict[str, Any]:
    if isinstance(attempt, Mapping):
        raw = dict(attempt)
    elif is_dataclass(attempt) and not isinstance(attempt, type):
        raw = asdict(attempt)
    else:
        raw = {
            key: getattr(attempt, key)
            for key in (
                "handler", "label", "engine", "status", "elapsed_ms",
                "error_code", "error", "quality_reason",
            )
            if hasattr(attempt, key)
        }
    return {
        "handler": str(raw.get("handler") or ""),
        "label": str(raw.get("label") or ""),
        "engine": str(raw.get("engine") or ""),
        "status": int(raw.get("status") or 0),
        "elapsed_ms": max(0, int(raw.get("elapsed_ms") or 0)),
        "error_code": str(raw.get("error_code") or ""),
        "error": str(raw.get("error") or "")[:500],
        "quality_reason": str(raw.get("quality_reason") or ""),
    }


def _quality_diagnostics(quality: Any | None) -> dict[str, Any]:
    if quality is None:
        return {
            "evaluated": False,
            "ok": None,
            "score": None,
            "reason": "",
            "paywall_suspected": False,
            "components": {},
        }
    metrics = getattr(quality, "metrics", {})
    if not isinstance(metrics, Mapping):
        metrics = {}
    score = getattr(quality, "score", None)
    return {
        "evaluated": True,
        "ok": bool(getattr(quality, "ok", False)),
        "score": float(score) if isinstance(score, (int, float)) else None,
        "reason": str(getattr(quality, "reason", "") or ""),
        "paywall_suspected": bool(getattr(quality, "paywall_suspected", False)),
        "components": dict(metrics),
    }


def build_diagnostics(
    *,
    request_id: str,
    total_latency_ms: int,
    attempts: Iterable[Any] = (),
    quality: Any | None = None,
) -> dict[str, Any]:
    """Build the stable, opt-in diagnostics schema used by fetch/discover/batch."""

    history = [_attempt_mapping(attempt) for attempt in attempts]
    engine_timings: dict[str, int] = {}
    for attempt in history:
        engine = attempt["engine"] or "unknown"
        engine_timings[engine] = engine_timings.get(engine, 0) + attempt["elapsed_ms"]
    return {
        "request_id": request_id or new_request_id(),
        "total_latency_ms": max(0, int(total_latency_ms)),
        "engine_timings_ms": engine_timings,
        "attempts": history,
        "quality": _quality_diagnostics(quality),
    }


def attach_diagnostics(
    result: dict[str, Any],
    *,
    request_id: str,
    total_latency_ms: int,
    attempts: Iterable[Any] = (),
    quality: Any | None = None,
) -> dict[str, Any]:
    """Attach diagnostics without changing the default result envelope."""

    result["diagnostics"] = build_diagnostics(
        request_id=request_id,
        total_latency_ms=total_latency_ms,
        attempts=attempts,
        quality=quality,
    )
    return result


def aggregate_diagnostics(
    *,
    request_id: str,
    total_latency_ms: int,
    items: Iterable[tuple[str, Mapping[str, Any]]],
) -> dict[str, Any]:
    """Aggregate child diagnostics for batch commands without losing per-URL detail."""

    engine_timings: dict[str, int] = {}
    attempts: list[dict[str, Any]] = []
    quality_items: list[dict[str, Any]] = []
    for scope, result in items:
        diagnostics = result.get("diagnostics")
        if not isinstance(diagnostics, Mapping):
            continue
        for engine, elapsed in dict(diagnostics.get("engine_timings_ms") or {}).items():
            engine_timings[str(engine)] = engine_timings.get(str(engine), 0) + max(0, int(elapsed or 0))
        child_request_id = str(diagnostics.get("request_id") or "")
        for attempt in diagnostics.get("attempts") or []:
            normalized = _attempt_mapping(attempt)
            normalized["scope"] = scope
            normalized["request_id"] = child_request_id
            attempts.append(normalized)
        quality = diagnostics.get("quality")
        if isinstance(quality, Mapping):
            quality_items.append({
                "scope": scope,
                "request_id": child_request_id,
                "evaluated": bool(quality.get("evaluated")),
                "ok": quality.get("ok"),
                "score": quality.get("score"),
                "reason": str(quality.get("reason") or ""),
                "paywall_suspected": bool(quality.get("paywall_suspected")),
                "components": dict(quality.get("components") or {}),
            })
    return {
        "request_id": request_id or new_request_id(),
        "total_latency_ms": max(0, int(total_latency_ms)),
        "engine_timings_ms": engine_timings,
        "attempts": attempts,
        "quality": {
            "evaluated": any(item["evaluated"] for item in quality_items),
            "items": quality_items,
        },
    }
