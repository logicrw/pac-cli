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
