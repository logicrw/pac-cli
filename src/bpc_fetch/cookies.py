"""Cookie vault: per-site session cookies stored outside the repository.

Subscription sites (theinformation.com, wsj.com, ...) require an authenticated
session that no engine can synthesize.  The vault stores one cookie header per
registrable domain under the user data directory, with two storage backends:

- ``file`` (default): plain ``<domain>.txt`` containing the raw Cookie header.
  The directory carries ``0700`` permissions; cookies never enter the repo.
- ``keychain`` (macOS): stores the header in the login keychain under the
  service name ``pac-cli cookie vault``.

Cookies are injected only when the fetch target's registrable domain matches
the vault entry, and only toward that domain plus archive.today mirrors when
the archive path is taken (mirrors serve the same snapshot).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .sites import domain_from_url

try:
    from platformdirs import user_data_dir
except ImportError:  # pragma: no cover
    def user_data_dir(appname: str, appauthor: str | None = None) -> str:
        return str(Path.home() / ".local" / "share" / appname)


KEYCHAIN_SERVICE = "pac-cli cookie vault"


def vault_root() -> Path:
    """Directory holding per-domain cookie files (created 0700)."""
    override = os.environ.get("PAC_COOKIE_DIR", "").strip()
    root = Path(override).expanduser() if override else Path(
        user_data_dir("pac-cli", "pac-cli")
    ) / "cookies"
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def vault_backend() -> str:
    value = os.environ.get("PAC_COOKIE_BACKEND", "").strip().lower()
    if value in {"file", "keychain"}:
        return value
    return "keychain" if sys.platform == "darwin" else "file"


def _keychain_args(domain: str) -> list[str]:
    return ["-s", KEYCHAIN_SERVICE, "-a", domain]


def store(domain: str, header: str) -> Path | None:
    """Persist a cookie header for a registrable domain.

    Returns the vault file path for the ``file`` backend, ``None`` otherwise.
    """
    domain = domain.strip().casefold().lstrip(".")
    header = header.strip()
    if not domain or not header:
        raise ValueError("domain and header must be non-empty")

    if vault_backend() == "keychain":
        result = subprocess.run(
            ["security", "add-generic-password", "-w", header, "-U", *_keychain_args(domain)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"keychain write failed: {result.stderr.strip()}")
        return None

    path = vault_root() / f"{domain}.txt"
    path.write_text(header + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def load(domain: str) -> str:
    """Return the stored cookie header for a domain, or ``''``."""
    domain = domain.strip().casefold().lstrip(".")
    if not domain:
        return ""

    if vault_backend() == "keychain":
        result = subprocess.run(
            ["security", "find-generic-password", "-w", *_keychain_args(domain)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    path = vault_root() / f"{domain}.txt"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def delete(domain: str) -> bool:
    """Remove a domain entry. Returns True when something was removed."""
    domain = domain.strip().casefold().lstrip(".")

    if vault_backend() == "keychain":
        result = subprocess.run(
            ["security", "delete-generic-password", *_keychain_args(domain)],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    path = vault_root() / f"{domain}.txt"
    if path.exists():
        path.unlink()
        return True
    return False


def list_domains() -> list[dict[str, str]]:
    """List vault entries with a non-secret preview (cookie count, names only)."""
    entries: list[dict[str, str]] = []

    if vault_backend() == "keychain":
        dump = subprocess.run(
            ["security", "dump-keychain"],
            capture_output=True,
            text=True,
        )
        if dump.returncode != 0:
            return entries
        import re

        # Dump structure per entry:
        #   0x00000007 <blob>="<service>"   (or "svce"<blob>= on older dumps)
        #   0x00000008 <blob>=<NULL>
        #   "acct"<blob>="<account/domain>"
        block = re.compile(
            r'(?:0x00000007 <blob>|"svce"<blob>)="' + re.escape(KEYCHAIN_SERVICE)
            + r'"\s*\n\s*(?:0x00000008 <blob>=<NULL>\s*\n\s*)?"acct"<blob>="([^"]+)"'
        )
        for match in block.finditer(dump.stdout):
            domain = match.group(1)
            header = load(domain)
            entries.append(_preview(domain, header))
        return entries

    for path in sorted(vault_root().glob("*.txt")):
        domain = path.stem
        header = path.read_text(encoding="utf-8").strip()
        entries.append(_preview(domain, header))
    return entries


def _preview(domain: str, header: str) -> dict[str, str]:
    names = [part.split("=", 1)[0].strip() for part in header.split(";") if "=" in part]
    return {"domain": domain, "cookies": ", ".join(names)}


def cookie_header_for_url(url: str) -> str:
    """Resolve the vault cookie header matching a URL's registrable domain."""
    try:
        domain = domain_from_url(url)
    except Exception:
        return ""
    if not domain:
        return ""
    return load(domain)
