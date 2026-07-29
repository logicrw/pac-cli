"""Playwright-based browser fetch for block_js / dom_cleanup sites."""
from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from .sites import SiteStrategy

BROWSER_TIMEOUT = 30000


@dataclass
class BrowserResult:
    ok: bool
    html: str = ""
    status: int = 0
    engine: str = "playwright"
    dom_result: dict | None = None
    error_code: str = ""
    error_msg: str = ""


async def ensure_browser() -> dict:
    """Check if Playwright Chromium is installed. Returns status dict."""
    try:
        from playwright._impl._driver import compute_driver_executable
        driver = compute_driver_executable()
        return {"ok": True, "driver": str(driver)}
    except Exception:
        pass
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            version = browser.version
            await browser.close()
            return {"ok": True, "version": version}
    except Exception as e:
        return {"ok": False, "error": str(e), "install_cmd": "playwright install chromium"}


class BrowserPool:
    """Reusable browser context pool for batch operations."""

    def __init__(self, max_contexts: int = 3):
        self._pw = None
        self._browser: Browser | None = None
        self._max = max_contexts
        self._sem = asyncio.Semaphore(max_contexts)

    async def start(self):
        # Prefer patchright if installed (§15.2.7 optional)
        try:
            from patchright.async_api import async_playwright as _ap

            self._engine = "patchright"
            self._pw = await _ap().start()
        except Exception:
            self._engine = "playwright"
            self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    @asynccontextmanager
    async def page(self, strategy: SiteStrategy | None = None):
        async with self._sem:
            # late import avoids circular import with strategy.py
            from .strategy import build_headers

            headers = build_headers(strategy) if strategy else {}
            ua = headers.pop("User-Agent", None) or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
            )
            ctx = await self._browser.new_context(
                user_agent=ua,
                extra_http_headers=headers or None,
            )
            pg = await ctx.new_page()
            try:
                yield pg
            finally:
                await pg.close()
                await ctx.close()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_):
        await self.stop()


def _build_route_patterns(strategy: SiteStrategy) -> list[str]:
    """Convert BPC block_regex to Playwright route glob patterns."""
    if not strategy.block_regex:
        return []
    regex_str = strategy.block_regex
    patterns = []
    for part in re.split(r'\|', regex_str):
        part = part.strip().strip("()")
        glob = _regex_to_glob(part)
        if glob:
            patterns.append(glob)
    if not patterns:
        patterns.append(f"**/*{strategy.domain}*paywall*")
    return patterns


def _regex_to_glob(regex_part: str) -> str:
    """Best-effort convert a simple regex fragment to a glob pattern."""
    s = regex_part.replace("\\.", ".").replace("\\/", "/")
    s = re.sub(r'\.\+', '*', s)
    s = re.sub(r'\.\*', '*', s)
    s = re.sub(r'\([^)]*\)', '*', s)
    s = re.sub(r'\[[^\]]*\]', '?', s)
    s = re.sub(r'[\\^$]', '', s)
    if not s or s == '*':
        return ""
    if not s.startswith("*"):
        s = "**/" + s
    if not s.endswith("*"):
        s = s + "*"
    return s


async def fetch_for_strategy(
    url: str,
    strategy: SiteStrategy | None,
    pool: BrowserPool | None = None,
) -> BrowserResult:
    """Fetch with route blocking + unhide; return BrowserResult."""
    own_pool = pool is None
    if own_pool:
        pool = BrowserPool(max_contexts=1)
        await pool.start()
    engine = getattr(pool, "_engine", "playwright")
    try:
        async with pool.page(strategy) as page:
            if strategy:
                route_patterns = _build_route_patterns(strategy)
                for pattern in route_patterns:
                    try:
                        await page.route(pattern, lambda route: route.abort())
                    except Exception:
                        pass
            for provider in [
                "piano.io", "tinypass.com", "poool.fr", "zephr.com",
                "pelcro.com", "sophi.io", "cxense.com", "fortress-client",
            ]:
                try:
                    await page.route(f"**/*{provider}*", lambda route: route.abort())
                except Exception:
                    pass

            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
                status = resp.status if resp else 0
            except Exception as e:
                return BrowserResult(
                    ok=False, engine=engine, error_code="BROWSER_UNAVAILABLE", error_msg=str(e)[:300]
                )

            try:
                await page.wait_for_selector(
                    "article, [data-article], .article-body, .story-body, .post-content",
                    timeout=8000,
                )
            except Exception:
                pass
            await page.wait_for_timeout(2000)

            await page.evaluate(
                """() => {
                document.querySelectorAll(
                  '[class*="paywall"],[class*="gate"],[class*="piano"],[id*="paywall"],[class*="subscriber"]'
                ).forEach(el => { if (el.style) el.style.display = 'none'; });
                document.querySelectorAll(
                  'article,[data-article],.article-body,.story-body,.post-content'
                ).forEach(el => {
                  el.style.overflow = 'visible';
                  el.style.maxHeight = 'none';
                  el.style.height = 'auto';
                  el.style.visibility = 'visible';
                });
                document.body.style.overflow = 'auto';
                document.documentElement.style.overflow = 'auto';
            }"""
            )
            await page.wait_for_timeout(500)
            dom_result = await extract_article_dom(page)
            html = await page.content()
            return BrowserResult(
                ok=True,
                html=html,
                status=status or 200,
                engine=engine,
                dom_result=dom_result,
            )
    except Exception as e:
        return BrowserResult(
            ok=False, engine=engine, error_code="BROWSER_UNAVAILABLE", error_msg=str(e)[:300]
        )
    finally:
        if own_pool:
            await pool.stop()


async def fetch_with_browser(
    url: str,
    strategy: SiteStrategy,
    pool: BrowserPool | None = None,
) -> tuple[str, int]:
    """Back-compat wrapper."""
    br = await fetch_for_strategy(url, strategy, pool=pool)
    return br.html, br.status if br.ok else 0


async def extract_article_dom(page: Page) -> dict | None:
    """Extract article content directly from page DOM. More reliable than trafilatura for JS-rendered pages."""
    return await page.evaluate("""() => {
        // Find main article container
        const selectors = [
            'article[data-body-id]', 'article .article-body', 'article .story-body',
            '.article__body', '.post-content', '.entry-content',
            '[data-component="body"]', '.story-text', '.article-text',
            'article'
        ];
        let container = null;
        for (const sel of selectors) {
            container = document.querySelector(sel);
            if (container && container.innerText.length > 200) break;
        }
        if (!container) return null;

        // Extract paragraphs
        const paragraphs = [];
        container.querySelectorAll('p').forEach(p => {
            const text = p.innerText.trim();
            if (text.length > 20) paragraphs.push(text);
        });

        // Extract images
        const images = [];
        container.querySelectorAll('img[src]').forEach(img => {
            const src = img.src;
            if (src && !src.includes('pixel') && !src.includes('tracking') && !src.includes('logo') && !src.includes('icon')) {
                const alt = img.alt || '';
                images.push({src, alt});
            }
        });

        // Title
        const title = document.querySelector('h1')?.innerText?.trim() || document.title.split('|')[0].trim();

        const text = paragraphs.join('\\n\\n');
        if (text.length < 100) return null;

        return {title, text, images, paragraph_count: paragraphs.length};
    }""")

