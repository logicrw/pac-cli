"""SSRF guard (§15.3 A6): block private hosts; re-check redirects hop-by-hop.

环境适配：
- PAC_SSRF=off 可整体关闭（本机/受信环境调试用，默认开启）
- fake-ip 网段（Surge/Clash 的 198.18.0.0/15）：全部解析结果落在 fake-ip
  段时同样说明 DNS 在远端，跳过 hostname 的私网检查
- 若代理要求“只在远端解析”且本机 DNS 无法解析，可显式设置
  PAC_SSRF_TRUST_PROXY_DNS=1。默认仍 fail-closed，避免 NO_PROXY / 代理绕过 SSRF。
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

_FAKE_IP_NETWORKS = (ipaddress.ip_network("198.18.0.0/15"),)

_PROXY_ENV_VARS = (
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
    "ALL_PROXY", "all_proxy",
)


class SSRFBlocked(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _ssrf_disabled() -> bool:
    return os.environ.get("PAC_SSRF", "").strip().lower() in ("0", "off", "false", "no")


def _proxy_configured() -> bool:
    return any(os.environ.get(v) for v in _PROXY_ENV_VARS)


def _trust_proxy_dns() -> bool:
    return os.environ.get("PAC_SSRF_TRUST_PROXY_DNS", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _is_fake_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _FAKE_IP_NETWORKS)


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
    if _ssrf_disabled():
        return
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
    # Hostnames are resolved locally even when a proxy exists.  Merely having
    # HTTPS_PROXY set is not a security boundary: NO_PROXY can still send the
    # request directly, and a remote proxy may itself be able to reach private
    # addresses.  The only fail-open mode is an explicit opt-in for environments
    # whose hostnames genuinely cannot be resolved locally.
    try:
        infos = socket.getaddrinfo(h, None)
    except socket.gaierror as e:
        if _proxy_configured() and _trust_proxy_dns():
            return
        raise SSRFBlocked(f"dns_failed:{h}:{e}") from e
    ips = [info[4][0] for info in infos]
    if ips and all(_is_fake_ip(ip) for ip in ips):
        return  # fake-ip：真实解析发生在代理远端
    for ip in ips:
        if _is_private_ip(ip):
            raise SSRFBlocked(f"resolves_private:{h}->{ip}")
