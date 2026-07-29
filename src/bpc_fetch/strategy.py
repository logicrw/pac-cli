"""HTTP / browser / archive fetch with site plan (§15)."""
from __future__ import annotations

import random
import time
from typing import Any
from urllib.parse import quote

import httpx

from .quality import html_has_content, html_looks_paywalled, quality_check
from .result import classify_http_failure, fail_result, ok_result
from .sites import SiteStrategy
from .ssrf import SSRFBlocked, assert_public_url

UA_GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
UA_BINGBOT = "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)"
UA_FACEBOOKBOT = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
UA_NORMAL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
)

REFERER_GOOGLE = "https://www.google.com/"
REFERER_FACEBOOK = "https://www.facebook.com/"
REFERER_TWITTER = "https://t.co/"

TIMEOUT = 30.0
GOOGLEBOT_TIMEOUT = 10.0  # §15.2.3


def build_headers(strategy: SiteStrategy | None) -> dict[str, str]:
    headers: dict[str, str] = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if strategy is None:
        headers["User-Agent"] = UA_GOOGLEBOT
        headers["Referer"] = REFERER_GOOGLE
        return headers

    if strategy.useragent_custom:
        headers["User-Agent"] = strategy.useragent_custom
    else:
        ua = (strategy.useragent or "").lower()
        if ua == "googlebot":
            headers["User-Agent"] = UA_GOOGLEBOT
        elif ua == "bingbot":
            headers["User-Agent"] = UA_BINGBOT
        elif ua in ("facebookbot", "facebook"):
            headers["User-Agent"] = UA_FACEBOOKBOT
        else:
            headers["User-Agent"] = UA_NORMAL

    # referer_custom wins (§15.2 / A3)
    if strategy.referer_custom:
        headers["Referer"] = strategy.referer_custom
    else:
        ref = (strategy.referer or "").lower()
        if ref == "google":
            headers["Referer"] = REFERER_GOOGLE
        elif ref == "facebook":
            headers["Referer"] = REFERER_FACEBOOK
        elif ref == "twitter":
            headers["Referer"] = REFERER_TWITTER
        elif not strategy.useragent and not strategy.useragent_custom:
            headers["Referer"] = REFERER_GOOGLE

    if strategy.random_ip:
        ip = (
            f"{random.randint(1, 223)}.{random.randint(0, 255)}."
            f"{random.randint(0, 255)}.{random.randint(1, 254)}"
        )
        headers["X-Forwarded-For"] = ip

    return headers


def build_fallback_headers() -> dict[str, str]:
    return {
        "User-Agent": UA_GOOGLEBOT,
        "Referer": REFERER_GOOGLE,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _hit_label(strategy: SiteStrategy | None, step: str) -> str:
    if step == "http_primary" and strategy:
        if strategy.referer_custom:
            return "http_referer_custom"
        if strategy.useragent_custom:
            return "http_ua_custom"
        if strategy.useragent:
            return f"http_ua_{strategy.useragent}"
        return "http_headers"
    return step


def build_plan(
    strategy: SiteStrategy | None,
    use_browser: bool | None = None,
    *,
    force_archive: bool = False,
) -> list[str]:
    steps = ["http_primary"]
    # googlebot fallback if primary is not already a bot UA (§15.2.3)
    is_bot = False
    if strategy:
        ua = (strategy.useragent or "").lower()
        is_bot = ua in ("googlebot", "bingbot", "facebookbot", "facebook") or bool(
            strategy.useragent_custom and "google" in strategy.useragent_custom.lower()
        )
    if not is_bot:
        steps.append("http_googlebot_fallback")

    want_browser = use_browser
    if want_browser is None:
        want_browser = bool(
            strategy
            and (
                strategy.block_regex
                or strategy.needs_browser_cleanup()
                or strategy.useragent_custom
            )
        )
    if want_browser:
        steps.append("browser_cleanup")

    # archive: after http+browser fail, or force
    steps.append("archive_is")
    steps.append("archive_org")
    return steps


async def fetch_page(
    url: str,
    strategy: SiteStrategy | None = None,
    client: httpx.AsyncClient | None = None,
    *,
    timeout: float = TIMEOUT,
) -> tuple[str, int]:
    headers = build_headers(strategy) if strategy else build_fallback_headers()
    own = client is None
    if own:
        client = httpx.AsyncClient(follow_redirects=True, timeout=timeout)
    assert client is not None
    try:
        # hop-by-hop SSRF: disable auto redirect, validate each hop
        r = await client.get(url, headers=headers, follow_redirects=False)
        hops = 0
        while r.is_redirect and hops < 10:
            loc = r.headers.get("location")
            if not loc:
                break
            from urllib.parse import urljoin

            nxt = urljoin(str(r.url), loc)
            assert_public_url(nxt)
            r = await client.get(nxt, headers=headers, follow_redirects=False)
            hops += 1
        if r.is_redirect:
            return r.text, r.status_code
        return r.text, r.status_code
    finally:
        if own:
            await client.aclose()


async def _try_extract_ok(
    html: str,
    url: str,
    domain: str,
    *,
    dom_result: dict | None,
    allow_partial: bool,
    strategy_hit: list[str],
    rule_version: str,
    engine: str,
    t0: float,
    full_markdown: bool,
) -> dict[str, Any] | None:
    from .extract import extract_article, article_to_markdown

    article = extract_article(html, url, dom_result=dom_result)
    text = article.get("text") or ""
    title = article.get("title") or ""
    q = quality_check(text, title, allow_partial=allow_partial)
    if not q.ok:
        return None
    md = article_to_markdown(article, images_dir="images")
    return ok_result(
        url=url,
        domain=domain,
        title=title,
        markdown=md,
        strategy_hit=strategy_hit,
        rule_version=rule_version,
        engine=engine,
        latency_ms=int((time.perf_counter() - t0) * 1000),
        paywall_suspected=q.paywall_suspected,
        full_markdown=full_markdown,
    )


async def fetch_article(
    url: str,
    strategy: SiteStrategy | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    use_browser: bool | None = None,
    allow_partial: bool = False,
    rule_version: str = "",
    force_archive: bool = False,
    full_markdown: bool = False,
    domain: str | None = None,
) -> dict[str, Any]:
    """Plan-based fetch → ArticleResult envelope."""
    from .sites import domain_from_url

    t0 = time.perf_counter()
    domain = domain or domain_from_url(url)
    strategy_hit: list[str] = []
    warnings: list[str] = []
    last_html = ""
    last_status = 0
    last_dom: dict | None = None

    try:
        assert_public_url(url)
    except SSRFBlocked as e:
        return fail_result(
            url=url,
            domain=domain,
            error_code="SSRF_BLOCKED",
            failure_class="config",
            error=str(e),
            strategy_hit=strategy_hit,
            rule_version=rule_version,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )

    plan = build_plan(strategy, use_browser, force_archive=force_archive)
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(follow_redirects=False, timeout=TIMEOUT)
    assert client is not None

    try:
        for step in plan:
            label = _hit_label(strategy, step)

            if step == "http_primary":
                try:
                    html, status = await fetch_page(url, strategy, client, timeout=TIMEOUT)
                except SSRFBlocked as e:
                    return fail_result(
                        url=url,
                        domain=domain,
                        error_code="SSRF_BLOCKED",
                        failure_class="config",
                        error=str(e),
                        strategy_hit=strategy_hit + [label],
                        rule_version=rule_version,
                        latency_ms=int((time.perf_counter() - t0) * 1000),
                    )
                except Exception as e:
                    strategy_hit.append(label + "_error")
                    last_status = 0
                    continue
                strategy_hit.append(label)
                last_html, last_status = html, status
                if status == 200 and html_has_content(html) and not html_looks_paywalled(html):
                    got = await _try_extract_ok(
                        html, url, domain,
                        dom_result=None,
                        allow_partial=allow_partial,
                        strategy_hit=strategy_hit,
                        rule_version=rule_version,
                        engine="http",
                        t0=t0,
                        full_markdown=full_markdown,
                    )
                    if got:
                        return got
                continue

            if step == "http_googlebot_fallback":
                fb = SiteStrategy(domain=domain, useragent="googlebot")
                try:
                    html, status = await fetch_page(
                        url, fb, client, timeout=GOOGLEBOT_TIMEOUT
                    )
                except Exception:
                    strategy_hit.append("http_googlebot_fallback_error")
                    continue
                strategy_hit.append("http_googlebot_fallback")
                last_html, last_status = html, status
                if status == 200 and html_has_content(html) and not html_looks_paywalled(html):
                    got = await _try_extract_ok(
                        html, url, domain,
                        dom_result=None,
                        allow_partial=allow_partial,
                        strategy_hit=strategy_hit,
                        rule_version=rule_version,
                        engine="http",
                        t0=t0,
                        full_markdown=full_markdown,
                    )
                    if got:
                        return got
                continue

            if step == "browser_cleanup":
                try:
                    from .browser import BrowserPool, fetch_for_strategy

                    pool = BrowserPool(max_contexts=1)
                    await pool.start()
                    try:
                        br = await fetch_for_strategy(url, strategy, pool=pool)
                    finally:
                        await pool.stop()
                    strategy_hit.append("browser_cleanup")
                    if not br.ok:
                        strategy_hit.append("browser_cleanup_fail")
                        continue
                    last_html = br.html
                    last_status = br.status or 200
                    last_dom = br.dom_result
                    got = await _try_extract_ok(
                        br.html, url, domain,
                        dom_result=br.dom_result,
                        allow_partial=allow_partial,
                        strategy_hit=strategy_hit,
                        rule_version=rule_version,
                        engine=br.engine or "browser",
                        t0=t0,
                        full_markdown=full_markdown,
                    )
                    if got:
                        return got
                except Exception as e:
                    strategy_hit.append("browser_cleanup_error")
                    warnings.append(f"browser:{e}")
                continue

            if step in ("archive_is", "archive_org"):
                # only if previous steps failed quality (we're in loop) — always try near end
                try:
                    if step == "archive_is":
                        arch = f"https://archive.is/newest/{quote(url, safe='')}"
                        hit = "archive_is"
                    else:
                        arch = f"https://web.archive.org/web/2/{url}"
                        hit = "archive_org"
                    assert_public_url(arch)
                    html, status = await fetch_page(
                        arch,
                        SiteStrategy(domain=domain, useragent=""),
                        client,
                        timeout=TIMEOUT,
                    )
                    strategy_hit.append(hit)
                    last_html, last_status = html, status
                    if status == 200 and html_has_content(html):
                        got = await _try_extract_ok(
                            html, url, domain,
                            dom_result=None,
                            allow_partial=allow_partial,
                            strategy_hit=strategy_hit,
                            rule_version=rule_version,
                            engine=hit,
                            t0=t0,
                            full_markdown=full_markdown,
                        )
                        if got:
                            return got
                except Exception:
                    strategy_hit.append(f"{step}_error")
                continue

        # final failure classification
        if last_html:
            from .extract import extract_article, article_to_markdown

            article = extract_article(last_html, url, dom_result=last_dom)
            text = article.get("text") or ""
            title = article.get("title") or ""
            q = quality_check(text, title, allow_partial=allow_partial)
            md = article_to_markdown(article) if text else ""
            if q.error_code == "PAYWALL_REMAINING" or q.paywall_suspected:
                return fail_result(
                    url=url,
                    domain=domain,
                    error_code="PAYWALL_REMAINING",
                    failure_class="strategy",
                    error=q.reason,
                    strategy_hit=strategy_hit,
                    rule_version=rule_version,
                    latency_ms=int((time.perf_counter() - t0) * 1000),
                    markdown=md if allow_partial else "",
                    full_markdown=full_markdown,
                    warnings=warnings,
                )
            if q.error_code == "EXTRACT_FAILED" or not text:
                return fail_result(
                    url=url,
                    domain=domain,
                    error_code="EXTRACT_FAILED",
                    failure_class="extract",
                    error=q.reason or "empty_text",
                    strategy_hit=strategy_hit,
                    rule_version=rule_version,
                    latency_ms=int((time.perf_counter() - t0) * 1000),
                    warnings=warnings,
                )

        code, fclass = classify_http_failure(last_status, last_html)
        return fail_result(
            url=url,
            domain=domain,
            error_code=code,
            failure_class=fclass,
            error=f"HTTP {last_status}" if last_status else "fetch_failed",
            strategy_hit=strategy_hit,
            rule_version=rule_version,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            http_status=last_status or None,
            warnings=warnings,
        )
    finally:
        if own_client:
            await client.aclose()


# Back-compat thin wrapper for any leftover callers
async def fetch_with_retries(
    url: str,
    strategy: SiteStrategy | None = None,
    client: httpx.AsyncClient | None = None,
    use_browser: bool | None = None,
) -> tuple[str, int, dict | None]:
    r = await fetch_article(
        url, strategy, client=client, use_browser=use_browser, full_markdown=True
    )
    if r.get("ok"):
        # re-fetch not available; return empty html marker — callers should migrate
        return "", 200, None
    return "", int(r.get("http_status") or 0), None
