"""Contract tests for the scheduled upstream-rule synchronization workflow."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml


WORKFLOW = Path(__file__).parents[1] / ".github/workflows/sync-bpc-rules.yml"
UPDATER_SCRIPT = Path(__file__).parents[1] / "scripts/sync_upstream_rules.py"


def _load_updater_module():
    spec = importlib.util.spec_from_file_location("sync_upstream_rules", UPDATER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Yaml12SafeLoader(yaml.SafeLoader):
    """Do not coerce the GitHub Actions key `on` to boolean (YAML 1.1)."""


_Yaml12SafeLoader.yaml_implicit_resolvers = {
    key: [
        (tag, pattern)
        for tag, pattern in resolvers
        if tag != "tag:yaml.org,2002:bool"
    ]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _load() -> tuple[dict, str]:
    assert WORKFLOW.is_file(), f"missing workflow: {WORKFLOW}"
    text = WORKFLOW.read_text(encoding="utf-8")
    document = yaml.load(text, Loader=_Yaml12SafeLoader)
    assert isinstance(document, dict)
    return document, text


def _steps(document: dict) -> list[dict]:
    return [
        step
        for job in document.get("jobs", {}).values()
        for step in job.get("steps", [])
        if isinstance(step, dict)
    ]


def _run(step: dict) -> str:
    return str(step.get("run", ""))


def test_workflow_has_exact_daily_schedule_and_manual_trigger():
    document, _ = _load()

    triggers = document["on"]
    assert triggers["schedule"] == [{"cron": "23 3 * * *"}]
    assert "workflow_dispatch" in triggers


def test_workflow_serializes_runs_and_declares_only_required_write_permissions():
    document, _ = _load()

    concurrency = document.get("concurrency")
    assert concurrency
    assert concurrency.get("group")
    assert concurrency.get("cancel-in-progress") is not None
    assert document.get("permissions") == {
        "contents": "write",
        "pull-requests": "write",
    }


def test_workflow_sets_up_python_installs_project_and_runs_central_updater():
    document, _ = _load()
    steps = _steps(document)
    uses = [str(step.get("uses", "")) for step in steps]
    commands = "\n".join(_run(step) for step in steps)

    assert any(use.startswith("actions/checkout@") for use in uses)
    assert any(use.startswith("actions/setup-python@") for use in uses)
    assert re.search(r"python\s+-m\s+pip\s+install|pip\s+install", commands)
    assert UPDATER_SCRIPT.is_file()
    assert re.search(
        r"python\s+(?:scripts/sync_upstream_rules\.py|-m\s+bpc_fetch\.rules\.upstream)",
        commands,
    )


def test_workflow_fetches_updates_with_shallow_clone_and_records_commit():
    document, _ = _load()
    commands = "\n".join(_run(step) for step in _steps(document))

    assert re.search(r"git\s+clone[^\n]*(--depth(?:=|\s+)1|--filter=blob:none)", commands)
    assert "--no-tags" in commands
    assert "bpc_updates" in commands
    assert re.search(r"git\s+(?:-C\s+\S+\s+)?rev-parse\s+HEAD", commands)


def test_workflow_audits_manifest_fields_and_runs_full_tests_before_pr():
    document, _ = _load()
    steps = _steps(document)
    runs = [_run(step) for step in steps]

    updater_index = next(
        index
        for index, command in enumerate(runs)
        if "sync_upstream_rules.py" in command or "bpc_fetch.rules.upstream" in command
    )
    audit_index = next(
        index
        for index, command in enumerate(runs)
        if "upstream-manifest.json" in command
        and "schema_version" in command
        and "updates_source_commit" in command
        and "sha256" in command
    )
    tests_index = next(
        index
        for index, command in enumerate(runs)
        if "pip install" not in command and re.search(r"\bpytest\b", command)
    )
    pr_index = next(
        index
        for index, command in enumerate(runs)
        if re.search(r"\bgh\s+pr\s+(create|edit)\b", command)
    )

    assert updater_index < audit_index < tests_index < pr_index
    assert "tests" in runs[tests_index]


def test_workflow_and_autonomous_clone_have_bounded_runtime():
    document, _ = _load()
    job = next(iter(document["jobs"].values()))
    script = UPDATER_SCRIPT.read_text(encoding="utf-8")

    assert 1 <= int(job["timeout-minutes"]) <= 20
    assert "timeout=" in script


def test_updater_rejects_oversized_updates_file_before_unbounded_read(tmp_path):
    updater = _load_updater_module()
    updates = tmp_path / "sites_updated.json"
    updates.write_bytes(b"x" * (updater.MAX_UPDATED_RULES_BYTES + 1))

    with pytest.raises(updater.UpstreamRuleError, match="size"):
        updater._read_updates_bytes(updates)


def test_updater_failure_is_visible_in_workflow_log():
    document, _ = _load()
    updater_step = next(step for step in _steps(document) if step.get("id") == "updater")
    command = _run(updater_step)

    assert re.search(r"\|\s*tee\s+", command)
    assert not re.search(r">\s*[\"']?\$RUNNER_TEMP/sync-result", command)


def test_install_step_uses_plain_validation_dependency_names():
    document, _ = _load()
    install_step = next(
        step
        for step in _steps(document)
        if "Install project" in str(step.get("name", ""))
    )

    assert "pytest" in _run(install_step)
    assert "TEST_RUNNER" not in str(install_step)


def test_pr_branch_is_stable_and_workflow_never_pushes_main_directly():
    document, text = _load()
    steps = _steps(document)
    commands = "\n".join(_run(step) for step in steps)
    env = document.get("env", {})
    branch = env.get("SYNC_BRANCH") or env.get("PR_BRANCH")

    assert isinstance(branch, str) and branch
    assert "${{" not in branch
    assert not re.search(r"(date|time|run_id|run_number|sha)", branch, re.IGNORECASE)
    assert branch != "main"
    assert branch in commands or "$SYNC_BRANCH" in commands or "${SYNC_BRANCH}" in commands
    assert not re.search(r"git\s+push[^\n]*(origin\s+)?main\b", commands)
    assert re.search(r"gh\s+pr\s+(create|edit)", commands)
    assert "base main" in commands or "--base=main" in commands
    assert "@users.noreply.github.com" in text


def test_pr_creation_is_diff_guarded_and_contains_no_personal_machine_details():
    document, text = _load()
    steps = _steps(document)
    pr_step = next(
        step for step in steps if re.search(r"\bgh\s+pr\s+(create|edit)\b", _run(step))
    )
    condition = str(pr_step.get("if", ""))
    commands = "\n".join(_run(step) for step in steps)

    assert condition or re.search(r"git\s+diff\s+--quiet|git\s+status\s+--porcelain", _run(pr_step))
    assert not re.search(r"/Users/|/home/|runner\.workspace", text, re.IGNORECASE)
    assert not re.search(r"git\s+push\s+[^\n]*(?:\s--force(?:\s|$)|\s-f(?:\s|$))", commands)


def test_pr_lifecycle_only_reuses_open_pr_and_creation_failure_is_actionable():
    document, _ = _load()
    pr_step = next(
        step for step in _steps(document) if re.search(r"\bgh\s+pr\s+(create|edit)\b", _run(step))
    )
    command = _run(pr_step)

    assert re.search(r"gh\s+pr\s+(?:list|view)[^\n]*--state\s+open", command)
    assert "Allow GitHub Actions to create and approve pull requests" in command
    assert re.search(r"gh\s+pr\s+create[^\n]*\|\|", command)


def test_action_references_are_pinned_to_full_commit_shas():
    document, _ = _load()
    uses = [str(step.get("uses", "")) for step in _steps(document) if step.get("uses")]

    assert uses
    for action in uses:
        assert re.search(r"@[0-9a-f]{40}$", action), action


def test_checkout_credentials_are_not_persisted_during_validation():
    document, _ = _load()
    steps = _steps(document)
    checkout = next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@"))
    pr_step = next(
        step for step in steps if re.search(r"\bgit\s+push\b", _run(step))
    )
    command = _run(pr_step)

    assert checkout.get("with", {}).get("persist-credentials") in (False, "false")
    assert "gh auth setup-git" in command
    assert command.index("gh auth setup-git") < command.index("git push")
