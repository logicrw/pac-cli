"""A1 parse tests."""
from pathlib import Path

from bpc_fetch.sites import (
    PUBLIC_SUFFIX_LIST_VERSION, SITES_JS_DEFAULT, _build_strategy, domain_from_url,
    entries_to_domain_map, parse_sites_js,
)


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



def test_psl_resolver_handles_global_multilevel_cctlds() -> None:
    assert PUBLIC_SUFFIX_LIST_VERSION == "2026-08-17_18-44-50_UTC"
    cases = {
        "https://news.example.com.cn/story": "example.com.cn",
        "https://a.b.example.co.uk/story": "example.co.uk",
        "https://a.example.com.au/story": "example.com.au",
        "https://a.example.co.id/story": "example.co.id",
        "https://a.example.edu.tw/story": "example.edu.tw",
        "https://a.example.gov.uk/story": "example.gov.uk",
    }
    assert {url: domain_from_url(url) for url in cases} == cases


def test_psl_resolver_supports_wildcards_exceptions_private_rules_and_ips() -> None:
    assert domain_from_url("https://a.b.ck/story") == "a.b.ck"
    assert domain_from_url("https://a.www.ck/story") == "www.ck"
    assert domain_from_url("https://project.github.io/story") == "project.github.io"
    assert domain_from_url("http://127.0.0.1/story") == "127.0.0.1"
