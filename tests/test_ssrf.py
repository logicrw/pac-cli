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
