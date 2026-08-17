"""Stealth Playwright driver, proxy resolver, context pool, and rate limiter.

The module preserves the original public functions while providing reusable
per-domain browser contexts, deterministic fingerprint masking, resource
interception, proxy selection, and access-control classification.
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

BROWSER_TIMEOUT = int(os.environ.get("PAC_BROWSER_TIMEOUT_MS", "30000"))
ARTICLE_WAIT_TIMEOUT = int(os.environ.get("PAC_BROWSER_ARTICLE_WAIT_MS", "8000"))
BROWSER_SETTLE_MS = int(os.environ.get("PAC_BROWSER_SETTLE_MS", "1200"))
DEFAULT_MAX_CONTEXTS = max(1, int(os.environ.get("PAC_BROWSER_MAX_CONTEXTS", "3")))
DEFAULT_CONTEXT_IDLE_SECONDS = max(
    0.0, float(os.environ.get("PAC_BROWSER_CONTEXT_IDLE_SECONDS", "180"))
)
DEFAULT_DOMAIN_CONCURRENCY = max(1, int(os.environ.get("PAC_DOMAIN_CONCURRENCY", "2")))
DEFAULT_DOMAIN_RATE = max(0.01, float(os.environ.get("PAC_DOMAIN_RATE_PER_SECOND", "1.0")))
DEFAULT_DOMAIN_BURST = max(1.0, float(os.environ.get("PAC_DOMAIN_BURST", "2")))
DEFAULT_BROWSER_DRIVER = os.environ.get("PAC_BROWSER_DRIVER", "auto").strip().casefold() or "auto"

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

_STEALTH_SCRIPT_BODY = r"""
const define = (target, property, descriptor) => {
    try {
        Object.defineProperty(target, property, Object.assign({configurable: true}, descriptor));
    } catch (_) {
        return false;
    }
    return true;
};

const nativeToString = Function.prototype.toString;
const nativeSources = new WeakMap();
const markNative = (fn, name) => {
    try {
        nativeSources.set(fn, `function ${name || fn.name || ''}() { [native code] }`);
    } catch (_) {
        return fn;
    }
    return fn;
};

const patchedToString = markNative(function toString() {
    if (typeof this === 'function' && nativeSources.has(this)) {
        return nativeSources.get(this);
    }
    return nativeToString.call(this);
}, 'toString');

define(Function.prototype, 'toString', {
    value: patchedToString,
    writable: true,
    enumerable: false
});

const navigatorPrototype = Object.getPrototypeOf(navigator);
define(navigatorPrototype, 'webdriver', {get: markNative(function webdriver() { return undefined; }, 'get webdriver')});
define(navigatorPrototype, 'platform', {get: markNative(function platform() { return cfg.platform; }, 'get platform')});
define(navigatorPrototype, 'hardwareConcurrency', {get: markNative(function hardwareConcurrency() { return cfg.hardwareConcurrency; }, 'get hardwareConcurrency')});
define(navigatorPrototype, 'deviceMemory', {get: markNative(function deviceMemory() { return cfg.deviceMemory; }, 'get deviceMemory')});
define(navigatorPrototype, 'languages', {get: markNative(function languages() { return cfg.languages.slice(); }, 'get languages')});
define(navigatorPrototype, 'language', {get: markNative(function language() { return cfg.languages[0]; }, 'get language')});

const pluginEntries = [
    {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
    {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: ''},
    {name: 'Native Client', filename: 'internal-nacl-plugin', description: ''}
];
const plugins = pluginEntries.map((entry, index) => {
    const plugin = Object.create(Plugin.prototype);
    define(plugin, 'name', {value: entry.name, enumerable: true});
    define(plugin, 'filename', {value: entry.filename, enumerable: true});
    define(plugin, 'description', {value: entry.description, enumerable: true});
    define(plugin, 'length', {value: index === 0 ? 2 : 0, enumerable: true});
    return plugin;
});
define(plugins, 'item', {value: markNative(function item(index) { return this[index] || null; }, 'item')});
define(plugins, 'namedItem', {value: markNative(function namedItem(name) { return this.find(plugin => plugin.name === name) || null; }, 'namedItem')});
define(plugins, 'refresh', {value: markNative(function refresh() { return undefined; }, 'refresh')});
define(plugins, Symbol.toStringTag, {value: 'PluginArray'});
define(navigatorPrototype, 'plugins', {get: markNative(function pluginsGetter() { return plugins; }, 'get plugins')});

if (!window.chrome) {
    define(window, 'chrome', {value: {}, writable: false, enumerable: true});
}
if (!window.chrome.runtime) {
    const runtime = {
        OnInstalledReason: {CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update'},
        OnRestartRequiredReason: {APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic'},
        PlatformArch: {ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64'},
        PlatformNaclArch: {ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64'},
        PlatformOs: {ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win'},
        RequestUpdateCheckStatus: {NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available'},
        connect: markNative(function connect() { return {name: '', onDisconnect: {addListener() {}}, onMessage: {addListener() {}}, postMessage() {}}; }, 'connect'),
        sendMessage: markNative(function sendMessage() { return Promise.resolve(undefined); }, 'sendMessage')
    };
    define(window.chrome, 'runtime', {value: runtime, enumerable: true});
}
if (!window.chrome.app) {
    define(window.chrome, 'app', {value: {isInstalled: false, InstallState: {DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed'}, RunningState: {CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running'}}, enumerable: true});
}

if (navigator.permissions && navigator.permissions.query) {
    const originalPermissionsQuery = navigator.permissions.query.bind(navigator.permissions);
    const permissionsQuery = markNative(function query(parameters) {
        const name = parameters && parameters.name;
        if (name === 'notifications') {
            const state = typeof Notification !== 'undefined' ? Notification.permission : 'default';
            return Promise.resolve({state, onchange: null, addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false; }});
        }
        return originalPermissionsQuery(parameters);
    }, 'query');
    define(Object.getPrototypeOf(navigator.permissions), 'query', {value: permissionsQuery, writable: true});
}

const patchWebGL = (prototype) => {
    if (!prototype || !prototype.getParameter) return;
    const originalGetParameter = prototype.getParameter;
    const getParameter = markNative(function getParameter(parameter) {
        if (parameter === 37445) return cfg.webglVendor;
        if (parameter === 37446) return cfg.webglRenderer;
        return originalGetParameter.call(this, parameter);
    }, 'getParameter');
    define(prototype, 'getParameter', {value: getParameter, writable: true});
};
patchWebGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
patchWebGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);

const noise = (x, y, channel) => {
    let value = (cfg.seed ^ Math.imul((x + 1), 73856093) ^ Math.imul((y + 1), 19349663) ^ Math.imul((channel + 1), 83492791)) >>> 0;
    value ^= value << 13;
    value ^= value >>> 17;
    value ^= value << 5;
    return (value & 1) === 0 ? -1 : 1;
};

if (window.CanvasRenderingContext2D) {
    const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    const getImageData = markNative(function getImageData(sx, sy, sw, sh, settings) {
        const imageData = originalGetImageData.call(this, sx, sy, sw, sh, settings);
        const data = imageData && imageData.data;
        if (data && data.length >= 4) {
            const pixelCount = Math.max(1, Math.floor(data.length / 4));
            const sampleCount = Math.min(8, pixelCount);
            for (let index = 0; index < sampleCount; index += 1) {
                const pixel = (Math.imul(cfg.seed + index + 1, 2654435761) >>> 0) % pixelCount;
                const offset = pixel * 4;
                for (let channel = 0; channel < 3; channel += 1) {
                    data[offset + channel] = Math.max(0, Math.min(255, data[offset + channel] + noise(sx + pixel, sy + index, channel)));
                }
            }
        }
        return imageData;
    }, 'getImageData');
    define(CanvasRenderingContext2D.prototype, 'getImageData', {value: getImageData, writable: true});

    const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
    const toDataURL = markNative(function toDataURL() {
        try {
            const context = this.getContext('2d');
            if (!context || this.width < 1 || this.height < 1) {
                return originalToDataURL.apply(this, arguments);
            }
            const x = (cfg.seed >>> 3) % this.width;
            const y = (cfg.seed >>> 7) % this.height;
            const original = originalGetImageData.call(context, x, y, 1, 1);
            const modified = originalGetImageData.call(context, x, y, 1, 1);
            for (let channel = 0; channel < 3; channel += 1) {
                modified.data[channel] = Math.max(0, Math.min(255, modified.data[channel] + noise(x, y, channel)));
            }
            context.putImageData(modified, x, y);
            const result = originalToDataURL.apply(this, arguments);
            context.putImageData(original, x, y);
            return result;
        } catch (_) {
            return originalToDataURL.apply(this, arguments);
        }
    }, 'toDataURL');
    define(HTMLCanvasElement.prototype, 'toDataURL', {value: toDataURL, writable: true});
}

if (window.AnalyserNode && AnalyserNode.prototype.getFloatFrequencyData) {
    const originalGetFloatFrequencyData = AnalyserNode.prototype.getFloatFrequencyData;
    const getFloatFrequencyData = markNative(function getFloatFrequencyData(array) {
        originalGetFloatFrequencyData.call(this, array);
        if (array && array.length) {
            const limit = Math.min(array.length, 32);
            for (let index = 0; index < limit; index += 7) {
                array[index] += noise(index, array.length, 0) * 1e-7;
            }
        }
    }, 'getFloatFrequencyData');
    define(AnalyserNode.prototype, 'getFloatFrequencyData', {value: getFloatFrequencyData, writable: true});
}

if (window.OfflineAudioContext && OfflineAudioContext.prototype.startRendering) {
    const originalStartRendering = OfflineAudioContext.prototype.startRendering;
    const startRendering = markNative(function startRendering() {
        return originalStartRendering.apply(this, arguments).then(buffer => {
            try {
                const channel = buffer.getChannelData(0);
                if (channel && channel.length > 100) {
                    const index = (cfg.seed >>> 5) % channel.length;
                    channel[index] += noise(index, channel.length, 1) * 1e-8;
                }
            } catch (_) {}
            return buffer;
        });
    }, 'startRendering');
    define(OfflineAudioContext.prototype, 'startRendering', {value: startRendering, writable: true});
}

try {
    const originalContentWindow = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
    if (originalContentWindow && originalContentWindow.get) {
        define(HTMLIFrameElement.prototype, 'contentWindow', {
            get: markNative(function contentWindow() {
                const value = originalContentWindow.get.call(this);
                return value || window;
            }, 'get contentWindow')
        });
    }
} catch (_) {}

try {
    define(screen, 'availTop', {get: markNative(function availTop() { return 0; }, 'get availTop')});
    define(screen, 'availLeft', {get: markNative(function availLeft() { return 0; }, 'get availLeft')});
} catch (_) {}
"""

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
    port = parsed.port or default_port
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    server = f"{scheme}://{host}:{port}"
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    return ProxySettings(server=server, username=username, password=password, bypass=bypass)


def resolve_proxy(url: str, strategy: SiteStrategy | None = None) -> ProxySettings | None:
    """Resolve a proxy using rule override, per-domain mapping, and environment fallback.

    Precedence is: ``strategy.extra['proxy']`` / ``proxy_server``;
    ``PAC_PROXY_<DOMAIN>``; ``PAC_PROXY_MAP`` or ``PAC_DOMAIN_PROXIES``;
    ``PAC_PROXY``; then ``HTTPS_PROXY`` / ``https_proxy``.
    """

    parsed = urlparse(url)
    domain = (parsed.hostname or (strategy.domain if strategy else "")).casefold().rstrip(".")
    explicit = ""
    bypass = os.environ.get("PAC_PROXY_BYPASS", "").strip()
    if strategy is not None and isinstance(strategy.extra, Mapping):
        raw = strategy.extra.get("proxy") or strategy.extra.get("proxy_server")
        if isinstance(raw, Mapping):
            server = str(raw.get("server") or "").strip()
            if server:
                normalized = _normalize_proxy(server, str(raw.get("bypass") or bypass))
                if normalized is not None:
                    return ProxySettings(
                        server=normalized.server,
                        username=str(raw.get("username") or normalized.username),
                        password=str(raw.get("password") or normalized.password),
                        bypass=str(raw.get("bypass") or normalized.bypass),
                    )
        elif raw:
            explicit = str(raw).strip()

    if not explicit and domain:
        labels = domain.split(".")
        for index in range(max(1, len(labels) - 1)):
            suffix = ".".join(labels[index:])
            if suffix.count(".") < 1:
                continue
            explicit = os.environ.get(_domain_env_key(suffix), "").strip()
            if explicit:
                break
    if not explicit and domain:
        explicit = _proxy_from_mapping(domain)
    if explicit:
        return _normalize_proxy(explicit, bypass)

    if domain and _matches_no_proxy(domain):
        return None
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
    return _normalize_proxy(fallback, bypass)


def _stealth_seed(domain: str, user_agent: str, proxy: ProxySettings | None) -> int:
    material = f"{domain}\0{user_agent}\0{proxy.server if proxy else ''}".encode("utf-8")
    return int.from_bytes(hashlib.blake2s(material, digest_size=4).digest(), "big")


def _build_stealth_script(
    *,
    domain: str,
    user_agent: str,
    proxy: ProxySettings | None,
) -> str:
    platform = "MacIntel" if "Macintosh" in user_agent else "Win32"
    if "Linux" in user_agent and "Android" not in user_agent:
        platform = "Linux x86_64"
    seed = _stealth_seed(domain, user_agent, proxy)
    configurations = (
        ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(TM) Plus Graphics 655, OpenGL 4.1)"),
        ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0)"),
        ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon Pro 560X OpenGL Engine, OpenGL 4.1)"),
    )
    vendor, renderer = configurations[seed % len(configurations)]
    config = {
        "seed": seed,
        "platform": platform,
        "hardwareConcurrency": (4, 8, 12, 16)[seed % 4],
        "deviceMemory": (4, 8, 8, 16)[(seed >> 2) % 4],
        "languages": ["en-US", "en"],
        "webglVendor": vendor,
        "webglRenderer": renderer,
    }
    return "(() => {\nconst cfg = " + json.dumps(config, ensure_ascii=False) + ";\n" + _STEALTH_SCRIPT_BODY + "\n})();"


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
                        # Explicit Camoufox still fails open to Playwright.  This
                        # keeps the optional dependency from becoming a hard
                        # runtime requirement when its browser binary is absent.
                        pass

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
        proxy = resolve_proxy(url, strategy)
        allow_cookies = bool(strategy and strategy.allow_cookies)
        explicit_user_agent = bool(
            strategy and (strategy.useragent or strategy.useragent_custom)
        )
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
        script = _build_stealth_script(
            domain=key.domain,
            user_agent=key.user_agent,
            proxy=proxy,
        )
        await context.add_init_script(script=script)
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

    @asynccontextmanager
    async def page(
        self,
        strategy: SiteStrategy | None = None,
        *,
        url: str = "",
        domain: str = "",
    ) -> AsyncIterator[Page]:
        if not self._started:
            await self.start()
        parsed_domain = domain or (urlparse(url).hostname or (strategy.domain if strategy else ""))
        normalized_url = url or (f"https://{parsed_domain}/" if parsed_domain else "https://example.com/")
        async with self._sem:
            key, headers, proxy = self._context_key(strategy, normalized_url, parsed_domain)
            entry = await self._acquire_context(key, headers, proxy)
            page: Page | None = None
            reusable = True
            try:
                page = await entry.context.new_page()
                page.set_default_timeout(BROWSER_TIMEOUT)
                page.set_default_navigation_timeout(BROWSER_TIMEOUT)
                yield page
            except BaseException:
                reusable = False
                raise
            finally:
                if page is not None:
                    if not key.allow_cookies:
                        await self._clear_page_storage(page)
                    try:
                        await page.close()
                    except Exception:
                        reusable = False
                await self._release_context(entry, reusable)

    async def stats(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "started": self._started,
                "engine": self._engine,
                "max_contexts": self._max,
                "total_contexts": self._total_contexts,
                "idle_contexts": len(self._idle_lru),
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


def _build_route_patterns(strategy: SiteStrategy) -> list[str]:
    """Convert a BPC ``block_regex`` into best-effort Playwright globs."""

    if not strategy.block_regex:
        return []
    patterns: list[str] = []
    for part in re.split(r"\|", strategy.block_regex):
        part = part.strip().strip("()")
        glob = _regex_to_glob(part)
        if glob:
            patterns.append(glob)
    if not patterns:
        patterns.append(f"**/*{strategy.domain}*paywall*")
    return patterns


def _regex_to_glob(regex_part: str) -> str:
    """Best-effort conversion of a simple regex fragment to a route glob."""

    value = regex_part.replace("\\.", ".").replace("\\/", "/")
    value = re.sub(r"\.\+", "*", value)
    value = re.sub(r"\.\*", "*", value)
    value = re.sub(r"\([^)]*\)", "*", value)
    value = re.sub(r"\[[^\]]*\]", "?", value)
    value = re.sub(r"[\\^$]", "", value)
    if not value or value == "*":
        return ""
    if not value.startswith("*"):
        value = "**/" + value
    if not value.endswith("*"):
        value += "*"
    return value


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
    *,
    general_patterns: list[re.Pattern[str]],
    strategy_patterns: list[re.Pattern[str]],
    block_images: bool,
) -> None:
    request = route.request
    resource_type = str(request.resource_type)
    url = str(request.url)

    abort = False
    if resource_type != "document" and _is_tracking_request(url):
        abort = True
    elif resource_type in {"script", "xhr", "fetch"} and _is_paywall_provider(url):
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


async def fetch_for_strategy(
    url: str,
    strategy: SiteStrategy | None,
    pool: BrowserPool | None = None,
) -> BrowserResult:
    """Fetch a page with stealth masking, interception, proxying, and pooling."""

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

    try:
        async with limiter.limit(domain):
            async with active_pool.page(strategy, url=url, domain=domain) as page:
                general_patterns = _compile_general_block_regexes(strategy) if strategy else []
                strategy_patterns = _compile_strategy_block_regex(strategy)
                block_images = _truthy_env("PAC_BROWSER_BLOCK_IMAGES", True)
                await page.route(
                    "**/*",
                    partial(
                        _handle_resource_route,
                        general_patterns=general_patterns,
                        strategy_patterns=strategy_patterns,
                        block_images=block_images,
                    ),
                )

                def dismiss_dialog(dialog: Any) -> None:
                    try:
                        asyncio.create_task(dialog.dismiss())
                    except Exception:
                        return

                page.on("dialog", dismiss_dialog)
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
