import asyncio
from types import SimpleNamespace
import pytest

from bpc_fetch import cli


@pytest.fixture(autouse=True)
def _disable_real_lazy_sync(monkeypatch):
    monkeypatch.setenv("PAC_RULES_AUTO_SYNC", "off")


def _fetch_args(tmp_path, *, full=False, images=False):
    return SimpleNamespace(
        url="https://example.com/article",
        sites_js=None,
        out_dir=tmp_path,
        allow_partial=False,
        archive=False,
        full=full,
        no_browser=False,
        use_browser=None,
        images=images,
    )


def test_fetch_out_dir_writes_full_markdown_from_single_fetch(monkeypatch, tmp_path):
    markdown = "x" * 25_000
    calls = []

    async def fake_fetch_article(url, strategy, **kwargs):
        calls.append((url, kwargs))
        return {
            "ok": True,
            "title": "Test Article",
            "markdown": markdown,
            "content_chars": len(markdown),
            "truncated": False,
            "warnings": [],
        }

    monkeypatch.setattr("bpc_fetch.strategy.fetch_article", fake_fetch_article)
    monkeypatch.setattr(
        "bpc_fetch.rules.store.get_sites_map_with_version",
        lambda sites_js: ({}, "test-version", []),
    )

    result = asyncio.run(cli._cmd_fetch(_fetch_args(tmp_path)))

    expected_path = tmp_path / "test-article" / "test-article.md"
    assert len(calls) == 1
    assert calls[0][1]["full_markdown"] is True
    assert expected_path.read_text(encoding="utf-8") == markdown
    assert result["path"] == str(expected_path)
    assert result["markdown"] == markdown[:20_000]
    assert result["truncated"] is True
    assert result["content_chars"] == len(markdown)


def test_fetch_images_opt_in_downloads_once_and_reports_saved_count(monkeypatch, tmp_path):
    image_urls = ["https://example.com/one.jpg", "https://example.com/two.jpg"]
    download_calls = []

    async def fake_fetch_article(url, strategy, **kwargs):
        return {
            "ok": True,
            "title": "Image Article",
            "markdown": "body",
            "warnings": [],
            "_image_urls": image_urls,
        }

    async def fake_download_images(urls, out_dir):
        download_calls.append((urls, out_dir))
        return [out_dir / "one.jpg"]

    monkeypatch.setattr("bpc_fetch.strategy.fetch_article", fake_fetch_article)
    monkeypatch.setattr("bpc_fetch.extract.download_images", fake_download_images)
    monkeypatch.setattr(
        "bpc_fetch.rules.store.get_sites_map_with_version",
        lambda sites_js: ({}, "test-version", []),
    )

    result = asyncio.run(cli._cmd_fetch(_fetch_args(tmp_path, images=True)))

    article_dir = tmp_path / "image-article"
    assert download_calls == [(image_urls, article_dir / "images")]
    assert result["images"] == 1
    assert "_image_urls" not in result


def test_fetch_default_does_not_download_images_or_leak_private_urls(monkeypatch, tmp_path):
    download_calls = []

    async def fake_fetch_article(url, strategy, **kwargs):
        return {
            "ok": True,
            "title": "No Images Article",
            "markdown": "body",
            "warnings": [],
            "_image_urls": ["https://example.com/image.jpg"],
        }

    async def fake_download_images(urls, out_dir):
        download_calls.append((urls, out_dir))
        return []

    monkeypatch.setattr("bpc_fetch.strategy.fetch_article", fake_fetch_article)
    monkeypatch.setattr("bpc_fetch.extract.download_images", fake_download_images)
    monkeypatch.setattr(
        "bpc_fetch.rules.store.get_sites_map_with_version",
        lambda sites_js: ({}, "test-version", []),
    )

    result = asyncio.run(cli._cmd_fetch(_fetch_args(tmp_path)))

    assert download_calls == []
    assert result["images"] == 0
    assert "_image_urls" not in result


def test_fetch_images_without_out_dir_warns_and_does_not_download(monkeypatch):
    download_calls = []

    async def fake_fetch_article(url, strategy, **kwargs):
        return {
            "ok": True,
            "title": "No Output Directory",
            "markdown": "body",
            "warnings": [],
            "_image_urls": ["https://example.com/image.jpg"],
        }

    async def fake_download_images(urls, out_dir):
        download_calls.append((urls, out_dir))
        return []

    monkeypatch.setattr("bpc_fetch.strategy.fetch_article", fake_fetch_article)
    monkeypatch.setattr("bpc_fetch.extract.download_images", fake_download_images)
    monkeypatch.setattr(
        "bpc_fetch.rules.store.get_sites_map_with_version",
        lambda sites_js: ({}, "test-version", []),
    )

    result = asyncio.run(cli._cmd_fetch(_fetch_args(None, images=True)))

    assert download_calls == []
    assert result["images"] == 0
    assert "images_requires_out_dir" in result["warnings"]
    assert "_image_urls" not in result


def test_batch_out_dir_writes_only_successes_without_refetching(monkeypatch, tmp_path):
    markdown = "y" * 3_000
    urls = ["https://example.com/good", "https://example.com/bad"]
    calls = []

    async def fake_fetch_article(url, strategy, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/bad"):
            return {
                "ok": False,
                "title": "Failed Article",
                "markdown": "failure body",
                "content_chars": 12,
                "truncated": False,
                "path": None,
                "_image_urls": ["https://example.com/failed-image.jpg"],
            }
        return {
            "ok": True,
            "title": "Good Article",
            "markdown": markdown,
            "content_chars": len(markdown),
            "truncated": False,
            "path": None,
            "_image_urls": ["https://example.com/image.jpg"],
        }

    monkeypatch.setattr("bpc_fetch.strategy.fetch_article", fake_fetch_article)
    monkeypatch.setattr(
        "bpc_fetch.rules.store.get_sites_map_with_version",
        lambda sites_js: ({}, "test-version", []),
    )
    args = SimpleNamespace(
        urls=urls,
        file=None,
        out_dir=tmp_path,
        concurrency=2,
        max=10,
        allow_partial=False,
        full=False,
        sites_js=None,
    )

    result = asyncio.run(cli._cmd_batch(args))

    expected_path = tmp_path / "good-article" / "good-article.md"
    assert [url for url, _ in calls] == urls
    assert all(kwargs["full_markdown"] is True for _, kwargs in calls)
    assert expected_path.read_text(encoding="utf-8") == markdown
    assert result["results"][0]["path"] == str(expected_path)
    assert result["results"][0]["markdown"] == markdown[:2_000]
    assert result["results"][0]["truncated"] is True
    assert result["results"][0]["content_chars"] == len(markdown)
    assert result["results"][1]["path"] is None
    assert all("_image_urls" not in item for item in result["results"])
    assert list(tmp_path.rglob("*.md")) == [expected_path]


def test_fetch_image_flags_share_one_default_false_destination(monkeypatch, capsys):
    parsed = []

    async def fake_dispatch(args):
        parsed.append(args)
        return {"ok": True}

    monkeypatch.setattr(cli, "_dispatch", fake_dispatch)

    for argv in (
        ["pac", "fetch", "https://example.com/article"],
        ["pac", "fetch", "https://example.com/article", "--images"],
        ["pac", "fetch", "https://example.com/article", "--no-images"],
    ):
        monkeypatch.setattr("sys.argv", argv)
        cli.main()

    capsys.readouterr()
    assert [args.images for args in parsed] == [False, True, False]
    assert all(not hasattr(args, "no_images") for args in parsed)


def test_fetch_lazy_sync_runs_once_before_sites_load_and_merges_unique_warnings(monkeypatch):
    events = []
    async def fake_fetch_article(*args, **kwargs):
        return {"ok": True, "title": "x", "markdown": "body", "warnings": ["same"]}
    monkeypatch.setattr("bpc_fetch.strategy.fetch_article", fake_fetch_article)
    monkeypatch.setattr(
        "bpc_fetch.rules.sync.maybe_sync_rules",
        lambda **kwargs: events.append(("sync", kwargs)) or {"warnings": ["same", "sync_failed"]},
    )
    monkeypatch.setattr(
        "bpc_fetch.rules.store.get_sites_map_with_version",
        lambda sites_js: events.append(("load", sites_js)) or ({}, "v", ["store"]),
    )

    result = asyncio.run(cli._cmd_fetch(_fetch_args(None)))

    assert [event[0] for event in events] == ["sync", "load"]
    assert result["warnings"] == ["same", "sync_failed", "store", "rule_missing"]


def test_batch_no_rule_sync_is_forwarded_once(monkeypatch):
    calls = []
    async def fake_fetch_article(*args, **kwargs):
        return {"ok": True, "markdown": "body"}
    monkeypatch.setattr("bpc_fetch.strategy.fetch_article", fake_fetch_article)
    monkeypatch.setattr(
        "bpc_fetch.rules.sync.maybe_sync_rules",
        lambda **kwargs: calls.append(kwargs) or {"warnings": []},
    )
    monkeypatch.setattr(
        "bpc_fetch.rules.store.get_sites_map_with_version",
        lambda sites_js: ({}, "v", []),
    )
    args = SimpleNamespace(urls=["https://example.com/a"], file=None, out_dir=None,
                           concurrency=1, max=10, allow_partial=False, full=False,
                           sites_js=None, no_rule_sync=True)

    result = asyncio.run(cli._cmd_batch(args))

    assert result["ok"] is True
    assert calls == [{"sites_js": None, "disabled": True}]


def test_fetch_and_batch_parse_no_rule_sync(monkeypatch, capsys):
    parsed = []
    async def fake_dispatch(args):
        parsed.append(args)
        return {"ok": True}
    monkeypatch.setattr(cli, "_dispatch", fake_dispatch)
    for argv in (["pac", "fetch", "https://example.com/a", "--no-rule-sync"],
                 ["pac", "batch", "https://example.com/a", "--no-rule-sync"]):
        monkeypatch.setattr("sys.argv", argv)
        cli.main()
    capsys.readouterr()
    assert [args.no_rule_sync for args in parsed] == [True, True]


def test_doctor_uses_browser_launch_probe_result(monkeypatch):
    calls = []

    async def fake_ensure_browser():
        calls.append(True)
        return {
            "ok": False,
            "error": "Executable doesn't exist",
            "install_cmd": "playwright install chromium",
        }

    monkeypatch.setattr("bpc_fetch.browser.ensure_browser", fake_ensure_browser)
    monkeypatch.setattr(
        "bpc_fetch.rules.store.get_sites_map_with_version",
        lambda sites_js: ({}, "test-version", []),
    )
    monkeypatch.setattr("bpc_fetch.rules.store.load_manifest", lambda: {})

    result = asyncio.run(
        cli._dispatch(SimpleNamespace(command="doctor", sites_js=None))
    )

    assert calls == [True]
    assert result["playwright"] is True
    assert result["chromium_installed"] is False
    assert "Executable doesn't exist" in result["issues"]
    assert "playwright install chromium" in result["issues"]
