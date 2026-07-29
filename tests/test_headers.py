"""A3 headers referer_custom priority."""
from bpc_fetch.sites import SiteStrategy
from bpc_fetch.strategy import build_headers


def test_referer_custom_wins():
    st = SiteStrategy(
        domain="wsj.com",
        referer="google",
        referer_custom="https://www.drudgereport.com/",
    )
    h = build_headers(st)
    assert h["Referer"] == "https://www.drudgereport.com/"


def test_referer_enum_when_no_custom():
    st = SiteStrategy(domain="ft.com", referer="google")
    h = build_headers(st)
    assert h["Referer"] == "https://www.google.com/"
