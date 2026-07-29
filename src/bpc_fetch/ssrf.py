"""SSRF guard (§15.3 A6): block private hosts; re-check redirects hop-by-hop."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class SSRFBlocked(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False  # not an IP literal
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def assert_public_url(url: str) -> None:
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme in ("file", "ftp", "data", "javascript"):
        raise SSRFBlocked(f"blocked_scheme:{scheme}")
    if scheme not in ("http", "https"):
        raise SSRFBlocked(f"unsupported_scheme:{scheme or 'none'}")
    host = parsed.hostname or ""
    if not host:
        raise SSRFBlocked("empty_host")
    h = host.lower().rstrip(".")
    if h in ("localhost", "localhost.localdomain") or h.endswith(".localhost"):
        raise SSRFBlocked("localhost")
    if h == "0.0.0.0":
        raise SSRFBlocked("unspecified")
    # literal IP
    try:
        ipaddress.ip_address(h)
    except ValueError:
        pass  # hostname — resolve below
    else:
        if _is_private_ip(h):
            raise SSRFBlocked(f"private_ip:{h}")
        return
    # resolve DNS
    try:
        infos = socket.getaddrinfo(h, None)
    except socket.gaierror as e:
        raise SSRFBlocked(f"dns_failed:{h}:{e}") from e
    for info in infos:
        ip = info[4][0]
        if _is_private_ip(ip):
            raise SSRFBlocked(f"resolves_private:{h}->{ip}")
