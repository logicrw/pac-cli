"""A2 merge whole-entry replace (§15.1.1)."""
from pathlib import Path

from bpc_fetch.rules.sync import merge_to_domain_map, merge_updated_into_entries
from bpc_fetch.sites import entries_to_domain_map


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


def test_general_blockers_use_source_domains_exclusions_and_stable_deduplication():
    entries = {
        "Global": {
            "domain": "source.example",
            "block_regex_general": r"shared|{domain}",
            "excluded_domains": ["excluded.example"],
        },
        "Duplicate": {
            "domain": "source.example",
            "block_regex_general": r"shared|{domain}",
            "excluded_domains": ["excluded.example"],
        },
        "Excluded": {"domain": "excluded.example"},
        "Excluded subdomain": {"domain": "news.excluded.example"},
        "Unrelated": {"domain": "target.example"},
    }

    sites = entries_to_domain_map(entries)

    assert sites["source.example"].block_regex_general == r"shared|{domain}"
    assert sites["source.example"].excluded_domains == ["excluded.example"]
    assert "block_regex_general" not in sites["source.example"].extra
    assert "excluded_domains" not in sites["source.example"].extra
    assert sites["excluded.example"].general_block_regexes == []
    assert sites["news.excluded.example"].general_block_regexes == []
    assert sites["target.example"].general_block_regexes == [r"shared|source\.example"]


def test_explicit_empty_group_general_blocker_registers_no_rule():
    entries = {
        "Opt-in only": {
            "domain": "###_opt_in",
            "group": [],
            "block_regex_general": "must-not-apply",
        },
        "Target": {"domain": "target.example"},
    }

    assert entries_to_domain_map(entries)["target.example"].general_block_regexes == []


def test_no_group_general_blocker_registers_its_domain_as_source():
    entries = {
        "Pure global": {
            "domain": "###_global",
            "block_regex_general": r"tracker/{domain}",
        },
        "Target": {"domain": "target.example"},
    }

    assert entries_to_domain_map(entries)["target.example"].general_block_regexes == [
        r"tracker/\#\#\#_global"
    ]


def test_nonempty_group_materializes_each_source_and_replaces_matching_exception():
    entries = {
        "Grouped": {
            "domain": "###_grouped",
            "group": ["one.example", "two.example", "three.example"],
            "block_regex_general": r"parent/{domain}",
            "excluded_domains": ["parent-excluded.example"],
            "exception": [
                {
                    "domain": "two.example",
                    "block_regex_general": r"exception/{domain}",
                    "excluded_domains": ["exception-excluded.example"],
                },
                {"domain": ["three.example"], "allow_cookies": 1},
            ],
        },
        "Target": {"domain": "target.example"},
    }

    assert entries_to_domain_map(entries)["target.example"].general_block_regexes == [
        r"parent/one\.example",
        r"exception/two\.example",
    ]
