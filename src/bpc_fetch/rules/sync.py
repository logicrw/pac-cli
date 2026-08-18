"""Synchronize BPC rules with offline-first stale-while-revalidate support.

Manual ``pac rules sync`` remains synchronous.  Fetch and batch workflows can
use :func:`schedule_rules_revalidation` to read cached rules immediately and,
when stale, launch a detached best-effort refresh that never waits for upstream
network I/O on the extraction path.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from ..sites import SITES_JS_DEFAULT, _extract_entries, entries_to_domain_map
from ..ssrf import assert_public_url
from .paths import (
    cache_map_path,
    manifest_path,
    rules_root,
    sites_js_path,
    sites_updated_path,
    snapshot_path,
)

DEFAULT_UPDATED_URL = os.environ.get(
    "PAC_SITES_UPDATED_URL",
    "https://raw.githubusercontent.com/logicrw/pac-cli/main/data/sites_updated.json",
)
SITES_JS_URL = os.environ.get(
    "PAC_SITES_JS_URL",
    "https://raw.githubusercontent.com/logicrw/pac-cli/main/data/sites.js",
).strip()
MAX_SITES_JS_ZIP_BYTES = 5_000_000
MAX_ZIP_MEMBERS = 1_000
MAX_ZIP_COMPRESSION_RATIO = 200
DEFAULT_RULES_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_RULES_RETRY_BACKOFF_SECONDS = 60 * 60
DEFAULT_SWR_LOCK_STALE_SECONDS = 15 * 60
DEFAULT_LOCK_HARD_STALE_SECONDS = 2 * 60 * 60
_SWR_LOCK_NAME = ".rules-swr.lock"
_SYNC_LOCK_NAME = ".rules-sync.lock"
_SWR_CHILD_ENV = "PAC_RULES_SWR_CHILD"
_SWR_LOCK_ENV = "PAC_RULES_SWR_LOCK"
_SWR_LOCK_TOKEN_ENV = "PAC_RULES_SWR_LOCK_TOKEN"
_SWR_NONBLOCKING: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "pac_rules_swr_nonblocking", default=False
)


def download_bytes(
    url: str,
    *,
    client: httpx.Client | None = None,
    max_bytes: int = 10_000_000,
) -> bytes:
    """Download bounded bytes with SSRF-safe, manually validated redirects."""

    current = url
    owns_client = client is None
    active_client = client or httpx.Client(timeout=60.0, follow_redirects=False)
    try:
        for redirects in range(6):
            assert_public_url(current)
            with active_client.stream(
                "GET",
                current,
                timeout=60.0,
                follow_redirects=False,
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise RuntimeError("redirect missing Location")
                    if redirects == 5:
                        raise RuntimeError("redirect limit exceeded")
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise RuntimeError(f"HTTP {response.status_code}")

                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except ValueError as exc:
                        raise RuntimeError("invalid Content-Length") from exc
                    if declared_size < 0:
                        raise RuntimeError("invalid Content-Length")
                    if declared_size > max_bytes:
                        raise RuntimeError(
                            f"response too large: {declared_size} > {max_bytes}"
                        )

                data = bytearray()
                for chunk in response.iter_bytes():
                    if len(data) + len(chunk) > max_bytes:
                        raise RuntimeError(
                            f"response too large: more than {max_bytes} bytes"
                        )
                    data.extend(chunk)
                return bytes(data)
        raise RuntimeError("redirect limit exceeded")
    finally:
        if owns_client:
            active_client.close()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_datetime() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> str:
    return _format_utc(_now_datetime())


def _parse_utc(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _rules_ttl_seconds(ttl_seconds: int | None = None) -> int:
    if ttl_seconds is not None:
        return max(0, int(ttl_seconds))
    raw = os.environ.get("PAC_RULES_TTL_SECONDS", str(DEFAULT_RULES_TTL_SECONDS))
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_RULES_TTL_SECONDS


def _retry_backoff_seconds() -> int:
    raw = os.environ.get(
        "PAC_RULES_RETRY_BACKOFF_SECONDS",
        str(DEFAULT_RULES_RETRY_BACKOFF_SECONDS),
    )
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_RULES_RETRY_BACKOFF_SECONDS


def _swr_lock_stale_seconds() -> int:
    raw = os.environ.get(
        "PAC_RULES_SWR_LOCK_STALE_SECONDS",
        str(DEFAULT_SWR_LOCK_STALE_SECONDS),
    )
    try:
        return max(30, int(raw))
    except ValueError:
        return DEFAULT_SWR_LOCK_STALE_SECONDS


def _load_manifest_safe() -> dict[str, Any]:
    committed = snapshot_path()
    if committed.exists():
        try:
            snapshot = json.loads(committed.read_text(encoding="utf-8"))
            manifest = snapshot.get("manifest") if isinstance(snapshot, dict) else None
            if isinstance(manifest, dict):
                return manifest
        except (OSError, json.JSONDecodeError):
            pass
    path = manifest_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _sync_skip_reason(sites_js: Path | None, disabled: bool) -> str:
    if disabled:
        return "disabled_cli"
    if os.environ.get("PAC_RULES_AUTO_SYNC", "").strip().lower() in {
        "0",
        "off",
        "false",
        "no",
    }:
        return "disabled_env"
    if os.environ.get("PAC_RULES_PIN", "").strip():
        return "pinned"
    if sites_js is not None:
        return "explicit_sites_js"
    return ""


def _freshness_state(
    *,
    now: datetime | None = None,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    current = (now or _now_datetime()).astimezone(timezone.utc)
    path = snapshot_path() if snapshot_path().exists() else manifest_path()
    if not path.exists():
        return {"fresh": False, "reason": "missing", "manifest": {}}

    if path == snapshot_path():
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            manifest = snapshot.get("manifest") if isinstance(snapshot, dict) else None
        except (OSError, json.JSONDecodeError):
            manifest = None
    else:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = None
    if not isinstance(manifest, dict):
        return {"fresh": False, "reason": "corrupt", "manifest": {}}

    revalidate_after = _parse_utc(manifest.get("revalidate_after"))
    if revalidate_after is not None and current < revalidate_after:
        return {
            "fresh": False,
            "reason": "backoff",
            "manifest": manifest,
            "revalidate_after": _format_utc(revalidate_after),
        }

    fetched_at = _parse_utc(manifest.get("fetched_at"))
    if fetched_at is None:
        return {"fresh": False, "reason": "corrupt", "manifest": manifest}

    age = (current - fetched_at).total_seconds()
    if age < 0:
        return {"fresh": False, "reason": "expired", "manifest": manifest}
    if bool(manifest.get("stale")):
        return {"fresh": False, "reason": "expired", "manifest": manifest}
    if age < _rules_ttl_seconds(ttl_seconds):
        return {"fresh": True, "reason": "fresh", "manifest": manifest}
    return {"fresh": False, "reason": "expired", "manifest": manifest}


@contextmanager
def swr_nonblocking_mode():
    """Make :func:`maybe_sync_rules` schedule SWR instead of doing network I/O.

    The context-local switch lets CLI fetch/batch retain the historical
    ``maybe_sync_rules`` call signature, which keeps monkeypatch-based API
    tests and external wrappers compatible without placing upstream I/O on the
    command's critical path.
    """

    token = _SWR_NONBLOCKING.set(True)
    try:
        yield
    finally:
        _SWR_NONBLOCKING.reset(token)


def maybe_sync_rules(
    sites_js: Path | None = None,
    disabled: bool = False,
    now: datetime | None = None,
    ttl_seconds: int | None = None,
) -> dict:
    """Backward-compatible synchronous freshness check.

    Existing API callers retain the historical behavior.  CLI fetch/batch enter
    :func:`swr_nonblocking_mode`, causing this same public function to schedule
    revalidation instead of performing upstream I/O.
    """

    if _SWR_NONBLOCKING.get():
        return schedule_rules_revalidation(
            sites_js=sites_js,
            disabled=disabled,
            now=now,
            ttl_seconds=ttl_seconds,
        )

    empty = {"attempted": False, "reason": "", "warnings": []}
    skip_reason = _sync_skip_reason(sites_js, disabled)
    if skip_reason:
        return {**empty, "reason": skip_reason}

    legacy_ttl = 24 * 60 * 60 if ttl_seconds is None else ttl_seconds
    state = _freshness_state(now=now, ttl_seconds=legacy_ttl)
    if state["fresh"]:
        return {**empty, "reason": "fresh"}
    if state["reason"] == "backoff":
        return {**empty, "reason": "backoff"}

    try:
        result = sync_rules()
        warnings = list(result.get("warnings") or [])
        if not result.get("ok", False):
            warnings.append(f"rule_sync_failed:{result.get('error') or 'unknown'}")
        return {
            "attempted": True,
            "reason": state["reason"],
            "warnings": list(dict.fromkeys(warnings)),
        }
    except Exception as exc:
        return {
            "attempted": True,
            "reason": state["reason"],
            "warnings": [f"rule_sync_error:{exc}"],
        }


def _swr_lock_path() -> Path:
    return rules_root() / _SWR_LOCK_NAME


def _sync_lock_path() -> Path:
    return rules_root() / _SYNC_LOCK_NAME


def _read_lock_payload(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_is_stale(path: Path, *, stale_seconds: int | None = None) -> bool:
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return True
    threshold = _swr_lock_stale_seconds() if stale_seconds is None else max(30, stale_seconds)
    if age < threshold:
        return False
    # PID reuse or a permanently hung owner must not wedge rule updates forever.
    # Normal sync operations are bounded by network timeouts, so two hours is a
    # conservative hard ceiling before the lock is eligible for recovery.
    if age >= max(DEFAULT_LOCK_HARD_STALE_SECONDS, threshold * 4):
        return True
    payload = _read_lock_payload(path)
    try:
        owner_pid = int(payload.get("owner_pid") or 0)
    except (TypeError, ValueError):
        owner_pid = 0
    return not _pid_is_alive(owner_pid)


def _acquire_owned_lock(path: Path, *, stale_seconds: int | None = None) -> str | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    for _ in range(2):
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            if _lock_is_stale(path, stale_seconds=stale_seconds):
                try:
                    path.unlink()
                except OSError:
                    return None
                continue
            return None
        payload = json.dumps(
            {
                "owner_pid": os.getpid(),
                "created_at": _now(),
                "token": token,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return token
    return None


def _release_owned_lock(path: Path, token: str) -> None:
    if not token:
        return
    try:
        payload = _read_lock_payload(path)
        if payload.get("token") != token:
            return
        path.unlink(missing_ok=True)
    except OSError:
        return


def _acquire_swr_lock(path: Path) -> str | None:
    return _acquire_owned_lock(path)


def _release_swr_lock(path: Path, token: str) -> None:
    _release_owned_lock(path, token)


def _detached_process_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        detached = int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
        new_group = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
        kwargs["creationflags"] = detached | new_group
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _reap_detached_process(process: subprocess.Popen[Any]) -> None:
    try:
        process.wait()
    except Exception:
        return


def _spawn_detached_process(command: list[str], environment: dict[str, str]) -> None:
    process = subprocess.Popen(
        command,
        env=environment,
        **_detached_process_kwargs(),
    )
    reaper = threading.Thread(
        target=_reap_detached_process,
        args=(process,),
        name="pac-rules-child-reaper",
        daemon=True,
    )
    reaper.start()


def schedule_rules_revalidation(
    sites_js: Path | None = None,
    disabled: bool = False,
    now: datetime | None = None,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Schedule a detached SWR refresh and return immediately.

    No upstream request is performed by this function.  It only reads the tiny
    local manifest, acquires a process-wide lock file, and spawns a detached
    child when the cached rules are stale.
    """

    empty: dict[str, Any] = {
        "attempted": False,
        "scheduled": False,
        "reason": "",
        "warnings": [],
    }
    skip_reason = _sync_skip_reason(sites_js, disabled)
    if skip_reason:
        return {**empty, "reason": skip_reason}

    state = _freshness_state(now=now, ttl_seconds=ttl_seconds)
    if state["fresh"]:
        return {**empty, "reason": "fresh"}
    if state["reason"] == "backoff":
        return {**empty, "reason": "backoff"}

    lock_path = _swr_lock_path()
    lock_token = _acquire_swr_lock(lock_path)
    if not lock_token:
        return {**empty, "reason": "already_revalidating"}

    environment = os.environ.copy()
    environment[_SWR_CHILD_ENV] = "1"
    environment[_SWR_LOCK_ENV] = str(lock_path)
    environment[_SWR_LOCK_TOKEN_ENV] = lock_token
    code = (
        "from bpc_fetch.rules.sync import _swr_child_main; "
        "raise SystemExit(_swr_child_main())"
    )
    if bool(getattr(sys, "frozen", False)):
        command = [sys.executable]
    else:
        command = [sys.executable, "-c", code]
    try:
        _spawn_detached_process(command, environment)
    except Exception as exc:
        _release_swr_lock(lock_path, lock_token)
        return {
            "attempted": True,
            "scheduled": False,
            "reason": state["reason"],
            "warnings": [f"rule_sync_schedule_error:{exc}"],
        }

    return {
        "attempted": True,
        "scheduled": True,
        "reason": state["reason"],
        "warnings": [],
    }


def _swr_child_main() -> int:
    """Detached child entry point used by :func:`schedule_rules_revalidation`."""

    lock_raw = os.environ.get(_SWR_LOCK_ENV, "").strip()
    lock_path = Path(lock_raw) if lock_raw else _swr_lock_path()
    lock_token = os.environ.get(_SWR_LOCK_TOKEN_ENV, "").strip()
    try:
        result = sync_rules()
        return 0 if result.get("ok", False) else 1
    except BaseException:
        return 1
    finally:
        _release_swr_lock(lock_path, lock_token)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace path atomically using a temporary file beside the target."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _validated_sites_js(data: bytes) -> None:
    import re

    text = data.decode("utf-8")
    candidate = re.sub(r"^var defaultSites\s*=\s*", "", text.strip())
    candidate = re.sub(r";\s*$", "", candidate)
    candidate = re.sub(
        r"^var grouped_sites\s*=\s*\{.*?\};\s*",
        "",
        candidate,
        flags=re.DOTALL,
    )
    entries = _extract_entries(candidate)
    if not entries or not entries_to_domain_map(entries):
        raise ValueError("invalid sites.js: no usable domains")


def _load_base_entries(base_js: Path) -> dict[str, dict]:
    import re

    text = base_js.read_text(encoding="utf-8").strip()
    text = re.sub(r"^var defaultSites\s*=\s*", "", text)
    text = re.sub(r";\s*$", "", text)
    text = re.sub(
        r"^var grouped_sites\s*=\s*\{.*?\};\s*",
        "",
        text,
        flags=re.DOTALL,
    )
    return _extract_entries(text)


def merge_updated_into_entries(
    base_entries: dict[str, dict],
    updated: dict,
) -> dict[str, dict]:
    """Replace complete upstream entries by site name, preserving Phase 1 semantics."""

    output = dict(base_entries)
    if not isinstance(updated, dict):
        return output
    for name, properties in updated.items():
        if isinstance(properties, dict):
            output[name] = properties
    return output


def merge_to_domain_map(base_js: Path, updated: dict | None) -> dict:
    entries = _load_base_entries(base_js)
    if updated:
        entries = merge_updated_into_entries(entries, updated)
    return entries_to_domain_map(entries)


def _install_base_from_zip(zip_path: Path) -> Path | None:
    """Extract a bounded, validated sites.js member from a BPC release ZIP."""

    rules_root().mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as archive:
        members = archive.infolist()
        if len(members) > MAX_ZIP_MEMBERS:
            raise ValueError("ZIP contains too many members")
        candidates = [info for info in members if Path(info.filename).name == "sites.js"]
        if not candidates:
            return None
        if len(candidates) != 1:
            raise ValueError("ZIP must contain exactly one sites.js")
        info = candidates[0]
        if info.flag_bits & 0x1:
            raise ValueError("encrypted sites.js is not supported")
        if info.file_size > MAX_SITES_JS_ZIP_BYTES:
            raise ValueError("sites.js exceeds the permitted size limit")
        if info.file_size and (
            info.compress_size == 0
            or info.file_size > info.compress_size * MAX_ZIP_COMPRESSION_RATIO
        ):
            raise ValueError("sites.js has a suspicious compression ratio")
        target = sites_js_path()
        data = archive.read(info)
        _validated_sites_js(data)
        _atomic_write_bytes(target, data)
        return target


def sync_rules(
    *,
    from_zip: Path | None = None,
    updated_url: str | None = None,
    offline: bool = False,
) -> dict:
    """Run one serialized rules sync and atomically commit the runtime snapshot."""

    if os.environ.get("PAC_RULES_PIN", "").strip():
        return {
            "ok": True,
            "skipped": True,
            "reason": "pinned",
            "warnings": [],
            "sources": [],
        }

    lock_path = _sync_lock_path()
    lock_token = _acquire_owned_lock(lock_path, stale_seconds=30 * 60)
    if not lock_token:
        return {
            "ok": False,
            "error_code": "INTERNAL",
            "error": "rules sync already running",
            "warnings": ["rules_sync_busy"],
            "sources": [],
        }
    try:
        return _sync_rules_impl(
            from_zip=from_zip,
            updated_url=updated_url,
            offline=offline,
        )
    finally:
        _release_owned_lock(lock_path, lock_token)


def _sync_rules_impl(
    *,
    from_zip: Path | None = None,
    updated_url: str | None = None,
    offline: bool = False,
) -> dict:
    """Run an explicit rules sync and atomically publish a usable local cache."""

    previous_manifest = _load_manifest_safe()
    previous_rule_version = str(previous_manifest.get("rule_version") or "")
    previous_content_hash = str(previous_manifest.get("content_hash") or "")
    warnings: list[str] = []
    sources: list[str] = []
    rules_root().mkdir(parents=True, exist_ok=True)

    remote_base_requested = bool(from_zip is None and not offline and SITES_JS_URL)
    remote_base_ok = False
    remote_updated_ok = False

    base = sites_js_path()
    if from_zip is not None:
        try:
            installed = _install_base_from_zip(from_zip)
        except Exception as exc:
            return {
                "ok": False,
                "error_code": "INTERNAL",
                "error": f"invalid sites.js zip {from_zip}: {exc}",
            }
        if installed:
            base = installed
            sources.append(f"zip:{from_zip}")
        else:
            return {
                "ok": False,
                "error_code": "INTERNAL",
                "error": f"sites.js not found in zip {from_zip}",
            }
    elif remote_base_requested:
        try:
            data = download_bytes(SITES_JS_URL)
            _validated_sites_js(data)
            _atomic_write_bytes(base, data)
            sources.append(f"remote_js:{SITES_JS_URL}")
            remote_base_ok = True
        except Exception as exc:
            warnings.append(f"remote_sites_js_error:{exc}")

    if not base.exists():
        if SITES_JS_DEFAULT.exists():
            data = SITES_JS_DEFAULT.read_bytes()
            _validated_sites_js(data)
            _atomic_write_bytes(base, data)
            sources.append(f"bundled:{SITES_JS_DEFAULT}")
            warnings.append("using_bundled_base")
        else:
            return {
                "ok": False,
                "error_code": "INTERNAL",
                "error": "no base sites.js available",
                "recovery_hint": "pac rules sync --from-zip <bpc.zip>",
            }
    elif not any(source.startswith(("zip:", "remote_js:")) for source in sources):
        if not sources:
            sources.append(f"local:{base}")
        warnings.append("using_bundled_base")

    updated: dict | None = None
    url = updated_url or DEFAULT_UPDATED_URL
    remote_updated_requested = bool(not offline and url)
    if remote_updated_requested:
        try:
            data = download_bytes(url)
            candidate = json.loads(data.decode("utf-8"))
            if isinstance(candidate, dict):
                updated = candidate
                _atomic_write_text(
                    sites_updated_path(),
                    json.dumps(updated, ensure_ascii=False, indent=2),
                )
                sources.append(f"updated:{url}")
                remote_updated_ok = True
            else:
                warnings.append("updated_invalid_shape")
        except Exception as exc:
            warnings.append(f"updated_error:{exc}")

    if updated is None and sites_updated_path().exists():
        try:
            candidate = json.loads(sites_updated_path().read_text(encoding="utf-8"))
            if isinstance(candidate, dict):
                updated = candidate
                sources.append(f"updated_cache:{sites_updated_path()}")
            else:
                warnings.append("updated_cache_corrupt")
        except Exception:
            warnings.append("updated_cache_corrupt")

    domain_map = merge_to_domain_map(base, updated)
    cache_data = {domain: asdict(strategy) for domain, strategy in domain_map.items()}
    raw = json.dumps(cache_data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    content_hash = _sha256_bytes(raw)
    content_hash_label = f"sha256:{content_hash}"
    _atomic_write_text(
        cache_map_path(),
        json.dumps(cache_data, ensure_ascii=False, indent=2),
    )

    normal_online_refresh = from_zip is None and not offline
    upstream_refresh_ok = (
        (not remote_base_requested or remote_base_ok)
        and (not remote_updated_requested or remote_updated_ok)
    )
    freshness_ok = not normal_online_refresh or upstream_refresh_ok
    using_bundled = "using_bundled_base" in warnings
    stale = using_bundled or (normal_online_refresh and not upstream_refresh_ok)

    now_dt = _now_datetime()
    now_text = _format_utc(now_dt)
    previous_fetched = str(previous_manifest.get("fetched_at") or "")
    if freshness_ok:
        fetched_at = now_text
        revalidate_after = ""
    else:
        fetched_at = previous_fetched or now_text
        revalidate_after = _format_utc(
            now_dt + timedelta(seconds=_retry_backoff_seconds())
        )

    if (
        not freshness_ok
        and previous_rule_version
        and previous_content_hash == content_hash_label
    ):
        rule_version = previous_rule_version
    else:
        rule_version = f"{now_text}#sha256:{content_hash[:12]}"

    manifest: dict[str, Any] = {
        "rule_version": rule_version,
        "fetched_at": fetched_at,
        "last_attempt_at": now_text,
        "sources": sources,
        "site_count": len(domain_map),
        "content_hash": content_hash_label,
        "stale": stale,
        "using_bundled_base": using_bundled,
        "warnings": warnings,
    }
    if revalidate_after:
        manifest["revalidate_after"] = revalidate_after
    _atomic_write_text(
        manifest_path(),
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )
    # Runtime readers prefer this single-file commit record.  Legacy files are
    # still maintained for compatibility, but a crash before this write leaves
    # the previous coherent snapshot visible to fetch/batch callers.
    snapshot = {
        "schema_version": 1,
        "manifest": manifest,
        "cache": cache_data,
    }
    _atomic_write_text(
        snapshot_path(),
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
    )

    return {
        "ok": True,
        "rule_version": rule_version,
        "site_count": len(domain_map),
        "sources": sources,
        "warnings": warnings,
        "stale": stale,
        "manifest_path": str(manifest_path()),
        "cache_path": str(cache_map_path()),
        "snapshot_path": str(snapshot_path()),
    }