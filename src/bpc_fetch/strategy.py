"""Asynchronous chain-of-responsibility fetch pipeline for PAC-Engine.

The module keeps the original public API and result envelope while replacing
its monolithic waterfall with composable HTTP, browser, and archive
handlers.  Every successful candidate is validated by the structural quality
gate before the pipeline short-circuits.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urljoin, urlparse

import httpx

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
except ImportError:  # optional protocol-level impersonation
    CurlAsyncSession = None  # type: ignore[assignment,misc]

from .quality import (
    QualityResult,
    classify_access_control_page,
    html_has_content,
    quality_check,
)
from .result import (
    attach_diagnostics, classify_http_failure, fail_result, new_request_id, ok_result,
)
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
CURL_CFFI_IMPERSONATE = os.environ.get("PAC_CURL_IMPERSONATE", "chrome").strip() or "chrome"
try:
    ARCHIVE_IS_COOLDOWN_S = max(0.0, float(os.environ.get("PAC_ARCHIVE_COOLDOWN_S", "300")))
except ValueError:
    ARCHIVE_IS_COOLDOWN_S = 300.0
_archive_is_fail_at: dict[str, float] = {}
# Mirror family of archive.today: short-suffix hosts redirect among each
# other, so including them all only widens availability when one is slow or
# refusing connections.  archive.gf is dead and intentionally excluded.
ARCHIVE_TODAY_HOSTS = tuple(
    host.strip().lower()
    for host in os.environ.get(
        "PAC_ARCHIVE_TODAY_HOSTS",
        "archive.today,archive.ph,archive.vn,archive.md,archive.is,archive.li,archive.fo",
    ).split(",")
    if host.strip()
) or ("archive.today", "archive.ph")
WAYBACK_AVAILABLE_ENDPOINT = "https://archive.org/wayback/available"
_AUTO_HTTP_PROXY = object()

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
    # Caller-supplied cookie header (``--cookie`` / ``PAC_COOKIE_FILE``).
    # Applied only to the target domain and archive.today mirror gateways,
    # never to third-party reader gateways.
    cookie_header: str = ""


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


StrategyHandler = AsyncHandler


class Pipeline:
    """Thin orchestration wrapper around the existing handler chain."""

    def __init__(self, first_handler: AsyncHandler) -> None:
        self._first_handler = first_handler

    async def run(self, context: Context) -> dict[str, Any] | None:
        return await self._first_handler.handle(context)


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
                            cookie_header=context.options.cookie_header,
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
    """Browser handler using the selected driver, resource blocking, and pooling."""

    async def process(self, context: Context) -> dict[str, Any] | None:
        if "browser_cleanup" not in context.options.plan:
            return None
        started_at = time.perf_counter()
        try:
            from .browser import fetch_for_strategy

            browser_result = await fetch_for_strategy(
                context.url, context.strategy, cookie_header=context.options.cookie_header
            )
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


class MultiGatewayArchiveHandler(AsyncHandler):
    """Resilient archive ladder: archive.today/ph -> Wayback -> reader gateway.

    The handler is activated by the same ``archive_is`` / ``archive_org`` plan
    markers used by Phase 2.  This deliberately keeps planning and CLI behavior
    stable while making the archive step internally composite.
    """

    async def process(self, context: Context) -> dict[str, Any] | None:
        if not any(step in {"archive_is", "archive_org"} for step in context.options.plan):
            return None

        result = await self._try_archive_today(context)
        if result is not None:
            return result

        result = await self._try_wayback(context)
        if result is not None:
            return result

        result = await self._try_firecrawl_gateway(context)
        if result is not None:
            return result

        return await self._try_reader_gateway(context)

    async def _try_archive_today(self, context: Context) -> dict[str, Any] | None:
        last_failure = _archive_is_fail_at.get(context.domain)
        if last_failure and time.monotonic() - last_failure < ARCHIVE_IS_COOLDOWN_S:
            context.strategy_hit.append("archive_is_skipped_cooldown")
            return None

        primary_failed = False
        for index, host in enumerate(ARCHIVE_TODAY_HOSTS):
            label = "archive_is" if index == 0 else ("archive_ph" if host == "archive.ph" else f"archive_{host.split('.')[0]}")
            archive_url = f"https://{host}/newest/{quote(context.url, safe='')}"
            outcome = await self._attempt_html_gateway(
                context,
                archive_url,
                label=label,
                engine=label,
                failure_code="ARCHIVE_FAILED",
            )
            if outcome.result is not None:
                return outcome.result
            if outcome.cooldown:
                primary_failed = True
            if outcome.fatal_result is not None:
                return outcome.fatal_result

        if primary_failed:
            _archive_is_fail_at[context.domain] = time.monotonic()
        return None

    async def _try_wayback(self, context: Context) -> dict[str, Any] | None:
        lookup_started = time.perf_counter()
        lookup_url = f"{WAYBACK_AVAILABLE_ENDPOINT}?url={quote(context.url, safe='')}"
        try:
            assert_public_url(lookup_url)
            lookup_html, lookup_status = await self._limited_fetch(
                context,
                lookup_url,
                SiteStrategy(domain="archive.org", useragent=""),
                timeout=TIMEOUT,
            )
        except SSRFBlocked as exc:
            context.strategy_hit.append("archive_org_lookup_error")
            context.note_failure(
                "SSRF_BLOCKED",
                error=str(exc),
                failure_class="config",
                engine="archive_org",
            )
            context.record_attempt(
                handler=self.__class__.__name__,
                label="archive_org_lookup_error",
                engine="archive_org",
                status=0,
                started_at=lookup_started,
                error_code="SSRF_BLOCKED",
                error=str(exc),
            )
            return context.failure_result()
        except Exception as exc:
            context.strategy_hit.append("archive_org_lookup_error")
            context.note_failure(
                "ARCHIVE_FAILED",
                error=f"archive_org_lookup:{exc}",
                failure_class="network",
                engine="archive_org",
            )
            context.record_attempt(
                handler=self.__class__.__name__,
                label="archive_org_lookup_error",
                engine="archive_org",
                status=0,
                started_at=lookup_started,
                error_code="ARCHIVE_FAILED",
                error=str(exc),
            )
            return None

        context.strategy_hit.append("archive_org_lookup")
        context.record_attempt(
            handler=self.__class__.__name__,
            label="archive_org_lookup",
            engine="archive_org",
            status=lookup_status,
            started_at=lookup_started,
            error_code="" if lookup_status == 200 else "ARCHIVE_FAILED",
            error="" if lookup_status == 200 else f"HTTP {lookup_status}",
        )
        if lookup_status != 200:
            context.note_failure(
                "ARCHIVE_FAILED",
                error=f"archive_org_lookup:HTTP {lookup_status}",
                failure_class="network",
                status=lookup_status,
                engine="archive_org",
            )
            return None

        snapshot_url = _parse_wayback_snapshot_url(lookup_html)
        if not snapshot_url:
            context.note_failure(
                "ARCHIVE_FAILED",
                error="archive_org:no_available_snapshot",
                failure_class="network",
                status=lookup_status,
                engine="archive_org",
            )
            return None

        parsed = urlparse(snapshot_url)
        snapshot_host = (parsed.hostname or "").casefold().rstrip(".")
        if not (
            snapshot_host == "web.archive.org"
            or snapshot_host.endswith(".archive.org")
        ):
            context.note_failure(
                "ARCHIVE_FAILED",
                error=f"archive_org:unexpected_snapshot_host:{snapshot_host or 'empty'}",
                failure_class="network",
                engine="archive_org",
            )
            return None

        outcome = await self._attempt_html_gateway(
            context,
            snapshot_url,
            label="archive_org",
            engine="archive_org",
            failure_code="ARCHIVE_FAILED",
        )
        if outcome.fatal_result is not None:
            return outcome.fatal_result
        return outcome.result

    async def _try_firecrawl_gateway(self, context: Context) -> dict[str, Any] | None:
        """Cloud scrape gateway backed by the caller's Firecrawl subscription.

        Activated by ``PAC_FIRECRAWL_API_KEY`` (or ``FIRECRAWL_API_KEY``).
        Firecrawl's cloud fleet passes the bot walls that local engines cannot
        (DataDome, Cloudflare Turnstile on archive.today mirrors), so it runs
        after the free archive tiers and before generic reader gateways.
        Output stays subject to the same quality gate as every other engine.
        """
        api_key = os.environ.get("PAC_FIRECRAWL_API_KEY", "").strip() or os.environ.get(
            "FIRECRAWL_API_KEY", ""
        ).strip()
        if not api_key:
            return None

        started_at = time.perf_counter()
        label = "firecrawl_gateway"
        api_url = "https://api.firecrawl.dev/v1/scrape"
        payload = {"url": context.url, "formats": ["markdown"], "onlyMainContent": False}
        try:
            assert_public_url(api_url)
            response = await context.client.post(
                api_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=TIMEOUT,
            )
            status = int(response.status_code)
            body = response.text
        except SSRFBlocked as exc:
            context.strategy_hit.append("firecrawl_gateway_error")
            context.note_failure(
                "SSRF_BLOCKED",
                error=str(exc),
                failure_class="config",
                engine="firecrawl",
            )
            return None
        except Exception as exc:
            context.strategy_hit.append("firecrawl_gateway_error")
            context.note_failure(
                "NETWORK",
                error=f"firecrawl_gateway:{exc}",
                failure_class="network",
                engine="firecrawl",
            )
            return None

        context.strategy_hit.append("firecrawl_gateway")
        context.record_attempt(
            handler=self.__class__.__name__,
            label=label,
            engine="firecrawl",
            status=status,
            started_at=started_at,
            error_code="",
            error="",
        )
        if status != 200:
            context.note_failure(
                "ARCHIVE_FAILED",
                error=f"firecrawl_gateway:HTTP {status}",
                failure_class="network",
                engine="firecrawl",
            )
            return None

        try:
            markdown = (response.json().get("data") or {}).get("markdown") or ""
        except Exception:
            markdown = ""
        if not markdown.strip():
            context.note_failure(
                "EXTRACT_FAILED",
                error="firecrawl_gateway:empty_markdown",
                failure_class="extract",
                engine="firecrawl",
            )
            return None

        # Firecrawl hands back markdown; wrap it in minimal article HTML so the
        # same extraction + quality gate pipeline grades this candidate like
        # every other engine.  No special-cased success path.
        import html as _html_mod
        import re as _re_mod

        # Prefer the first markdown heading as the article title; fall back to
        # the URL so the extractor always receives a well-formed shell.
        title_match = _re_mod.search(r"^#\s+(.+)$", markdown, _re_mod.M)
        fallback_title = (
            title_match.group(1).strip() if title_match else context.url
        )
        body_html = (
            "<!DOCTYPE html><html><head><title>"
            + _html_mod.escape(fallback_title, quote=True)
            + "</title></head><body><article><pre>"
            + _html_mod.escape(markdown)
            + "</pre></article></body></html>"
        )
        evaluation = await _evaluate_candidate(
            body_html,
            context.url,
            context.domain,
            dom_result=None,
            allow_partial=context.options.allow_partial,
            strategy_hit=context.strategy_hit,
            rule_version=context.options.rule_version,
            engine="firecrawl",
            t0=context.started_at,
            full_markdown=context.options.full_markdown,
            warnings=context.warnings,
        )
        context.last_quality = evaluation.quality
        if evaluation.result is not None:
            context.strategy_hit.append("firecrawl_gateway_ok")
            return evaluation.result
        context.note_failure(
            "QUALITY_GATE",
            error="firecrawl_gateway:quality_gate",
            failure_class="quality",
            engine="firecrawl",
        )
        return None

    async def _try_reader_gateway(self, context: Context) -> dict[str, Any] | None:
        template = _reader_gateway_template(context.strategy)
        if not template:
            return None
        try:
            gateway_url = _format_reader_gateway_url(template, context.url)
        except ValueError as exc:
            context.add_warning(f"reader_gateway:{exc}")
            return None

        outcome = await self._attempt_html_gateway(
            context,
            gateway_url,
            label="reader_gateway",
            engine="reader_gateway",
            failure_code="ARCHIVE_FAILED",
        )
        if outcome.fatal_result is not None:
            return outcome.fatal_result
        return outcome.result

    async def _limited_fetch(
        self,
        context: Context,
        url: str,
        strategy: SiteStrategy | None,
        *,
        timeout: float,
        cookie_header: str = "",
    ) -> tuple[str, int]:
        from .browser import get_domain_rate_limiter

        gateway_domain = urlparse(url).hostname or context.domain
        limiter = get_domain_rate_limiter()
        async with limiter.limit(gateway_domain):
            async with asyncio.timeout(timeout):
                return await fetch_page(
                    url,
                    strategy,
                    context.client,
                    timeout=timeout,
                    cookie_header=cookie_header,
                )

    async def _attempt_html_gateway(
        self,
        context: Context,
        gateway_url: str,
        *,
        label: str,
        engine: str,
        failure_code: str,
    ) -> "_GatewayOutcome":
        started_at = time.perf_counter()
        gateway_host = (urlparse(gateway_url).hostname or "").casefold().rstrip(".")
        is_archive_today_mirror = gateway_host in ARCHIVE_TODAY_HOSTS or any(
            gateway_host == host or gateway_host == f"www.{host}" for host in ARCHIVE_TODAY_HOSTS
        )
        # Caller cookies unlock the archive.today mirror family only; they are
        # deliberately withheld from other gateways (reader proxies, wayback)
        # to avoid leaking credentials to third parties.
        gateway_cookie = (
            context.options.cookie_header if (context.options.cookie_header and is_archive_today_mirror) else ""
        )
        try:
            assert_public_url(gateway_url)
            html, status = await self._limited_fetch(
                context,
                gateway_url,
                SiteStrategy(domain=gateway_host or context.domain, useragent=""),
                timeout=TIMEOUT,
                cookie_header=gateway_cookie,
            )
        except SSRFBlocked as exc:
            error_label = f"{label}_error"
            context.strategy_hit.append(error_label)
            context.note_failure(
                "SSRF_BLOCKED",
                error=str(exc),
                failure_class="config",
                engine=engine,
            )
            context.record_attempt(
                handler=self.__class__.__name__,
                label=error_label,
                engine=engine,
                status=0,
                started_at=started_at,
                error_code="SSRF_BLOCKED",
                error=str(exc),
            )
            return _GatewayOutcome(
                fatal_result=context.failure_result(),
                cooldown=label.startswith("archive_"),
            )
        except Exception as exc:
            error_label = f"{label}_error"
            context.strategy_hit.append(error_label)
            context.note_failure(
                failure_code,
                error=f"{label}:{exc}",
                failure_class="network",
                engine=engine,
            )
            context.record_attempt(
                handler=self.__class__.__name__,
                label=error_label,
                engine=engine,
                status=0,
                started_at=started_at,
                error_code=failure_code,
                error=str(exc),
            )
            return _GatewayOutcome(cooldown=label in {"archive_is", "archive_ph", "archive_today"})

        context.strategy_hit.append(label)
        context.note_response(html, status, engine=engine)
        access = classify_access_control_page(html, status=status)
        if access.detected:
            error = (
                f"{label}:{'BOT_CHALLENGE' if access.challenge else 'HTTP_BLOCKED'}:"
                f"{access.reason}"
            )
            context.note_failure(
                failure_code,
                error=error,
                failure_class="network",
                status=status,
                engine=engine,
            )
            context.record_attempt(
                handler=self.__class__.__name__,
                label=label,
                engine=engine,
                status=status,
                started_at=started_at,
                error_code=failure_code,
                error=error,
            )
            return _GatewayOutcome(
                cooldown=label.startswith("archive_") and status in {403, 429, 503},
            )

        if 200 <= status < 300 and html:
            evaluation = await _evaluate_candidate(
                html,
                context.url,
                context.domain,
                dom_result=None,
                allow_partial=context.options.allow_partial,
                strategy_hit=context.strategy_hit,
                rule_version=context.options.rule_version,
                engine=engine,
                t0=context.started_at,
                full_markdown=context.options.full_markdown,
                warnings=context.warnings,
            )
            context.last_quality = evaluation.quality
            if evaluation.result is not None:
                context.record_attempt(
                    handler=self.__class__.__name__,
                    label=label,
                    engine=engine,
                    status=status,
                    started_at=started_at,
                    quality_reason=str(getattr(evaluation.quality, "reason", "pass")),
                )
                return _GatewayOutcome(result=evaluation.result)

            code, error, failure_class = _quality_failure(evaluation)
            context.note_failure(
                code,
                error=error,
                failure_class=failure_class,
                status=status,
                engine=engine,
            )
            context.record_attempt(
                handler=self.__class__.__name__,
                label=label,
                engine=engine,
                status=status,
                started_at=started_at,
                error_code=code,
                error=error,
                quality_reason=str(getattr(evaluation.quality, "reason", "")),
            )
            return _GatewayOutcome()

        error = f"{label}:HTTP {status}" if status else f"{label}:fetch_failed"
        context.note_failure(
            failure_code,
            error=error,
            failure_class="network",
            status=status,
            engine=engine,
        )
        context.record_attempt(
            handler=self.__class__.__name__,
            label=label,
            engine=engine,
            status=status,
            started_at=started_at,
            error_code=failure_code,
            error=error,
        )
        return _GatewayOutcome(
            cooldown=label.startswith("archive_") and status in {403, 429, 503},
        )


@dataclass
class _GatewayOutcome:
    result: dict[str, Any] | None = None
    fatal_result: dict[str, Any] | None = None
    cooldown: bool = False


class ArchiveFallbackHandler(MultiGatewayArchiveHandler):
    """Backward-compatible Phase 2 class name for the composite archive handler."""


def _parse_wayback_snapshot_url(payload: str) -> str:
    try:
        document = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(document, Mapping):
        return ""
    archived = document.get("archived_snapshots")
    if not isinstance(archived, Mapping):
        return ""
    closest = archived.get("closest")
    if not isinstance(closest, Mapping):
        return ""
    available = closest.get("available")
    status = str(closest.get("status") or "")
    snapshot_url = str(closest.get("url") or "").strip()
    if available is False or status != "200" or not snapshot_url:
        return ""
    return snapshot_url


def _reader_gateway_template(strategy: SiteStrategy | None) -> str:
    if strategy is not None and isinstance(strategy.extra, Mapping):
        value = strategy.extra.get("reader_gateway") or strategy.extra.get("reader_gateway_url")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return os.environ.get("PAC_READER_GATEWAY", "").strip()


def _format_reader_gateway_url(template: str, target_url: str) -> str:
    value = template.strip()
    if not value:
        raise ValueError("empty_template")
    encoded = quote(target_url, safe="")
    if "{url_encoded}" in value:
        result = value.replace("{url_encoded}", encoded)
    elif "{url}" in value:
        result = value.replace("{url}", target_url)
    else:
        separator = "&" if "?" in value else "?"
        result = f"{value}{separator}url={encoded}"
    parsed = urlparse(result)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("invalid_template_result")
    return result


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

    cookie = _cookie_header(strategy)
    if cookie and "Cookie" not in headers:
        headers["Cookie"] = cookie

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


class _AdaptiveHttpClient:
    """Owned async transport that prefers curl_cffi and fails open to httpx.

    Internally owned clients can select a proxy per attempt.  ``httpx`` clients
    are cached per proxy identity so the Phase 4 circuit breaker can fail over
    without recreating a TCP pool for every request.  Caller-supplied clients
    remain untouched for backward compatibility and deterministic tests.
    """

    def __init__(self, *, timeout: float = TIMEOUT) -> None:
        self._timeout = float(timeout)
        self._curl: Any | None = None
        self._httpx_sessions: dict[str, httpx.AsyncClient] = {}
        self._curl_disabled = False
        self._curl_failure = ""
        self.last_transport = "httpx"

    @property
    def curl_failure(self) -> str:
        return self._curl_failure

    async def _curl_session(self) -> Any | None:
        if self._curl_disabled or CurlAsyncSession is None:
            return None
        if not _curl_cffi_enabled():
            self._curl_disabled = True
            return None
        if self._curl is None:
            try:
                self._curl = CurlAsyncSession()
            except Exception as exc:
                self._curl_failure = str(exc)
                self._curl_disabled = True
                return None
        return self._curl

    async def _httpx_session(self, proxy_override: Any = _AUTO_HTTP_PROXY) -> httpx.AsyncClient:
        proxy_url = ""
        trust_env = True
        key = "env"
        if proxy_override is None:
            trust_env = False
            key = "direct"
        elif proxy_override is not _AUTO_HTTP_PROXY:
            trust_env = False
            proxy_url = str(proxy_override.as_url())
            key = "proxy:" + repr(proxy_override.cache_key())

        session = self._httpx_sessions.get(key)
        if session is None:
            kwargs: dict[str, Any] = {
                "follow_redirects": False,
                "timeout": self._timeout,
                "trust_env": trust_env,
            }
            if proxy_url:
                kwargs["proxy"] = proxy_url
            session = httpx.AsyncClient(**kwargs)
            self._httpx_sessions[key] = session
        return session

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        strategy: SiteStrategy | None,
        proxy_override: Any = _AUTO_HTTP_PROXY,
    ) -> Any:
        curl_session = await self._curl_session()
        if curl_session is not None:
            try:
                request_kwargs: dict[str, Any] = {
                    "headers": dict(headers),
                    "allow_redirects": False,
                    "timeout": float(timeout),
                    "impersonate": CURL_CFFI_IMPERSONATE,
                    "default_headers": False,
                }
                cookies = _strategy_cookies(strategy)
                if cookies:
                    request_kwargs["cookies"] = cookies
                request_kwargs.update(
                    _curl_proxy_kwargs(
                        url,
                        strategy,
                        proxy_override=proxy_override,
                    )
                )
                response = await curl_session.get(url, **request_kwargs)
                self.last_transport = "curl_cffi"
                return response
            except Exception as exc:
                self._curl_failure = str(exc)
                self._curl_disabled = True

        httpx_session = await self._httpx_session(proxy_override)
        self.last_transport = "httpx"
        return await httpx_session.get(
            url,
            headers=dict(headers),
            follow_redirects=False,
            timeout=float(timeout),
        )

    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        timeout: float = TIMEOUT,
    ) -> Any:
        """Plain httpx POST for JSON APIs (cloud scrape gateways).

        curl_cffi impersonation is pointless against API endpoints and its
        header rewriting can corrupt Bearer auth, so this path always uses a
        stock httpx session without proxy steering.
        """
        httpx_session = await self._httpx_session(_AUTO_HTTP_PROXY)
        self.last_transport = "httpx"
        return await httpx_session.post(
            url,
            headers=dict(headers or {}),
            json=json,
            timeout=float(timeout),
        )

    async def aclose(self) -> None:
        if self._curl is not None:
            await _close_async_client(self._curl)
            self._curl = None
        sessions = list(self._httpx_sessions.values())
        self._httpx_sessions.clear()
        for session in sessions:
            await session.aclose()


def _curl_cffi_enabled() -> bool:
    raw = os.environ.get("PAC_CURL_CFFI", "auto").strip().casefold()
    return raw not in {"0", "false", "off", "no", "disabled", "httpx"}


def _is_curl_client(client: Any) -> bool:
    if CurlAsyncSession is None:
        return False
    try:
        return isinstance(client, CurlAsyncSession)
    except TypeError:
        return False


def _strategy_cookies(strategy: SiteStrategy | None) -> Mapping[str, str] | None:
    """Return explicit rule cookies without inventing cookie values.

    Upstream rules normally use ``allow_cookies`` as a persistence flag rather
    than carrying credentials.  This helper only forwards literal mapping data
    if a local override explicitly supplies it under a supported key.
    """

    if strategy is None or not isinstance(strategy.extra, Mapping):
        return None
    for key in ("cookies", "custom_cookies"):
        value = strategy.extra.get(key)
        if isinstance(value, Mapping):
            result = {
                str(cookie_name): str(cookie_value)
                for cookie_name, cookie_value in value.items()
                if str(cookie_name).strip()
            }
            return result or None
    return None


def _cookie_header(strategy: SiteStrategy | None) -> str:
    if strategy is None or not isinstance(strategy.extra, Mapping):
        return ""
    value = strategy.extra.get("cookie") or strategy.extra.get("cookie_header")
    if isinstance(value, str):
        return value.strip()
    return ""


def _curl_proxy_kwargs(
    url: str,
    strategy: SiteStrategy | None,
    *,
    proxy_override: Any = _AUTO_HTTP_PROXY,
) -> dict[str, Any]:
    """Translate PAC proxy selection into curl_cffi request kwargs."""

    proxy: Any | None
    if proxy_override is _AUTO_HTTP_PROXY:
        try:
            from .browser import resolve_proxy

            proxy = resolve_proxy(url, strategy)
        except Exception:
            proxy = None
    else:
        proxy = proxy_override
    if proxy is None:
        return {}
    result: dict[str, Any] = {"proxy": proxy.server}
    if proxy.username or proxy.password:
        result["proxy_auth"] = (proxy.username, proxy.password)
    return result


async def _close_async_client(client: Any) -> None:
    """Close either an httpx client or curl_cffi AsyncSession safely."""

    method = getattr(client, "aclose", None)
    if method is None:
        method = getattr(client, "close", None)
    if method is None:
        return
    result = method()
    if inspect.isawaitable(result):
        await result


def _response_status(response: Any) -> int:
    try:
        return int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _response_text(response: Any) -> str:
    value = getattr(response, "text", "")
    if callable(value):
        value = value()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _response_header(response: Any, name: str) -> str:
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    try:
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
    except Exception:
        return ""
    return str(value or "")


def _response_url(response: Any, fallback: str) -> str:
    value = getattr(response, "url", None)
    return str(value or fallback)


def _is_redirect_response(response: Any) -> bool:
    return _response_status(response) in {301, 302, 303, 307, 308}


async def _request_once(
    client: Any,
    url: str,
    *,
    headers: Mapping[str, str],
    timeout: float,
    strategy: SiteStrategy | None,
    proxy_override: Any = _AUTO_HTTP_PROXY,
) -> Any:
    if isinstance(client, _AdaptiveHttpClient):
        return await client.get(
            url,
            headers=headers,
            timeout=timeout,
            strategy=strategy,
            proxy_override=proxy_override,
        )
    if _is_curl_client(client):
        request_kwargs: dict[str, Any] = {
            "headers": dict(headers),
            "allow_redirects": False,
            "timeout": float(timeout),
            "impersonate": CURL_CFFI_IMPERSONATE,
            "default_headers": False,
        }
        cookies = _strategy_cookies(strategy)
        if cookies:
            request_kwargs["cookies"] = cookies
        request_kwargs.update(
            _curl_proxy_kwargs(url, strategy, proxy_override=proxy_override)
        )
        return await client.get(url, **request_kwargs)
    return await client.get(
        url,
        headers=dict(headers),
        follow_redirects=False,
    )


async def fetch_page(
    url: str,
    strategy: SiteStrategy | None = None,
    client: httpx.AsyncClient | None = None,
    *,
    timeout: float = TIMEOUT,
    cookie_header: str = "",
) -> tuple[str, int]:
    """Fetch HTML with SSRF-safe redirects, TLS impersonation, and proxy failover.

    Internally owned transports rotate through ``PAC_PROXIES`` when a proxy is
    rejected by a WAF.  A caller-supplied client is never replaced or silently
    reconfigured.
    """

    assert_public_url(url)
    headers = build_headers(strategy) if strategy else build_fallback_headers()
    cookie_header = (cookie_header or "").strip() or _cookie_header(strategy)
    if cookie_header and "Cookie" not in headers:
        headers["Cookie"] = cookie_header

    own_client = client is None
    active_client: Any = client
    if active_client is None:
        active_client = _AdaptiveHttpClient(timeout=timeout)

    managed_failover = isinstance(active_client, _AdaptiveHttpClient)
    if managed_failover:
        try:
            from .browser import resolve_proxy_candidates

            proxy_candidates: list[Any] = resolve_proxy_candidates(url, strategy)
        except Exception:
            proxy_candidates = [_AUTO_HTTP_PROXY]
    else:
        proxy_candidates = [_AUTO_HTTP_PROXY]

    last_text = ""
    last_status = 0
    last_transport_error: Exception | None = None
    try:
        for proxy in proxy_candidates:
            current_url = url
            try:
                for redirect_count in range(MAX_REDIRECTS + 1):
                    assert_public_url(current_url)
                    response = await _request_once(
                        active_client,
                        current_url,
                        headers=headers,
                        timeout=timeout,
                        strategy=strategy,
                        proxy_override=proxy,
                    )
                    status = _response_status(response)
                    text = _response_text(response)
                    last_text, last_status = text, status
                    if not _is_redirect_response(response):
                        break
                    location = _response_header(response, "location")
                    if not location or redirect_count >= MAX_REDIRECTS:
                        break
                    current_url = urljoin(_response_url(response, current_url), location)
                    assert_public_url(current_url)
                else:
                    raise RuntimeError("redirect limit exceeded")
            except SSRFBlocked:
                raise
            except Exception as exc:
                if managed_failover and proxy is not _AUTO_HTTP_PROXY and proxy is not None:
                    last_transport_error = exc
                    continue
                raise

            if managed_failover and proxy is not _AUTO_HTTP_PROXY and proxy is not None:
                try:
                    from .browser import get_proxy_circuit_breaker

                    breaker = get_proxy_circuit_breaker()
                    error_code, _ = _classify_http_response(last_status, last_text)
                    if error_code in {"BOT_CHALLENGE", "HTTP_BLOCKED"}:
                        breaker.mark_failure(proxy, error_code)
                        continue
                    if error_code == "NETWORK":
                        continue
                    if 200 <= last_status < 400:
                        breaker.mark_success(proxy)
                except Exception:
                    pass
            return last_text, last_status
        if last_transport_error is not None:
            raise last_transport_error
        return last_text, last_status
    finally:
        if own_client:
            await _close_async_client(active_client)


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
        handlers.append(MultiGatewayArchiveHandler())
    if not handlers:
        handlers.append(DirectHttpHandler())
    for current, following in zip(handlers, handlers[1:]):
        current.set_next(following)
    return handlers[0]


def _build_pipeline(plan: Sequence[str]) -> Pipeline:
    return Pipeline(_build_handler_chain(plan))


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
    diagnostics: bool = False,
    request_id: str | None = None,
    cookie_header: str = "",
) -> dict[str, Any]:
    """Fetch and extract an article through the asynchronous handler chain."""

    from .sites import domain_from_url

    started_at = time.perf_counter()
    resolved_domain = domain or domain_from_url(url)
    diagnostic_request_id = (request_id or new_request_id()) if diagnostics else ""
    try:
        assert_public_url(url)
    except SSRFBlocked as exc:
        elapsed = int((time.perf_counter() - started_at) * 1000)
        result = fail_result(
            url=url,
            domain=resolved_domain,
            error_code="SSRF_BLOCKED",
            failure_class="config",
            error=str(exc),
            strategy_hit=[],
            rule_version=rule_version,
            latency_ms=elapsed,
        )
        if diagnostics:
            attach_diagnostics(
                result,
                request_id=diagnostic_request_id,
                total_latency_ms=elapsed,
                attempts=[{
                    "handler": "SSRFGuard",
                    "label": "initial_url",
                    "engine": "validation",
                    "status": 0,
                    "elapsed_ms": elapsed,
                    "error_code": "SSRF_BLOCKED",
                    "error": str(exc),
                    "quality_reason": "",
                }],
            )
        return result

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
        cookie_header=(cookie_header or "").strip(),
    )

    own_client = client is None
    active_client: Any = client
    if active_client is None:
        active_client = _AdaptiveHttpClient(timeout=TIMEOUT)
    context = Context(
        url=url,
        domain=resolved_domain,
        strategy=strategy,
        options=options,
        client=active_client,
        started_at=started_at,
    )

    try:
        pipeline = _build_pipeline(plan)
        result = await pipeline.run(context)
        if result is None:
            result = context.failure_result()
        if diagnostics:
            attach_diagnostics(
                result,
                request_id=diagnostic_request_id,
                total_latency_ms=context.elapsed_ms(),
                attempts=context.attempts,
                quality=context.last_quality,
            )
        return result
    except SSRFBlocked as exc:
        context.note_failure(
            "SSRF_BLOCKED",
            error=str(exc),
            failure_class="config",
        )
        result = context.failure_result()
        if diagnostics:
            attach_diagnostics(
                result,
                request_id=diagnostic_request_id,
                total_latency_ms=context.elapsed_ms(),
                attempts=context.attempts,
                quality=context.last_quality,
            )
        return result
    except Exception as exc:
        context.note_failure(
            "INTERNAL",
            error=str(exc),
            failure_class="config",
            engine=context.last_engine,
        )
        result = context.failure_result()
        if diagnostics:
            attach_diagnostics(
                result,
                request_id=diagnostic_request_id,
                total_latency_ms=context.elapsed_ms(),
                attempts=context.attempts,
                quality=context.last_quality,
            )
        return result
    finally:
        if own_client:
            await _close_async_client(active_client)


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
