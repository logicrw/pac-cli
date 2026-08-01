#!/usr/bin/env python3
"""Autonomously refresh the repository's central BPC upstream mirror."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from bpc_fetch.rules.sync import download_bytes
from bpc_fetch.rules.upstream import (
    OFFICIAL_MASTER_ARCHIVE_URL,
    UPDATES_REPOSITORY_URL,
    UpstreamRuleError,
    update_mirror,
)

MAX_UPDATED_RULES_BYTES = 1_000_000


def _read_updates_bytes(path: Path) -> bytes:
    with path.open("rb") as stream:
        content = stream.read(MAX_UPDATED_RULES_BYTES + 1)
    if len(content) > MAX_UPDATED_RULES_BYTES:
        raise UpstreamRuleError("sites_updated.json exceeds the permitted size limit")
    return content


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive-file", type=Path)
    parser.add_argument("--updates-file", type=Path)
    parser.add_argument("--updates-commit")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--compact", action="store_true")
    return parser.parse_args()


def _clone_updates(directory: Path) -> tuple[bytes, str]:
    checkout = directory / "bpc_updates"
    subprocess.run(
        [
            "git", "clone", "--depth", "1", "--no-tags", "--branch", "main",
            UPDATES_REPOSITORY_URL, str(checkout),
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        timeout=120,
    )
    commit = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    return _read_updates_bytes(checkout / "sites_updated.json"), commit


def main() -> int:
    args = _arguments()
    try:
        if args.updates_file and not args.updates_commit:
            raise UpstreamRuleError("--updates-file requires --updates-commit")
        if args.updates_commit and not args.updates_file:
            raise UpstreamRuleError("--updates-commit requires --updates-file")
        archive = args.archive_file.read_bytes() if args.archive_file else download_bytes(
            OFFICIAL_MASTER_ARCHIVE_URL, max_bytes=10_000_000
        )
        if args.updates_file:
            updates, commit = _read_updates_bytes(args.updates_file), args.updates_commit
        else:
            with tempfile.TemporaryDirectory(prefix="bpc-updates-") as temporary:
                updates, commit = _clone_updates(Path(temporary))
        result = update_mirror(root=args.root, base_archive=archive,
                               updated_rules=updates, updates_commit=commit)
        candidate = result.candidate
        output = {
            "ok": True,
            "changed": result.changed,
            "changed_paths": list(result.changed_paths),
            "extension_version": candidate.extension_version,
            "updates_commit": candidate.updates_commit,
            "domain_count": candidate.domain_count,
            "updated_entry_count": candidate.updated_entry_count,
            "sites_js_sha256": json.loads(candidate.manifest_json)["sites_js"]["sha256"],
            "sites_updated_sha256": json.loads(candidate.manifest_json)["sites_updated_json"]["sha256"],
            "sanitation": {"marker": candidate.sanitation_marker,
                           "removed_count": candidate.sanitation_removed_count},
        }
        print(json.dumps(output, separators=(",", ":") if args.compact else None,
                         indent=None if args.compact else 2))
        return 0
    except (OSError, subprocess.SubprocessError, UpstreamRuleError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)},
                         separators=(",", ":") if args.compact else None,
                         indent=None if args.compact else 2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
