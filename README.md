# pac-cli

Fetch paywalled / news articles as **Markdown** via an Agent-friendly **CLI**.

> URL → JSON (`markdown`, `strategy_hit`, `rule_version`, `error_code`, `failure_class`)

| | |
|--|--|
| **Local** | `~/Projects/pac-cli` |
| **GitHub** | https://github.com/logicrw/pac-cli (fork of bpc-fetch) |
| **Phase 1 license** | `docs/IMPLEMENTATION.md` §15 (approved 2026-07-29) |

## Install

Release **0.2.2** can be installed from a locally built wheel or directly from
source. No PyPI publication is implied.

```bash
cd ~/Projects/pac-cli
python3 -m venv .venv && source .venv/bin/activate
pip install dist/pac_cli-0.2.2-py3-none-any.whl  # after building the wheel
# Or, for a source/development install:
pip install -e .
playwright install chromium   # if needed
pac doctor --compact
```

## Usage

```bash
pac rules sync --compact          # force a rule sync now
pac rules sync --from-zip bypass-paywalls-chrome-clean.zip --compact
pac rules show wsj.com --compact
pac fetch "https://www.wsj.com/..." --compact
pac fetch "https://www.wsj.com/articles/example" --diagnostics --compact
pac fetch "https://www.wsj.com/..." --no-rule-sync --compact
pac fetch "https://www.wsj.com/..." --out-dir articles --images --compact
pac batch --file urls.txt --out-dir articles --max 10 --compact
python scripts/audit_rule_coverage.py --compact
```

- Default: JSON on stdout, markdown truncated at 20k chars (`--full` to disable).
- Write the same fetch result as full Markdown only with `--out-dir`; batch writes each successful result once.
- Image downloads are opt-in with `fetch --images` and require `--out-dir`.
- `pac doctor` launches Chromium to verify the executable, not only the Python package.
- Teaser/paywall text → `ok: false` unless `--allow-partial`.

### Automatic rule maintenance

- GitHub Actions checks the official BPC archive and update repository daily at
  `03:23 UTC`. A validated change updates this repository's rule mirror through
  a pull request; it never writes unreviewed upstream data directly to `main`.
- `fetch` and `batch` use stale-while-revalidate rule maintenance. By default, a
  valid runtime snapshot is considered fresh for 7 days; once stale, PAC keeps
  serving the last coherent snapshot and schedules one detached refresh child.
  There is no persistent daemon and no OS-specific scheduler.
- A failed check is fail-open: PAC keeps using the last valid cache or bundled
  snapshot and reports a warning instead of blocking article fetches.
- Use `--no-rule-sync`, `PAC_RULES_AUTO_SYNC=0`, an explicit `--sites-js`, or
  `PAC_RULES_PIN=/path/to/rules` to prevent the lazy network check. Override the
  TTL with `PAC_RULES_TTL_SECONDS` when needed.
- Rule-data changes can be mirrored automatically. New BPC execution semantics
  or fields still require an explicit PAC code change and tests.
- GitHub may disable scheduled workflows after 60 days without repository
  activity on a public repository. Re-enable the workflow and run
  `workflow_dispatch` once if that happens.

## Docker

Run PAC in a self-contained container with all browser and TLS engines pre-installed:

```bash
# Build the image
docker build -t logicrw/pac-cli .

# Run single fetch
docker run --rm logicrw/pac-cli fetch "https://www.economist.com/..." --compact

# Run diagnostic check
docker run --rm logicrw/pac-cli doctor --compact
```

## Docs

- [Architecture Design & Pipeline](docs/ARCHITECTURE.md)


## Diagnostics

`fetch`, `batch`, and `discover` accept `--diagnostics`. The flag is opt-in so the
default Agent JSON envelope stays small and stable. Diagnostic output includes a
request ID, total command latency, per-engine timing totals, ordered attempt
history, and quality-gate score/metrics. Do not treat diagnostic fields as article
content.

## Offline registrable-domain resolution

PAC vendors the Public Suffix List snapshot `2026-08-17_18-44-50_UTC` and builds
a trie lazily in-process. Domain resolution performs no network request. Updating
the PSL is therefore an explicit release-data change rather than hidden runtime
I/O.

## SSRF defense in depth / container egress

PAC validates HTTP(S) targets and redirects in-process, but application-level DNS
checks cannot eliminate every DNS-rebinding or network-namespace race. Production
deployments should also deny private, loopback, link-local, and metadata-address
egress outside the Python process. Never run PAC containers with `--network=host`.

For a dedicated Docker bridge on a Linux host using Docker's iptables firewall
backend, enforce the deny policy on the host's `DOCKER-USER` chain. The example
below reserves `172.30.0.0/24` only for PAC and blocks the main non-public IPv4
ranges before Docker forwards container traffic:

```bash
docker network create --subnet 172.30.0.0/24 pac-public
iptables -I DOCKER-USER -s 172.30.0.0/24 -d 10.0.0.0/8 -j REJECT
iptables -I DOCKER-USER -s 172.30.0.0/24 -d 172.16.0.0/12 -j REJECT
iptables -I DOCKER-USER -s 172.30.0.0/24 -d 192.168.0.0/16 -j REJECT
iptables -I DOCKER-USER -s 172.30.0.0/24 -d 169.254.0.0/16 -j REJECT
iptables -I DOCKER-USER -s 172.30.0.0/24 -d 100.64.0.0/10 -j REJECT
iptables -I DOCKER-USER -s 172.30.0.0/24 -d 127.0.0.0/8 -j REJECT
```

If Docker uses the nftables backend, express the same destination policy in the
host nftables ruleset instead of copying the iptables commands verbatim. If IPv6
is enabled, also deny `::1/128`, `fc00::/7`, and `fe80::/10`. Cloud deployments
should additionally deny their provider metadata endpoints at the VPC, firewall,
or egress-policy layer. For fully offline rule inspection, use Docker
`--network=none` together with pinned or bundled rules.

## Agent Skill

The minimal Agent Skill is at [`skills/pac-cli/SKILL.md`](skills/pac-cli/SKILL.md).
Load it before delegating PAC fetches so the agent runs `pac doctor`, respects
the batch cap, and branches on structured errors rather than guessing.

## Tests

```bash
pip install -e ".[test]"
pytest -q
```

The `eval` extra is only for `scripts/run_eval.py`; PyYAML is not required by
the installed `pac` CLI.

## License / NOTICE

See [NOTICE](NOTICE). Derived from bpc-fetch (MIT) and BPC site rules (MIT). The vendored Public Suffix List snapshot is MPL-2.0. Educational / personal research; as-is.
