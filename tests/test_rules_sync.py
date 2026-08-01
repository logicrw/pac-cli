import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
import httpx

from bpc_fetch.rules import sync


VALID_BASE = 'var defaultSites = {"Base": {"domain": "base.example"}};\n'


def test_secure_download_rejects_private_initial_url_without_transport():
    requests = []
    client = httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request)))

    with pytest.raises(Exception, match="private_ip"):
        sync.download_bytes("http://127.0.0.1/rules", client=client)

    assert requests == []


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_secure_download_rejects_redirect_to_private_before_transport(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "test")
    requests = []
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/secret"})

    with pytest.raises(Exception, match="private_ip"):
        sync.download_bytes("https://example.com/start", client=_client(handler))
    assert requests == ["https://example.com/start"]


def test_secure_download_follows_relative_public_redirect(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "test")
    requests = []
    def handler(request):
        requests.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/rules"})
        return httpx.Response(200, content=b"rules")

    assert sync.download_bytes("https://example.com/start", client=_client(handler)) == b"rules"
    assert requests == ["https://example.com/start", "https://example.com/rules"]


def test_secure_download_rejects_more_than_five_redirects(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "test")
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": f"/{calls}"})

    with pytest.raises(Exception, match="redirect"):
        sync.download_bytes("https://example.com/start", client=_client(handler))
    assert calls == 6


def test_secure_download_rejects_oversize_response(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "test")
    with pytest.raises(Exception, match="large"):
        sync.download_bytes(
            "https://example.com/rules",
            client=_client(lambda request: httpx.Response(200, content=b"12345")),
            max_bytes=4,
        )


def test_secure_download_rejects_oversize_content_length_before_body(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "test")
    with pytest.raises(Exception, match="large"):
        sync.download_bytes(
            "https://example.com/rules",
            client=_client(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Length": "100"},
                    content=b"",
                )
            ),
            max_bytes=4,
        )


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


def test_oversized_zip_sites_js_is_rejected_before_read(tmp_path, monkeypatch):
    monkeypatch.setenv("PAC_RULES_DIR", str(tmp_path / "rules"))
    archive = tmp_path / "oversized.zip"
    payload = VALID_BASE.encode()
    payload += b" " * (sync.MAX_SITES_JS_ZIP_BYTES - len(payload) + 1)
    _write_zip(archive, payload)

    result = sync.sync_rules(from_zip=archive, offline=True)

    assert result["ok"] is False
    assert "size" in result["error"].lower()


def test_invalid_updated_shape_preserves_and_uses_existing_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("PAC_RULES_DIR", str(tmp_path / "rules"))
    rules = sync.rules_root()
    rules.mkdir(parents=True)
    sync.sites_js_path().write_text(VALID_BASE, encoding="utf-8")
    cached = {"Cached": {"domain": "cached.example"}}
    cached_raw = json.dumps(cached, indent=2)
    sync.sites_updated_path().write_text(cached_raw, encoding="utf-8")

    monkeypatch.setattr(
        sync,
        "download_bytes",
        lambda *args, **kwargs: b'["not", "a", "mapping"]',
    )

    result = sync.sync_rules(updated_url="https://example.test/updated", offline=False)

    assert result["ok"] is True
    assert "updated_invalid_shape" in result["warnings"]
    assert sync.sites_updated_path().read_text(encoding="utf-8") == cached_raw
    domain_cache = json.loads(sync.cache_map_path().read_text(encoding="utf-8"))
    assert "cached.example" in domain_cache
    assert any(source.startswith("updated_cache:") for source in result["sources"])


def test_malformed_updated_json_preserves_existing_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("PAC_RULES_DIR", str(tmp_path / "rules"))
    rules = sync.rules_root()
    rules.mkdir(parents=True)
    sync.sites_js_path().write_text(VALID_BASE, encoding="utf-8")
    cached_raw = '{"Cached": {"domain": "cached.example"}}'
    sync.sites_updated_path().write_text(cached_raw, encoding="utf-8")

    monkeypatch.setattr(sync, "download_bytes", lambda *args, **kwargs: b"not-json")
    result = sync.sync_rules(updated_url="https://example.com/updated")

    assert result["ok"] is True
    assert any(w.startswith("updated_error:") for w in result["warnings"])
    assert sync.sites_updated_path().read_text(encoding="utf-8") == cached_raw


def test_default_full_rules_url_is_stable_pac_mirror():
    assert sync.SITES_JS_URL == "https://raw.githubusercontent.com/logicrw/pac-cli/main/data/sites.js"


def test_default_updated_rules_url_is_stable_pac_mirror():
    assert sync.DEFAULT_UPDATED_URL == (
        "https://raw.githubusercontent.com/logicrw/pac-cli/main/data/sites_updated.json"
    )


def test_maybe_sync_fresh_manifest_skips_network(tmp_path, monkeypatch):
    monkeypatch.setenv("PAC_RULES_DIR", str(tmp_path / "rules"))
    sync.manifest_path().parent.mkdir(parents=True)
    sync.manifest_path().write_text('{"fetched_at":"2026-08-01T11:00:00Z"}')
    monkeypatch.setattr(sync, "sync_rules", lambda: pytest.fail("must not sync"))

    result = sync.maybe_sync_rules(now=datetime(2026, 8, 1, 12, tzinfo=timezone.utc))

    assert result == {"attempted": False, "reason": "fresh", "warnings": []}


def test_maybe_sync_future_manifest_is_not_treated_as_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("PAC_RULES_DIR", str(tmp_path / "rules"))
    sync.manifest_path().parent.mkdir(parents=True)
    sync.manifest_path().write_text('{"fetched_at":"2026-08-02T12:00:00Z"}')
    calls = []
    monkeypatch.setattr(
        sync,
        "sync_rules",
        lambda: calls.append(True) or {"ok": True, "warnings": []},
    )

    result = sync.maybe_sync_rules(
        now=datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    )

    assert result["attempted"] is True
    assert result["reason"] == "expired"
    assert calls == [True]


@pytest.mark.parametrize("manifest", [None, '{"fetched_at":"2026-07-30T00:00:00Z"}', "bad"])
def test_maybe_sync_missing_expired_or_corrupt_manifest_runs_once(tmp_path, monkeypatch, manifest):
    monkeypatch.setenv("PAC_RULES_DIR", str(tmp_path / "rules"))
    if manifest is not None:
        sync.manifest_path().parent.mkdir(parents=True)
        sync.manifest_path().write_text(manifest)
    calls = []
    monkeypatch.setattr(sync, "sync_rules", lambda: calls.append(True) or {"ok": True, "warnings": []})

    result = sync.maybe_sync_rules(now=datetime(2026, 8, 1, 12, tzinfo=timezone.utc))

    assert result["attempted"] is True
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("env", "sites_js", "disabled", "reason"),
    [
        ({"PAC_RULES_AUTO_SYNC": "off"}, None, False, "disabled_env"),
        ({"PAC_RULES_PIN": "/tmp/pin.json"}, None, False, "pinned"),
        ({}, Path("rules.js"), False, "explicit_sites_js"),
        ({}, None, True, "disabled_cli"),
    ],
)
def test_maybe_sync_disable_controls_skip(monkeypatch, env, sites_js, disabled, reason):
    monkeypatch.delenv("PAC_RULES_AUTO_SYNC", raising=False)
    monkeypatch.delenv("PAC_RULES_PIN", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sync, "sync_rules", lambda: pytest.fail("must not sync"))

    result = sync.maybe_sync_rules(sites_js=sites_js, disabled=disabled)

    assert result == {"attempted": False, "reason": reason, "warnings": []}


def test_explicit_sync_is_fully_disabled_when_rules_are_pinned(tmp_path, monkeypatch):
    rules_dir = tmp_path / "rules"
    monkeypatch.setenv("PAC_RULES_DIR", str(rules_dir))
    monkeypatch.setenv("PAC_RULES_PIN", str(tmp_path / "pinned-sites.js"))
    monkeypatch.setattr(sync, "download_bytes", lambda *args, **kwargs: pytest.fail("must not download"))

    result = sync.sync_rules()

    assert result == {
        "ok": True,
        "skipped": True,
        "reason": "pinned",
        "warnings": [],
        "sources": [],
    }
    assert not rules_dir.exists()


def test_explicit_offline_sync_uses_bundled_rules_without_network(tmp_path, monkeypatch):
    monkeypatch.setenv("PAC_RULES_DIR", str(tmp_path / "rules"))
    monkeypatch.delenv("PAC_RULES_PIN", raising=False)
    monkeypatch.setattr(sync, "download_bytes", lambda *args, **kwargs: pytest.fail("must not download"))

    result = sync.sync_rules(offline=True)

    assert result["ok"] is True
    assert result["site_count"] > 0
    assert any(source.startswith("bundled:") for source in result["sources"])


def test_maybe_sync_failure_is_fail_open(tmp_path, monkeypatch):
    monkeypatch.setenv("PAC_RULES_DIR", str(tmp_path / "rules"))
    monkeypatch.setattr(sync, "sync_rules", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    result = sync.maybe_sync_rules()

    assert result["attempted"] is True
    assert result["reason"] == "missing"
    assert result["warnings"] == ["rule_sync_error:boom"]


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
