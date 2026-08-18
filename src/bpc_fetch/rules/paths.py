"""Rule cache paths."""
from __future__ import annotations

import os
from pathlib import Path

try:
    from platformdirs import user_cache_dir
except ImportError:  # pragma: no cover
    def user_cache_dir(appname: str, appauthor: str | None = None) -> str:
        return str(Path.home() / ".cache" / appname)


def rules_root() -> Path:
    override = os.environ.get("PAC_RULES_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(user_cache_dir("pac-cli", "pac-cli")) / "rules"


def sites_js_path() -> Path:
    return rules_root() / "sites.js"


def sites_updated_path() -> Path:
    return rules_root() / "sites_updated.json"


def cache_map_path() -> Path:
    return rules_root() / "sites_cache.json"


def manifest_path() -> Path:
    return rules_root() / "rules_manifest.json"


def snapshot_path() -> Path:
    """Atomic runtime snapshot containing both cache data and manifest metadata."""
    return rules_root() / "rules_snapshot.json"
