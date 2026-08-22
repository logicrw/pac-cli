"""On-demand interactive fallback for authorized publisher sessions.

Default backend is Ego lite: PAC opens the app if needed, drives a dedicated
task space through ``ego-browser``, reuses the BPC/login state already in Ego
lite, and extracts title / final URL / article text. Cookies stay in the
browser. PAC does not quit Ego lite afterwards.

DrissionPage attach to a dedicated Chrome user-data-dir remains available
behind ``PAC_INTERACTIVE_BACKEND=drissionpage``. This path is never part of
``pac batch``.

Hard rules:
- Do not import or copy a daily Chrome Default profile.
- Do not copy, export, or inject publisher cookies into PAC.
- No overlay deletion, no paywall-provider blocking, no CAPTCHA solving.
- Only create/close PAC-owned tabs in the PAC task space. Concurrency is 1.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import re
import json
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from http.client import HTTPConnection
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from .result import fail_result
from .sites import domain_from_url
from .ssrf import SSRFBlocked, assert_public_url

ENGINE = "interactive_cdp"
BACKEND_EGO = "ego"
BACKEND_DRISSION = "drissionpage"
EGO_TASK_SPACE = "pac-cli-interactive"
DEFAULT_CDP = "127.0.0.1:9222"
DEFAULT_TIMEOUT_S = 45.0
LOCK = asyncio.Lock()

_SECRET_KEYS = frozenset({
    "cookie",
    "cookies",
    "cookie_header",
    "set-cookie",
    "set_cookie",
    "document.cookie",
})
_USER_DATA_DIR_RE = re.compile(r"--user-data-dir(?:=|\s+)(\"[^\"]+\"|'[^']+'|\S+)", re.I)

# Read-only DOM snapshot. Must never read document.cookie or mutate overlays.
READ_ONLY_EXTRACT_JS = r"""
(() => {
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
  const paragraphs = [];
  if (container) {
    for (const paragraph of container.querySelectorAll('p')) {
      const text = (paragraph.innerText || '').trim();
      if (meaningfulLength(text) >= 15) paragraphs.push(text);
    }
    if (!paragraphs.length) {
      const directText = (container.innerText || '').trim();
      if (meaningfulLength(directText) >= 80) paragraphs.push(directText);
    }
  } else {
    const bodyText = (document.body && document.body.innerText || '').trim();
    if (meaningfulLength(bodyText) >= 80) paragraphs.push(bodyText);
  }
  const images = [];
  const seenImages = new Set();
  if (container) {
    for (const image of container.querySelectorAll('img')) {
      const source = image.currentSrc || image.src || image.getAttribute('data-src') || '';
      if (!source || seenImages.has(source)) continue;
      const lower = source.toLowerCase();
      if (['pixel', 'tracking', 'logo', 'icon', 'avatar', 'badge', 'spinner', 'data:image'].some(marker => lower.includes(marker))) continue;
      seenImages.add(source);
      images.push({src: source, alt: image.alt || ''});
      if (images.length >= 20) break;
    }
  }
  const title = (document.querySelector('h1')?.innerText || document.title || '').trim().split('|')[0].trim();
  const text = paragraphs.join('\n\n');
  const html = meaningfulLength(text) >= 200
    ? ''
    : (document.documentElement ? document.documentElement.outerHTML : '');
  return {
    title,
    url: location.href,
    text,
    html,
    images,
    paragraph_count: paragraphs.length,
    container_text_chars: container ? meaningfulLength(container.innerText || '') : 0,
    body_text_chars: meaningfulLength(document.body ? document.body.innerText : ''),
  };
})()
"""

# Read-only readiness probe. No DOM mutation, no cookie access.
ARTICLE_READY_JS = r"""
(() => {
  const selectors = [
    'article[data-body-id]', 'article .article-body', 'article .story-body',
    '[itemprop="articleBody"]', '.article__body', '.article-body',
    '.post-content', '.entry-content', '[data-component="body"]',
    '.story-text', '.article-text', '.story-body', 'article', 'main'
  ];
  return selectors.some(selector => document.querySelector(selector));
})()
"""

_DAILY_USER_DATA_MARKERS = (
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/Google/Chrome Canary",
    "Library/Application Support/Google/Chrome Beta",
    "Library/Application Support/Google/Chrome Dev",
    "Library/Application Support/Chromium",
    "Library/Application Support/Microsoft Edge",
    "Library/Application Support/BraveSoftware/Brave-Browser",
    "Library/Application Support/Arc",
    "Library/Application Support/Dia",
    "Library/Application Support/company.thebrowser.dia",
    "Library/Application Support/Vivaldi",
    "Library/Application Support/com.operasoftware.Opera",
    ".config/google-chrome",
    ".config/chromium",
    ".config/microsoft-edge",
    ".config/BraveSoftware",
    "AppData/Local/Google/Chrome/User Data",
    "AppData/Local/Microsoft/Edge/User Data",
    "AppData/Local/BraveSoftware/Brave-Browser/User Data",
    "AppData/Local/Chromium/User Data",
)


class InteractiveError(Exception):
    """Config/safety failure for the interactive CDP path."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "INTERNAL",
        failure_class: str = "config",
        recovery_hint: str = "",
    ):
        super().__init__(message)
        self.error_code = error_code
        self.failure_class = failure_class
        self.recovery_hint = recovery_hint


class ForbiddenProfileError(InteractiveError):
    pass


class ForbiddenCdpError(InteractiveError):
    pass


class InteractiveCookieRejected(InteractiveError):
    pass


class InteractiveBusy(InteractiveError):
    pass


@dataclass(frozen=True)
class InteractiveSettings:
    backend: str = BACKEND_EGO
    profile_dir: Path | None = None
    cdp_host: str = "127.0.0.1"
    cdp_port: int = 9222
    timeout_s: float = DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class PageSnapshot:
    title: str
    final_url: str
    html: str
    text: str
    images: list[dict[str, str]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    preexisting_tab_ids: tuple[str, ...] = ()
    owned_tab_id: str = ""
    closed_tab_ids: tuple[str, ...] = ()
    remaining_tab_ids: tuple[str, ...] = ()


class InteractiveBackend(Protocol):
    name: str

    def snapshot(self, url: str, settings: InteractiveSettings) -> PageSnapshot:
        """Attach, open one tab, extract read-only, close only that tab."""


def recovery_profile_hint() -> str:
    return (
        "For Chrome attach, set PAC_INTERACTIVE_BACKEND=drissionpage and pass a "
        "dedicated --interactive-profile (never Chrome Default), with CDP on 127.0.0.1."
    )


def recovery_ego_hint() -> str:
    return (
        "Install Ego lite, keep Bypass Paywalls Clean in that browser, and ensure "
        "``ego-browser`` is on PATH. PAC opens Ego lite on demand for --interactive."
    )


def _ego_autostart_enabled() -> bool:
    raw = os.environ.get("PAC_INTERACTIVE_EGO_AUTOSTART", "1").strip().casefold()
    return raw not in {"0", "false", "off", "no"}


def find_ego_lite_app() -> Path:
    raw = os.environ.get("PAC_INTERACTIVE_EGO_APP", "").strip()
    candidates = []
    if raw:
        candidates.append(Path(raw).expanduser())
    candidates.extend((
        Path("/Applications/ego lite.app"),
        Path.home() / "Applications/ego lite.app",
    ))
    for path in candidates:
        if path.is_dir():
            return path.resolve()
    raise InteractiveError(
        "Ego lite.app was not found in /Applications",
        error_code="BROWSER_UNAVAILABLE",
        recovery_hint=recovery_ego_hint(),
    )


def ego_lite_is_running() -> bool:
    completed = subprocess.run(
        ["pgrep", "-f", r"/Contents/MacOS/ego lite"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and bool(completed.stdout.strip())


def launch_ego_lite() -> Path:
    app = find_ego_lite_app()
    completed = subprocess.run(
        ["open", "-g", str(app)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "open failed").strip()
        raise InteractiveError(
            f"failed to launch Ego lite: {detail[:300]}",
            error_code="BROWSER_UNAVAILABLE",
            recovery_hint=recovery_ego_hint(),
        )
    return app


def _ego_ready_probe(binary: str) -> bool:
    try:
        completed = subprocess.run(
            [binary, "nodejs"],
            input="await listTaskSpaces()\ncliLog('PAC_EGO_READY')\n",
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    blob = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    return "PAC_EGO_READY" in blob


def ensure_ego_lite_running(*, binary: str, timeout_s: float) -> None:
    deadline = time.monotonic() + max(float(timeout_s), 5.0)
    launched = False
    while time.monotonic() < deadline:
        if _ego_ready_probe(binary):
            return
        if not _ego_autostart_enabled():
            raise InteractiveError(
                "Ego lite is not running",
                error_code="BROWSER_UNAVAILABLE",
                recovery_hint="start Ego lite, or leave PAC_INTERACTIVE_EGO_AUTOSTART=1",
            )
        if not launched or not ego_lite_is_running():
            launch_ego_lite()
            launched = True
        time.sleep(0.4)
    raise InteractiveError(
        "Ego lite did not become ready after launch",
        error_code="BROWSER_UNAVAILABLE",
        recovery_hint=recovery_ego_hint(),
    )


def parse_cdp_endpoint(raw: str | None) -> tuple[str, int]:
    text = (raw or "").strip() or os.environ.get("PAC_INTERACTIVE_CDP", "").strip() or DEFAULT_CDP
    if text.isdigit():
        host, port = "127.0.0.1", int(text)
    elif text.startswith("["):
        match = re.fullmatch(r"\[([^\]]+)\]:(\d+)", text)
        if not match:
            raise ForbiddenCdpError(
                f"invalid CDP endpoint: {text}",
                error_code="BROWSER_UNAVAILABLE",
                recovery_hint="use 127.0.0.1:9222",
            )
        host, port = match.group(1), int(match.group(2))
    else:
        host, separator, port_text = text.rpartition(":")
        if not separator or not host or not port_text.isdigit():
            raise ForbiddenCdpError(
                f"invalid CDP endpoint: {text}",
                error_code="BROWSER_UNAVAILABLE",
                recovery_hint="use 127.0.0.1:9222",
            )
        port = int(port_text)
    return assert_loopback_cdp(host, port)


def assert_loopback_cdp(host: str, port: int) -> tuple[str, int]:
    if port < 1 or port > 65535:
        raise ForbiddenCdpError(
            f"invalid CDP port: {port}",
            error_code="BROWSER_UNAVAILABLE",
            recovery_hint="use a loopback port such as 9222",
        )
    hostname = (host or "").strip().strip("[]")
    if not hostname:
        raise ForbiddenCdpError(
            "empty CDP host",
            error_code="BROWSER_UNAVAILABLE",
            recovery_hint="use 127.0.0.1:9222",
        )
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ForbiddenCdpError(
            f"CDP host did not resolve: {hostname}",
            error_code="BROWSER_UNAVAILABLE",
            recovery_hint="use 127.0.0.1:9222",
        ) from exc
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not addresses:
        raise ForbiddenCdpError(
            f"CDP host produced no addresses: {hostname}",
            error_code="BROWSER_UNAVAILABLE",
            recovery_hint="use 127.0.0.1:9222",
        )
    if any(not addr.is_loopback for addr in addresses):
        raise ForbiddenCdpError(
            f"CDP host is not loopback: {hostname}",
            error_code="BROWSER_UNAVAILABLE",
            recovery_hint="bind Chrome DevTools to 127.0.0.1 only",
        )
    if any(addr.version == 4 for addr in addresses):
        return "127.0.0.1", port
    return "::1", port


def assert_dedicated_profile(path: Path | str | None) -> Path:
    raw = str(path or "").strip() or os.environ.get("PAC_INTERACTIVE_PROFILE", "").strip()
    if not raw:
        raise ForbiddenProfileError(
            "interactive mode requires --interactive-profile or PAC_INTERACTIVE_PROFILE",
            error_code="BROWSER_UNAVAILABLE",
            recovery_hint=recovery_profile_hint(),
        )
    profile = Path(raw).expanduser()
    try:
        resolved = profile.resolve(strict=False)
    except OSError as exc:
        raise ForbiddenProfileError(
            f"interactive profile is not resolvable: {profile}",
            error_code="BROWSER_UNAVAILABLE",
            recovery_hint=recovery_profile_hint(),
        ) from exc
    if resolved.exists() and not resolved.is_dir():
        raise ForbiddenProfileError(
            f"interactive profile is not a directory: {resolved}",
            error_code="BROWSER_UNAVAILABLE",
            recovery_hint=recovery_profile_hint(),
        )
    rendered = str(resolved)
    home = str(Path.home().resolve())
    for marker in _DAILY_USER_DATA_MARKERS:
        if marker in rendered:
            raise ForbiddenProfileError(
                "interactive profile cannot be a daily browser user-data-dir",
                error_code="BROWSER_UNAVAILABLE",
                recovery_hint=recovery_profile_hint(),
            )
        if rendered.startswith(f"{home}/{marker}") or rendered.startswith(f"{home}\\{marker}"):
            raise ForbiddenProfileError(
                "interactive profile cannot be a daily browser user-data-dir",
                error_code="BROWSER_UNAVAILABLE",
                recovery_hint=recovery_profile_hint(),
            )
    if resolved.name in {"Default", "Chrome", "Chromium", "User Data"} and any(
        marker in str(resolved.parent) for marker in _DAILY_USER_DATA_MARKERS
    ):
        raise ForbiddenProfileError(
            "interactive profile cannot be a daily browser Default profile",
            error_code="BROWSER_UNAVAILABLE",
            recovery_hint=recovery_profile_hint(),
        )
    return resolved


def _require_public_article_url(candidate: str) -> None:
    target = (candidate or "").strip()
    scheme = (urlparse(target).scheme or "").casefold()
    if scheme not in {"http", "https"}:
        raise InteractiveError(
            f"interactive extract landed on a non-http URL ({scheme or 'none'})",
            error_code="BROWSER_UNAVAILABLE",
            recovery_hint="retry; the Ego tab had not finished navigating to the article",
        )
    try:
        assert_public_url(target)
    except SSRFBlocked as exc:
        raise InteractiveError(
            str(exc),
            error_code="SSRF_BLOCKED",
            failure_class="config",
            recovery_hint="interactive extract still requires a public http(s) article URL",
        ) from exc


def reject_cookie_header(cookie_header: str | None) -> None:
    if (cookie_header or "").strip():
        raise InteractiveCookieRejected(
            "interactive mode refuses PAC cookie headers; keep the session in the dedicated profile",
            error_code="INTERNAL",
            recovery_hint="unset PAC_COOKIE / --cookie and use the dedicated Chromium profile instead",
        )


def parse_user_data_dir(command_line: str) -> Path | None:
    match = _USER_DATA_DIR_RE.search(command_line or "")
    if not match:
        return None
    raw = match.group(1).strip().strip("\"'")
    if not raw:
        return None
    return Path(raw).expanduser()


def assert_process_uses_profile(command_line: str, profile: Path) -> None:
    user_data = parse_user_data_dir(command_line)
    if user_data is None:
        raise ForbiddenProfileError(
            "attached Chromium has no --user-data-dir; refusing the daily Default profile",
            error_code="BROWSER_UNAVAILABLE",
            recovery_hint=recovery_profile_hint(),
        )
    try:
        resolved = user_data.resolve(strict=False)
    except OSError as exc:
        raise ForbiddenProfileError(
            f"attached --user-data-dir is not resolvable: {user_data}",
            error_code="BROWSER_UNAVAILABLE",
            recovery_hint=recovery_profile_hint(),
        ) from exc
    assert_dedicated_profile(resolved)
    if resolved != profile.resolve(strict=False):
        raise ForbiddenProfileError(
            "attached Chromium user-data-dir does not match the dedicated interactive profile",
            error_code="BROWSER_UNAVAILABLE",
            recovery_hint=recovery_profile_hint(),
        )


def list_cdp_listeners(port: int) -> list[tuple[int, str]]:
    """Return (pid, listen address) pairs for TCP LISTEN on port."""

    completed = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-F", "pn"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return []
    listeners: list[tuple[int, str]] = []
    pid = 0
    for line in completed.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid:
            listeners.append((pid, line[1:]))
    return listeners


def process_command_line(pid: int) -> str:
    completed = subprocess.run(
        ["ps", "-o", "args=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def assert_cdp_already_listening(host: str, port: int) -> tuple[int, str]:
    listeners = list_cdp_listeners(port)
    if not listeners:
        raise ForbiddenCdpError(
            f"no Chromium is listening on {host}:{port}",
            error_code="BROWSER_UNAVAILABLE",
            recovery_hint=recovery_profile_hint(),
        )
    for pid, address in listeners:
        listen_host = address.rsplit(":", 1)[0].strip("[]")
        if listen_host in {"*", "0.0.0.0", "::", "localhost"}:
            if listen_host != "localhost":
                raise ForbiddenCdpError(
                    f"CDP is bound to a non-loopback address: {address}",
                    error_code="BROWSER_UNAVAILABLE",
                    recovery_hint="restart Chromium with --remote-debugging-address=127.0.0.1",
                )
        else:
            try:
                addr = ipaddress.ip_address(listen_host)
            except ValueError as exc:
                raise ForbiddenCdpError(
                    f"CDP listen address is not an IP: {address}",
                    error_code="BROWSER_UNAVAILABLE",
                    recovery_hint="bind Chrome DevTools to 127.0.0.1 only",
                ) from exc
            if not addr.is_loopback:
                raise ForbiddenCdpError(
                    f"CDP is bound to a non-loopback address: {address}",
                    error_code="BROWSER_UNAVAILABLE",
                    recovery_hint="restart Chromium with --remote-debugging-address=127.0.0.1",
                )
    pid, _address = listeners[0]
    command = process_command_line(pid)
    if not command:
        raise ForbiddenCdpError(
            f"could not read command line for CDP pid {pid}",
            error_code="BROWSER_UNAVAILABLE",
            recovery_hint=recovery_profile_hint(),
        )
    return pid, command


def fetch_cdp_version(host: str, port: int, timeout_s: float = 2.0) -> dict[str, Any]:
    if host not in {"127.0.0.1", "::1"}:
        raise ForbiddenCdpError(
            f"CDP version probe host is not loopback: {host}",
            error_code="BROWSER_UNAVAILABLE",
            recovery_hint="bind Chrome DevTools to 127.0.0.1 only",
        )
    connection = HTTPConnection(host, port, timeout=timeout_s)
    try:
        connection.request("GET", "/json/version")
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise ForbiddenCdpError(
                "CDP version endpoint redirected; refusing to follow",
                error_code="BROWSER_UNAVAILABLE",
                recovery_hint="bind Chrome DevTools to 127.0.0.1 only",
            )
        if response.status != 200:
            raise ForbiddenCdpError(
                f"CDP version endpoint returned HTTP {response.status}",
                error_code="BROWSER_UNAVAILABLE",
                recovery_hint=recovery_profile_hint(),
            )
        payload = response.read().decode("utf-8", errors="replace")
    finally:
        connection.close()
    data = json.loads(payload)
    websocket = str(data.get("webSocketDebuggerUrl") or "")
    parsed = urlparse(websocket)
    ws_host = (parsed.hostname or "").strip("[]")
    if ws_host:
        try:
            addr = ipaddress.ip_address(ws_host)
        except ValueError as exc:
            raise ForbiddenCdpError(
                f"CDP websocket host is not an IP: {ws_host}",
                error_code="BROWSER_UNAVAILABLE",
                recovery_hint="bind Chrome DevTools to 127.0.0.1 only",
            ) from exc
        if not addr.is_loopback:
            raise ForbiddenCdpError(
                f"CDP websocket is not loopback: {ws_host}",
                error_code="BROWSER_UNAVAILABLE",
                recovery_hint="bind Chrome DevTools to 127.0.0.1 only",
            )
    return data


def _timeout_s(timeout_s: float | None) -> float:
    if timeout_s is None:
        raw = os.environ.get("PAC_INTERACTIVE_TIMEOUT_MS", "").strip()
        timeout_s = int(raw) / 1000 if raw.isdigit() else DEFAULT_TIMEOUT_S
    return max(float(timeout_s), 1.0)


def resolve_backend_name(explicit: str | None = None) -> str:
    raw = (explicit or os.environ.get("PAC_INTERACTIVE_BACKEND", "") or BACKEND_EGO).strip().casefold()
    if raw in {"ego", "ego-lite", "ego_lite", "egolite"}:
        return BACKEND_EGO
    if raw in {"drission", "drissionpage"}:
        return BACKEND_DRISSION
    raise InteractiveError(
        f"unknown interactive backend: {raw}",
        error_code="BROWSER_UNAVAILABLE",
        recovery_hint="use PAC_INTERACTIVE_BACKEND=ego or drissionpage",
    )


def resolve_settings(
    *,
    profile: Path | str | None,
    cdp: str | None,
    timeout_s: float | None = None,
    backend: str = BACKEND_EGO,
) -> InteractiveSettings:
    timeout = _timeout_s(timeout_s)
    if backend != BACKEND_DRISSION:
        return InteractiveSettings(backend=backend, timeout_s=timeout)
    profile_dir = assert_dedicated_profile(profile)
    host, port = parse_cdp_endpoint(cdp)
    return InteractiveSettings(
        backend=backend,
        profile_dir=profile_dir,
        cdp_host=host,
        cdp_port=port,
        timeout_s=timeout,
    )


def assert_tab_isolation(snapshot: PageSnapshot) -> None:
    preexisting = set(snapshot.preexisting_tab_ids)
    remaining = set(snapshot.remaining_tab_ids)
    closed = set(snapshot.closed_tab_ids)
    missing = preexisting - remaining
    if missing and missing != {snapshot.owned_tab_id}:
        unexpected = missing - {snapshot.owned_tab_id}
        if unexpected:
            raise InteractiveError(
                "interactive extract closed or lost an unrelated tab",
                error_code="INTERNAL",
                recovery_hint="stop and inspect the dedicated Chromium window; PAC must not touch other tabs",
            )
    extra_closed = closed - {snapshot.owned_tab_id}
    if extra_closed:
        raise InteractiveError(
            "interactive extract closed an unrelated tab",
            error_code="INTERNAL",
            recovery_hint="stop and inspect the dedicated Chromium window; PAC must not touch other tabs",
        )


def _contains_secret_key(value: Any) -> str:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if str(key).casefold() in _SECRET_KEYS or normalized in _SECRET_KEYS:
                return str(key)
            found = _contains_secret_key(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _contains_secret_key(nested)
            if found:
                return found
    return ""


def redact_secrets(result: dict[str, Any]) -> dict[str, Any]:
    leaked = _contains_secret_key(result)
    if not leaked:
        return result
    raise InteractiveError(
        f"interactive result attempted to include secret key {leaked}",
        error_code="INTERNAL",
        recovery_hint="interactive extract must never copy cookies into PAC output",
    )


def provenance(settings: InteractiveSettings, backend: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "backend": backend,
        "profile_kind": "ego-lite" if backend == BACKEND_EGO else "dedicated",
        "cookie_copied": False,
        "paywall_cleanup": False,
        "route_interception": False,
        "owned_tab_only": True,
        "concurrency": 1,
    }
    if backend == BACKEND_DRISSION:
        payload["cdp_host"] = settings.cdp_host
        payload["cdp_port"] = settings.cdp_port
    return {"interactive": payload}


def _lock_path(settings: InteractiveSettings) -> Path:
    key = str(settings.profile_dir) if settings.profile_dir else f"backend:{settings.backend}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"pac-interactive-{digest}.lock"


class _FileLock:
    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        if fcntl is not None:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                handle.close()
                raise InteractiveBusy(
                    "another interactive extract is already running",
                    error_code="LIMIT_EXCEEDED",
                    recovery_hint="wait for the current interactive fetch to finish; concurrency is 1",
                ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


def _load_drissionpage():
    try:
        from DrissionPage import Chromium
    except ImportError as exc:
        raise InteractiveError(
            "DrissionPage is not installed",
            error_code="BROWSER_UNAVAILABLE",
            recovery_hint="pip install 'pac-cli[interactive]' or pip install DrissionPage",
        ) from exc
    return Chromium


class DrissionPageBackend:
    name = BACKEND_DRISSION

    def snapshot(self, url: str, settings: InteractiveSettings) -> PageSnapshot:
        _pid, command = assert_cdp_already_listening(settings.cdp_host, settings.cdp_port)
        assert_process_uses_profile(command, settings.profile_dir)
        fetch_cdp_version(settings.cdp_host, settings.cdp_port)
        Chromium = _load_drissionpage()
        browser = Chromium(addr_or_opts=f"{settings.cdp_host}:{settings.cdp_port}")
        actual = getattr(browser, "user_data_path", "") or ""
        if actual:
            assert_process_uses_profile(f"--user-data-dir={actual}", settings.profile_dir)
        preexisting = _tab_ids(browser)
        tab = browser.new_tab(url=None, new_window=False, background=True)
        owned_id = str(getattr(tab, "tab_id", "") or "")
        if not owned_id:
            try:
                tab.close()
            except Exception:
                pass
            raise InteractiveError(
                "DrissionPage opened a tab without an id",
                error_code="BROWSER_UNAVAILABLE",
                recovery_hint=recovery_profile_hint(),
            )
        closed: list[str] = []
        raw: Any = {}
        extract_error: Exception | None = None
        try:
            tab.get(url, timeout=settings.timeout_s)
            _wait_for_article_container(tab, timeout_s=min(2.0, settings.timeout_s))
            raw = tab.run_js(READ_ONLY_EXTRACT_JS)
        except Exception as exc:
            extract_error = exc
        finally:
            remaining_before_close = _tab_ids(browser)
            if owned_id in remaining_before_close:
                try:
                    tab.close()
                    closed.append(owned_id)
                except Exception:
                    pass
        remaining = _tab_ids(browser)
        if extract_error is not None:
            raise extract_error
        payload = raw if isinstance(raw, dict) else {}
        return _snapshot_from_payload(
            payload,
            url=url,
            preexisting=preexisting,
            owned_tab_id=owned_id,
            closed_tab_ids=tuple(closed),
            remaining_tab_ids=remaining,
        )


def _images_from_payload(payload: dict[str, Any]) -> list[dict[str, str]]:
    images: list[dict[str, str]] = []
    for item in payload.get("images") or []:
        if isinstance(item, dict) and item.get("src"):
            images.append({"src": str(item.get("src") or ""), "alt": str(item.get("alt") or "")})
    return images


def _snapshot_from_payload(
    payload: dict[str, Any],
    *,
    url: str,
    preexisting: tuple[str, ...],
    owned_tab_id: str,
    closed_tab_ids: tuple[str, ...],
    remaining_tab_ids: tuple[str, ...],
) -> PageSnapshot:
    return PageSnapshot(
        title=str(payload.get("title") or ""),
        final_url=str(payload.get("url") or url),
        html=str(payload.get("html") or ""),
        text=str(payload.get("text") or ""),
        images=_images_from_payload(payload),
        metrics={
            "paragraph_count": int(payload.get("paragraph_count") or 0),
            "container_text_chars": int(payload.get("container_text_chars") or 0),
            "body_text_chars": int(payload.get("body_text_chars") or 0),
        },
        preexisting_tab_ids=preexisting,
        owned_tab_id=owned_tab_id,
        closed_tab_ids=closed_tab_ids,
        remaining_tab_ids=remaining_tab_ids,
    )


def _tab_ids(browser: Any) -> tuple[str, ...]:
    try:
        return tuple(str(tab_id) for tab_id in (browser.tab_ids or []))
    except Exception:
        return ()


def _wait_for_article_container(tab: Any, timeout_s: float) -> None:
    deadline = time.monotonic() + max(timeout_s, 0.0)
    while True:
        try:
            if tab.run_js(ARTICLE_READY_JS):
                return
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return
        time.sleep(0.2)


def _ego_extract_script(url: str, timeout_s: float) -> str:
    return f"""
const url = {json.dumps(url)}
const timeoutS = {float(timeout_s)}
const extractJs = {json.dumps(READ_ONLY_EXTRACT_JS)}
const readyJs = {json.dumps(ARTICLE_READY_JS)}
const task = await useOrCreateTaskSpace({json.dumps(EGO_TASK_SPACE)})
if (!task || !task.id) {{
  throw new Error('ego-browser did not select the PAC task space')
}}
const preexisting = (await listTabs()).map(tab => String(tab.targetId || ''))
let owned = ''
let payload = {{}}
let error = ''
try {{
  const tab = await createTab(url)
  owned = String(tab && tab.targetId || '')
  const navDeadline = Date.now() + timeoutS * 1000
  while (Date.now() < navDeadline) {{
    try {{
      const info = await pageInfo()
      const href = String(info && info.url || '')
      if (/^https?:\\/\\//i.test(href)) break
    }} catch (_) {{}}
    await wait(0.2)
  }}
  const deadline = Date.now() + Math.min(2000, timeoutS * 1000)
  while (Date.now() < deadline) {{
    try {{
      if (await js(readyJs)) break
    }} catch (_) {{}}
    await wait(0.2)
  }}
  payload = await js(extractJs)
}} catch (exc) {{
  error = String(exc)
}}
if (owned) {{
  try {{ await closeTab(owned) }} catch (_) {{}}
  await wait(0.3)
}}
const remaining = (await listTabs()).map(tab => String(tab.targetId || ''))
cliLog('PAC_RESULT ' + JSON.stringify({{
  error,
  owned,
  preexisting,
  remaining,
  closed: owned && !remaining.includes(owned) ? [owned] : [],
  payload,
}}))
"""


def _parse_ego_stdout(stdout: str, stderr: str = "") -> dict[str, Any]:
    marker = "PAC_RESULT "
    blob = f"{stdout or ''}\n{stderr or ''}"
    for line in reversed(blob.splitlines()):
        if line.startswith(marker):
            return json.loads(line[len(marker):])
    raise InteractiveError(
        "ego-browser produced no extract result",
        error_code="BROWSER_UNAVAILABLE",
        recovery_hint=recovery_ego_hint(),
    )


def _run_ego_browser(script: str, timeout_s: float) -> subprocess.CompletedProcess[str]:
    binary = shutil.which("ego-browser")
    if not binary:
        raise InteractiveError(
            "ego-browser is not on PATH",
            error_code="BROWSER_UNAVAILABLE",
            recovery_hint=recovery_ego_hint(),
        )
    ensure_ego_lite_running(binary=binary, timeout_s=min(25.0, max(timeout_s, 5.0)))
    try:
        return subprocess.run(
            [binary, "nodejs"],
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout_s + 15,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise InteractiveError(
            "ego-browser timed out",
            error_code="BROWSER_UNAVAILABLE",
            recovery_hint=recovery_ego_hint(),
        ) from exc


class EgoLiteBackend:
    name = BACKEND_EGO

    def snapshot(self, url: str, settings: InteractiveSettings) -> PageSnapshot:
        script = _ego_extract_script(url, settings.timeout_s)
        if "cookie" in script.casefold():
            raise InteractiveError(
                "interactive extract script must not mention cookies",
                error_code="INTERNAL",
                recovery_hint="fix READ_ONLY_EXTRACT_JS",
            )
        completed = _run_ego_browser(script, settings.timeout_s)
        combined = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        if completed.returncode != 0 and "PAC_RESULT " not in combined:
            detail = (completed.stderr or completed.stdout or "ego-browser failed")[-500:]
            if "user is controlling" in detail.casefold():
                raise InteractiveError(
                    "Ego lite task space is under user control",
                    error_code="BROWSER_UNAVAILABLE",
                    recovery_hint="finish or hand back the Ego lite space, then retry",
                )
            raise InteractiveError(
                detail.strip() or "ego-browser failed",
                error_code="BROWSER_UNAVAILABLE",
                recovery_hint=recovery_ego_hint(),
            )
        body = _parse_ego_stdout(completed.stdout or "", completed.stderr or "")
        if body.get("error"):
            raise InteractiveError(
                str(body.get("error"))[:500],
                error_code="BROWSER_UNAVAILABLE",
                recovery_hint=recovery_ego_hint(),
            )
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        owned = str(body.get("owned") or "")
        return _snapshot_from_payload(
            payload,
            url=url,
            preexisting=tuple(str(item) for item in (body.get("preexisting") or []) if item),
            owned_tab_id=owned,
            closed_tab_ids=tuple(str(item) for item in (body.get("closed") or []) if item),
            remaining_tab_ids=tuple(str(item) for item in (body.get("remaining") or []) if item),
        )


def default_backend() -> InteractiveBackend:
    if resolve_backend_name() == BACKEND_DRISSION:
        return DrissionPageBackend()
    return EgoLiteBackend()


def _fail(
    *,
    url: str,
    domain: str,
    exc: InteractiveError,
    settings: InteractiveSettings | None,
    backend: str,
    started_at: float,
    rule_version: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = extra or {}
    if settings is not None:
        payload.update(provenance(settings, backend))
    return fail_result(
        url=url,
        domain=domain,
        error_code=exc.error_code,
        failure_class=exc.failure_class,
        error=str(exc),
        strategy_hit=[f"{ENGINE}:{backend}"],
        rule_version=rule_version,
        recovery_hint=exc.recovery_hint,
        latency_ms=int((time.perf_counter() - started_at) * 1000),
        engine=ENGINE,
        extra=payload,
    )


async def fetch_interactive(
    url: str,
    *,
    profile: Path | str | None = None,
    cdp: str | None = None,
    cookie_header: str = "",
    allow_partial: bool = False,
    full_markdown: bool = False,
    rule_version: str = "",
    backend: InteractiveBackend | None = None,
    diagnostics: bool = False,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Fetch one article through an already-running dedicated Chromium profile."""

    started_at = time.perf_counter()
    domain = domain_from_url(url)
    active_backend = backend or default_backend()
    backend_name = getattr(active_backend, "name", BACKEND_EGO)
    settings: InteractiveSettings | None = None
    try:
        reject_cookie_header(cookie_header)
        try:
            assert_public_url(url)
        except SSRFBlocked as exc:
            raise InteractiveError(
                str(exc),
                error_code="SSRF_BLOCKED",
                failure_class="config",
                recovery_hint="interactive extract still requires a public http(s) article URL",
            ) from exc
        if "cookie" in READ_ONLY_EXTRACT_JS.casefold() or "cookie" in ARTICLE_READY_JS.casefold():
            raise InteractiveError(
                "interactive extract script must not mention cookies",
                error_code="INTERNAL",
                recovery_hint="fix READ_ONLY_EXTRACT_JS",
            )
        settings = resolve_settings(profile=profile, cdp=cdp, backend=backend_name)
    except InteractiveError as exc:
        return redact_secrets(_fail(
            url=url,
            domain=domain,
            exc=exc,
            settings=settings,
            backend=backend_name,
            started_at=started_at,
            rule_version=rule_version,
        ))

    file_lock = _FileLock(_lock_path(settings))
    try:
        async with LOCK:
            file_lock.acquire()
            try:
                snapshot = await asyncio.to_thread(active_backend.snapshot, url, settings)
                assert_tab_isolation(snapshot)
            finally:
                file_lock.release()
    except InteractiveError as exc:
        return redact_secrets(_fail(
            url=url,
            domain=domain,
            exc=exc,
            settings=settings,
            backend=backend_name,
            started_at=started_at,
            rule_version=rule_version,
        ))
    except Exception as exc:
        wrapped = InteractiveError(
            str(exc)[:500],
            error_code="BROWSER_UNAVAILABLE",
            failure_class="config",
            recovery_hint=recovery_profile_hint(),
        )
        return redact_secrets(_fail(
            url=url,
            domain=domain,
            exc=wrapped,
            settings=settings,
            backend=backend_name,
            started_at=started_at,
            rule_version=rule_version,
        ))

    from .strategy import _evaluate_candidate, _quality_failure

    try:
        _require_public_article_url(snapshot.final_url or url)
    except InteractiveError as exc:
        return redact_secrets(_fail(
            url=url,
            domain=domain,
            exc=exc,
            settings=settings,
            backend=backend_name,
            started_at=started_at,
            rule_version=rule_version,
        ))

    hit = f"{ENGINE}:{backend_name}"
    evaluation = await _evaluate_candidate(
        snapshot.html,
        snapshot.final_url or url,
        domain,
        dom_result={
            "title": snapshot.title,
            "text": snapshot.text,
            "images": snapshot.images,
            **snapshot.metrics,
        },
        allow_partial=allow_partial,
        strategy_hit=[hit],
        rule_version=rule_version,
        engine=ENGINE,
        t0=started_at,
        full_markdown=full_markdown,
    )
    extra = provenance(settings, backend_name)
    extra["final_url"] = snapshot.final_url or url
    if diagnostics:
        extra["interactive_tabs"] = {
            "preexisting": len(snapshot.preexisting_tab_ids),
            "owned": snapshot.owned_tab_id,
            "closed": list(snapshot.closed_tab_ids),
        }
        if request_id:
            extra["request_id"] = request_id
    if evaluation.result is not None:
        evaluation.result.update(extra)
        return redact_secrets(evaluation.result)

    code, error, failure_class = _quality_failure(evaluation)
    failed = fail_result(
        url=snapshot.final_url or url,
        domain=domain,
        error_code=code,
        failure_class=failure_class,
        error=error,
        strategy_hit=[hit],
        rule_version=rule_version,
        recovery_hint=(
            "confirm the article is readable in the dedicated profile, then retry; "
            "teasers and challenge shells are reported honestly"
        ),
        latency_ms=int((time.perf_counter() - started_at) * 1000),
        engine=ENGINE,
        extra=extra,
    )
    return redact_secrets(failed)
