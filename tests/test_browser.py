import asyncio

from bpc_fetch import browser


def test_ensure_browser_requires_successful_chromium_launch(monkeypatch):
    class FailingPlaywright:
        async def __aenter__(self):
            raise RuntimeError("Chromium executable is missing")

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(browser, "async_playwright", FailingPlaywright)

    result = asyncio.run(browser.ensure_browser())

    assert result == {
        "ok": False,
        "error": "Chromium executable is missing",
        "install_cmd": "playwright install chromium",
    }


def test_pool_stop_waits_for_active_page(monkeypatch):
    class FakePage:
        def set_default_timeout(self, value):
            pass

        def set_default_navigation_timeout(self, value):
            pass

        async def close(self):
            pass

    class FakeContext:
        async def new_page(self):
            return FakePage()

        async def close(self):
            pass

    async def exercise():
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
            allow_cookies=True,
            explicit_user_agent=False,
            explicit_locale=False,
            explicit_timezone=False,
            explicit_viewport=False,
        )
        entry = browser._IdleContext(1, key, FakeContext(), 0.0)
        monkeypatch.setattr(pool, "_context_key", lambda *a, **k: (key, {}, None))
        monkeypatch.setattr(pool, "_acquire_context", lambda *a, **k: asyncio.sleep(0, result=entry))

        released = asyncio.Event()
        entered = asyncio.Event()

        async def holder():
            async with pool.page(url="https://example.com"):
                entered.set()
                await released.wait()

        holder_task = asyncio.create_task(holder())
        await entered.wait()
        stop_task = asyncio.create_task(pool.stop())
        await asyncio.sleep(0)
        assert stop_task.done() is False
        released.set()
        await holder_task
        await stop_task
        assert pool._started is False
        assert pool._active_pages == 0

    asyncio.run(exercise())
