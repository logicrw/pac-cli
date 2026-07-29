"""Sync BPC rules: bundled/base zip + sites_updated merge (§15.1.1 / §15.2.5)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..sites import (
    SITES_JS_DEFAULT,
    _extract_entries,
    entries_to_domain_map,
)
from .paths import (
    cache_map_path,
    manifest_path,
    rules_root,
    sites_js_path,
    sites_updated_path,
)

DEFAULT_UPDATED_URL = os.environ.get(
    "PAC_SITES_UPDATED_URL",
    "https://gitflic.ru/project/magnolia1234/bpc_updates/blob/raw?file=sites_updated.json",
)
# Full sites.js remote optional — often 404; empty = skip
SITES_JS_URL = os.environ.get("PAC_SITES_JS_URL", "").strip()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_base_entries(base_js: Path) -> dict[str, dict]:
    text = base_js.read_text(encoding="utf-8")
    text = text.strip()
    import re
    text = re.sub(r"^var defaultSites\s*=\s*", "", text)
    text = re.sub(r";\s*$", "", text)
    text = re.sub(r"^var grouped_sites\s*=\s*\{.*?\};\s*", "", text, flags=re.DOTALL)
    return _extract_entries(text)


def merge_updated_into_entries(base_entries: dict[str, dict], updated: dict) -> dict[str, dict]:
    """§15.1.1: whole-entry replace by site name (key of updated).

    Then rebuild domain map via entries_to_domain_map (group + exception).
    """
    out = dict(base_entries)
    if not isinstance(updated, dict):
        return out
    for name, props in updated.items():
        if not isinstance(props, dict):
            continue
        # whole replace by site name
        out[name] = props
    return out


def merge_to_domain_map(base_js: Path, updated: dict | None) -> dict:
    entries = _load_base_entries(base_js)
    if updated:
        entries = merge_updated_into_entries(entries, updated)
    return entries_to_domain_map(entries)


def _install_base_from_zip(zip_path: Path) -> Path | None:
    """Extract sites.js from BPC release zip into rules_root."""
    rules_root().mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        candidates = [n for n in zf.namelist() if n.endswith("sites.js") or n == "sites.js"]
        if not candidates:
            # try nested
            candidates = [n for n in zf.namelist() if n.endswith("/sites.js")]
        if not candidates:
            return None
        name = candidates[0]
        target = sites_js_path()
        with zf.open(name) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        return target


def sync_rules(
    *,
    from_zip: Path | None = None,
    updated_url: str | None = None,
    offline: bool = False,
) -> dict:
    """Run rules sync. Always leaves a usable cache when bundled base exists."""
    warnings: list[str] = []
    sources: list[str] = []
    rules_root().mkdir(parents=True, exist_ok=True)

    # 1) base sites.js
    base = sites_js_path()
    if from_zip is not None:
        installed = _install_base_from_zip(from_zip)
        if installed:
            base = installed
            sources.append(f"zip:{from_zip}")
        else:
            return {
                "ok": False,
                "error_code": "INTERNAL",
                "error": f"sites.js not found in zip {from_zip}",
            }
    elif SITES_JS_URL and not offline:
        try:
            r = httpx.get(SITES_JS_URL, timeout=60.0, follow_redirects=True)
            if r.status_code == 200 and "defaultSites" in r.text:
                base.write_text(r.text, encoding="utf-8")
                sources.append(f"remote_js:{SITES_JS_URL}")
            else:
                warnings.append("remote_sites_js_failed")
        except Exception as e:
            warnings.append(f"remote_sites_js_error:{e}")

    if not base.exists():
        if SITES_JS_DEFAULT.exists():
            shutil.copy2(SITES_JS_DEFAULT, base)
            sources.append(f"bundled:{SITES_JS_DEFAULT}")
            warnings.append("using_bundled_base")
        else:
            return {
                "ok": False,
                "error_code": "INTERNAL",
                "error": "no base sites.js available",
                "recovery_hint": "pac rules sync --from-zip <bpc.zip>",
            }
    else:
        if "zip:" not in "".join(sources) and "remote_js:" not in "".join(sources):
            if not sources:
                sources.append(f"local:{base}")
            if base.resolve() != SITES_JS_DEFAULT.resolve() and "using_bundled_base" not in warnings:
                # still note if content matches bundled path origin
                pass
            if not any(s.startswith("zip:") or s.startswith("remote_js:") for s in sources):
                warnings.append("using_bundled_base")

    # 2) sites_updated.json
    updated: dict | None = None
    url = updated_url or DEFAULT_UPDATED_URL
    if not offline and url:
        try:
            r = httpx.get(url, timeout=60.0, follow_redirects=True)
            if r.status_code == 200:
                updated = r.json()
                sites_updated_path().write_text(
                    json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                sources.append(f"updated:{url}")
            else:
                warnings.append(f"updated_http_{r.status_code}")
        except Exception as e:
            warnings.append(f"updated_error:{e}")
    if updated is None and sites_updated_path().exists():
        try:
            updated = json.loads(sites_updated_path().read_text(encoding="utf-8"))
            sources.append(f"updated_cache:{sites_updated_path()}")
        except Exception:
            warnings.append("updated_cache_corrupt")

    # 3) merge → domain map
    domain_map = merge_to_domain_map(base, updated)
    cache_data = {k: asdict(v) for k, v in domain_map.items()}
    raw = json.dumps(cache_data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    content_hash = _sha256_bytes(raw)
    cache_map_path().write_text(
        json.dumps(cache_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    rule_version = f"{_now()}#sha256:{content_hash[:12]}"
    stale = "using_bundled_base" in warnings or not any(
        s.startswith("zip:") or s.startswith("remote_js:") for s in sources
    )
    manifest = {
        "rule_version": rule_version,
        "fetched_at": _now(),
        "sources": sources,
        "site_count": len(domain_map),
        "content_hash": f"sha256:{content_hash}",
        "stale": stale,
        "using_bundled_base": "using_bundled_base" in warnings,
        "warnings": warnings,
    }
    manifest_path().write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "rule_version": rule_version,
        "site_count": len(domain_map),
        "sources": sources,
        "warnings": warnings,
        "stale": stale,
        "manifest_path": str(manifest_path()),
        "cache_path": str(cache_map_path()),
    }
