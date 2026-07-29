# pac-cli

Fetch paywalled / news articles as **Markdown** via an Agent-friendly **CLI**.

> URL → JSON (`markdown`, `strategy_hit`, `rule_version`, `error_code`, `failure_class`)

| | |
|--|--|
| **Local** | `~/Projects/pac-cli` |
| **GitHub** | https://github.com/logicrw/pac-cli (fork of bpc-fetch) |
| **Phase 1 license** | `docs/IMPLEMENTATION.md` §15 (approved 2026-07-29) |

## Install

```bash
cd ~/Projects/pac-cli
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium   # if needed
pac doctor --compact
```

## Usage

```bash
pac rules sync --compact          # bundled base + optional sites_updated
pac rules show wsj.com --compact
pac fetch "https://www.wsj.com/..." --compact
pac batch --file urls.txt --max 10 --compact
```

- Default: JSON on stdout, markdown truncated at 20k chars (`--full` to disable).
- Write files only with `--out-dir`.
- Teaser/paywall text → `ok: false` unless `--allow-partial`.

## Docs

| Doc | Role |
|-----|------|
| [IMPLEMENTATION.md](docs/IMPLEMENTATION.md) | Phase 1 plan + **§15 expert approval** |
| [TARGET-ARCHITECTURE.md](docs/TARGET-ARCHITECTURE.md) | Phase **1.5+** only (not week-1) |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Product background |

## Tests

```bash
pip install pytest
pytest -q
```

## License / NOTICE

See [NOTICE](NOTICE). Derived from bpc-fetch (MIT) and BPC site rules (MIT). Educational / personal research; as-is.
