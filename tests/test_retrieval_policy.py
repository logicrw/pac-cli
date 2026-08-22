"""Final 29-source retrieval policy contracts."""

import asyncio

from bpc_fetch.retrieval_policy import (
    load_retrieval_policies,
    may_use_firecrawl,
    retrieval_policy_for_domain,
)
from bpc_fetch.strategy import CloudBudget


def test_retrieval_policy_covers_exactly_final_twenty_nine_sources():
    policies = load_retrieval_policies()
    assert len(policies) == 29
    assert len({policy.domain for policy in policies}) == 29
    domains = {
        "wsj.com", "reuters.com", "bloomberg.com", "ft.com", "theinformation.com",
        "axios.com", "scmp.com", "theregister.com", "404media.co", "sifted.eu",
    }
    assert all(retrieval_policy_for_domain(domain) is not None for domain in domains)


def test_cloud_fallback_is_limited_to_allowed_outlets_and_failure_codes():
    assert may_use_firecrawl("wsj.com", "BOT_CHALLENGE") is True
    assert may_use_firecrawl("reuters.com", "HTTP_BLOCKED") is True
    assert may_use_firecrawl("wsj.com", "EXTRACT_FAILED") is False
    assert may_use_firecrawl("bloomberg.com", "BOT_CHALLENGE") is False
    assert may_use_firecrawl("ft.com", "HTTP_BLOCKED") is False
    assert may_use_firecrawl("theinformation.com", "BOT_CHALLENGE") is False
    assert may_use_firecrawl("unknown.example", "BOT_CHALLENGE") is False


def test_cloud_budget_allows_only_its_configured_number_of_attempts():
    async def consume():
        budget = CloudBudget(2)
        return await asyncio.gather(*(budget.try_consume() for _ in range(4))), budget.used_calls

    granted, used_calls = asyncio.run(consume())
    assert sum(granted) == 2
    assert used_calls == 2


def test_paywalled_sources_have_explicit_failure_and_provenance_modes():
    bloomberg = retrieval_policy_for_domain("bloomberg.com")
    information = retrieval_policy_for_domain("theinformation.com")

    assert bloomberg.representation_mode == "yahoo_bloomberg_candidate"
    assert bloomberg.failure_mode == "paywall_remaining"
    assert information.credential_mode == "vault_required_for_fulltext"
