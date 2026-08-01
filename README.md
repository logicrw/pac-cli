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
- Before the first `fetch` or `batch` after the local rule manifest becomes 24
  hours old, PAC performs one lazy sync from this repository's stable raw
  mirror. There is no background daemon and no OS-specific scheduler.
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

## Docs

| Doc | Role |
|-----|------|
| [IMPLEMENTATION.md](docs/IMPLEMENTATION.md) | Phase 1 plan + **§15 expert approval** |
| [TARGET-ARCHITECTURE.md](docs/TARGET-ARCHITECTURE.md) | Phase **1.5+** only (not week-1) |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Product background |

## Agent Skill

The minimal Agent Skill is at [`skills/pac-cli/SKILL.md`](skills/pac-cli/SKILL.md).
Load it before delegating PAC fetches so the agent runs `pac doctor`, respects
the batch cap, and branches on structured errors rather than guessing.

## Tests

```bash
pip install -e ".[eval]"
pip install pytest
pytest -q
```

The `eval` extra is only for `scripts/run_eval.py`; PyYAML is not required by
the installed `pac` CLI.

## License / NOTICE

See [NOTICE](NOTICE). Derived from bpc-fetch (MIT) and BPC site rules (MIT). Educational / personal research; as-is.
