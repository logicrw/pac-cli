# Curated Source Migration

`news-scraper-final` is a reference-only repository for source candidates,
historical RSS/Atom endpoints, and observed failure modes. PAC owns discovery,
retrieval policy, credentials, quality, and provenance.

## Final 29-Source Configuration

The bundled `src/bpc_fetch/data/curated_sources.json` mirrors the current
29-domain `news-scraper-final` working registry. This is the user-approved
final selected source configuration, including Axios, SCMP, The Register,
404 Media, and Sifted.

The historical 48-domain probe is retained only as reference evidence in
`news-scraper-final`; it is not an implicit PAC candidate allowlist. PAC does
not add historical-only domains unless the user explicitly changes the final
source configuration.

## Feed Status

Every feed is one of:

- `candidate`: copied from the reference repository and inert at runtime;
- `verified`: tested for stable, canonical publisher URLs and a documented
  scope; eligible for automatic discovery;
- `disabled`: retained for history but not eligible for discovery.

A Feed is never promoted merely because an endpoint responds with HTTP 200.
Technical verification requires parseable RSS/Atom, publisher-domain article
links, and entry dates; the separate per-source discovery policy records whether
the Feed is intended for all outlet news or only finance/technology coverage.

A credential-free health probe on 2026-08-22 verified 129 of 130 registered
Feed endpoints as parseable RSS/Atom with publisher-domain article links. Those
129 entries are `verified` for **discovery transport only**, not for editorial
scope completeness or full-text retrieval. The Information public feed returned
HTTP 403 and is `disabled`; Reuters and Barron's have no public Feed entries and
therefore use later Bing/vault discovery policies.

## Runtime Order

For a topic/domain query PAC uses:

```text
verified official feed (scope must match query)
→ Bing News site: query
→ Google News title-only signal (future bounded fallback)
```

Candidate feeds do not create network calls. They are a review queue, not a
production allowlist.

Feed health does not imply full text. Retrieval is governed by
`source_retrieval_policies.json`. For FT/Bloomberg-class authorized sessions,
`pac fetch --interactive` is the on-demand Ego lite path and is not part of
`pac batch`.

## Evidence

- `news-scraper-final/sources.yaml` (current working registry, read 2026-08-22)
- `news-scraper-final/agent_data/rss_discovery.json` (historical feed discovery)
- `news-scraper-final/agent_data/probe_*.json` (historical source outcomes)
- `outputs/news-scraper-final-assessment.md` (migration assessment)
