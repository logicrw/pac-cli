import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "audit_rule_coverage.py"


def run_audit(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def test_reports_nested_field_counts_statuses_and_deterministic_sorting(tmp_path: Path) -> None:
    sites_js = tmp_path / "sites.js"
    sites_js.write_text(
        '''var defaultSites = {
          "Grouped": {
            "domain": "###_grouped",
            "group": ["a.example", "b.example"],
            "useragent": "googlebot",
            "allow_cookies": 1,
            "mystery": true,
            "_private": true,
            "exception": [{
              "domain": "exception.example",
              "referer": "google",
              "mystery": true,
              "nested_only": 1
            }]
          },
          "Single": {
            "domain": "single.example",
            "amp": true,
            "block_regex_general": "tracker-general",
            "block_regex_str": "tracker",
            "excluded_domains": ["skip.example"],
            "zeta": true
          }
        };''',
        encoding="utf-8",
    )

    result = run_audit("--sites-js", str(sites_js), "--compact")

    assert result.returncode == 0, result.stderr
    assert "\n" not in result.stdout.strip()
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert report["sites_js"] == str(sites_js)
    assert report["entry_count"] == 2
    assert report["domain_count"] == 4
    assert report["field_counts"] == [
        {"field": "domain", "count": 3, "status": "structural"},
        {"field": "mystery", "count": 2, "status": "unmodeled"},
        {"field": "allow_cookies", "count": 1, "status": "modeled_not_executed"},
        {"field": "amp", "count": 1, "status": "modeled_not_executed"},
        {"field": "block_regex_general", "count": 1, "status": "executed"},
        {"field": "block_regex_str", "count": 1, "status": "executed"},
        {"field": "exception", "count": 1, "status": "structural"},
        {"field": "excluded_domains", "count": 1, "status": "executed"},
        {"field": "group", "count": 1, "status": "structural"},
        {"field": "nested_only", "count": 1, "status": "unmodeled"},
        {"field": "referer", "count": 1, "status": "executed"},
        {"field": "useragent", "count": 1, "status": "executed"},
        {"field": "zeta", "count": 1, "status": "unmodeled"},
    ]
    assert report["unmodeled_fields"] == [
        {"field": "mystery", "count": 2, "status": "unmodeled"},
        {"field": "nested_only", "count": 1, "status": "unmodeled"},
        {"field": "zeta", "count": 1, "status": "unmodeled"},
    ]


def test_invalid_input_emits_json_error_and_exits_nonzero(tmp_path: Path) -> None:
    missing = tmp_path / "missing.js"

    result = run_audit("--sites-js", str(missing), "--compact")

    assert result.returncode != 0
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert report["sites_js"] == str(missing)
    assert report["error"]
