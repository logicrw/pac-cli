"""Final 29-source discovery policy contracts."""

from bpc_fetch.source_policy import discovery_policy_for_domain, load_discovery_policies


def test_policy_inventory_covers_exactly_final_twenty_nine_sources():
    policies = load_discovery_policies()
    assert len(policies) == 29
    assert len({policy.domain for policy in policies}) == 29


def test_reuters_uses_daily_sitemap_with_bing_fallback_and_barrons_is_bing_only():
    reuters = discovery_policy_for_domain("reuters.com")
    barrons = discovery_policy_for_domain("barrons.com")

    assert reuters is not None
    assert reuters.discovery_mode == "firecrawl_daily_sitemap"
    assert reuters.feed_urls == ()
    assert "bing_site" in reuters.fallbacks

    assert barrons is not None
    assert barrons.discovery_mode == "bing_site"
    assert barrons.feed_urls == ()
    assert "bing_site" in barrons.fallbacks


def test_the_information_uses_public_article_sitemap_with_bing_fallback():
    policy = discovery_policy_for_domain("theinformation.com")
    assert policy is not None
    assert policy.discovery_mode == "sitemap_articles"
    assert policy.feed_urls == ("https://www.theinformation.com/sitemap-articles.xml",)
    assert "bing_site" in policy.fallbacks


def test_verified_feed_source_records_date_quality_and_all_feed_mode():
    ft = discovery_policy_for_domain("www.ft.com")
    nikkei = discovery_policy_for_domain("asia.nikkei.com")

    assert ft is not None
    assert ft.discovery_mode == "all_verified_feeds"
    assert len(ft.feed_urls) == 7
    assert len(ft.focus_feed_urls) == 4
    assert ft.date_quality == "present"
    assert nikkei is not None and nikkei.date_quality == "missing"
