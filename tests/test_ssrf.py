"""A6 SSRF."""
import pytest

from bpc_fetch.ssrf import SSRFBlocked, assert_public_url


def test_block_localhost():
    with pytest.raises(SSRFBlocked):
        assert_public_url("http://localhost/x")


def test_block_file():
    with pytest.raises(SSRFBlocked):
        assert_public_url("file:///etc/passwd")


def test_block_private_ip():
    with pytest.raises(SSRFBlocked):
        assert_public_url("http://127.0.0.1/")


def test_allow_public_hostname_without_dns(monkeypatch):
    """Hostname path: mock DNS to a public address."""
    import socket

    def fake_getaddrinfo(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert_public_url("https://news.example/article")


def test_fake_ip_bypass(monkeypatch):
    """fake-ip（Surge/Clash 198.18/15）环境：hostname DNS 检查跳过。"""
    import socket

    def fake_getaddrinfo(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("198.18.56.58", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert_public_url("https://example.com/")


def test_proxy_env_still_checks_private_dns(monkeypatch):
    """A proxy env var must not disable hostname SSRF validation."""
    import socket

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))
        ],
    )
    with pytest.raises(SSRFBlocked, match="resolves_private"):
        assert_public_url("https://internal.example/")


def test_proxy_remote_dns_requires_explicit_opt_in(monkeypatch):
    import socket

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("PAC_SSRF_TRUST_PROXY_DNS", "1")

    def fail_dns(*args, **kwargs):
        raise socket.gaierror("local DNS unavailable")

    monkeypatch.setattr(socket, "getaddrinfo", fail_dns)
    assert_public_url("https://public-via-proxy.example/")


def test_ssrf_off_switch(monkeypatch):
    monkeypatch.setenv("PAC_SSRF", "off")
    assert_public_url("http://127.0.0.1/")


def test_literal_ip_still_blocked_with_proxy(monkeypatch):
    """代理环境下字面内网 IP 仍拦截。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    with pytest.raises(SSRFBlocked):
        assert_public_url("http://127.0.0.1/")
