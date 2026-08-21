"""Cookie vault: file backend round-trip and URL resolution."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from bpc_fetch import cookies


@pytest.fixture
def file_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("PAC_COOKIE_DIR", str(tmp_path))
    monkeypatch.setenv("PAC_COOKIE_BACKEND", "file")
    monkeypatch.setattr(cookies, "vault_backend", lambda: "file")
    return tmp_path


def test_store_and_load_roundtrip(file_backend):
    cookies.store("example.com", "token=abc; session=xyz")
    assert cookies.load("example.com") == "token=abc; session=xyz"


def test_load_missing_domain_returns_empty(file_backend):
    assert cookies.load("not-stored.test") == ""


def test_delete_removes_entry(file_backend):
    cookies.store("example.com", "a=1")
    assert cookies.delete("example.com") is True
    assert cookies.load("example.com") == ""
    assert cookies.delete("example.com") is False


def test_store_rejects_empty(file_backend):
    with pytest.raises(ValueError):
        cookies.store("", "a=1")
    with pytest.raises(ValueError):
        cookies.store("example.com", "   ")


def test_list_shows_names_not_values(file_backend):
    cookies.store("example.com", "secretvalue123=hidden; other=x")
    entries = cookies.list_domains()
    assert entries == [{"domain": "example.com", "cookies": "secretvalue123, other"}]
    joined = str(entries)
    assert "hidden" not in joined
    assert "=x" not in joined


def test_cookie_header_for_url_matches_registrable_domain(file_backend, monkeypatch):
    monkeypatch.setattr(
        cookies, "domain_from_url",
        lambda url: "theinformation.com",
    )
    cookies.store("theinformation.com", "token=pro")
    assert cookies.cookie_header_for_url("https://www.theinformation.com/articles/x") == "token=pro"


def test_cookie_header_for_url_miss_returns_empty(file_backend, monkeypatch):
    monkeypatch.setattr(
        cookies, "domain_from_url",
        lambda url: "other.test",
    )
    assert cookies.cookie_header_for_url("https://other.test/a") == ""


def test_vault_files_are_mode_600(file_backend):
    cookies.store("example.com", "a=1")
    st = (file_backend / "example.com.txt").stat()
    assert st.st_mode & 0o777 == 0o600
