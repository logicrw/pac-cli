"""Deterministic mirror of the official Bypass Paywalls Clean rule snapshots."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..sites import _extract_entries, entries_to_domain_map

OFFICIAL_MASTER_ARCHIVE_URL = (
    "https://gitflic.ru/project/magnolia1234/bpc_uploads/blob/raw?file="
    "bypass-paywalls-chrome-clean-master.zip"
)
UPDATES_REPOSITORY_URL = "https://gitflic.ru/project/magnolia1234/bpc_updates.git"
MAX_SITES_JS_BYTES = 5_000_000
MAX_MANIFEST_JSON_BYTES = 1_000_000
MAX_ARCHIVE_MEMBERS = 1_000
MAX_ARCHIVE_COMPRESSION_RATIO = 200


class UpstreamRuleError(RuntimeError):
    """An upstream snapshot is malformed or unsafe to mirror."""


@dataclass(frozen=True)
class BaseArchive:
    sites_js: bytes
    extension_version: str | None
    domain_count: int


@dataclass(frozen=True)
class SanitizedSites:
    content: bytes
    marker: str
    removed_count: int


@dataclass(frozen=True)
class Candidate:
    sites_js: bytes
    updated_json: bytes
    manifest_json: bytes
    extension_version: str | None
    updates_commit: str
    domain_count: int
    updated_entry_count: int
    sanitation_marker: str
    sanitation_removed_count: int


@dataclass(frozen=True)
class UpdateResult:
    changed: bool
    changed_paths: tuple[str, ...]
    candidate: Candidate


def _domain_count(content: bytes) -> int:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UpstreamRuleError("sites.js is not valid UTF-8") from exc
    text = re.sub(r"^var defaultSites\s*=\s*", "", text.strip())
    text = re.sub(r";\s*$", "", text)
    text = re.sub(r"^var grouped_sites\s*=\s*\{.*?\};\s*", "", text, flags=re.DOTALL)
    try:
        domains = entries_to_domain_map(_extract_entries(text))
    except Exception as exc:
        raise UpstreamRuleError("sites.js cannot be parsed") from exc
    if not domains:
        raise UpstreamRuleError("sites.js contains no usable domain")
    return len(domains)


def _validate_archive_member(
    info: zipfile.ZipInfo, *, label: str, max_bytes: int
) -> None:
    if info.flag_bits & 0x1:
        raise UpstreamRuleError(f"encrypted {label} is not supported")
    if info.file_size > max_bytes:
        raise UpstreamRuleError(f"{label} exceeds the permitted size limit")
    if info.file_size and (
        info.compress_size == 0
        or info.file_size > info.compress_size * MAX_ARCHIVE_COMPRESSION_RATIO
    ):
        raise UpstreamRuleError(f"{label} has a suspicious compression ratio")


def parse_base_archive(content: bytes) -> BaseArchive:
    """Read the required members directly from an in-memory ZIP."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if len(archive.infolist()) > MAX_ARCHIVE_MEMBERS:
                raise UpstreamRuleError("archive contains too many members")
            sites_members = [name for name in archive.namelist() if Path(name).name == "sites.js"]
            if len(sites_members) != 1:
                raise UpstreamRuleError("archive must contain exactly one sites.js")
            _validate_archive_member(
                archive.getinfo(sites_members[0]),
                label="sites.js",
                max_bytes=MAX_SITES_JS_BYTES,
            )
            sites_js = archive.read(sites_members[0])
            # The extension manifest lives beside the selected sites.js; archives may
            # legitimately include additional manifests for custom/MV2 builds.
            sites_parent = Path(sites_members[0]).parent
            sibling_manifests = [
                name for name in archive.namelist()
                if Path(name).name == "manifest.json" and Path(name).parent == sites_parent
            ]
            all_manifests = [name for name in archive.namelist() if Path(name).name == "manifest.json"]
            manifests = sibling_manifests or (all_manifests if len(all_manifests) == 1 else [])
            version = None
            if len(manifests) > 1:
                raise UpstreamRuleError("archive contains multiple root manifest.json files")
            if manifests:
                _validate_archive_member(
                    archive.getinfo(manifests[0]),
                    label="manifest.json",
                    max_bytes=MAX_MANIFEST_JSON_BYTES,
                )
                try:
                    manifest = json.loads(archive.read(manifests[0]).decode("utf-8"))
                    raw_version = manifest.get("version") if isinstance(manifest, dict) else None
                    version = str(raw_version) if raw_version is not None else None
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise UpstreamRuleError("invalid manifest.json") from exc
    except (zipfile.BadZipFile, OSError) as exc:
        raise UpstreamRuleError("invalid upstream ZIP archive") from exc
    return BaseArchive(sites_js, version, _domain_count(sites_js))


def _reject_credential_markers(text: str, *, allow_access_token: bool) -> None:
    if re.search(r"-----BEGIN (?:[A-Z ]* )?PRIVATE KEY-----", text, re.I):
        raise UpstreamRuleError("private credential material found in rules")
    if re.search(
        r"[\"']?Authorization[\"']?\s*:\s*[\"']?Bearer\s+",
        text,
        re.I,
    ):
        raise UpstreamRuleError("Bearer credential found in rules")
    if re.search(
        r"\b(?:"
        r"(?:AKIA|ASIA)[0-9A-Z]{16}|"
        r"gh[pousr]_[A-Za-z0-9]{12,}|"
        r"github_pat_[A-Za-z0-9_]{12,}|"
        r"glpat-[A-Za-z0-9_-]{12,}|"
        r"npm_[A-Za-z0-9]{12,}|"
        r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{12,}|"
        r"xox[baprs]-[A-Za-z0-9-]{12,}|"
        r"xapp-[A-Za-z0-9-]{12,}|"
        r"sk-(?:proj-)?[A-Za-z0-9_-]{12,}"
        r")",
        text,
    ):
        raise UpstreamRuleError("secret credential token prefix found in rules")
    if not allow_access_token and "x-access-token" in text.lower():
        raise UpstreamRuleError("x-access-token credential found in updated rules")


def _is_isolated_cs_param_line(line: str) -> bool:
    match = re.match(r"^\s*cs_param\s*:\s*", line, re.I)
    if not match:
        return False
    value = line[match.end():].rstrip("\r\n")
    if not value.startswith("{"):
        return False

    pairs = {"{": "}", "[": "]", "(": ")"}
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in ("'", '"', "`"):
            quote = character
        elif character in pairs:
            stack.append(pairs[character])
        elif character in pairs.values():
            if not stack or character != stack.pop():
                return False
            if not stack:
                suffix = value[index + 1:]
                return re.fullmatch(r"\s*[,;]?\s*(?://[^\r\n]*)?", suffix) is not None
    return False


def parse_updated_rules(content: bytes) -> dict:
    try:
        text = content.decode("utf-8")
        _reject_credential_markers(text, allow_access_token=False)
        value = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpstreamRuleError("updated rules must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise UpstreamRuleError("updated rules must be a top-level mapping")
    if any(not isinstance(entry, dict) for entry in value.values()):
        raise UpstreamRuleError("each updated rule must be a mapping")
    return value


def sanitize_sites_js(content: bytes) -> SanitizedSites:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UpstreamRuleError("sites.js is not valid UTF-8") from exc
    _reject_credential_markers(text, allow_access_token=True)

    kept: list[str] = []
    removed = 0
    for line in text.splitlines(keepends=True):
        if "x-access-token" not in line.lower():
            kept.append(line)
            continue
        # Only an isolated, whole cs_param property line may be discarded.
        if _is_isolated_cs_param_line(line):
            removed += 1
        else:
            raise UpstreamRuleError("ambiguous x-access-token structure in sites.js")
    # Git snapshots use stable LF endings even when upstream mixes CRLF and LF.
    result = "".join(kept).replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    if b"x-access-token" in result.lower():
        raise UpstreamRuleError("x-access-token remained after sanitation")
    return SanitizedSites(result, "x-access-token", removed)


def _canonical_updated(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def build_candidate(*, base_archive: bytes, updated_rules: bytes, updates_commit: str,
                    existing_domain_count: int) -> Candidate:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", updates_commit):
        raise UpstreamRuleError("updates commit must be a 40-character Git commit hash")
    base = parse_base_archive(base_archive)
    sanitized = sanitize_sites_js(base.sites_js)
    count = _domain_count(sanitized.content)
    if existing_domain_count and count * 100 < existing_domain_count * 80:
        raise UpstreamRuleError("catastrophic shrink guard: candidate is below 80% of existing domains")
    updated = parse_updated_rules(updated_rules)
    updated_json = _canonical_updated(updated)
    manifest = {
        "schema_version": 1,
        "upstream_extension_version": base.extension_version,
        "updates_source_commit": updates_commit,
        "sites_js": {"sha256": hashlib.sha256(sanitized.content).hexdigest(), "domain_count": count},
        "sites_updated_json": {"sha256": hashlib.sha256(updated_json).hexdigest(), "entry_count": len(updated)},
        "sanitation": {"marker": sanitized.marker, "removed_count": sanitized.removed_count},
    }
    manifest_json = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return Candidate(sanitized.content, updated_json, manifest_json, base.extension_version,
                     updates_commit, count, len(updated), sanitized.marker, sanitized.removed_count)


def atomic_write_snapshots(root: Path, snapshots: dict[str, bytes]) -> None:
    """Prepare sibling temporary files, then atomically replace each destination."""
    prepared: list[tuple[Path, Path]] = []
    try:
        for relative, content in snapshots.items():
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
            temporary = Path(name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            prepared.append((temporary, destination))
        for temporary, destination in prepared:
            os.replace(temporary, destination)
    finally:
        for temporary, _ in prepared:
            temporary.unlink(missing_ok=True)


def update_mirror(*, root: Path, base_archive: bytes, updated_rules: bytes,
                  updates_commit: str,
                  atomic_writer: Callable[[Path, dict[str, bytes]], None] = atomic_write_snapshots) -> UpdateResult:
    root = Path(root)
    current_sites = root / "data/sites.js"
    existing_count = _domain_count(current_sites.read_bytes()) if current_sites.exists() else 0
    candidate = build_candidate(base_archive=base_archive, updated_rules=updated_rules,
                                updates_commit=updates_commit, existing_domain_count=existing_count)
    snapshots = {
        "data/sites.js": candidate.sites_js,
        "data/sites_updated.json": candidate.updated_json,
        "data/upstream-manifest.json": candidate.manifest_json,
    }
    changed = tuple(path for path, body in snapshots.items()
                    if not (root / path).exists() or (root / path).read_bytes() != body)
    if changed:
        atomic_writer(root, snapshots)
    return UpdateResult(bool(changed), changed, candidate)
