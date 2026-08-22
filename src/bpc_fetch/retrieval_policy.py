"""Explicit per-outlet retrieval controls for PAC's final source list."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files


@dataclass(frozen=True)
class RetrievalPolicy:
    domain: str
    browser_mode: str
    credential_mode: str
    firecrawl_mode: str
    firecrawl_failure_codes: frozenset[str]
    representation_mode: str
    failure_mode: str


@lru_cache(maxsize=1)
def load_retrieval_policies() -> tuple[RetrievalPolicy, ...]:
    path = files("bpc_fetch").joinpath("data/source_retrieval_policies.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        RetrievalPolicy(
            domain=str(raw["domain"]),
            browser_mode=str(raw["browser_mode"]),
            credential_mode=str(raw["credential_mode"]),
            firecrawl_mode=str(raw["firecrawl_mode"]),
            firecrawl_failure_codes=frozenset(str(code) for code in raw.get("firecrawl_failure_codes", [])),
            representation_mode=str(raw["representation_mode"]),
            failure_mode=str(raw["failure_mode"]),
        )
        for raw in payload.get("policies", [])
    )


def retrieval_policy_for_domain(domain: str) -> RetrievalPolicy | None:
    normalized = domain.casefold().removeprefix("www.").rstrip(".")
    return next((policy for policy in load_retrieval_policies() if policy.domain == normalized), None)


def may_use_yahoo_bloomberg_representation(domain: str) -> bool:
    """Return whether an outlet explicitly permits Yahoo Bloomberg representation."""
    policy = retrieval_policy_for_domain(domain)
    return bool(policy and policy.representation_mode == "yahoo_bloomberg_candidate")


def may_use_firecrawl(domain: str, failure_code: str) -> bool:
    """Return whether a selected outlet permits one cloud fallback attempt."""
    policy = retrieval_policy_for_domain(domain)
    return bool(
        policy
        and policy.firecrawl_mode == "on_bot_or_http_block"
        and failure_code in policy.firecrawl_failure_codes
    )
