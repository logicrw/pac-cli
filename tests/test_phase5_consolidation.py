"""Phase 5 contract tests for opt-in diagnostics and frozen default envelopes."""
from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace

import httpx

from bpc_fetch import cli, strategy
from bpc_fetch.discover import discover_articles
from bpc_fetch.quality import QualityResult
from bpc_fetch.result import build_diagnostics, ok_result


def test_build_diagnostics_has_stable_engine_attempt_and_quality_shape() -> None:
    quality = QualityResult(
        ok=True,
        paywall_suspected=False,
        reason="pass",
        score=0.81,
        metrics={"content_chars": 1200, "link_density": 0.03},
    )
    diagnostics = build_diagnostics(
        request_id="req-1",
        total_latency_ms=42,
        attempts=[
            {"handler": "DirectHttpHandler", "label": "http_primary", "engine": "http", "status": 200, "elapsed_ms": 12},
            {"handler": "StealthBrowserHandler", "label": "browser_cleanup", "engine": "camoufox", "status": 200, "elapsed_ms": 25},
        ],
        quality=quality,
    )
    assert diagnostics["request_id"] == "req-1"
    assert diagnostics["total_latency_ms"] == 42
    assert diagnostics["engine_timings_ms"] == {"http": 12, "camoufox": 25}
    assert diagnostics["attempts"][0]["label"] == "http_primary"
    assert diagnostics["quality"]["score"] == 0.81
    assert diagnostics["quality"]["components"]["content_chars"] == 1200


def test_fetch_article_diagnostics_are_opt_in(monkeypatch) -> None:
    class FakePipeline:
        async def run(self, context):
            started = time.perf_counter()
            context.last_quality = QualityResult(
                ok=True,
                paywall_suspected=False,
                reason="pass",
                score=0.9,
                metrics={"content_chars": 900},
            )
            context.record_attempt(
                handler="DirectHttpHandler",
                label="http_primary",
                engine="http",
                status=200,
                started_at=started,
                quality_reason="pass",
            )
            return ok_result(
                url=context.url,
                domain=context.domain,
                title="Article",
                markdown="body",
                engine="http",
                full_markdown=True,
            )

    monkeypatch.setattr(strategy, "assert_public_url", lambda url: None)
    monkeypatch.setattr(strategy, "_build_pipeline", lambda plan: FakePipeline())

    plain = asyncio.run(
        strategy.fetch_article(
            "https://example.com/a", client=object(), full_markdown=True
        )
    )
    detailed = asyncio.run(
        strategy.fetch_article(
            "https://example.com/a",
            client=object(),
            full_markdown=True,
            diagnostics=True,
            request_id="req-fetch",
        )
    )
    assert "diagnostics" not in plain
    assert detailed["diagnostics"]["request_id"] == "req-fetch"
    assert detailed["diagnostics"]["engine_timings_ms"] == {"http": 0}
    assert detailed["diagnostics"]["quality"]["components"]["content_chars"] == 900


def test_cli_parses_diagnostics_only_for_fetch_batch_and_discover(monkeypatch, capsys) -> None:
    parsed = []

    async def fake_dispatch(args):
        parsed.append(args)
        return {"ok": True}

    monkeypatch.setattr(cli, "_dispatch", fake_dispatch)
    for argv in (
        ["pac", "fetch", "https://example.com/a", "--diagnostics"],
        ["pac", "batch", "https://example.com/a", "--diagnostics"],
        ["pac", "discover", "example.com", "--diagnostics"],
    ):
        monkeypatch.setattr("sys.argv", argv)
        cli.main()

    capsys.readouterr()
    assert [args.command for args in parsed] == ["fetch", "batch", "discover"]
    assert all(args.diagnostics is True for args in parsed)


def test_discover_diagnostics_include_probe_history(monkeypatch) -> None:
    rss = """<?xml version="1.0"?><rss><channel><item><title>One</title><link>https://example.com/one</link></item></channel></rss>"""

    async def fake_safe_get(client, url, **kwargs):
        request = httpx.Request("GET", url)
        return httpx.Response(200, headers={"content-type": "application/rss+xml"}, text=rss, request=request)

    monkeypatch.setattr("bpc_fetch.discover._safe_get", fake_safe_get)
    result = asyncio.run(
        discover_articles(
            "https://8.8.8.8/feed",
            diagnostics=True,
            request_id="req-discover",
        )
    )
    assert result["ok"] is True
    assert result["diagnostics"]["request_id"] == "req-discover"
    assert result["diagnostics"]["attempts"][0]["label"] == "base"
    assert result["diagnostics"]["engine_timings_ms"].keys() == {"discover_http"}
    assert result["diagnostics"]["quality"]["evaluated"] is False



def test_owned_lock_write_failure_cleans_exclusive_lock(tmp_path, monkeypatch) -> None:
    from bpc_fetch.rules import sync

    lock_path = tmp_path / "sync.lock"
    monkeypatch.setattr(sync.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync failed")))
    try:
        sync._acquire_owned_lock(lock_path)
    except OSError as exc:
        assert "fsync failed" in str(exc)
    else:
        raise AssertionError("lock acquisition must surface a failed fsync")
    assert lock_path.exists() is False


def test_owned_lock_has_hard_stale_ceiling_even_if_pid_is_alive(tmp_path) -> None:
    from bpc_fetch.rules import sync

    lock_path = tmp_path / "sync.lock"
    lock_path.write_text(
        json.dumps({"owner_pid": os.getpid(), "token": "old", "created_at": "old"}),
        encoding="utf-8",
    )
    old = time.time() - sync.DEFAULT_LOCK_HARD_STALE_SECONDS - 60
    os.utime(lock_path, (old, old))
    assert sync._lock_is_stale(lock_path, stale_seconds=30) is True


def test_browser_page_cleanup_survives_cancellation_and_releases_lease(monkeypatch) -> None:
    from bpc_fetch import browser

    cleanup_started = asyncio.Event()
    cleanup_continue = asyncio.Event()

    class FakePage:
        def set_default_timeout(self, value):
            return None

        def set_default_navigation_timeout(self, value):
            return None

        async def evaluate(self, script):
            cleanup_started.set()
            await cleanup_continue.wait()

        async def close(self):
            return None

    class FakeContext:
        async def new_page(self):
            return FakePage()

        async def clear_permissions(self):
            return None

        async def clear_cookies(self):
            return None

        async def close(self):
            return None

    async def exercise() -> None:
        pool = browser.BrowserPool(max_contexts=1)
        pool._started = True
        pool._browser = object()
        key = browser._ContextKey(
            domain="example.com",
            user_agent="ua",
            headers=(),
            proxy=None,
            locale="en-US",
            timezone_id="UTC",
            viewport_width=1000,
            viewport_height=700,
            allow_cookies=False,
            explicit_user_agent=False,
            explicit_locale=False,
            explicit_timezone=False,
            explicit_viewport=False,
        )
        entry = browser._IdleContext(1, key, FakeContext(), 0.0)
        monkeypatch.setattr(pool, "_context_key", lambda *args, **kwargs: (key, {}, None))
        monkeypatch.setattr(
            pool,
            "_acquire_context",
            lambda *args, **kwargs: asyncio.sleep(0, result=entry),
        )

        async def holder() -> None:
            async with pool.page(url="https://example.com"):
                return None

        holder_task = asyncio.create_task(holder())
        await cleanup_started.wait()
        holder_task.cancel()
        await asyncio.sleep(0)
        cleanup_continue.set()
        try:
            await holder_task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancellation must remain visible to the caller")

        assert pool._active_pages == 0
        await asyncio.wait_for(pool.stop(), timeout=1.0)
        assert pool._started is False

    asyncio.run(exercise())


def test_detached_rule_child_is_reaped_without_blocking_scheduler(monkeypatch) -> None:
    from bpc_fetch.rules import sync

    calls = []

    class FakeProcess:
        def wait(self):
            calls.append("wait")
            return 0

    class FakeThread:
        def __init__(self, *, target, args, name, daemon):
            calls.append(("thread", name, daemon))
            self.target = target
            self.args = args

        def start(self):
            calls.append("start")
            self.target(*self.args)

    monkeypatch.setattr(sync.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(sync.threading, "Thread", FakeThread)

    sync._spawn_detached_process(["python", "-c", "pass"], {"PAC_TEST": "1"})
    assert calls == [
        ("thread", "pac-rules-child-reaper", True),
        "start",
        "wait",
    ]
