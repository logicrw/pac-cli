"""A2 merge whole-entry replace (§15.1.1)."""
from pathlib import Path

from bpc_fetch.rules.sync import merge_to_domain_map, merge_updated_into_entries


def _base_entries_min(tmp_path: Path) -> Path:
    js = tmp_path / "sites.js"
    js.write_text(
        """var defaultSites = {
  "Site A": {
    domain: "a.example",
    allow_cookies: 1,
    block_regex: /paywall\\.js/
  },
  "Site B": {
    domain: "b.example",
    useragent: "googlebot"
  }
};
""",
        encoding="utf-8",
    )
    return js


def test_whole_entry_replace_drops_missing_fields(tmp_path: Path):
    base = _base_entries_min(tmp_path)
    updated = {
        "Site A": {
            "domain": "a.example",
            "allow_cookies": 1,
            "useragent": "googlebot",
        }
    }
    m = merge_to_domain_map(base, updated)
    assert m["a.example"].useragent == "googlebot"
    assert m["a.example"].block_regex == ""
    assert m["b.example"].useragent == "googlebot"


def test_merge_entries_by_name():
    base = {
        "Site A": {"domain": "a.example", "block_regex_str": "x", "allow_cookies": 1},
    }
    updated = {"Site A": {"domain": "a.example", "useragent": "googlebot"}}
    out = merge_updated_into_entries(base, updated)
    assert "block_regex_str" not in out["Site A"]
    assert out["Site A"]["useragent"] == "googlebot"
