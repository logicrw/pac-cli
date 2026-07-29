"""A1 parse tests."""
from pathlib import Path

from bpc_fetch.sites import _build_strategy, entries_to_domain_map, parse_sites_js, SITES_JS_DEFAULT


def test_wsj_referer_custom_from_bundled():
    assert SITES_JS_DEFAULT.exists()
    sites = parse_sites_js(SITES_JS_DEFAULT)
    wsj = sites.get("wsj.com")
    assert wsj is not None
    assert wsj.referer_custom == "https://www.drudgereport.com/"
    assert "drudge" in wsj.referer_custom


def test_cs_dompurify_is_dom_cleanup_not_archive():
    st = _build_strategy("example.com", "Ex", {"cs_dompurify": 1, "domain": "example.com"})
    assert st.needs_browser_cleanup() is True
    assert st.bypass_type() == "dom_cleanup"
    assert st.bypass_type() != "archive"


def test_group_and_exception_expand():
    entries = {
        "GroupX": {
            "domain": "###_test_group",
            "group": ["a.example", "b.example"],
            "useragent": "googlebot",
            "exception": [
                {"domain": "b.example", "useragent": "", "referer_custom": "https://ref.example/"}
            ],
        }
    }
    m = entries_to_domain_map(entries)
    assert "a.example" in m
    assert m["a.example"].useragent == "googlebot"
    assert m["b.example"].referer_custom == "https://ref.example/"
