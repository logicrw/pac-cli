"""Strict contract tests for the central upstream rule mirror updater."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest


def _upstream():
    from bpc_fetch.rules import upstream

    return upstream


def _sites(domains: list[str], *, extra_line: str = "") -> bytes:
    rules = "\n".join(
        f'  "Site {index}": {{ domain: "{domain}" }},'
        for index, domain in enumerate(domains)
    )
    return (
        "var defaultSites = {\n"
        + rules
        + (f"\n{extra_line}" if extra_line else "")
        + "\n};\n"
    ).encode()


def _archive(*, sites: list[tuple[str, bytes]], version: str | None = "3.2.1") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, body in sites:
            archive.writestr(name, body)
        if version is not None:
            archive.writestr(
                "bypass-paywalls-clean-master/manifest.json",
                json.dumps({"version": version}),
            )
    return output.getvalue()


def _updates(domains: list[str]) -> bytes:
    return json.dumps(
        {domain: {"domain": domain, "allow_cookies": 1} for domain in domains},
        sort_keys=True,
    ).encode()


def _seed_output(root: Path, *, domains: list[str]) -> dict[str, bytes]:
    data = root / "data"
    data.mkdir()
    files = {
        "sites.js": _sites(domains),
        "sites_updated.json": _updates(domains),
        "upstream-manifest.json": b'{"old":true}\n',
    }
    for name, body in files.items():
        (data / name).write_bytes(body)
    return files


def test_official_archive_url_is_fixed_https_master_zip():
    upstream = _upstream()

    url = upstream.OFFICIAL_MASTER_ARCHIVE_URL
    assert url.startswith("https://gitflic.ru/project/magnolia1234/")
    assert "master" in url
    assert url.endswith(".zip")


def test_base_archive_requires_exactly_one_sites_js_and_reads_manifest_version():
    upstream = _upstream()
    body = _sites(["one.example"])

    parsed = upstream.parse_base_archive(
        _archive(sites=[("bypass-paywalls-clean-master/sites.js", body)])
    )

    assert parsed.sites_js == body
    assert parsed.extension_version == "3.2.1"
    assert parsed.domain_count == 1


@pytest.mark.parametrize("sites", [[], [("a/sites.js", b"x"), ("b/sites.js", b"y")]])
def test_base_archive_rejects_missing_or_duplicate_sites_js(sites):
    upstream = _upstream()

    with pytest.raises(upstream.UpstreamRuleError, match="exactly one.*sites.js"):
        upstream.parse_base_archive(_archive(sites=sites))


def test_base_archive_reads_in_memory_without_extracting_traversal_members(tmp_path, monkeypatch):
    upstream = _upstream()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("root/sites.js", _sites(["safe.example"]))
        zipped.writestr("../../escaped.txt", "must not be written")

    monkeypatch.chdir(tmp_path)
    parsed = upstream.parse_base_archive(archive.getvalue())

    assert parsed.domain_count == 1
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_base_archive_rejects_oversized_sites_member_before_parsing():
    upstream = _upstream()
    sites = _sites(["safe.example"])
    oversized = sites + b" " * (upstream.MAX_SITES_JS_BYTES - len(sites) + 1)

    with pytest.raises(upstream.UpstreamRuleError, match="size"):
        upstream.parse_base_archive(
            _archive(sites=[("root/sites.js", oversized)])
        )


def test_base_archive_rejects_oversized_manifest_member_before_decoding():
    upstream = _upstream()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("root/sites.js", _sites(["safe.example"]))
        archive.writestr(
            "root/manifest.json",
            b" " * (upstream.MAX_MANIFEST_JSON_BYTES + 1),
        )

    with pytest.raises(upstream.UpstreamRuleError, match="manifest.*size"):
        upstream.parse_base_archive(output.getvalue())


def test_base_archive_rejects_excessive_member_count():
    upstream = _upstream()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("root/sites.js", _sites(["safe.example"]))
        for index in range(upstream.MAX_ARCHIVE_MEMBERS):
            archive.writestr(f"root/empty-{index}.txt", b"")

    with pytest.raises(upstream.UpstreamRuleError, match="too many"):
        upstream.parse_base_archive(output.getvalue())


def test_updated_rules_require_utf8_json_top_level_mapping():
    upstream = _upstream()

    for invalid in (b"\xff", b"[]", b'"scalar"'):
        with pytest.raises(upstream.UpstreamRuleError):
            upstream.parse_updated_rules(invalid)

    assert upstream.parse_updated_rules(b'{"a.example":{"domain":"a.example"}}') == {
        "a.example": {"domain": "a.example"}
    }


def test_updated_rule_entries_must_be_mappings():
    upstream = _upstream()
    payload = json.dumps({"Broken": ["not", "a", "mapping"]}).encode()

    with pytest.raises(upstream.UpstreamRuleError, match="mapping"):
        upstream.parse_updated_rules(payload)


def test_updated_rules_reject_credential_markers():
    upstream = _upstream()
    payload = json.dumps(
        {
            "Unsafe": {
                "domain": "unsafe.example",
                "Authorization": "Bearer FAKE_TEST_VALUE",
            }
        }
    ).encode()

    with pytest.raises(upstream.UpstreamRuleError, match="credential"):
        upstream.parse_updated_rules(payload)


def test_candidate_sites_must_parse_to_at_least_one_domain():
    upstream = _upstream()

    with pytest.raises(upstream.UpstreamRuleError, match="domain"):
        upstream.build_candidate(
            base_archive=_archive(sites=[("root/sites.js", b"var defaultSites = {};\n")]),
            updated_rules=_updates(["one.example"]),
            updates_commit="a" * 40,
            existing_domain_count=1,
        )


def test_isolated_access_token_property_is_removed_and_counted():
    upstream = _upstream()
    fake = '  cs_param: {headers: {"x-access-token": "FAKE_TEST_VALUE"}},'
    source = _sites(["safe.example"], extra_line=fake)

    sanitized = upstream.sanitize_sites_js(source)

    assert b"x-access-token" not in sanitized.content
    assert sanitized.removed_count == 1
    assert sanitized.marker == "x-access-token"


def test_access_token_embedded_with_rule_structure_fails_closed():
    upstream = _upstream()
    ambiguous = _sites(
        ["safe.example"],
        extra_line='  "Other": { domain: "other.example", x-access-token: "FAKE" },',
    )

    with pytest.raises(upstream.UpstreamRuleError, match="x-access-token"):
        upstream.sanitize_sites_js(ambiguous)


def test_access_token_cs_param_with_same_line_sibling_fails_closed():
    upstream = _upstream()
    source = _sites(
        ["safe.example"],
        extra_line=(
            '  cs_param: {headers: {"x-access-token": "FAKE_TEST_VALUE"}}, '
            'domain: "must-not-be-dropped.example",'
        ),
    )

    with pytest.raises(upstream.UpstreamRuleError, match="ambiguous|x-access-token"):
        upstream.sanitize_sites_js(source)


@pytest.mark.parametrize(
    "credential",
    [
        "-----BEGIN PRIVATE KEY-----",
        'Authorization: "Bearer FAKE_TEST_CREDENTIAL"',
    ],
)
def test_high_risk_credentials_are_rejected_not_sanitized(credential):
    upstream = _upstream()

    with pytest.raises(upstream.UpstreamRuleError, match="credential|secret|private|Bearer"):
        upstream.sanitize_sites_js(_sites(["safe.example"], extra_line=credential))


def test_quoted_authorization_bearer_fails_closed():
    upstream = _upstream()
    source = _sites(
        ["unsafe.example"],
        extra_line='  "Authorization": "Bearer FAKE_TEST_VALUE",',
    )

    with pytest.raises(upstream.UpstreamRuleError, match="credential"):
        upstream.sanitize_sites_js(source)


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [
        ("AKIA", "F" * 16),
        ("ASIA", "F" * 16),
        ("sk_live_", "F" * 20),
        ("rk_live_", "F" * 20),
        ("xoxb-", "F" * 24),
        ("xapp-", "F" * 24),
        ("glpat-", "F" * 20),
        ("npm_", "F" * 36),
    ],
)
def test_obvious_vendor_token_prefixes_fail_closed(prefix, suffix):
    upstream = _upstream()

    with pytest.raises(upstream.UpstreamRuleError, match="credential|secret"):
        upstream.sanitize_sites_js(
            _sites(["unsafe.example"], extra_line=prefix + suffix)
        )


def test_catastrophic_shrink_guard_accepts_exactly_eighty_percent():
    upstream = _upstream()
    candidate = upstream.build_candidate(
        base_archive=_archive(
            sites=[("root/sites.js", _sites([f"d{i}.example" for i in range(8)]))]
        ),
        updated_rules=_updates(["updated.example"]),
        updates_commit="b" * 40,
        existing_domain_count=10,
    )

    assert candidate.domain_count == 8


def test_catastrophic_shrink_guard_rejects_below_eighty_percent():
    upstream = _upstream()

    with pytest.raises(upstream.UpstreamRuleError, match="80%|shrink"):
        upstream.build_candidate(
            base_archive=_archive(
                sites=[("root/sites.js", _sites([f"d{i}.example" for i in range(7)]))]
            ),
            updated_rules=_updates(["updated.example"]),
            updates_commit="c" * 40,
            existing_domain_count=10,
        )


def test_update_writes_three_snapshots_and_deterministic_manifest(tmp_path):
    upstream = _upstream()
    _seed_output(tmp_path, domains=["old.example"])
    sites = _sites(["one.example", "two.example"])
    archive = _archive(sites=[("root/sites.js", sites)], version="9.8.7")
    updates = _updates(["updated.example"])

    result = upstream.update_mirror(
        root=tmp_path,
        base_archive=archive,
        updated_rules=updates,
        updates_commit="d" * 40,
    )

    manifest_bytes = (tmp_path / "data/upstream-manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert result.changed is True
    assert set(result.changed_paths) == {
        "data/sites.js",
        "data/sites_updated.json",
        "data/upstream-manifest.json",
    }
    assert (tmp_path / "data/sites.js").read_bytes() == sites
    assert json.loads((tmp_path / "data/sites_updated.json").read_bytes()) == json.loads(updates)
    assert manifest == {
        "schema_version": 1,
        "upstream_extension_version": "9.8.7",
        "updates_source_commit": "d" * 40,
        "sites_js": {"sha256": hashlib.sha256(sites).hexdigest(), "domain_count": 2},
        "sites_updated_json": {
            "sha256": hashlib.sha256((tmp_path / "data/sites_updated.json").read_bytes()).hexdigest(),
            "entry_count": 1,
        },
        "sanitation": {"marker": "x-access-token", "removed_count": 0},
    }
    assert b"timestamp" not in manifest_bytes.lower()


def test_identical_rerun_is_noop_and_touches_nothing(tmp_path):
    upstream = _upstream()
    _seed_output(tmp_path, domains=["old.example"])
    kwargs = {
        "root": tmp_path,
        "base_archive": _archive(sites=[("root/sites.js", _sites(["same.example"]))]),
        "updated_rules": _updates(["updated.example"]),
        "updates_commit": "e" * 40,
    }
    upstream.update_mirror(**kwargs)
    paths = sorted((tmp_path / "data").iterdir())
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}

    result = upstream.update_mirror(**kwargs)

    assert result.changed is False
    assert result.changed_paths == ()
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths} == before


def test_candidate_failure_preserves_every_existing_snapshot(tmp_path):
    upstream = _upstream()
    original = _seed_output(tmp_path, domains=[f"old{i}.example" for i in range(10)])

    with pytest.raises(upstream.UpstreamRuleError):
        upstream.update_mirror(
            root=tmp_path,
            base_archive=_archive(sites=[("root/sites.js", _sites(["too-small.example"]))]),
            updated_rules=_updates(["updated.example"]),
            updates_commit="f" * 40,
        )

    assert {
        name: (tmp_path / "data" / name).read_bytes() for name in original
    } == original


def test_snapshot_set_is_committed_through_one_atomic_writer_call(tmp_path):
    upstream = _upstream()
    _seed_output(tmp_path, domains=["old.example"])
    calls = []

    def atomic_writer(root: Path, snapshots: dict[str, bytes]) -> None:
        calls.append((root, snapshots))

    result = upstream.update_mirror(
        root=tmp_path,
        base_archive=_archive(sites=[("root/sites.js", _sites(["new.example"]))]),
        updated_rules=_updates(["updated.example"]),
        updates_commit="1" * 40,
        atomic_writer=atomic_writer,
    )

    assert result.changed is True
    assert len(calls) == 1
    assert calls[0][0] == tmp_path
    assert set(calls[0][1]) == {
        "data/sites.js",
        "data/sites_updated.json",
        "data/upstream-manifest.json",
    }
