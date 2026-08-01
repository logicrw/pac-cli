# Changelog

All notable changes to PAC are documented here.

## 0.2.2

- Added a daily GitHub Actions check that validates and sanitizes upstream BPC
  rule snapshots, audits fields, runs the full test suite, and opens a rules PR
  only when the central mirror changes.
- Added a 24-hour client-side lazy rule sync before the first `fetch` or `batch`,
  with fail-open cache reuse and pin/explicit-file/`--no-rule-sync` controls.
- Added SSRF-safe, hop-by-hop validated redirects and response-size limits for
  remote rule downloads.

## 0.2.1

- Added an honest quality gate that rejects known teaser, paywall, navigation,
  and challenge shells instead of counting them as full articles.
- Fixed CLI output-directory behavior, opt-in image downloads, and `doctor`'s
  Chromium launch verification.
- Added atomic, validated rule synchronization and reload, including ZIP input.
- Added rule-coverage auditing and support for `block_regex_general` with
  `excluded_domains`.
- Bundled `sites.js` in wheels so fresh installs retain the built-in rule set.
- Removed a legacy embedded access token from an unsupported field in the
  bundled rule snapshot; PAC did not execute that field.
- Added a minimal Agent Skill for bounded, structured PAC use.

Phase 1 live results remain environment-limited: the accepted result was
**5/48**, compared with the normalized baseline of **6/48**. This release does
not claim broad publisher coverage.
