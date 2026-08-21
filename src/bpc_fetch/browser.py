"""Browser drivers, proxy resolver, context pool, and rate limiter.

Camoufox provides the optional native anti-detect path. Plain Playwright is a
deterministic JavaScript-rendering fallback; PAC does not inject handcrafted
fingerprint shims.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import weakref
from collections import OrderedDict, defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any, AsyncIterator, Mapping, Sequence
from urllib.parse import unquote, urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

try:
    from camoufox.async_api import AsyncCamoufox
except ImportError:  # optional native anti-detect driver
    AsyncCamoufox = None  # type: ignore[assignment,misc]

try:
    from camoufox.async_api import AsyncNewContext as CamoufoxAsyncNewContext
except ImportError:  # stable camoufox releases may not expose this helper
    CamoufoxAsyncNewContext = None  # type: ignore[assignment,misc]

from .quality import classify_access_control_page
from .sites import SiteStrategy
from .ssrf import SSRFBlocked, assert_public_url


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value) if minimum is not None else value


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value) if minimum is not None else value


BROWSER_TIMEOUT = _env_int("PAC_BROWSER_TIMEOUT_MS", 30_000, minimum=1_000)
ARTICLE_WAIT_TIMEOUT = _env_int("PAC_BROWSER_ARTICLE_WAIT_MS", 8_000, minimum=0)
BROWSER_SETTLE_MS = _env_int("PAC_BROWSER_SETTLE_MS", 1_200, minimum=0)
DEFAULT_MAX_CONTEXTS = _env_int("PAC_BROWSER_MAX_CONTEXTS", 3, minimum=1)
DEFAULT_CONTEXT_IDLE_SECONDS = _env_float("PAC_BROWSER_CONTEXT_IDLE_SECONDS", 180.0, minimum=0.0)
DEFAULT_DOMAIN_CONCURRENCY = _env_int("PAC_DOMAIN_CONCURRENCY", 2, minimum=1)
DEFAULT_DOMAIN_RATE = _env_float("PAC_DOMAIN_RATE_PER_SECOND", 1.0, minimum=0.01)
DEFAULT_DOMAIN_BURST = _env_float("PAC_DOMAIN_BURST", 2.0, minimum=1.0)
DEFAULT_BROWSER_DRIVER = os.environ.get("PAC_BROWSER_DRIVER", "auto").strip().casefold() or "auto"
DEFAULT_PROXY_COOLDOWN_SECONDS = _env_float("PAC_PROXY_COOLDOWN_S", 300.0, minimum=1.0)

_TRACKING_DOMAIN_SUFFIXES = (
    "doubleclick.net",
    "googlesyndication.com",
    "google-analytics.com",
    "googletagmanager.com",
    "googleadservices.com",
    "scorecardresearch.com",
    "quantserve.com",
    "adnxs.com",
    "taboola.com",
    "outbrain.com",
    "criteo.com",
    "criteo.net",
    "segment.io",
    "segment.com",
    "mixpanel.com",
    "amplitude.com",
    "hotjar.com",
    "fullstory.com",
    "newrelic.com",
    "nr-data.net",
    "sentry.io",
    "clarity.ms",
    "chartbeat.com",
    "chartbeat.net",
    "parsely.com",
    "optimizely.com",
    "demdex.net",
    "omtrdc.net",
    "adsrvr.org",
    "rubiconproject.com",
    "pubmatic.com",
    "openx.net",
    "casalemedia.com",
    "amazon-adsystem.com",
)

_TRACKING_URL_RE = re.compile(
    r"(?:/collect(?:\?|$)|/telemetry(?:\?|$)|/analytics(?:\?|$)|/pixel(?:\?|$)|"
    r"/beacon(?:\?|$)|/events?/track(?:\?|$)|/pageview(?:\?|$)|"
    r"[?&](?:utm_source|gclid|fbclid)=)",
    re.IGNORECASE,
)

_PAYWALL_PROVIDER_SUFFIXES = (
    "piano.io",
    "tinypass.com",
    "poool.fr",
    "zephr.com",
    "pelcro.com",
    "sophi.io",
    "cxense.com",
)

_LAUNCH_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-dev-shm-usage",
    "--disable-features=AcceptCHFrame,BackForwardCache,MediaRouter,Translate",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-sync",
    "--metrics-recording-only",
    "--no-default-browser-check",
    "--no-first-run",
    "--password-store=basic",
    "--use-mock-keychain",
)


_UNHIDE_SCRIPT = r"""() => {
    const articleSelectors = [
        'article', '[data-article]', '[itemprop="articleBody"]',
        '.article-body', '.article__body', '.story-body', '.story-text',
        '.post-content', '.entry-content', '[data-component="body"]'
    ];
    for (const selector of articleSelectors) {
        for (const element of document.querySelectorAll(selector)) {
            if (!(element instanceof HTMLElement)) continue;
            element.style.setProperty('overflow', 'visible', 'important');
            element.style.setProperty('max-height', 'none', 'important');
            element.style.setProperty('height', 'auto', 'important');
            element.style.setProperty('visibility', 'visible', 'important');
            element.style.setProperty('opacity', '1', 'important');
            element.removeAttribute('hidden');
            element.removeAttribute('inert');
            if (element.getAttribute('aria-hidden') === 'true') {
                element.setAttribute('aria-hidden', 'false');
            }
        }
    }

    const overlaySelectors = [
        '[class*="paywall"]', '[id*="paywall"]', '[class*="content-gate"]',
        '[id*="content-gate"]', '[class*="subscription-wall"]',
        '[class*="regwall"]', '[class*="meter-wall"]', '[data-testid*="paywall"]'
    ];
    for (const selector of overlaySelectors) {
        for (const element of document.querySelectorAll(selector)) {
            if (!(element instanceof HTMLElement)) continue;
            const style = getComputedStyle(element);
            const rectangle = element.getBoundingClientRect();
            const fixedOverlay = (style.position === 'fixed' || style.position === 'sticky')
                && rectangle.width >= window.innerWidth * 0.5
                && rectangle.height >= window.innerHeight * 0.25;
            const shortControl = (element.innerText || '').trim().length < 1200;
            if (fixedOverlay || shortControl) {
                element.style.setProperty('display', 'none', 'important');
            }
        }
    }

    if (document.body) {
        document.body.style.setProperty('overflow', 'auto', 'important');
        document.body.style.setProperty('position', 'static', 'important');
    }
    if (document.documentElement) {
        document.documentElement.style.setProperty('overflow', 'auto', 'important');
    }
}"""


@dataclass
class BrowserResult:
    ok: bool
    html: str = ""
    status: int = 0
    engine: str = "playwright"
    dom_result: dict[str, Any] | None = None
    error_code: str = ""
    error_msg: str = ""
    final_url: str = ""
    challenge_provider: str = ""
    proxy_server: str = ""
    proxy_attempts: int = 0


@dataclass(frozen=True)
class ProxySettings:
    """Normalized Playwright proxy configuration."""

    server: str
    username: str = ""
    password: str = ""
    bypass: str = ""

    def as_playwright(self) -> dict[str, str]:
        value = {"server": self.server}
        if self.username:
            value["username"] = self.username
        if self.password:
            value["password"] = self.password
        if self.bypass:
            value["bypass"] = self.bypass
        return value

    def cache_key(self) -> tuple[str, str, str, str]:
        return (self.server, self.username, self.password, self.bypass)

    def as_url(self) -> str:
        """Return a credential-bearing proxy URL suitable for HTTP clients."""

        if not self.username and not self.password:
            return self.server
        parsed = urlparse(self.server)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        from urllib.parse import quote

        username = quote(self.username, safe="")
        password = quote(self.password, safe="")
        credentials = username
        if self.password:
            credentials += f":{password}"
        return f"{parsed.scheme}://{credentials}@{host}{port}"


@dataclass
class _ProxyFailureState:
    blocked_until: float
    error_code: str
    failures: int


class ProxyCircuitBreaker:
    """Process-local cooldown circuit breaker for proxy candidates."""

    _BLOCKING_CODES = frozenset({"BOT_CHALLENGE", "HTTP_BLOCKED"})

    def __init__(self, cooldown_seconds: float = DEFAULT_PROXY_COOLDOWN_SECONDS) -> None:
        self._cooldown_seconds = max(1.0, float(cooldown_seconds))
        self._states: dict[tuple[str, str, str, str], _ProxyFailureState] = {}

    def _key(self, proxy: ProxySettings) -> tuple[str, str, str, str]:
        return proxy.cache_key()

    def is_available(self, proxy: ProxySettings | None) -> bool:
        if proxy is None:
            return True
        key = self._key(proxy)
        state = self._states.get(key)
        if state is None:
            return True
        if time.monotonic() >= state.blocked_until:
            self._states.pop(key, None)
            return True
        return False

    def remaining(self, proxy: ProxySettings | None) -> float:
        if proxy is None:
            return 0.0
        state = self._states.get(self._key(proxy))
        if state is None:
            return 0.0
        return max(0.0, state.blocked_until - time.monotonic())

    def mark_failure(self, proxy: ProxySettings | None, error_code: str) -> None:
        if proxy is None or error_code not in self._BLOCKING_CODES:
            return
        key = self._key(proxy)
        previous = self._states.get(key)
        failures = 1 if previous is None else previous.failures + 1
        self._states[key] = _ProxyFailureState(
            blocked_until=time.monotonic() + self._cooldown_seconds,
            error_code=error_code,
            failures=failures,
        )

    def mark_success(self, proxy: ProxySettings | None) -> None:
        if proxy is not None:
            self._states.pop(self._key(proxy), None)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        now = time.monotonic()
        for key, state in list(self._states.items()):
            remaining = state.blocked_until - now
            if remaining <= 0:
                self._states.pop(key, None)
                continue
            output[key[0]] = {
                "cooldown_remaining_s": round(remaining, 3),
                "error_code": state.error_code,
                "failures": state.failures,
            }
        return output


_PROXY_CIRCUIT_BREAKER = ProxyCircuitBreaker()
_AUTO_PROXY = object()


def get_proxy_circuit_breaker() -> ProxyCircuitBreaker:
    return _PROXY_CIRCUIT_BREAKER


@dataclass(frozen=True)
class _ContextKey:
    domain: str
    user_agent: str
    headers: Sequence[tuple[str, str]]
    proxy: tuple[str, str, str, str] | None
    locale: str
    timezone_id: str
    viewport_width: int
    viewport_height: int
    allow_cookies: bool
    explicit_user_agent: bool
    explicit_locale: bool
    explicit_timezone: bool
    explicit_viewport: bool
    caller_cookie_fingerprint: str = ""


@dataclass
class _IdleContext:
    identifier: int
    key: _ContextKey
    context: BrowserContext
    last_used: float


@dataclass
class _BucketState:
    tokens: float
    updated_at: float
    in_flight: int
    condition: asyncio.Condition


class DomainRateLimiter:
    """Per-domain token bucket with an independent concurrency ceiling."""

    def __init__(
        self,
        *,
        rate_per_second: float = DEFAULT_DOMAIN_RATE,
        burst: float = DEFAULT_DOMAIN_BURST,
        max_concurrency: int = DEFAULT_DOMAIN_CONCURRENCY,
    ) -> None:
        self._rate = max(0.01, float(rate_per_second))
        self._burst = max(1.0, float(burst))
        self._max_concurrency = max(1, int(max_concurrency))
        self._states: dict[str, _BucketState] = {}
        self._states_lock = asyncio.Lock()

    async def _state_for(self, domain: str) -> _BucketState:
        normalized = (domain or "unknown").casefold().strip(".")
        async with self._states_lock:
            state = self._states.get(normalized)
            if state is None:
                state = _BucketState(
                    tokens=self._burst,
                    updated_at=time.monotonic(),
                    in_flight=0,
                    condition=asyncio.Condition(),
                )
                self._states[normalized] = state
            return state

    def _refill(self, state: _BucketState, now: float) -> None:
        elapsed = max(0.0, now - state.updated_at)
        state.tokens = min(self._burst, state.tokens + elapsed * self._rate)
        state.updated_at = now

    async def acquire(self, domain: str) -> None:
        state = await self._state_for(domain)
        while True:
            async with state.condition:
                now = time.monotonic()
                self._refill(state, now)
                if state.in_flight < self._max_concurrency and state.tokens >= 1.0:
                    state.tokens -= 1.0
                    state.in_flight += 1
                    return

                if state.in_flight >= self._max_concurrency:
                    await state.condition.wait()
                    continue
                token_delay = max(0.01, (1.0 - state.tokens) / self._rate)
                try:
                    await asyncio.wait_for(state.condition.wait(), timeout=token_delay)
                except TimeoutError:
                    continue

    async def release(self, domain: str) -> None:
        state = await self._state_for(domain)
        async with state.condition:
            if state.in_flight > 0:
                state.in_flight -= 1
            state.condition.notify_all()

    @asynccontextmanager
    async def limit(self, domain: str) -> AsyncIterator[None]:
        await self.acquire(domain)
        try:
            yield
        finally:
            await self.release(domain)


_DOMAIN_LIMITERS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, DomainRateLimiter]" = (
    weakref.WeakKeyDictionary()
)


def get_domain_rate_limiter() -> DomainRateLimiter:
    """Return an event-loop-local limiter safe across repeated ``asyncio.run`` calls."""

    loop = asyncio.get_running_loop()
    limiter = _DOMAIN_LIMITERS.get(loop)
    if limiter is None:
        limiter = DomainRateLimiter()
        _DOMAIN_LIMITERS[loop] = limiter
    return limiter


def _truthy_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().casefold() not in {"0", "false", "off", "no", "disabled"}


def _domain_env_key(domain: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", domain.upper()).strip("_")
    return f"PAC_PROXY_{normalized}" if normalized else "PAC_PROXY_DOMAIN"


def _parse_proxy_mapping(raw: str) -> dict[str, str]:
    value = (raw or "").strip()
    if not value:
        return {}
    if value.startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, Mapping):
            return {}
        return {
            str(key).casefold().lstrip(".").rstrip("."): str(item).strip()
            for key, item in parsed.items()
            if str(item).strip()
        }

    result: dict[str, str] = {}
    for entry in re.split(r"[;\n]+", value):
        if "=" not in entry:
            continue
        key, proxy = entry.split("=", 1)
        key = key.strip().casefold().lstrip(".").rstrip(".")
        proxy = proxy.strip()
        if key and proxy:
            result[key] = proxy
    return result


def _proxy_from_mapping(domain: str) -> str:
    merged: dict[str, str] = {}
    for variable in ("PAC_DOMAIN_PROXIES", "PAC_PROXY_MAP"):
        merged.update(_parse_proxy_mapping(os.environ.get(variable, "")))
    if not merged:
        return ""

    normalized = domain.casefold().rstrip(".")
    candidates = sorted(
        (
            key
            for key in merged
            if key == "*" or normalized == key or normalized.endswith("." + key)
        ),
        key=lambda key: (key == "*", -len(key)),
    )
    if not candidates:
        return ""
    return merged[candidates[0]]


def _matches_no_proxy(domain: str) -> bool:
    raw = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    normalized = domain.casefold().rstrip(".")
    for item in raw.split(","):
        candidate = item.strip().casefold()
        if not candidate:
            continue
        if candidate == "*":
            return True
        host = candidate.split(":", 1)[0].lstrip(".").rstrip(".")
        if normalized == host or normalized.endswith("." + host):
            return True
    return False


def _normalize_proxy(raw: str, bypass: str = "") -> ProxySettings | None:
    value = (raw or "").strip()
    if not value or value.casefold() in {"off", "none", "direct", "direct://", "false", "0"}:
        return None
    if "://" not in value:
        value = "http://" + value
    parsed = urlparse(value)
    scheme = parsed.scheme.casefold()
    if scheme == "socks5h":
        scheme = "socks5"
    if scheme not in {"http", "https", "socks4", "socks5"}:
        return None
    if not parsed.hostname:
        return None
    default_port = 1080 if scheme.startswith("socks") else (443 if scheme == "https" else 80)
    try:
        parsed_port = parsed.port
    except ValueError:
        return None
    port = parsed_port or default_port
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    server = f"{scheme}://{host}:{port}"
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    return ProxySettings(server=server, username=username, password=password, bypass=bypass)


def _parse_proxy_pool(raw: str) -> list[str]:
    """Parse ``PAC_PROXIES`` as an ordered comma/newline-separated pool."""

    values: list[str] = []
    for item in re.split(r"[,\n]+", raw or ""):
        value = item.strip()
        if value:
            values.append(value)
    return values


def _strategy_proxy_settings(
    strategy: SiteStrategy | None,
    *,
    default_bypass: str,
) -> tuple[ProxySettings | None, bool]:
    """Return an explicit per-strategy proxy while preserving mapping metadata.

    The boolean indicates whether the strategy explicitly supplied a proxy
    setting even when that value resolves to direct/off.  This prevents lower
    precedence environment proxies from overriding an intentional strategy
    choice and preserves Phase 3 mapping-level ``bypass`` semantics.
    """

    if strategy is None or not isinstance(strategy.extra, Mapping):
        return None, False
    if "proxy" not in strategy.extra and "proxy_server" not in strategy.extra:
        return None, False
    raw = strategy.extra.get("proxy")
    if raw is None:
        raw = strategy.extra.get("proxy_server")
    if isinstance(raw, Mapping):
        server = str(raw.get("server") or "").strip()
        if not server:
            return None, True
        proxy = _normalize_proxy(
            server,
            str(raw.get("bypass") or default_bypass),
        )
        if proxy is None:
            return None, True
        username = str(raw.get("username") or proxy.username or "")
        password = str(raw.get("password") or proxy.password or "")
        return (
            ProxySettings(
                server=proxy.server,
                username=username,
                password=password,
                bypass=proxy.bypass,
            ),
            True,
        )
    value = str(raw or "").strip()
    return _normalize_proxy(value, default_bypass), True


def _proxy_candidate_values(
    url: str,
    strategy: SiteStrategy | None = None,
    *,
    strategy_explicit: bool = False,
) -> list[str]:
    parsed = urlparse(url)
    domain = (parsed.hostname or (strategy.domain if strategy else "")).casefold().rstrip(".")
    explicit_values: list[str] = []

    if not strategy_explicit and domain:
        labels = domain.split(".")
        for index in range(max(1, len(labels) - 1)):
            suffix = ".".join(labels[index:])
            if suffix.count(".") < 1:
                continue
            value = os.environ.get(_domain_env_key(suffix), "").strip()
            if value:
                explicit_values.append(value)
                break
    if not strategy_explicit and not explicit_values and domain:
        value = _proxy_from_mapping(domain)
        if value:
            explicit_values.append(value)

    if not strategy_explicit and not explicit_values and domain and _matches_no_proxy(domain):
        return []

    values = list(explicit_values)
    values.extend(_parse_proxy_pool(os.environ.get("PAC_PROXIES", "")))
    fallback = (
        os.environ.get("PAC_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("ALL_PROXY")
        or os.environ.get("all_proxy")
        or ""
    )
    if not strategy_explicit and fallback:
        values.append(fallback)
    return values


def resolve_proxy_candidates(
    url: str,
    strategy: SiteStrategy | None = None,
) -> list[ProxySettings | None]:
    """Return ordered healthy proxy candidates for a request.

    Existing explicit per-strategy/domain precedence is preserved.  The
    ``PAC_PROXIES`` ordered pool is appended after the selected explicit proxy
    and before the legacy process-wide fallback.  Cooled-down proxies are
    skipped.  If every configured proxy is cooling down, the one closest to
    expiry is returned as a half-open probe rather than leaking traffic onto a
    direct connection.
    """

    bypass = os.environ.get("PAC_PROXY_BYPASS", "").strip()
    strategy_proxy, strategy_explicit = _strategy_proxy_settings(
        strategy,
        default_bypass=bypass,
    )

    candidates: list[ProxySettings] = []
    seen: set[tuple[str, str, str, str]] = set()

    if strategy_explicit:
        if strategy_proxy is None:
            return [None]
        candidates.append(strategy_proxy)
        seen.add(strategy_proxy.cache_key())

    raw_values = _proxy_candidate_values(
        url,
        strategy,
        strategy_explicit=strategy_explicit,
    )
    for raw in raw_values:
        proxy = _normalize_proxy(raw, bypass)
        if proxy is None:
            continue
        key = proxy.cache_key()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(proxy)

    if not candidates:
        return [None]

    breaker = get_proxy_circuit_breaker()
    available = [proxy for proxy in candidates if breaker.is_available(proxy)]
    if available:
        return available
    half_open = min(candidates, key=breaker.remaining)
    return [half_open]

def resolve_proxy(url: str, strategy: SiteStrategy | None = None) -> ProxySettings | None:
    """Backward-compatible single-proxy resolver."""

    return resolve_proxy_candidates(url, strategy)[0]






def _browser_driver_preference() -> str:
    value = os.environ.get("PAC_BROWSER_DRIVER", DEFAULT_BROWSER_DRIVER).strip().casefold() or "auto"
    aliases = {
        "chrome": "playwright",
        "chromium": "playwright",
        "pw": "playwright",
        "fox": "camoufox",
    }
    value = aliases.get(value, value)
    if value not in {"auto", "camoufox", "playwright", "patchright"}:
        return "auto"
    return value


def _camoufox_headless_value() -> bool | str:
    headless = _truthy_env("PAC_BROWSER_HEADLESS", True)
    if not headless:
        return False
    virtual = os.environ.get("PAC_CAMOUFOX_VIRTUAL_DISPLAY", "").strip().casefold()
    if virtual in {"1", "true", "yes", "on", "virtual"}:
        return "virtual"
    return True


async def ensure_browser() -> dict[str, Any]:
    """Check whether Playwright Chromium can be launched."""

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            version = browser.version
            await browser.close()
            return {"ok": True, "version": version}
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "install_cmd": "playwright install chromium",
        }


async def probe_camoufox() -> dict[str, Any]:
    """Launch and close Camoufox to report optional-driver runtime health."""

    if AsyncCamoufox is None:
        return {"ok": False, "installed": False, "error": "camoufox not installed"}
    manager: Any | None = None
    try:
        manager = AsyncCamoufox(
            headless=_camoufox_headless_value(),
            main_world_eval=True,
        )
        browser = await manager.__aenter__()
        version = str(getattr(browser, "version", "") or "")
        await manager.__aexit__(None, None, None)
        manager = None
        return {"ok": True, "installed": True, "version": version}
    except Exception as exc:
        return {"ok": False, "installed": True, "error": str(exc)[:500]}
    finally:
        if manager is not None:
            try:
                await manager.__aexit__(None, None, None)
            except Exception:
                pass


class BrowserPool:
    """Reusable, bounded, per-domain ``BrowserContext`` pool.

    Contexts are keyed by domain, headers, proxy, locale, and cookie policy.
    This prevents cross-domain cookie leakage while eliminating repeated browser
    and context startup overhead during batch extraction.
    """

    def __init__(
        self,
        max_contexts: int = DEFAULT_MAX_CONTEXTS,
        *,
        context_idle_seconds: float = DEFAULT_CONTEXT_IDLE_SECONDS,
    ) -> None:
        self._pw: Any | None = None
        self._browser: Browser | Any | None = None
        self._camoufox_manager: Any | None = None
        self._driver_warning = ""
        self._max = max(1, int(max_contexts))
        self._idle_seconds = max(0.0, float(context_idle_seconds))
        self._sem = asyncio.Semaphore(self._max)
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._lease_condition = asyncio.Condition()
        self._active_pages = 0
        self._idle_by_key: dict[_ContextKey, list[_IdleContext]] = defaultdict(list)
        self._idle_lru: OrderedDict[int, _IdleContext] = OrderedDict()
        self._total_contexts = 0
        self._next_identifier = 1
        self._engine = "playwright"
        self._started = False
        self._stopping = False

    async def start(self) -> None:
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            self._stopping = False
            self._driver_warning = ""
            requested_driver = _browser_driver_preference()

            if requested_driver in {"auto", "camoufox"} and AsyncCamoufox is not None:
                try:
                    await self._start_camoufox()
                    self._started = True
                    return
                except Exception as exc:
                    self._driver_warning = f"camoufox_fallback:{exc}"
                    await self._cleanup_camoufox_manager()
                    self._browser = None
                    if requested_driver == "camoufox":
                        raise RuntimeError(f"Camoufox requested but unavailable: {exc}") from exc

            await self._start_playwright(requested_driver)
            self._started = True

    async def _start_camoufox(self) -> None:
        if AsyncCamoufox is None:
            raise RuntimeError("camoufox is not installed")
        launch_options: dict[str, Any] = {
            "headless": _camoufox_headless_value(),
            # Required because PAC's browser cleanup intentionally modifies the
            # live document after navigation. Read-only DOM extraction still
            # runs through Playwright's isolated world.
            "main_world_eval": True,
        }
        manager = AsyncCamoufox(**launch_options)
        self._camoufox_manager = manager
        try:
            self._browser = await manager.__aenter__()
        except Exception:
            raise
        self._pw = None
        self._engine = "camoufox"

    async def _start_playwright(self, requested_driver: str) -> None:
        if requested_driver == "patchright":
            try:
                from patchright.async_api import async_playwright as patchright_async_playwright

                self._engine = "patchright"
                self._pw = await patchright_async_playwright().start()
            except Exception as exc:
                if self._driver_warning:
                    self._driver_warning += f";patchright_fallback:{exc}"
                else:
                    self._driver_warning = f"patchright_fallback:{exc}"
                self._engine = "playwright"
                self._pw = await async_playwright().start()
        else:
            self._engine = "playwright"
            self._pw = await async_playwright().start()

        headless = _truthy_env("PAC_BROWSER_HEADLESS", True)
        launch_args = list(_LAUNCH_ARGS)
        if _truthy_env("PAC_BROWSER_NO_SANDBOX", False):
            launch_args.extend(("--no-sandbox", "--disable-setuid-sandbox"))
        executable_path = os.environ.get("PAC_BROWSER_EXECUTABLE_PATH", "").strip() or None
        launch_options: dict[str, Any] = {
            "headless": headless,
            "args": launch_args,
        }
        if executable_path:
            launch_options["executable_path"] = executable_path
        try:
            self._browser = await self._pw.chromium.launch(**launch_options)
        except Exception:
            try:
                await self._pw.stop()
            finally:
                self._pw = None
            raise

    async def _cleanup_camoufox_manager(self) -> None:
        manager = self._camoufox_manager
        self._camoufox_manager = None
        if manager is None:
            return
        try:
            await manager.__aexit__(None, None, None)
        except Exception:
            return

    async def stop(self) -> None:
        async with self._start_lock:
            if not self._started and self._pw is None and self._camoufox_manager is None:
                return
            self._stopping = True
            async with self._lease_condition:
                await self._lease_condition.wait_for(lambda: self._active_pages == 0)
            idle_contexts: list[BrowserContext | Any] = []
            async with self._lock:
                idle_contexts = [entry.context for entry in self._idle_lru.values()]
                self._idle_lru.clear()
                self._idle_by_key.clear()
                self._total_contexts = 0
            for context in idle_contexts:
                try:
                    await context.close()
                except Exception:
                    continue

            if self._camoufox_manager is not None:
                await self._cleanup_camoufox_manager()
            else:
                if self._browser is not None:
                    try:
                        await self._browser.close()
                    except Exception:
                        pass
                if self._pw is not None:
                    try:
                        await self._pw.stop()
                    except Exception:
                        pass

            self._browser = None
            self._pw = None
            self._started = False
            self._stopping = False

    def _context_key(
        self,
        strategy: SiteStrategy | None,
        url: str,
        domain: str,
        proxy_override: ProxySettings | None | object = _AUTO_PROXY,
    ) -> tuple[_ContextKey, dict[str, str], ProxySettings | None]:
        from .strategy import UA_NORMAL, build_headers

        headers = build_headers(strategy) if strategy else {}
        user_agent = headers.pop("User-Agent", None) or UA_NORMAL
        locale_raw = os.environ.get("PAC_BROWSER_LOCALE")
        timezone_raw = os.environ.get("PAC_BROWSER_TIMEZONE")
        viewport_width_raw = os.environ.get("PAC_BROWSER_VIEWPORT_WIDTH")
        viewport_height_raw = os.environ.get("PAC_BROWSER_VIEWPORT_HEIGHT")
        locale = (locale_raw or "en-US").strip() or "en-US"
        timezone_id = (timezone_raw or "America/New_York").strip() or "America/New_York"
        viewport_width = max(800, int(viewport_width_raw or "1365"))
        viewport_height = max(600, int(viewport_height_raw or "768"))
        proxy = resolve_proxy(url, strategy) if proxy_override is _AUTO_PROXY else proxy_override
        allow_cookies = bool(strategy and strategy.allow_cookies)
        explicit_user_agent = bool(
            strategy and (strategy.useragent or strategy.useragent_custom)
        )
        caller_cookie_fingerprint = ""
        if strategy is not None and isinstance(strategy.extra, Mapping):
            value = strategy.extra.get("_caller_cookie_fingerprint")
            if isinstance(value, str):
                caller_cookie_fingerprint = value
        key = _ContextKey(
            domain=domain.casefold().rstrip("."),
            user_agent=user_agent,
            headers=tuple(sorted((str(header_name), str(value)) for header_name, value in headers.items())),
            proxy=proxy.cache_key() if proxy else None,
            locale=locale,
            timezone_id=timezone_id,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            allow_cookies=allow_cookies,
            explicit_user_agent=explicit_user_agent,
            explicit_locale=locale_raw is not None,
            explicit_timezone=timezone_raw is not None,
            explicit_viewport=viewport_width_raw is not None or viewport_height_raw is not None,
            caller_cookie_fingerprint=caller_cookie_fingerprint,
        )
        return key, headers, proxy

    async def _close_entry(self, entry: _IdleContext) -> None:
        try:
            await entry.context.close()
        except Exception:
            return

    def _remove_idle_locked(self, entry: _IdleContext) -> None:
        self._idle_lru.pop(entry.identifier, None)
        values = self._idle_by_key.get(entry.key)
        if values is not None:
            self._idle_by_key[entry.key] = [item for item in values if item.identifier != entry.identifier]
            if not self._idle_by_key[entry.key]:
                self._idle_by_key.pop(entry.key, None)

    async def _prune_expired(self) -> None:
        if self._idle_seconds <= 0:
            return
        threshold = time.monotonic() - self._idle_seconds
        expired: list[_IdleContext] = []
        async with self._lock:
            for entry in list(self._idle_lru.values()):
                if entry.last_used > threshold:
                    continue
                self._remove_idle_locked(entry)
                self._total_contexts = max(0, self._total_contexts - 1)
                expired.append(entry)
        for entry in expired:
            await self._close_entry(entry)

    async def _new_context(
        self,
        key: _ContextKey,
        headers: Mapping[str, str],
        proxy: ProxySettings | None,
    ) -> BrowserContext:
        if self._browser is None:
            raise RuntimeError("browser pool is not started")

        if self._engine == "camoufox":
            return await self._new_camoufox_context(key, headers, proxy)
        return await self._new_playwright_context(key, headers, proxy)

    async def _new_playwright_context(
        self,
        key: _ContextKey,
        headers: Mapping[str, str],
        proxy: ProxySettings | None,
    ) -> BrowserContext:
        if self._browser is None:
            raise RuntimeError("browser pool is not started")
        options: dict[str, Any] = {
            "user_agent": key.user_agent,
            "locale": key.locale,
            "timezone_id": key.timezone_id,
            "viewport": {"width": key.viewport_width, "height": key.viewport_height},
            "screen": {"width": key.viewport_width, "height": key.viewport_height},
            "device_scale_factor": 1.0,
            "is_mobile": False,
            "has_touch": False,
            "color_scheme": "light",
            "extra_http_headers": dict(headers) or None,
            "ignore_https_errors": _truthy_env("PAC_BROWSER_IGNORE_HTTPS_ERRORS", False),
            "accept_downloads": False,
        }
        if proxy is not None:
            options["proxy"] = proxy.as_playwright()
        context = await self._browser.new_context(**options)
        return context

    async def _new_camoufox_context(
        self,
        key: _ContextKey,
        headers: Mapping[str, str],
        proxy: ProxySettings | None,
    ) -> BrowserContext:
        if self._browser is None:
            raise RuntimeError("Camoufox browser pool is not started")

        # Let Camoufox own fingerprint fields by default.  PAC overrides are
        # applied only when the caller/rule explicitly asks for them, avoiding
        # a Chromium-looking JS fingerprint layered over a Firefox-derived
        # native engine.
        options: dict[str, Any] = {
            "extra_http_headers": dict(headers) or None,
            "ignore_https_errors": _truthy_env("PAC_BROWSER_IGNORE_HTTPS_ERRORS", False),
            "accept_downloads": False,
        }
        if key.explicit_user_agent:
            options["user_agent"] = key.user_agent
        if key.explicit_locale:
            options["locale"] = key.locale
        if key.explicit_timezone:
            options["timezone_id"] = key.timezone_id
        if key.explicit_viewport:
            options["viewport"] = {
                "width": key.viewport_width,
                "height": key.viewport_height,
            }
            options["screen"] = {
                "width": key.viewport_width,
                "height": key.viewport_height,
            }

        proxy_value = proxy.as_playwright() if proxy is not None else None
        if CamoufoxAsyncNewContext is not None:
            try:
                context = await CamoufoxAsyncNewContext(
                    self._browser,
                    proxy=proxy_value,
                    **options,
                )
                return context
            except TypeError:
                # Compatibility with stable Camoufox releases that do not yet
                # expose the newer per-context helper signature.
                pass

        if proxy_value is not None:
            options["proxy"] = proxy_value
        context = await self._browser.new_context(**options)
        return context

    async def _acquire_context(
        self,
        key: _ContextKey,
        headers: Mapping[str, str],
        proxy: ProxySettings | None,
    ) -> _IdleContext:
        await self._prune_expired()
        evicted: _IdleContext | None = None
        create_new = False
        async with self._lock:
            matching = self._idle_by_key.get(key)
            if matching:
                entry = matching.pop()
                self._idle_lru.pop(entry.identifier, None)
                if not matching:
                    self._idle_by_key.pop(key, None)
                return entry

            if self._total_contexts >= self._max and self._idle_lru:
                _, evicted = self._idle_lru.popitem(last=False)
                values = self._idle_by_key.get(evicted.key, [])
                self._idle_by_key[evicted.key] = [
                    item for item in values if item.identifier != evicted.identifier
                ]
                if not self._idle_by_key[evicted.key]:
                    self._idle_by_key.pop(evicted.key, None)
                self._total_contexts = max(0, self._total_contexts - 1)

            if self._total_contexts < self._max:
                self._total_contexts += 1
                create_new = True

        if evicted is not None:
            await self._close_entry(evicted)
        if not create_new:
            raise RuntimeError("browser context pool capacity invariant violated")
        try:
            context = await self._new_context(key, headers, proxy)
        except Exception:
            async with self._lock:
                self._total_contexts = max(0, self._total_contexts - 1)
            raise
        identifier = self._next_identifier
        self._next_identifier += 1
        return _IdleContext(identifier, key, context, time.monotonic())

    async def _release_context(self, entry: _IdleContext, reusable: bool) -> None:
        if not reusable or self._stopping:
            async with self._lock:
                self._total_contexts = max(0, self._total_contexts - 1)
            await self._close_entry(entry)
            return
        try:
            await entry.context.clear_permissions()
            if not entry.key.allow_cookies:
                await entry.context.clear_cookies()
        except Exception:
            async with self._lock:
                self._total_contexts = max(0, self._total_contexts - 1)
            await self._close_entry(entry)
            return

        entry.last_used = time.monotonic()
        async with self._lock:
            self._idle_by_key[entry.key].append(entry)
            self._idle_lru[entry.identifier] = entry

    @staticmethod
    async def _clear_page_storage(page: Page) -> None:
        try:
            await page.evaluate(
                """async () => {
                    try { localStorage.clear(); } catch (_) {}
                    try { sessionStorage.clear(); } catch (_) {}
                    try {
                        if (indexedDB.databases) {
                            const databases = await indexedDB.databases();
                            await Promise.all(databases.map(database => new Promise(resolve => {
                                if (!database.name) return resolve();
                                const request = indexedDB.deleteDatabase(database.name);
                                request.onsuccess = request.onerror = request.onblocked = () => resolve();
                            })));
                        }
                    } catch (_) {}
                }"""
            )
        except Exception:
            return

    async def _finalize_page_lease(
        self,
        *,
        entry: _IdleContext | None,
        page: Page | None,
        reusable: bool,
        clear_storage: bool,
    ) -> None:
        try:
            if page is not None and entry is not None:
                if clear_storage:
                    await self._clear_page_storage(page)
                try:
                    await page.close()
                except Exception:
                    reusable = False
            if entry is not None:
                await self._release_context(entry, reusable)
        finally:
            async with self._lease_condition:
                self._active_pages = max(0, self._active_pages - 1)
                self._lease_condition.notify_all()

    @staticmethod
    async def _await_cleanup(task: asyncio.Task[None]) -> None:
        first_cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError as exc:
                if task.done():
                    raise
                if first_cancellation is None:
                    first_cancellation = exc
        if first_cancellation is not None:
            raise first_cancellation

    @asynccontextmanager
    async def page(
        self,
        strategy: SiteStrategy | None = None,
        *,
        url: str = "",
        domain: str = "",
        proxy_override: ProxySettings | None | object = _AUTO_PROXY,
    ) -> AsyncIterator[Page]:
        if not self._started:
            await self.start()
        parsed_domain = domain or (urlparse(url).hostname or (strategy.domain if strategy else ""))
        normalized_url = url or (f"https://{parsed_domain}/" if parsed_domain else "https://example.com/")
        async with self._sem:
            async with self._lease_condition:
                if self._stopping or not self._started:
                    raise RuntimeError("browser pool is stopping")
                self._active_pages += 1

            entry: _IdleContext | None = None
            page: Page | None = None
            reusable = True
            key: _ContextKey | None = None
            try:
                key, headers, proxy = self._context_key(
                    strategy, normalized_url, parsed_domain, proxy_override
                )
                entry = await self._acquire_context(key, headers, proxy)
                page = await entry.context.new_page()
                page.set_default_timeout(BROWSER_TIMEOUT)
                page.set_default_navigation_timeout(BROWSER_TIMEOUT)
                yield page
            except BaseException:
                reusable = False
                raise
            finally:
                cleanup_task = asyncio.create_task(
                    self._finalize_page_lease(
                        entry=entry,
                        page=page,
                        reusable=reusable,
                        clear_storage=bool(key is not None and not key.allow_cookies),
                    )
                )
                await self._await_cleanup(cleanup_task)

    async def stats(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "started": self._started,
                "engine": self._engine,
                "max_contexts": self._max,
                "total_contexts": self._total_contexts,
                "idle_contexts": len(self._idle_lru),
                "active_pages": self._active_pages,
                "driver_warning": self._driver_warning,
            }

    async def __aenter__(self) -> "BrowserPool":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()


BrowserContextPool = BrowserPool


_SHARED_POOLS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, BrowserPool]" = (
    weakref.WeakKeyDictionary()
)


async def get_shared_browser_pool(max_contexts: int | None = None) -> BrowserPool:
    """Return an event-loop-local shared browser pool and start it lazily."""

    loop = asyncio.get_running_loop()
    pool = _SHARED_POOLS.get(loop)
    requested_max = max_contexts or DEFAULT_MAX_CONTEXTS
    if pool is None:
        pool = BrowserPool(max_contexts=requested_max)
        _SHARED_POOLS[loop] = pool
    await pool.start()
    return pool


async def close_shared_browser_pool() -> None:
    """Close the shared pool associated with the current event loop."""

    loop = asyncio.get_running_loop()
    pool = _SHARED_POOLS.pop(loop, None)
    if pool is not None:
        await pool.stop()


def _compile_general_block_regexes(strategy: SiteStrategy) -> list[re.Pattern[str]]:
    """Compile global BPC blockers for a target, ignoring malformed rules."""

    compiled: list[re.Pattern[str]] = []
    for pattern in strategy.general_block_regexes:
        try:
            compiled.append(re.compile(pattern))
        except re.error:
            continue
    return compiled


def _compile_strategy_block_regex(strategy: SiteStrategy | None) -> list[re.Pattern[str]]:
    if strategy is None or not strategy.block_regex:
        return []
    try:
        return [re.compile(strategy.block_regex)]
    except re.error:
        compiled: list[re.Pattern[str]] = []
        for part in re.split(r"\|", strategy.block_regex):
            part = part.strip().strip("()")
            if not part:
                continue
            try:
                compiled.append(re.compile(part))
            except re.error:
                continue
        return compiled


async def _handle_general_block_route(
    route: Any,
    patterns: list[re.Pattern[str]],
) -> None:
    """Back-compatible handler for BPC global script/XHR/fetch blockers."""

    request = route.request
    should_abort = (
        request.resource_type in {"script", "xhr", "fetch"}
        and any(pattern.search(request.url) for pattern in patterns)
    )
    if should_abort:
        await route.abort()
    else:
        await route.continue_()


def _host_matches_suffix(host: str, suffixes: Sequence[str]) -> bool:
    normalized = host.casefold().rstrip(".")
    return any(normalized == suffix or normalized.endswith("." + suffix) for suffix in suffixes)


def _is_tracking_request(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return _host_matches_suffix(host, _TRACKING_DOMAIN_SUFFIXES) or bool(_TRACKING_URL_RE.search(url))


def _is_paywall_provider(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return _host_matches_suffix(host, _PAYWALL_PROVIDER_SUFFIXES) or "fortress-client" in url.casefold()


async def _handle_resource_route(
    route: Any,
    request: Any = None,
    *args: Any,
    general_patterns: list[re.Pattern[str]],
    strategy_patterns: list[re.Pattern[str]],
    block_images: bool,
    allow_paywall_cleanup: bool = False,
    ssrf_state: dict[str, str] | None = None,
    **kwargs: Any,
) -> None:
    req = request if request is not None else getattr(route, "request", None)
    resource_type = str(req.resource_type) if req is not None else str(getattr(route.request, "resource_type", ""))
    url = str(req.url) if req is not None else str(getattr(route.request, "url", ""))

    parsed = urlparse(url)
    scheme = parsed.scheme.casefold()
    if scheme not in {"about", "blob", "data"}:
        validation_url = url
        if scheme == "ws":
            validation_url = "http:" + url[len("ws:"):]
        elif scheme == "wss":
            validation_url = "https:" + url[len("wss:"):]
        try:
            await asyncio.to_thread(assert_public_url, validation_url)
        except SSRFBlocked as exc:
            if ssrf_state is not None and resource_type == "document":
                ssrf_state["document"] = str(exc)
            try:
                await route.abort()
            except Exception:
                pass
            return

    abort = False
    if resource_type != "document" and _is_tracking_request(url):
        abort = True
    elif (
        allow_paywall_cleanup
        and resource_type in {"script", "xhr", "fetch"}
        and _is_paywall_provider(url)
    ):
        abort = True
    elif resource_type in {"script", "xhr", "fetch"} and any(
        pattern.search(url) for pattern in general_patterns
    ):
        abort = True
    elif resource_type in {"script", "xhr", "fetch", "websocket"} and any(
        pattern.search(url) for pattern in strategy_patterns
    ):
        abort = True
    elif resource_type == "media":
        abort = True
    elif block_images and resource_type == "image":
        abort = True

    try:
        if abort:
            await route.abort()
        else:
            await route.continue_()
    except Exception:
        return


async def _evaluate_page_script(
    page: Page,
    script: str,
    *,
    engine: str,
    modifies_dom: bool,
) -> Any:
    """Evaluate a script with Camoufox main-world semantics when required."""

    if engine == "camoufox" and modifies_dom:
        source = script.strip()
        if source.startswith("() =>") or source.startswith("function"):
            source = f"({source})()"
        return await page.evaluate("mw:" + source)
    return await page.evaluate(script)


def _navigation_error_code(error: BaseException) -> str:
    message = str(error).casefold()
    browser_unavailable_markers = (
        "executable doesn't exist",
        "browser_type.launch",
        "failed to launch",
        "playwright install",
        "target page, context or browser has been closed",
        "camoufox executable",
        "camoufox fetch",
        "browser not found",
    )
    if any(marker in message for marker in browser_unavailable_markers):
        return "BROWSER_UNAVAILABLE"
    return "NETWORK"


def _caller_cookie_pairs(cookie_header: str) -> list[dict[str, str]]:
    """Parse a raw Cookie header into host-only browser cookie values."""
    pairs: list[dict[str, str]] = []
    for item in cookie_header.split(";"):
        name, separator, value = item.strip().partition("=")
        if separator and name and value:
            pairs.append({"name": name, "value": value})
    return pairs


async def _install_caller_cookies(page: Page, url: str, cookie_header: str) -> None:
    """Install caller cookies for the target URL only, never as global headers."""
    pairs = _caller_cookie_pairs(cookie_header)
    if not pairs:
        return
    await page.context.add_cookies([{**pair, "url": url} for pair in pairs])


async def _fetch_for_strategy_single(
    url: str,
    strategy: SiteStrategy | None,
    pool: BrowserPool | None = None,
    *,
    proxy_override: ProxySettings | None | object = _AUTO_PROXY,
    cookie_header: str = "",
) -> BrowserResult:
    """Fetch a page with interception, proxying, pooling, and the selected browser driver."""

    own_pool = False
    active_pool = pool
    try:
        if active_pool is None:
            if _truthy_env("PAC_BROWSER_SHARED_POOL", True):
                active_pool = await get_shared_browser_pool()
            else:
                active_pool = BrowserPool(max_contexts=1)
                await active_pool.start()
                own_pool = True
        elif not active_pool._started:
            await active_pool.start()
    except Exception as exc:
        return BrowserResult(
            ok=False,
            engine=getattr(active_pool, "_engine", "playwright"),
            error_code="BROWSER_UNAVAILABLE",
            error_msg=str(exc)[:500],
        )

    assert active_pool is not None
    engine = getattr(active_pool, "_engine", "playwright")
    domain = (urlparse(url).hostname or (strategy.domain if strategy else "")).casefold()
    limiter = get_domain_rate_limiter()

    # Caller cookies need their own pooled context, but must not be installed
    # as context-wide HTTP headers: those headers leak to cross-origin assets.
    page_strategy = strategy
    cookie_header = (cookie_header or "").strip()
    if cookie_header:
        base_extra = dict(strategy.extra) if (strategy is not None and isinstance(strategy.extra, dict)) else {}
        page_strategy = SiteStrategy(
            domain=strategy.domain if strategy else domain,
            name=strategy.name if strategy else "",
            useragent=strategy.useragent if strategy else "",
            useragent_custom=strategy.useragent_custom if strategy else "",
            referer=strategy.referer if strategy else "",
            referer_custom=strategy.referer_custom if strategy else "",
            allow_cookies=True,
            extra={
                **base_extra,
                "_caller_cookie_fingerprint": hashlib.sha256(
                    cookie_header.encode("utf-8")
                ).hexdigest(),
            },
        )

    try:
        async with limiter.limit(domain):
            async with active_pool.page(
                page_strategy, url=url, domain=domain, proxy_override=proxy_override
            ) as page:
                general_patterns = _compile_general_block_regexes(strategy) if strategy else []
                strategy_patterns = _compile_strategy_block_regex(strategy)
                block_images = _truthy_env("PAC_BROWSER_BLOCK_IMAGES", True)
                allow_paywall_cleanup = _truthy_env("PAC_BROWSER_PAYWALL_CLEANUP", False)
                ssrf_state: dict[str, str] = {}
                await page.route(
                    "**/*",
                    partial(
                        _handle_resource_route,
                        general_patterns=general_patterns,
                        strategy_patterns=strategy_patterns,
                        block_images=block_images,
                        allow_paywall_cleanup=allow_paywall_cleanup,
                        ssrf_state=ssrf_state,
                    ),
                )

                def dismiss_dialog(dialog: Any) -> None:
                    try:
                        asyncio.create_task(dialog.dismiss())
                    except Exception:
                        return

                page.on("dialog", dismiss_dialog)
                await _install_caller_cookies(page, url, cookie_header)
                response = None
                navigation_error: BaseException | None = None
                nonfatal_errors: list[str] = []
                try:
                    response = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=BROWSER_TIMEOUT,
                    )
                except PlaywrightTimeoutError as exc:
                    navigation_error = exc
                except Exception as exc:
                    navigation_error = exc

                if ssrf_state.get("document"):
                    return BrowserResult(
                        ok=False,
                        status=0,
                        engine=engine,
                        error_code="SSRF_BLOCKED",
                        error_msg=ssrf_state["document"],
                        final_url=page.url,
                    )

                status = response.status if response is not None else 0
                try:
                    await page.wait_for_selector(
                        "article, [data-article], [itemprop='articleBody'], .article-body, "
                        ".story-body, .post-content, .entry-content",
                        timeout=ARTICLE_WAIT_TIMEOUT,
                    )
                except Exception as exc:
                    nonfatal_errors.append(f"selector_wait:{exc}")

                if BROWSER_SETTLE_MS > 0:
                    try:
                        await page.wait_for_timeout(BROWSER_SETTLE_MS)
                    except Exception as exc:
                        nonfatal_errors.append(f"settle_wait:{exc}")
                try:
                    await _evaluate_page_script(
                        page,
                        """() => {
                            const height = Math.max(document.body?.scrollHeight || 0, document.documentElement?.scrollHeight || 0);
                            if (height > window.innerHeight * 2) {
                                window.scrollTo(0, Math.min(height * 0.35, 1600));
                                window.scrollTo(0, 0);
                            }
                        }""",
                        engine=engine,
                        modifies_dom=True,
                    )
                except Exception as exc:
                    nonfatal_errors.append(f"scroll:{exc}")
                if allow_paywall_cleanup:
                    try:
                        await _evaluate_page_script(
                            page,
                            _UNHIDE_SCRIPT,
                            engine=engine,
                            modifies_dom=True,
                        )
                    except Exception as exc:
                        nonfatal_errors.append(f"unhide:{exc}")
                    try:
                        await page.wait_for_timeout(250)
                    except Exception as exc:
                        nonfatal_errors.append(f"post_unhide_wait:{exc}")

                try:
                    dom_result = await extract_article_dom(page)
                except Exception:
                    dom_result = None
                try:
                    html = await page.content()
                except Exception as exc:
                    code = _navigation_error_code(exc)
                    return BrowserResult(
                        ok=False,
                        status=status,
                        engine=engine,
                        error_code=code,
                        error_msg=str(exc)[:500],
                        final_url=page.url,
                    )
                try:
                    title = await page.title()
                except Exception:
                    title = str((dom_result or {}).get("title") or "")

                access = classify_access_control_page(html, title=title, status=status)
                if access.detected:
                    return BrowserResult(
                        ok=False,
                        html=html,
                        status=status,
                        engine=engine,
                        dom_result=dom_result,
                        error_code="BOT_CHALLENGE" if access.challenge else "HTTP_BLOCKED",
                        error_msg=access.reason,
                        final_url=page.url,
                        challenge_provider=access.provider,
                    )
                if status in {401, 403, 407, 429, 451}:
                    return BrowserResult(
                        ok=False,
                        html=html,
                        status=status,
                        engine=engine,
                        dom_result=dom_result,
                        error_code="HTTP_BLOCKED",
                        error_msg=f"HTTP {status}",
                        final_url=page.url,
                    )
                if status >= 500:
                    return BrowserResult(
                        ok=False,
                        html=html,
                        status=status,
                        engine=engine,
                        dom_result=dom_result,
                        error_code="NETWORK",
                        error_msg=f"HTTP {status}",
                        final_url=page.url,
                    )
                if navigation_error is not None and not html.strip():
                    return BrowserResult(
                        ok=False,
                        status=status,
                        engine=engine,
                        error_code=_navigation_error_code(navigation_error),
                        error_msg=(str(navigation_error) + ("; " + "; ".join(nonfatal_errors) if nonfatal_errors else ""))[:500],
                        final_url=page.url,
                    )

                return BrowserResult(
                    ok=True,
                    html=html,
                    status=status or 200,
                    engine=engine,
                    dom_result=dom_result,
                    final_url=page.url,
                )
    except Exception as exc:
        return BrowserResult(
            ok=False,
            engine=engine,
            error_code=_navigation_error_code(exc),
            error_msg=str(exc)[:500],
        )
    finally:
        if own_pool:
            await active_pool.stop()


async def fetch_for_strategy(
    url: str,
    strategy: SiteStrategy | None,
    pool: BrowserPool | None = None,
    *,
    cookie_header: str = "",
) -> BrowserResult:
    """Fetch through the ordered proxy pool with circuit-breaker failover."""

    candidates = resolve_proxy_candidates(url, strategy)
    breaker = get_proxy_circuit_breaker()
    last_result: BrowserResult | None = None
    for attempt_number, proxy in enumerate(candidates, start=1):
        result = await _fetch_for_strategy_single(
            url,
            strategy,
            pool=pool,
            proxy_override=proxy,
            cookie_header=cookie_header,
        )
        result.proxy_server = proxy.server if proxy is not None else ""
        result.proxy_attempts = attempt_number
        if result.ok:
            breaker.mark_success(proxy)
            return result
        last_result = result
        if proxy is not None:
            if result.error_code in {"BOT_CHALLENGE", "HTTP_BLOCKED"}:
                breaker.mark_failure(proxy, result.error_code)
                continue
            if result.error_code == "NETWORK":
                # A dead proxy must not prevent the rest of the configured pool
                # from being attempted.  Do not cool it down here; transient
                # transport failures should get another chance on a later call.
                continue
        return result

    if last_result is not None:
        return last_result
    return BrowserResult(
        ok=False,
        error_code="NETWORK",
        error_msg="no proxy candidate available",
    )


async def fetch_with_browser(
    url: str,
    strategy: SiteStrategy,
    pool: BrowserPool | None = None,
) -> tuple[str, int]:
    """Back-compatible tuple wrapper around :func:`fetch_for_strategy`."""

    result = await fetch_for_strategy(url, strategy, pool=pool)
    return result.html, result.status if result.ok else 0


async def extract_article_dom(page: Page) -> dict[str, Any] | None:
    """Extract article prose, images, and structural metrics from the live DOM."""

    return await page.evaluate(
        r"""() => {
            const meaningfulLength = value => Array.from(value || '').filter(character => !/\s/u.test(character)).length;
            const selectors = [
                'article[data-body-id]', 'article .article-body', 'article .story-body',
                '[itemprop="articleBody"]', '.article__body', '.article-body',
                '.post-content', '.entry-content', '[data-component="body"]',
                '.story-text', '.article-text', '.story-body', 'article', 'main'
            ];
            const candidates = [];
            for (const selector of selectors) {
                for (const element of document.querySelectorAll(selector)) {
                    if (!candidates.includes(element)) candidates.push(element);
                }
            }
            let container = null;
            let maximumText = 0;
            for (const candidate of candidates) {
                const length = meaningfulLength(candidate.innerText || '');
                if (length > maximumText) {
                    maximumText = length;
                    container = candidate;
                }
            }
            if (!container || maximumText < 80) return null;

            const paragraphNodes = Array.from(container.querySelectorAll('p'));
            const paragraphs = [];
            for (const paragraph of paragraphNodes) {
                const text = (paragraph.innerText || '').trim();
                if (meaningfulLength(text) >= 15) paragraphs.push(text);
            }
            if (!paragraphs.length) {
                const directText = (container.innerText || '').trim();
                if (meaningfulLength(directText) >= 80) paragraphs.push(directText);
            }

            const images = [];
            const seenImages = new Set();
            for (const image of container.querySelectorAll('img')) {
                const source = image.currentSrc || image.src || image.getAttribute('data-src') || image.getAttribute('data-lazy-src') || '';
                if (!source || seenImages.has(source)) continue;
                const lower = source.toLowerCase();
                if (['pixel', 'tracking', 'logo', 'icon', 'avatar', 'badge', 'spinner', 'data:image'].some(marker => lower.includes(marker))) continue;
                seenImages.add(source);
                images.push({src: source, alt: image.alt || ''});
                if (images.length >= 20) break;
            }

            const title = (document.querySelector('h1')?.innerText || document.title || '').trim().split('|')[0].trim();
            const text = paragraphs.join('\n\n');
            if (meaningfulLength(text) < 80) return null;

            const paragraphLengths = paragraphs.map(meaningfulLength);
            const paragraphMean = paragraphLengths.length
                ? paragraphLengths.reduce((sum, value) => sum + value, 0) / paragraphLengths.length
                : 0;
            const paragraphVariance = paragraphLengths.length > 1
                ? paragraphLengths.reduce((sum, value) => sum + Math.pow(value - paragraphMean, 2), 0) / paragraphLengths.length
                : 0;
            const paragraphCv = paragraphMean > 0 ? Math.sqrt(paragraphVariance) / paragraphMean : 0;
            const linkTextChars = Array.from(container.querySelectorAll('a')).reduce(
                (sum, anchor) => sum + meaningfulLength(anchor.innerText || ''),
                0
            );
            const containerTextChars = meaningfulLength(container.innerText || '');
            const bodyTextChars = meaningfulLength(document.body?.innerText || '');
            const htmlChars = document.documentElement?.outerHTML?.length || 0;

            let domDepth = 0;
            const stack = [[document.documentElement, 0]];
            while (stack.length) {
                const [node, depth] = stack.pop();
                if (!node) continue;
                domDepth = Math.max(domDepth, depth);
                if (depth >= 80) continue;
                for (const child of node.children || []) stack.push([child, depth + 1]);
            }

            let paywallAttributeCount = 0;
            let hiddenProseCount = 0;
            for (const element of document.querySelectorAll('*')) {
                const attributes = [
                    element.id || '',
                    element.className || '',
                    element.getAttribute?.('role') || '',
                    element.getAttribute?.('aria-label') || '',
                    element.getAttribute?.('data-testid') || ''
                ].join(' ');
                if (/(?:paywall|meter(?:ed)?|subscribe|subscription|subscriber|premium|registration|login|sign[-_ ]?in|content[-_ ]?gate|locked[-_ ]?content|overlay|modal)/i.test(attributes)) {
                    paywallAttributeCount += 1;
                }
                if (meaningfulLength(element.innerText || '') >= 200) {
                    const style = getComputedStyle(element);
                    if (style.overflow === 'hidden' || (style.maxHeight && style.maxHeight !== 'none' && style.maxHeight !== '0px')) {
                        hiddenProseCount += 1;
                    }
                }
            }

            return {
                title,
                text,
                images,
                paragraph_count: paragraphs.length,
                metrics: {
                    text_chars: containerTextChars,
                    html_chars: htmlChars,
                    paragraph_count: paragraphLengths.length,
                    paragraph_mean: paragraphMean,
                    paragraph_variance: paragraphVariance,
                    paragraph_cv: paragraphCv,
                    link_density: containerTextChars ? linkTextChars / containerTextChars : 0,
                    link_text_chars: linkTextChars,
                    link_count: container.querySelectorAll('a').length,
                    dom_depth: domDepth,
                    tag_count: document.querySelectorAll('*').length,
                    article_text_ratio: bodyTextChars ? containerTextChars / bodyTextChars : 0,
                    text_to_html_ratio: htmlChars ? bodyTextChars / htmlChars : 0,
                    article_container_present: container.tagName.toLowerCase() !== 'main',
                    form_count: document.forms.length,
                    button_count: document.querySelectorAll('button').length,
                    input_count: document.querySelectorAll('input').length,
                    paywall_attribute_count: paywallAttributeCount,
                    hidden_prose_count: hiddenProseCount
                }
            };
        }"""
    )
