import json
import os
import zipfile
from pathlib import Path

import pytest

from bpc_fetch.rules import sync


VALID_BASE = 'var defaultSites = {"Base": {"domain": "base.example"}};\n'


def _write_zip(path: Path, sites_js: bytes) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("nested/sites.js", sites_js)


def test_atomic_write_replace_failure_preserves_target_and_cleans_temp(tmp_path, monkeypatch):
    target = tmp_path / "sites.js"
    target.write_bytes(b"old")

    def fail_replace(source, destination):
        assert Path(destination) == target
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        sync._atomic_write_bytes(target, b"new")

    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


def test_malformed_zip_sites_js_preserves_existing_base(tmp_path, monkeypatch):
    monkeypatch.setenv("PAC_RULES_DIR", str(tmp_path / "rules"))
    base = sync.sites_js_path()
    base.parent.mkdir(parents=True)
    original = VALID_BASE.encode()
    base.write_bytes(original)
    archive = tmp_path / "bad.zip"
    _write_zip(archive, b"this is not a sites object")

    result = sync.sync_rules(from_zip=archive, offline=True)

    assert result["ok"] is False
    assert "invalid" in result["error"].lower()
    assert base.read_bytes() == original


def test_invalid_updated_shape_preserves_and_uses_existing_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("PAC_RULES_DIR", str(tmp_path / "rules"))
    rules = sync.rules_root()
    rules.mkdir(parents=True)
    sync.sites_js_path().write_text(VALID_BASE, encoding="utf-8")
    cached = {"Cached": {"domain": "cached.example"}}
    cached_raw = json.dumps(cached, indent=2)
    sync.sites_updated_path().write_text(cached_raw, encoding="utf-8")

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return ["not", "a", "mapping"]

    monkeypatch.setattr(sync.httpx, "get", lambda *args, **kwargs: Response())

    result = sync.sync_rules(updated_url="https://example.test/updated", offline=False)

    assert result["ok"] is True
    assert "updated_invalid_shape" in result["warnings"]
    assert sync.sites_updated_path().read_text(encoding="utf-8") == cached_raw
    domain_cache = json.loads(sync.cache_map_path().read_text(encoding="utf-8"))
    assert "cached.example" in domain_cache
    assert any(source.startswith("updated_cache:") for source in result["sources"])


def test_successful_sync_emits_valid_cache_and_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("PAC_RULES_DIR", str(tmp_path / "rules"))
    archive = tmp_path / "good.zip"
    _write_zip(archive, VALID_BASE.encode())

    result = sync.sync_rules(from_zip=archive, offline=True)

    cache = json.loads(sync.cache_map_path().read_text(encoding="utf-8"))
    manifest = json.loads(sync.manifest_path().read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert cache["base.example"]["domain"] == "base.example"
    assert manifest["site_count"] == 1
    assert manifest["content_hash"].startswith("sha256:")
    assert manifest["sources"] == [f"zip:{archive}"]
