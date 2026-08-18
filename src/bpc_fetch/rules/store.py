"""Load domain→SiteStrategy map + rule_version."""
from __future__ import annotations

import json
from pathlib import Path

from ..sites import (
    SITES_JS_DEFAULT,
    SiteStrategy,
    parse_sites_js,
    strategy_from_dict,
)
from .paths import cache_map_path, manifest_path, snapshot_path, sites_js_path


def _load_snapshot() -> tuple[dict, dict] | None:
    path = snapshot_path()
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    cache = value.get("cache")
    manifest = value.get("manifest")
    if not isinstance(cache, dict) or not isinstance(manifest, dict):
        return None
    return cache, manifest


def load_manifest() -> dict:
    snapshot = _load_snapshot()
    if snapshot is not None:
        return dict(snapshot[1])
    p = manifest_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_sites_map_with_version(
    sites_js: Path | None = None,
) -> tuple[dict[str, SiteStrategy], str, list[str]]:
    """Return (map, rule_version, warnings).

    Prefer PAC rules cache; fall back to bundled data/sites.js.
    """
    warnings: list[str] = []
    pin = __import__("os").environ.get("PAC_RULES_PIN", "").strip()
    if pin:
        p = Path(pin).expanduser()
        if p.exists():
            if p.suffix == ".js":
                return parse_sites_js(p), f"pin:{p}", warnings
            if p.suffix == ".json":
                data = json.loads(p.read_text(encoding="utf-8"))
                m = {k: strategy_from_dict(v) for k, v in data.items()}
                return m, f"pin:{p}", warnings

    # Explicit --sites-js
    if sites_js is not None:
        return parse_sites_js(sites_js), f"file:{sites_js}", warnings

    snapshot = _load_snapshot()
    if snapshot is not None:
        data, man = snapshot
        try:
            m = {k: strategy_from_dict(v) for k, v in data.items()}
            ver = man.get("rule_version") or f"snapshot:{snapshot_path()}"
            if man.get("stale") or man.get("using_bundled_base"):
                warnings.append("using_bundled_base")
            if man.get("stale"):
                warnings.append("rules_stale")
            return m, ver, warnings
        except Exception:
            warnings.append("snapshot_corrupt")

    cache = cache_map_path()
    man = load_manifest()
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            m = {k: strategy_from_dict(v) for k, v in data.items()}
            ver = man.get("rule_version") or f"cache:{cache}"
            if man.get("stale") or man.get("using_bundled_base"):
                warnings.append("using_bundled_base")
            if man.get("stale"):
                warnings.append("rules_stale")
            return m, ver, warnings
        except Exception:
            warnings.append("cache_corrupt")

    # bundled
    base = sites_js_path() if sites_js_path().exists() else SITES_JS_DEFAULT
    if not base.exists():
        return {}, "none", ["no_sites_js"]
    warnings.append("using_bundled_base")
    return parse_sites_js(base), f"bundled:{base.name}", warnings
