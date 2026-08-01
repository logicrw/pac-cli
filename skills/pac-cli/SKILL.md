---
name: pac-cli
description: Use when fetching user-authorized news articles with PAC.
version: 0.2.1
author: pac-cli contributors
license: MIT
metadata:
  hermes:
    tags: [articles, research, cli]
    related_skills: []
---

# PAC CLI

## Overview

Use PAC for bounded, personal research or other user-authorized article access.
PAC returns JSON; make decisions from `ok`, `error_code`, and `failure_class`.

## Workflow

1. Verify prerequisites. Completion: this returns `"ok": true`:
   ```bash
   pac doctor --compact
   ```
2. Optionally inspect the active rules before fetching:
   ```bash
   pac rules version --compact
   pac rules show wsj.com --compact
   ```
3. Fetch one article using the standard path:
   ```bash
   pac fetch "https://example.com/article" --compact
   ```
   Add `--out-dir articles` to save full Markdown. Add `--images` only with
   `--out-dir` and only when the user requested images.
4. Parse the JSON. Accept full content only when `ok` is true and
   `paywall_suspected` is false. Branch on the actual `error_code` and
   `failure_class`; consult [error-codes.md](references/error-codes.md).

## Batch Use

Use `pac batch --file urls.txt --max 10 --compact`. The default limit is 10 and
the hard cap is 25. Never split work to evade the cap or start an unbounded
crawl.

## Boundaries

- Never treat teaser or partial paywall text as a full article. Do not use
  `--allow-partial` unless the user explicitly asks for partial content, and
  label that content as partial.
- Never provide cookies, session tokens, credentials, or other secrets to PAC.
- Monitoring, scheduling, URL discovery, and crawl state remain the calling
  scraper's responsibility; PAC fetches supplied URLs.
- Follow [compliance.md](references/compliance.md).

## Verification Checklist

- [ ] `pac doctor --compact` passed before fetches.
- [ ] Every request was user-authorized and bounded.
- [ ] Results were classified from returned JSON, not inferred.
- [ ] Teasers were not represented as complete articles.
