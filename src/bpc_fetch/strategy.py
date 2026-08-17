"""Asynchronous chain-of-responsibility fetch pipeline for PAC-Engine.

The module keeps the original public API and result envelope while replacing
its monolithic waterfall with composable HTTP, stealth-browser, and archive
handlers.  Every successful candidate is validated by the structural quality
gate before the pipeline short-circuits.
"""
from __future__ import annotations

import asyncio
import os
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urljoin, urlparse

import httpx

from .quality import (
    QualityResult,
    classify_access_control_page,
    html_has_content,
    html_looks_paywalled,
    quality_check,
)
from .result import classify_http_failure, fail_result, ok_result
from .sites import SiteStrategy
from .ssrf import SSRFBlocked, assert_public_url

UA_GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
UA_BINGBOT = "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)"
UA_FACEBOOKBOT = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
UA_NORMAL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
)

REFERER_GOOGLE = "https://www.google.com/"
REFERER_FACEBOOK = "https://www.facebook.com/"
REFERER_TWITTER = "https://t.co/"

TIMEOUT = 30.0
GOOGLEBOT_TIMEOUT = 10.0
MAX_REDIRECTS = 10
ARCHIVE_IS_COOLDOWN_S = float(os.environ.get("PAC_ARCHIVE_COOLDOWN_S", "300"))
_archive_is_fail_at: dict[str, float] = {}

_FAILURE_PRIORITY = {
    "SSRF_BLOCKED": 100,
    "PAYWALL_REMAINING": 90,
    "BOT_CHALLENGE": 80,
    "HTTP_BLOCKED": 70,
    "BROWSER_UNAVAILABLE": 60,
    "NETWORK": 50,
    "EXTRACT_FAILED": 40,
    "ARCHIVE_FAILED": 30,
    "NO_STRATEGY": 20,
    "INTERNAL": 10,
}

_FAILURE_CLASS_BY_CODE = {
    "SSRF_BLOCKED": "config",
    "PAYWALL_REMAINING": "strategy",
    "BOT_CHALLENGE": "bot",
    "HTTP_BLOCKED": "bot",
    "BROWSER_UNAVAILABLE": "config",
    "NETWORK": "network",
    "EXTRACT_FAILED": "extract",
    "ARCHIVE_FAILED": "network",
    "NO_STRATEGY": "strategy",
    "INTERNAL": "config",
}


@dataclass(frozen=True)
class FetchOptions:
    """Immutable options consumed by the handler chain."""

    use_browser: bool | None
    allow_partial: bool
    rule_version: str
    force_archive: bool
    full_markdown: bool
    plan: Sequence[str]


@dataclass
class Attempt:
    """Internal attempt history retained by :class:`Context`."""

    handler: str
    label: str
    engine: str
    status: int
    elapsed_ms: int
    error_code: str = ""
    error: str = ""
    quality_reason: str = ""


@dataclass
class CandidateEvaluation:
    """Internal extraction and quality-gate outcome."""

    result: dict[str, Any] | None
    quality: QualityResult | Any | None
    article: dict[str, Any]
    markdown: str
    error: str = ""


@dataclass
class Context:
    """Mutable request context shared by all pipeline handlers."""

    url: str
    domain: str
    strategy: SiteStrategy | None
    options: FetchOptions
    client: Any
    started_at: float
    attempts: list[Attempt] = field(default_factory=list)
    strategy_hit: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    last_html: str = ""
    last_status: int = 0
    last_dom_result: dict[str, Any] | None = None
    last_engine: str = ""
    last_quality: QualityResult | Any | None = None
    best_error_code: str = ""
    best_failure_class: str = ""
    best_error: str = ""
    best_http_status: int | None = None
    best_engine: str = ""
    best_priority: int = -1

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started_at) * 1000)

    def add_warning(self, warning: str) -> None:
        value = (warning or "").strip()
        if value and value not in self.warnings:
            self.warnings.append(value)

    def record_attempt(
        self,
        *,
        handler: str,
        label: str,
        engine: str,
        status: int,
        started_at: float,
        error_code: str = "",
        error: str = "",
        quality_reason: str = "",
    ) -> None:
        self.attempts.append(
            Attempt(
                handler=handler,
                label=label,
                engine=engine,
                status=status,
                elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                error_code=error_code,
                error=error,
                quality_reason=quality_reason,
            )
        )

    def note_response(
        self,
        html: str,
        status: int,
        *,
        engine: str,
        dom_result: dict[str, Any] | None = None,
    ) -> None:
        self.last_html = html or ""
        self.last_status = int(status or 0)
        self.last_dom_result = dom_result
        self.last_engine = engine

    def note_failure(
        self,
        error_code: str,
        *,
        error: str,
        failure_class: str | None = None,
        status: int | None = None,
        engine: str = "",
    ) -> None:
        code = error_code if error_code in _FAILURE_PRIORITY else "INTERNAL"
        priority = _FAILURE_PRIORITY[code]
        if priority < self.best_priority:
            return
        self.best_priority = priority
        self.best_error_code = code
        self.best_failure_class = failure_class or _FAILURE_CLASS_BY_CODE[code]
        self.best_error = error or code
        self.best_http_status = status if status else None
        self.best_engine = engine or self.last_engine

    def failure_result(self) -> dict[str, Any]:
        code = self.best_error_code
        failure_class = self.best_failure_class
        error = self.best_error
        status = self.best_http_status
        engine = self.best_engine or self.last_engine

        if not code:
            code, failure_class = _classify_http_response(self.last_status, self.last_html)
            if code == "EXTRACT_FAILED" and not self.last_html:
                code = "NETWORK"
                failure_class = "network"
            error = f"HTTP {self.last_status}" if self.last_status else "fetch_failed"
            status = self.last_status or None

        recovery_hint = ""
        if code == "BROWSER_UNAVAILABLE":
            recovery_hint = "playwright install chromium"
        elif code == "BOT_CHALLENGE":
            recovery_hint = "retry later or use an authorized network path"
        elif code == "PAYWALL_REMAINING":
            recovery_hint = "retry with --allow-partial or an authorized archive source"

        return fail_result(
            url=self.url,
            domain=self.domain,
            error_code=code,
            failure_class=failure_class or _FAILURE_CLASS_BY_CODE.get(code, "config"),
            error=error,
            strategy_hit=self.strategy_hit,
            rule_version=self.options.rule_version,
            recovery_hint=recovery_hint,
            latency_ms=self.elapsed_ms(),
            engine=engine,
            http_status=status,
            warnings=self.warnings,
            full_markdown=self.options.full_markdown,
        )


class AsyncHandler(ABC):
    """Base class for the asynchronous chain of responsibility."""

    def __init__(self) -> None:
        self._next: AsyncHandler | None = None

    def set_next(self, handler: "AsyncHandler") -> "AsyncHandler":
        self._next = handler
        return handler

    async def handle(self, context: Context) -> dict[str, Any] | None:
        result = await self.process(context)
        if result is not None:
            return result
        if self._next is not None:
            return await self._next.handle(context)
        return None

    @abstractmethod
    async def process(self, context: Context) -> dict[str, Any] | None:
        """Attempt this handler and return a final envelope on success."""


class DirectHttpHandler(AsyncHandler):
    """Fast HTTP attempts using rule headers and a crawler-UA fallback."""

    async def process(self, context: Context) -> dict[str, Any] | None:
        steps = [
            step
            for step in context.options.plan
            if step in {"http_primary", "http_googlebot_fallback"}
        ]
        for step in steps:
            started_at = time.perf_counter()
            if step == "http_primary":
                active_strategy = context.strategy
                timeout = TIMEOUT
                label = _hit_label(context.strategy, step)
            else:
                active_strategy = SiteStrategy(domain=context.domain, useragent="googlebot")
                timeout = GOOGLEBOT_TIMEOUT
                label = "http_googlebot_fallback"

            try:
                from .browser import get_domain_rate_limiter

                limiter = get_domain_rate_limiter()
                async with limiter.limit(context.domain):
                    async with asyncio.timeout(timeout):
                        html, status = await fetch_page(
                            context.url,
                            active_strategy,
                            context.client,
                            timeout=timeout,
                        )
            except SSRFBlocked as exc:
                context.strategy_hit.append(label)
                context.note_failure(
                    "SSRF_BLOCKED",
                    error=str(exc),
                    failure_class="config",
                    engine="http",
                )
                context.record_attempt(
                    handler=self.__class__.__name__,
                    label=label,
                    engine="http",
                    status=0,
                    started_at=started_at,
                    error_code="SSRF_BLOCKED",
                    error=str(exc),
                )
                return context.failure_result()
            except TimeoutError as exc:
                error_label = f"{label}_error"
                context.strategy_hit.append(error_label)
                context.note_failure(
                    "NETWORK",
                    error=f"timeout after {timeout:.1f}s",
                    failure_class="network",
                    engine="http",
                )
                context.record_attempt(
                    handler=self.__class__.__name__,
                    label=error_label,
                    engine="http",
                    status=0,
                    started_at=started_at,
                    error_code="NETWORK",
                    error=str(exc) or "timeout",
                )
                continue
            except Exception as exc:
                error_label = f"{label}_error"
                context.strategy_hit.append(error_label)
                context.note_failure(
                    "NETWORK",
                    error=str(exc),
                    failure_class="network",
                    engine="http",
                )
                context.record_attempt(
                    handler=self.__class__.__name__,
                    label=error_label,
                    engine="http",
                    status=0,
                    started_at=started_at,
                    error_code="NETWORK",
                    error=str(exc),
                )
                continue

            context.strategy_hit.append(label)
            context.note_response(html, status, engine="http")
            access = classify_access_control_page(html, status=status)
            if access.detected:
                code = "BOT_CHALLENGE" if access.challenge else "HTTP_BLOCKED"
                context.note_failure(
                    code,
                    error=access.reason,
                    status=status,
                    engine="http",
                )
                context.record_attempt(
                    handler=self.__class__.__name__,
                    label=label,
                    engine="http",
                    status=status,
                    started_at=started_at,
                    error_code=code,
                    error=access.reason,
                )
                continue

            if 200 <= status < 300 and html:
                evaluation = await _evaluate_candidate(
                    html,
                    context.url,
                    context.domain,
                    dom_result=None,
                    allow_partial=context.options.allow_partial,
                    strategy_hit=context.strategy_hit,
                    rule_version=context.options.rule_version,
                    engine="http",
                    t0=context.started_at,
                    full_markdown=context.options.full_markdown,
                    warnings=context.warnings,
                )
                context.last_quality = evaluation.quality
                if evaluation.result is not None:
                    context.record_attempt(
                        handler=self.__class__.__name__,
                        label=label,
                        engine="http",
                        status=status,
                        started_at=started_at,
                        quality_reason=str(getattr(evaluation.quality, "reason", "pass")),
                    )
                    return evaluation.result
                code, error, failure_class = _quality_failure(evaluation)
                context.note_failure(
                    code,
                    error=error,
                    failure_class=failure_class,
                    status=status,
                    engine="http",
                )
                context.record_attempt(
                    handler=self.__class__.__name__,
                    label=label,
                    engine="http",
                    status=status,
                    started_at=started_at,
                    error_code=code,
                    error=error,
                    quality_reason=str(getattr(evaluation.quality, "reason", "")),
                )
                continue

            code, failure_class = _classify_http_response(status, html)
            error = f"HTTP {status}" if status else "empty_response"
            context.note_failure(
                code,
                error=error,
                failure_class=failure_class,
                status=status,
                engine="http",
            )
            context.record_attempt(
                handler=self.__class__.__name__,
                label=label,
                engine="http",
                status=status,
                started_at=started_at,
                error_code=code,
                error=error,
            )
        return None


class StealthBrowserHandler(AsyncHandler):
    """Playwright handler using stealth masks, resource blocking, and pooling."""

    async def process(self, context: Context) -> dict[str, Any] | None:
        if "browser_cleanup" not in context.options.plan:
            return None
        started_at = time.perf_counter()
        try:
            from .browser import fetch_for_strategy

            browser_result = await fetch_for_strategy(context.url, context.strategy)
        except Exception as exc:
            context.strategy_hit.extend(("browser_cleanup", "browser_cleanup_error"))
            context.add_warning(f"browser:{exc}")
            context.note_failure(
                "BROWSER_UNAVAILABLE",
                error=str(exc),
                failure_class="config",
                engine="browser",
            )
            context.record_attempt(
                handler=self.__class__.__name__,
                label="browser_cleanup_error",
                engine="browser",
                status=0,
                started_at=started_at,
                error_code="BROWSER_UNAVAILABLE",
                error=str(exc),
            )
            return None

        context.strategy_hit.append("browser_cleanup")
        context.note_response(
            browser_result.html,
            browser_result.status,
            engine=browser_result.engine or "browser",
            dom_result=browser_result.dom_result,
        )
        if not browser_result.ok:
            context.strategy_hit.append("browser_cleanup_fail")
            code = browser_result.error_code or _classify_http_response(
                browser_result.status,
                browser_result.html,
            )[0]
            failure_class = _FAILURE_CLASS_BY_CODE.get(code, "config")
            error = browser_result.error_msg or (
                f"HTTP {browser_result.status}" if browser_result.status else "browser_fetch_failed"
            )
            context.note_failure(
                code,
                error=error,
                failure_class=failure_class,
                status=browser_result.status,
                engine=browser_result.engine or "browser",
            )
            if browser_result.challenge_provider:
                context.add_warning(f"challenge_provider:{browser_result.challenge_provider}")
            if code in {"BROWSER_UNAVAILABLE", "NETWORK"} and error:
                context.add_warning(f"browser:{error}")
            context.record_attempt(
                handler=self.__class__.__name__,
                label="browser_cleanup_fail",
                engine=browser_result.engine or "browser",
                status=browser_result.status,
                started_at=started_at,
                error_code=code,
                error=error,
            )
            return None

        evaluation = await _evaluate_candidate(
            browser_result.html,
            context.url,
            context.domain,
            dom_result=browser_result.dom_result,
            allow_partial=context.options.allow_partial,
            strategy_hit=context.strategy_hit,
            rule_version=context.options.rule_version,
            engine=browser_result.engine or "browser",
            t0=context.started_at,
            full_markdown=context.options.full_markdown,
            warnings=context.warnings,
        )
        context.last_quality = evaluation.quality
        if evaluation.result is not None:
            context.record_attempt(
                handler=self.__class__.__name__,
                label="browser_cleanup",
                engine=browser_result.engine or "browser",
                status=browser_result.status,
                started_at=started_at,
                quality_reason=str(getattr(evaluation.quality, "reason", "pass")),
            )
            return evaluation.result

        code, error, failure_class = _quality_failure(evaluation)
        context.note_failure(
            code,
            error=error,
            failure_class=failure_class,
            status=browser_result.status,
            engine=browser_result.engine or "browser",
        )
        context.record_attempt(
            handler=self.__class__.__name__,
            label="browser_cleanup",
            engine=browser_result.engine or "browser",
            status=browser_result.status,
            started_at=started_at,
            error_code=code,
            error=error,
            quality_reason=str(getattr(evaluation.quality, "reason", "")),
        )
        return None


class ArchiveFallbackHandler(AsyncHandler):
    """Conditional archive.is and Internet Archive snapshot fallback."""

    async def process(self, context: Context) -> dict[str, Any] | None:
        steps = [
            step
            for step in context.options.plan
            if step in {"archive_is", "archive_org"}
        ]
        for step in steps:
            if step == "archive_is":
                last_failure = _archive_is_fail_at.get(context.domain)
                if last_failure and time.monotonic() - last_failure < ARCHIVE_IS_COOLDOWN_S:
                    context.strategy_hit.append("archive_is_skipped_cooldown")
                    continue

            started_at = time.perf_counter()
            if step == "archive_is":
                archive_url = f"https://archive.is/newest/{quote(context.url, safe='')}"
                label = "archive_is"
            else:
                archive_url = f"https://web.archive.org/web/2/{context.url}"
                label = "archive_org"

            try:
                assert_public_url(archive_url)
                from .browser import get_domain_rate_limiter

                archive_domain = urlparse(archive_url).hostname or label
                limiter = get_domain_rate_limiter()
                async with limiter.limit(archive_domain):
                    async with asyncio.timeout(TIMEOUT):
                        html, status = await fetch_page(
                            archive_url,
                            SiteStrategy(domain=context.domain, useragent=""),
                            context.client,
                            timeout=TIMEOUT,
                        )
            except SSRFBlocked as exc:
                context.strategy_hit.append(f"{step}_error")
                context.note_failure(
                    "SSRF_BLOCKED",
                    error=str(exc),
                    failure_class="config",
                    engine=label,
                )
                context.record_attempt(
                    handler=self.__class__.__name__,
                    label=f"{step}_error",
                    engine=label,
                    status=0,
                    started_at=started_at,
                    error_code="SSRF_BLOCKED",
                    error=str(exc),
                )
                return context.failure_result()
            except Exception as exc:
                if step == "archive_is":
                    _archive_is_fail_at[context.domain] = time.monotonic()
                context.strategy_hit.append(f"{step}_error")
                context.note_failure(
                    "ARCHIVE_FAILED",
                    error=str(exc),
                    failure_class="network",
                    engine=label,
                )
                context.record_attempt(
                    handler=self.__class__.__name__,
                    label=f"{step}_error",
                    engine=label,
                    status=0,
                    started_at=started_at,
                    error_code="ARCHIVE_FAILED",
                    error=str(exc),
                )
                continue

            context.strategy_hit.append(label)
            context.note_response(html, status, engine=label)
            if step == "archive_is" and status in {403, 429, 503}:
                _archive_is_fail_at[context.domain] = time.monotonic()

            access = classify_access_control_page(html, status=status)
            if access.detected:
                archive_error = (
                    f"{label}:{'BOT_CHALLENGE' if access.challenge else 'HTTP_BLOCKED'}:"
                    f"{access.reason}"
                )
                context.note_failure(
                    "ARCHIVE_FAILED",
                    error=archive_error,
                    failure_class="network",
                    status=status,
                    engine=label,
                )
                context.record_attempt(
                    handler=self.__class__.__name__,
                    label=label,
                    engine=label,
                    status=status,
                    started_at=started_at,
                    error_code="ARCHIVE_FAILED",
                    error=archive_error,
                )
                continue

            if 200 <= status < 300 and html:
                evaluation = await _evaluate_candidate(
                    html,
                    context.url,
                    context.domain,
                    dom_result=None,
                    allow_partial=context.options.allow_partial,
                    strategy_hit=context.strategy_hit,
                    rule_version=context.options.rule_version,
                    engine=label,
                    t0=context.started_at,
                    full_markdown=context.options.full_markdown,
                    warnings=context.warnings,
                )
                context.last_quality = evaluation.quality
                if evaluation.result is not None:
                    context.record_attempt(
                        handler=self.__class__.__name__,
                        label=label,
                        engine=label,
                        status=status,
                        started_at=started_at,
                        quality_reason=str(getattr(evaluation.quality, "reason", "pass")),
                    )
                    return evaluation.result
                code, error, failure_class = _quality_failure(evaluation)
                context.note_failure(
                    code,
                    error=error,
                    failure_class=failure_class,
                    status=status,
                    engine=label,
                )
                context.record_attempt(
                    handler=self.__class__.__name__,
                    label=label,
                    engine=label,
                    status=status,
                    started_at=started_at,
                    error_code=code,
                    error=error,
                    quality_reason=str(getattr(evaluation.quality, "reason", "")),
                )
                continue

            archive_error = f"{label}:HTTP {status}" if status else f"{label}:fetch_failed"
            context.note_failure(
                "ARCHIVE_FAILED",
                error=archive_error,
                failure_class="network",
                status=status,
                engine=label,
            )
            context.record_attempt(
                handler=self.__class__.__name__,
                label=label,
                engine=label,
                status=status,
                started_at=started_at,
                error_code="ARCHIVE_FAILED",
                error=archive_error,
            )
        return None


def build_headers(strategy: SiteStrategy | None) -> dict[str, str]:
    """Build request headers from a BPC site strategy."""

    headers: dict[str, str] = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if strategy is None:
        headers["User-Agent"] = UA_GOOGLEBOT
        headers["Referer"] = REFERER_GOOGLE
        return headers

    if strategy.useragent_custom:
        headers["User-Agent"] = strategy.useragent_custom
    else:
        user_agent = (strategy.useragent or "").lower()
        if user_agent == "googlebot":
            headers["User-Agent"] = UA_GOOGLEBOT
        elif user_agent == "bingbot":
            headers["User-Agent"] = UA_BINGBOT
        elif user_agent in {"facebookbot", "facebook"}:
            headers["User-Agent"] = UA_FACEBOOKBOT
        else:
            headers["User-Agent"] = UA_NORMAL

    if strategy.referer_custom:
        headers["Referer"] = strategy.referer_custom
    else:
        referer = (strategy.referer or "").lower()
        if referer == "google":
            headers["Referer"] = REFERER_GOOGLE
        elif referer == "facebook":
            headers["Referer"] = REFERER_FACEBOOK
        elif referer == "twitter":
            headers["Referer"] = REFERER_TWITTER
        elif not strategy.useragent and not strategy.useragent_custom:
            headers["Referer"] = REFERER_GOOGLE

    if strategy.random_ip:
        headers["X-Forwarded-For"] = (
            f"{random.randint(1, 223)}.{random.randint(0, 255)}."
            f"{random.randint(0, 255)}.{random.randint(1, 254)}"
        )
    return headers


def build_fallback_headers() -> dict[str, str]:
    """Return the stable crawler-UA fallback header set."""

    return {
        "User-Agent": UA_GOOGLEBOT,
        "Referer": REFERER_GOOGLE,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _hit_label(strategy: SiteStrategy | None, step: str) -> str:
    if step == "http_primary" and strategy:
        if strategy.referer_custom:
            return "http_referer_custom"
        if strategy.useragent_custom:
            return "http_ua_custom"
        if strategy.useragent:
            return f"http_ua_{strategy.useragent}"
        return "http_headers"
    return step


def build_plan(
    strategy: SiteStrategy | None,
    use_browser: bool | None = None,
    *,
    force_archive: bool = False,
) -> list[str]:
    """Build the backward-compatible execution-step list."""

    steps = ["http_primary"]
    is_bot = False
    if strategy:
        user_agent = (strategy.useragent or "").lower()
        is_bot = user_agent in {"googlebot", "bingbot", "facebookbot", "facebook"} or bool(
            strategy.useragent_custom and "google" in strategy.useragent_custom.lower()
        )
    if not is_bot:
        steps.append("http_googlebot_fallback")

    want_browser = use_browser
    if want_browser is None:
        want_browser = bool(
            strategy
            and (
                strategy.block_regex
                or strategy.needs_browser_cleanup()
                or strategy.useragent_custom
            )
        )
    if want_browser:
        steps.append("browser_cleanup")

    want_archive = force_archive or bool(
        strategy and (strategy.extra or {}).get("archive")
    )
    if want_archive:
        steps.extend(("archive_is", "archive_org"))
    return steps


def plan_execution_steps(
    strategy: SiteStrategy | None,
    use_browser: bool | None = None,
    *,
    force_archive: bool = False,
) -> list[str]:
    """Compatibility alias for callers that use the explicit planner name."""

    return build_plan(strategy, use_browser, force_archive=force_archive)


async def fetch_page(
    url: str,
    strategy: SiteStrategy | None = None,
    client: httpx.AsyncClient | None = None,
    *,
    timeout: float = TIMEOUT,
) -> tuple[str, int]:
    """Fetch HTML with manually validated redirect hops and SSRF protection."""

    assert_public_url(url)
    headers = build_headers(strategy) if strategy else build_fallback_headers()
    own_client = client is None
    active_client: Any = client
    if active_client is None:
        active_client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
            trust_env=True,
        )
    current_url = url
    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            response = await active_client.get(
                current_url,
                headers=headers,
                follow_redirects=False,
            )
            if not response.is_redirect:
                return response.text, int(response.status_code)
            location = response.headers.get("location")
            if not location:
                return response.text, int(response.status_code)
            if redirect_count >= MAX_REDIRECTS:
                return response.text, int(response.status_code)
            current_url = urljoin(str(response.url), location)
            assert_public_url(current_url)
        raise RuntimeError("redirect limit exceeded")
    finally:
        if own_client:
            await active_client.aclose()


def _classify_http_response(status: int, body: str) -> tuple[str, str]:
    access = classify_access_control_page(body, status=status)
    if access.detected:
        return ("BOT_CHALLENGE", "bot") if access.challenge else ("HTTP_BLOCKED", "bot")
    if status in {401, 403, 407, 429, 451}:
        return "HTTP_BLOCKED", "bot"
    if status == 0 or status >= 500:
        return "NETWORK", "network"
    if 200 <= status < 300:
        return "EXTRACT_FAILED", "extract"
    return classify_http_failure(status, body)


def _run_quality_check(
    text: str,
    title: str,
    *,
    allow_partial: bool,
    html: str,
    dom_result: Mapping[str, Any] | None,
) -> QualityResult | Any:
    try:
        return quality_check(
            text,
            title,
            allow_partial=allow_partial,
            html=html,
            dom_metrics=dom_result,
        )
    except TypeError:
        return quality_check(text, title, allow_partial=allow_partial)


async def _evaluate_candidate(
    html: str,
    url: str,
    domain: str,
    *,
    dom_result: dict[str, Any] | None,
    allow_partial: bool,
    strategy_hit: list[str],
    rule_version: str,
    engine: str,
    t0: float,
    full_markdown: bool,
    warnings: list[str] | None = None,
) -> CandidateEvaluation:
    try:
        from .extract import article_to_markdown, extract_article

        article = extract_article(html, url, dom_result=dom_result)
    except Exception as exc:
        return CandidateEvaluation(
            result=None,
            quality=None,
            article={},
            markdown="",
            error=f"extract_error:{exc}",
        )

    text = str(article.get("text") or "")
    title = str(article.get("title") or "")
    quality = _run_quality_check(
        text,
        title,
        allow_partial=allow_partial,
        html=html,
        dom_result=dom_result,
    )
    if not bool(getattr(quality, "ok", False)):
        return CandidateEvaluation(
            result=None,
            quality=quality,
            article=article,
            markdown="",
        )

    try:
        markdown = article_to_markdown(article, images_dir="images")
    except Exception as exc:
        return CandidateEvaluation(
            result=None,
            quality=quality,
            article=article,
            markdown="",
            error=f"markdown_error:{exc}",
        )

    successful_hits = list(strategy_hit)
    if not successful_hits or successful_hits[-1] != "final_quality_pass":
        successful_hits.append("final_quality_pass")
    result = ok_result(
        url=url,
        domain=domain,
        title=title,
        markdown=markdown,
        strategy_hit=successful_hits,
        rule_version=rule_version,
        engine=engine,
        latency_ms=int((time.perf_counter() - t0) * 1000),
        warnings=list(warnings or []),
        paywall_suspected=bool(getattr(quality, "paywall_suspected", False)),
        full_markdown=full_markdown,
        extra={"_image_urls": list(article.get("images") or [])},
    )
    return CandidateEvaluation(
        result=result,
        quality=quality,
        article=article,
        markdown=markdown,
    )


def _quality_failure(evaluation: CandidateEvaluation) -> tuple[str, str, str]:
    if evaluation.error:
        return "EXTRACT_FAILED", evaluation.error, "extract"
    quality = evaluation.quality
    if quality is None:
        return "EXTRACT_FAILED", "empty_extraction", "extract"
    code = str(getattr(quality, "error_code", "") or "EXTRACT_FAILED")
    if code not in _FAILURE_PRIORITY:
        code = "EXTRACT_FAILED"
    error = str(getattr(quality, "reason", "") or code)
    failure_class = _FAILURE_CLASS_BY_CODE.get(code, "extract")
    return code, error, failure_class


async def _try_extract_ok(
    html: str,
    url: str,
    domain: str,
    *,
    dom_result: dict[str, Any] | None,
    allow_partial: bool,
    strategy_hit: list[str],
    rule_version: str,
    engine: str,
    t0: float,
    full_markdown: bool,
    warnings: list[str] | None = None,
) -> dict[str, Any] | None:
    """Backward-compatible extraction helper used by existing callers/tests."""

    evaluation = await _evaluate_candidate(
        html,
        url,
        domain,
        dom_result=dom_result,
        allow_partial=allow_partial,
        strategy_hit=strategy_hit,
        rule_version=rule_version,
        engine=engine,
        t0=t0,
        full_markdown=full_markdown,
        warnings=warnings,
    )
    return evaluation.result


def _build_handler_chain(plan: Sequence[str]) -> AsyncHandler:
    handlers: list[AsyncHandler] = []
    if any(step in {"http_primary", "http_googlebot_fallback"} for step in plan):
        handlers.append(DirectHttpHandler())
    if "browser_cleanup" in plan:
        handlers.append(StealthBrowserHandler())
    if any(step in {"archive_is", "archive_org"} for step in plan):
        handlers.append(ArchiveFallbackHandler())
    if not handlers:
        handlers.append(DirectHttpHandler())
    for current, following in zip(handlers, handlers[1:]):
        current.set_next(following)
    return handlers[0]


async def fetch_article(
    url: str,
    strategy: SiteStrategy | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    use_browser: bool | None = None,
    allow_partial: bool = False,
    rule_version: str = "",
    force_archive: bool = False,
    full_markdown: bool = False,
    domain: str | None = None,
) -> dict[str, Any]:
    """Fetch and extract an article through the asynchronous handler chain."""

    from .sites import domain_from_url

    started_at = time.perf_counter()
    resolved_domain = domain or domain_from_url(url)
    try:
        assert_public_url(url)
    except SSRFBlocked as exc:
        return fail_result(
            url=url,
            domain=resolved_domain,
            error_code="SSRF_BLOCKED",
            failure_class="config",
            error=str(exc),
            strategy_hit=[],
            rule_version=rule_version,
            latency_ms=int((time.perf_counter() - started_at) * 1000),
        )

    plan = tuple(
        plan_execution_steps(
            strategy,
            use_browser,
            force_archive=force_archive,
        )
    )
    options = FetchOptions(
        use_browser=use_browser,
        allow_partial=allow_partial,
        rule_version=rule_version,
        force_archive=force_archive,
        full_markdown=full_markdown,
        plan=plan,
    )

    own_client = client is None
    active_client: Any = client
    if active_client is None:
        active_client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=TIMEOUT,
            trust_env=True,
        )
    context = Context(
        url=url,
        domain=resolved_domain,
        strategy=strategy,
        options=options,
        client=active_client,
        started_at=started_at,
    )

    try:
        chain = _build_handler_chain(plan)
        result = await chain.handle(context)
        if result is not None:
            return result
        return context.failure_result()
    except SSRFBlocked as exc:
        context.note_failure(
            "SSRF_BLOCKED",
            error=str(exc),
            failure_class="config",
        )
        return context.failure_result()
    except Exception as exc:
        context.note_failure(
            "INTERNAL",
            error=str(exc),
            failure_class="config",
            engine=context.last_engine,
        )
        return context.failure_result()
    finally:
        if own_client:
            await active_client.aclose()


async def fetch_with_retries(
    url: str,
    strategy: SiteStrategy | None = None,
    client: httpx.AsyncClient | None = None,
    use_browser: bool | None = None,
) -> tuple[str, int, dict[str, Any] | None]:
    """Backward-compatible thin wrapper for legacy callers."""

    result = await fetch_article(
        url,
        strategy,
        client=client,
        use_browser=use_browser,
        full_markdown=True,
    )
    if result.get("ok"):
        return "", 200, None
    return "", int(result.get("http_status") or 0), None
