"""Curated source registry contracts."""

from bpc_fetch.source_registry import (
    load_curated_sources,
    source_for_domain,
    verified_feeds_for_domain,
)


def test_registry_matches_the_final_twenty_nine_source_configuration():
    sources = load_curated_sources()
    domains = {source.domain for source in sources}
    assert len(sources) == 29
    assert len(domains) == 29
    assert sum(len(source.feeds) for source in sources) == 130
    assert {"axios.com", "scmp.com", "theregister.com", "404media.co", "sifted.eu"} <= domains
    assert {"adweek.com", "businessoffashion.com", "entrepreneur.com", "newyorker.com"}.isdisjoint(domains)


def test_only_health_verified_feeds_are_eligible_for_automatic_use():
    ft_feeds = verified_feeds_for_domain("ft.com")
    bloomberg_feeds = verified_feeds_for_domain("www.bloomberg.com")
    techcrunch_feeds = verified_feeds_for_domain("techcrunch.com")

    assert len(ft_feeds) == 7
    assert len(bloomberg_feeds) == 6
    assert len(techcrunch_feeds) == 11
    assert all(feed.status == "verified" for feed in (*ft_feeds, *bloomberg_feeds, *techcrunch_feeds))


def test_sources_without_public_feeds_or_with_403_feeds_are_not_eligible():
    reuters = source_for_domain("reuters.com")
    information = source_for_domain("theinformation.com")

    assert reuters is not None and reuters.feeds == ()
    assert information is not None
    assert [feed.status for feed in information.feeds] == ["disabled"]
    assert verified_feeds_for_domain("theinformation.com") == ()
