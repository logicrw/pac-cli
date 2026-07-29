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
from .paths import cache_map_path, manifest_path, rules_root, sites_js_path


def load_manifest() -> dict:
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
