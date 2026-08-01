# PAC Error Semantics

Branch on the returned pair; do not replace it with guessed classifications.

| `error_code` | Typical `failure_class` | Meaning / next action |
|---|---|---|
| `RULE_MISSING` | `config` | Requested rule is absent; inspect `pac rules show DOMAIN --compact`. |
| `NO_STRATEGY` | `strategy` | No effective bypass strategy; report the limitation. |
| `NETWORK` | `network` | Transport/server failure; a bounded retry may be appropriate. |
| `HTTP_BLOCKED` | `bot` or `strategy` | HTTP denial/rate limit; do not add credentials or evade controls. |
| `BOT_CHALLENGE` | `bot` | CAPTCHA/challenge detected; stop and report it. |
| `PAYWALL_REMAINING` | `strategy` | Output is teaser/paywall content, not a full article. |
| `EXTRACT_FAILED` | `extract` | No acceptable article body was extracted. |
| `BROWSER_UNAVAILABLE` | `config` | Chromium cannot run; use `pac doctor --compact` or `pac install-browser`. |
| `ARCHIVE_FAILED` | `strategy` | Archive path failed; report rather than claiming content. |
| `LIMIT_EXCEEDED` | `config` | Batch exceeded its configured/default cap; reduce the authorized set. |
| `SSRF_BLOCKED` | `config` | URL resolved to a disallowed target; do not bypass the guard. |
| `INTERNAL` | `config` | Invalid command/configuration or unexpected internal failure; run doctor and preserve the returned error. |

Successful results use an empty `error_code` and `failure_class: "none"`.
The declared failure classes are `strategy`, `bot`, `network`, `extract`,
`config`, and `none`. Preserve `recovery_hint`, `warnings`, and `strategy_hit`
when reporting diagnostics.
