"""CLI entrypoint for pac (and bpc-fetch alias)."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from .result import (
    BATCH_SUMMARY_CHARS, aggregate_diagnostics, build_diagnostics, new_request_id, truncate_markdown,
)
from .sites import SITES_JS_DEFAULT, domain_from_url


def _attach_command_diagnostics(
    result: dict,
    *,
    enabled: bool,
    request_id: str,
    started_at: float,
) -> dict:
    if enabled and "diagnostics" not in result:
        result["diagnostics"] = build_diagnostics(
            request_id=request_id or new_request_id(),
            total_latency_ms=int((time.perf_counter() - started_at) * 1000),
        )
    return result


def main():
    if os.environ.get("PAC_RULES_SWR_CHILD", "").strip() == "1":
        from .rules.sync import _swr_child_main

        raise SystemExit(_swr_child_main())

    parser = argparse.ArgumentParser(
        prog="pac",
        description="Fetch paywalled news articles as markdown (Agent-friendly JSON CLI)",
    )
    parser.add_argument("--compact", action="store_true", help="Minimal JSON output")
    parser.add_argument("--sites-js", type=Path, default=None, help="Override base sites.js")
    sub = parser.add_subparsers(dest="command")

    _common = argparse.ArgumentParser(add_help=False)
    _common.add_argument("--compact", action="store_true")
    _common.add_argument("--sites-js", type=Path, default=None)

    sub.add_parser("doctor", help="Verify setup and rules", parents=[_common])

    p_sites = sub.add_parser("sites", help="List sites in rules map", parents=[_common])
    p_sites.add_argument("--filter", type=str, default="")
    p_sites.add_argument("--strategy", type=str, default="")
    p_sites.add_argument("--limit", type=int, default=50)

    p_fetch = sub.add_parser("fetch", help="Fetch article as markdown JSON", parents=[_common])
    p_fetch.add_argument("url", help="Article URL")
    p_fetch.add_argument("--out-dir", type=Path, default=None, help="If set, write .md under this dir")
    image_group = p_fetch.add_mutually_exclusive_group()
    image_group.add_argument(
        "--images", dest="images", action="store_true", help="Download images when --out-dir set"
    )
    image_group.add_argument(
        "--no-images", dest="images", action="store_false", help="Do not download images (default)"
    )
    p_fetch.set_defaults(images=False)
    p_fetch.add_argument("--allow-partial", action="store_true", help="Accept teaser as ok")
    p_fetch.add_argument("--full", action="store_true", help="Do not truncate markdown in JSON")
    p_fetch.add_argument("--archive", action="store_true", help="Force archive steps earlier")
    p_fetch.add_argument("--use-browser", action="store_true", default=None)
    p_fetch.add_argument("--no-browser", action="store_true")
    p_fetch.add_argument("--no-rule-sync", action="store_true", help="Skip background rule revalidation")
    p_fetch.add_argument("--diagnostics", action="store_true", help="Include structured attempt and quality diagnostics")
    p_fetch.add_argument(
        "--cookie",
        dest="cookie",
        default=None,
        help="Cookie header for the target domain (e.g. 'datadome=...'); also via PAC_COOKIE",
    )
    p_fetch.add_argument(
        "--cookie-file",
        dest="cookie_file",
        default=None,
        help="File containing a cookie header (curl -c style Netscape cookies.txt or raw header)",
    )

    p_batch = sub.add_parser("batch", help="Batch fetch (default summary only)", parents=[_common])
    p_batch.add_argument("urls", nargs="*", help="URLs")
    p_batch.add_argument("--file", type=Path, default=None)
    p_batch.add_argument("--out-dir", type=Path, default=None)
    p_batch.add_argument("--concurrency", type=int, default=2)
    p_batch.add_argument(
        "--cloud-max-calls",
        type=int,
        default=0,
        help="Maximum Firecrawl calls across this batch (default 0)",
    )
    p_batch.add_argument("--max", type=int, default=10, help="Default cap 10, hard 25")
    p_batch.add_argument("--allow-partial", action="store_true")
    p_batch.add_argument("--full", action="store_true")
    p_batch.add_argument("--no-rule-sync", action="store_true", help="Skip background rule revalidation")
    p_batch.add_argument("--diagnostics", action="store_true", help="Include structured attempt and quality diagnostics")

    p_cookies = sub.add_parser("cookies", help="Manage the per-site cookie vault", parents=[_common])
    csub = p_cookies.add_subparsers(dest="cookies_cmd")
    c_list = csub.add_parser("list", help="List vault entries (no secrets shown)", parents=[_common])
    c_store = csub.add_parser("store", help="Store cookie header for a domain", parents=[_common])
    c_store.add_argument("domain", help="Registrable domain, e.g. theinformation.com")
    c_store.add_argument("--header", help="Raw Cookie header value; omit to read stdin")
    c_store.add_argument("--file", help="Read the header from a file (raw line or Netscape cookies.txt)")
    c_delete = csub.add_parser("delete", help="Remove a domain from the vault", parents=[_common])
    c_delete.add_argument("domain")
    c_import = csub.add_parser("import", help="Import cookies for a domain from a local Chromium-based browser (macOS)", parents=[_common])
    c_import.add_argument("domain", help="Target registrable domain, e.g. theinformation.com")
    c_import.add_argument("--browser", default="auto", help="Browser profile: auto (Dia/Chrome/Arc/Edge), or an explicit 'User Data' dir")

    p_feeds = sub.add_parser("feeds", help="Inspect curated source feed health", parents=[_common])
    fsub = p_feeds.add_subparsers(dest="feeds_cmd")
    f_health = fsub.add_parser("health", help="Probe public RSS/Atom feeds without credentials", parents=[_common])
    f_health.add_argument("--domains", default="", help="Comma-separated curated domains; default all 29")
    f_health.add_argument("--concurrency", type=int, default=4)
    f_health.add_argument("--out", type=Path, default=None, help="Write the JSON health report to this path")

    p_discover = sub.add_parser("discover", help="Discover recent articles from domain or RSS", parents=[_common])
    p_discover.add_argument("target", help="Domain, RSS URL, or news topic query")
    p_discover.add_argument("--query", "-q", type=str, default=None, help="Search keyword filter")
    p_discover.add_argument("--limit", type=int, default=20, help="Max articles to discover")
    p_discover.add_argument("--diagnostics", action="store_true", help="Include structured discovery diagnostics")

    sub.add_parser("install-browser", help="Install Playwright Chromium", parents=[_common])

    p_rules = sub.add_parser("rules", help="Rules sync / show / version", parents=[_common])
    rsub = p_rules.add_subparsers(dest="rules_cmd")
    p_sync = rsub.add_parser("sync", parents=[_common])
    p_sync.add_argument("--from-zip", type=Path, default=None, help="Install base sites.js from BPC zip")
    p_sync.add_argument("--offline", action="store_true")
    rsub.add_parser("version", parents=[_common])
    p_show = rsub.add_parser("show", parents=[_common])
    p_show.add_argument("domain", help="Domain e.g. wsj.com")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    command_started_at = time.perf_counter()
    try:
        result = asyncio.run(_dispatch(args))
        print(json.dumps(result, ensure_ascii=False, indent=None if args.compact else 2))
        if not result.get("ok", True):
            sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        err = {
            "ok": False,
            "error_code": "INTERNAL",
            "failure_class": "config",
            "error": str(e),
            "strategy_hit": [],
            "recovery_hint": "pac doctor --compact",
        }
        _attach_command_diagnostics(
            err,
            enabled=bool(getattr(args, "diagnostics", False)),
            request_id="",
            started_at=command_started_at,
        )
        print(json.dumps(err))
        sys.exit(1)


async def _dispatch(args) -> dict:
    if args.command == "doctor":
        return await _cmd_doctor(args)
    if args.command == "sites":
        return _cmd_sites(args)
    if args.command == "fetch":
        return await _cmd_fetch(args)
    if args.command == "batch":
        return await _cmd_batch(args)
    if args.command == "discover":
        return await _cmd_discover(args)
    if args.command == "install-browser":
        return _cmd_install_browser(args)
    if args.command == "rules":
        return await _cmd_rules(args)
    if args.command == "cookies":
        return _cmd_cookies(args)
    if args.command == "feeds":
        return await _cmd_feeds(args)
    return {
        "ok": False,
        "error_code": "INTERNAL",
        "failure_class": "config",
        "error": f"unknown command: {args.command}",
        "strategy_hit": [],
    }


async def _cmd_doctor(args) -> dict:
    import importlib.metadata
    import importlib.util
    import inspect

    from .rules.store import get_sites_map_with_version, load_manifest

    issues: list[str] = []
    sites, ver, warnings = get_sites_map_with_version(args.sites_js)
    warnings = list(warnings)
    man = load_manifest()

    try:
        import trafilatura  # noqa: F401
        traf_ok = True
    except ImportError:
        traf_ok = False
        issues.append("trafilatura not installed")

    try:
        import httpx  # noqa: F401
        httpx_ok = True
    except ImportError:
        httpx_ok = False
        issues.append("httpx not installed")

    pw_ok = False
    chromium_ok = False
    pw_ver = ""
    try:
        from playwright.async_api import async_playwright  # noqa: F401

        from .browser import ensure_browser

        pw_ok = True
        try:
            pw_ver = importlib.metadata.version("playwright")
        except importlib.metadata.PackageNotFoundError:
            pw_ver = "unknown"
        browser_status = await ensure_browser()
        chromium_ok = bool(browser_status.get("ok"))
        if not chromium_ok:
            issues.append(browser_status.get("error") or "Chromium could not be launched")
            if browser_status.get("install_cmd"):
                issues.append(browser_status["install_cmd"])
    except Exception:
        issues.append("playwright not installed")

    curl_health: dict[str, object] = {
        "installed": False,
        "version": "",
        "runtime_ok": False,
        "error": "",
    }
    if importlib.util.find_spec("curl_cffi") is not None:
        curl_health["installed"] = True
        try:
            curl_health["version"] = importlib.metadata.version("curl_cffi")
        except importlib.metadata.PackageNotFoundError:
            curl_health["version"] = "unknown"
        try:
            from curl_cffi.requests import AsyncSession

            session = AsyncSession()
            close_method = getattr(session, "aclose", None) or getattr(session, "close", None)
            if close_method is not None:
                close_result = close_method()
                if inspect.isawaitable(close_result):
                    await close_result
            curl_health["runtime_ok"] = True
        except Exception as exc:
            curl_health["error"] = str(exc)[:500]
            warnings.append(f"curl_cffi_runtime_unhealthy:{exc}")

    camoufox_health: dict[str, object] = {
        "installed": False,
        "version": "",
        "runtime_ok": False,
        "error": "",
    }
    if importlib.util.find_spec("camoufox") is not None:
        camoufox_health["installed"] = True
        try:
            camoufox_health["version"] = importlib.metadata.version("camoufox")
        except importlib.metadata.PackageNotFoundError:
            camoufox_health["version"] = "unknown"
        try:
            from .browser import probe_camoufox

            camoufox_status = await probe_camoufox()
            camoufox_health["runtime_ok"] = bool(camoufox_status.get("ok"))
            if not camoufox_health["runtime_ok"]:
                error = str(camoufox_status.get("error") or "Camoufox could not be launched")
                camoufox_health["error"] = error[:500]
                warnings.append(f"camoufox_runtime_unhealthy:{error}")
        except Exception as exc:
            camoufox_health["error"] = str(exc)[:500]
            warnings.append(f"camoufox_runtime_unhealthy:{exc}")

    if man.get("stale") or man.get("using_bundled_base"):
        warnings.append("base_stale_or_bundled")

    return {
        "ok": len(issues) == 0,
        "rule_version": ver,
        "site_count": len(sites),
        "manifest": {
            key: man.get(key)
            for key in (
                "rule_version",
                "sources",
                "stale",
                "site_count",
                "fetched_at",
                "last_attempt_at",
                "revalidate_after",
            )
            if man
        },
        "trafilatura": traf_ok,
        "httpx": httpx_ok,
        "playwright": pw_ok,
        "playwright_version": pw_ver,
        "chromium_installed": chromium_ok,
        "curl_cffi": curl_health,
        "camoufox": camoufox_health,
        "warnings": list(dict.fromkeys(warnings)),
        "issues": issues,
        "sites_js_default": str(SITES_JS_DEFAULT),
    }


def _cmd_sites(args) -> dict:
    from .rules.store import get_sites_map_with_version

    sites, ver, warnings = get_sites_map_with_version(args.sites_js)
    items = []
    for domain, st in sites.items():
        if args.filter and args.filter.lower() not in domain.lower():
            continue
        if args.strategy and args.strategy not in st.bypass_type():
            continue
        items.append({"domain": domain, "name": st.name, "bypass_type": st.bypass_type()})
        if len(items) >= args.limit:
            break
    return {
        "ok": True,
        "rule_version": ver,
        "count": len(items),
        "sites": items,
        "warnings": warnings,
    }


def _resolve_cookie_header(args) -> str:
    """Resolve caller cookies from --cookie-file / --cookie / PAC_COOKIE.

    Supports two file shapes: a raw cookie header line, or a Netscape
    cookies.txt export.  Only the header form participates in PAC's domain
    routing; the Netscape parser keeps the first matching domain-agnostic
    name=value pairs to keep behaviour predictable across engines.
    """
    import os as _os

    cookie_file = getattr(args, "cookie_file", None)
    if cookie_file:
        try:
            text = Path(cookie_file).read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            raise SystemExit(f"cookie file unreadable: {exc}") from exc
        if not text:
            raise SystemExit("cookie file is empty")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        netscape = [line for line in lines if not line.startswith("#")]
        # Netscape format: domain 	 flag 	 path 	 secure 	 expiry 	 name 	 value
        if len(netscape) == 1 and "	" not in netscape[0]:
            return netscape[0]
        pairs: list[str] = []
        for line in netscape:
            parts = line.split("\t")
            if len(parts) >= 7:
                name, value = parts[-2], parts[-1]
                if name and value:
                    pairs.append(f"{name}={value}")
        if pairs:
            return "; ".join(pairs)
        raise SystemExit(
            "cookie file not recognised: expected a raw header line or Netscape cookies.txt"
        )
    value = getattr(args, "cookie", None)
    if value is None:
        value = _os.environ.get("PAC_COOKIE", "")
    return (value or "").strip()


def _explicit_batch_cookie_scope_error(urls: list[str], cookie_header: str) -> str:
    """Reject one explicit cookie header for a multi-domain batch."""
    if not cookie_header:
        return ""
    domains = {domain_from_url(value) for value in urls}
    if len(domains) <= 1:
        return ""
    return (
        "explicit --cookie/--cookie-file/PAC_COOKIE requires all batch URLs to share one "
        "registrable domain; use the cookie vault for multi-domain batches"
    )


async def _cmd_fetch(args) -> dict:
    from .extract import download_images
    from .rules.store import get_sites_map_with_version
    from .rules.sync import maybe_sync_rules, swr_nonblocking_mode
    from .strategy import fetch_article

    t0 = time.perf_counter()
    diagnostics_enabled = bool(getattr(args, "diagnostics", False))
    request_id = new_request_id() if diagnostics_enabled else ""
    sites_js = getattr(args, "sites_js", None)
    with swr_nonblocking_mode():
        sync_result = maybe_sync_rules(
            sites_js=sites_js, disabled=bool(getattr(args, "no_rule_sync", False))
        )
    sites, ver, warnings = get_sites_map_with_version(sites_js)
    warnings = list(sync_result.get("warnings") or []) + list(warnings)
    domain = domain_from_url(args.url)
    strategy = sites.get(domain)
    if strategy is None:
        warnings = list(warnings) + ["rule_missing"]

    use_browser = None
    if getattr(args, "no_browser", False):
        use_browser = False
    elif getattr(args, "use_browser", None):
        use_browser = True

    result = await fetch_article(
        args.url,
        strategy,
        allow_partial=bool(getattr(args, "allow_partial", False)),
        rule_version=ver,
        force_archive=bool(getattr(args, "archive", False)),
        full_markdown=bool(getattr(args, "full", False) or args.out_dir),
        use_browser=use_browser,
        domain=domain,
        diagnostics=diagnostics_enabled,
        request_id=request_id or None,
        cookie_header=_resolve_cookie_header(args),
    )
    image_urls = result.pop("_image_urls", [])
    result["warnings"] = list(dict.fromkeys(list(result.get("warnings") or []) + list(warnings)))
    result["latency_ms"] = int((time.perf_counter() - t0) * 1000)

    if result.get("ok"):
        result["images"] = 0
    if getattr(args, "images", False) and not args.out_dir:
        result["warnings"].append("images_requires_out_dir")

    if result.get("ok") and args.out_dir:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        slug = _slugify(result.get("title") or domain)
        article_dir = out_dir / slug
        md_path = article_dir / f"{slug}.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        markdown = result.get("markdown") or ""
        md_path.write_text(markdown, encoding="utf-8")
        result["path"] = str(md_path)
        if getattr(args, "images", False):
            saved_images = await download_images(image_urls, article_dir / "images")
            result["images"] = len(saved_images)
        if not getattr(args, "full", False):
            result["markdown"], result["truncated"] = truncate_markdown(markdown)

    if diagnostics_enabled:
        total_latency_ms = int((time.perf_counter() - t0) * 1000)
        diagnostics = result.get("diagnostics")
        if isinstance(diagnostics, dict):
            diagnostics["request_id"] = request_id
            diagnostics["total_latency_ms"] = total_latency_ms
        else:
            result["diagnostics"] = build_diagnostics(
                request_id=request_id, total_latency_ms=total_latency_ms
            )
    return result


async def _cmd_batch(args) -> dict:
    import asyncio as aio

    started_at = time.perf_counter()
    diagnostics_enabled = bool(getattr(args, "diagnostics", False))
    batch_request_id = new_request_id() if diagnostics_enabled else ""

    from .rules.store import get_sites_map_with_version
    from .rules.sync import maybe_sync_rules, swr_nonblocking_mode
    from .strategy import CloudBudget, fetch_article

    urls = list(args.urls or [])
    if (
        urls == ["-"]
        or (args.file and str(args.file) == "-")
        or (not sys.stdin.isatty() and not urls and not args.file)
    ):
        urls = [
            line.strip()
            for line in sys.stdin.read().splitlines()
            if line.strip() and not line.startswith("#")
        ]
    elif args.file and args.file.exists():
        urls.extend(
            line.strip()
            for line in args.file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        )
    try:
        hard_cap = min(max(1, int(os.environ.get("PAC_BATCH_HARD_CAP", "25"))), 25)
    except ValueError:
        hard_cap = 25
    max_n = min(args.max or 10, hard_cap)
    if len(urls) > max_n:
        return _attach_command_diagnostics({
            "ok": False,
            "error_code": "LIMIT_EXCEEDED",
            "failure_class": "config",
            "error": f"url count {len(urls)} > max {max_n}",
            "strategy_hit": [],
        }, enabled=diagnostics_enabled, request_id=batch_request_id, started_at=started_at)
    if not urls:
        return _attach_command_diagnostics({
            "ok": False,
            "error_code": "INTERNAL",
            "failure_class": "config",
            "error": "no URLs",
            "strategy_hit": [],
        }, enabled=diagnostics_enabled, request_id=batch_request_id, started_at=started_at)

    explicit_cookie_header = _resolve_cookie_header(args)
    cookie_scope_error = _explicit_batch_cookie_scope_error(urls, explicit_cookie_header)
    if cookie_scope_error:
        return _attach_command_diagnostics({
            "ok": False,
            "error_code": "COOKIE_SCOPE_ERROR",
            "failure_class": "config",
            "error": cookie_scope_error,
            "strategy_hit": [],
        }, enabled=diagnostics_enabled, request_id=batch_request_id, started_at=started_at)

    sites_js = getattr(args, "sites_js", None)
    with swr_nonblocking_mode():
        sync_result = maybe_sync_rules(
            sites_js=sites_js, disabled=bool(getattr(args, "no_rule_sync", False))
        )
    sites, ver, warnings = get_sites_map_with_version(sites_js)
    warnings = list(dict.fromkeys(list(sync_result.get("warnings") or []) + list(warnings)))
    concurrency = int(args.concurrency or 2)
    if concurrency < 1 or concurrency > hard_cap:
        return _attach_command_diagnostics({
            "ok": False,
            "error_code": "LIMIT_EXCEEDED",
            "failure_class": "config",
            "error": f"concurrency must be between 1 and {hard_cap}",
            "strategy_hit": [],
        }, enabled=diagnostics_enabled, request_id=batch_request_id, started_at=started_at)
    cloud_max_calls = int(getattr(args, "cloud_max_calls", 0) or 0)
    if cloud_max_calls < 0:
        return _attach_command_diagnostics({
            "ok": False,
            "error_code": "LIMIT_EXCEEDED",
            "failure_class": "config",
            "error": "cloud-max-calls must be non-negative",
            "strategy_hit": [],
        }, enabled=diagnostics_enabled, request_id=batch_request_id, started_at=started_at)
    cloud_budget = CloudBudget(cloud_max_calls)
    sem = aio.Semaphore(concurrency)
    results = []

    async def one(index: int, u: str) -> dict:
        async with sem:
            domain = domain_from_url(u)
            st = sites.get(domain)
            r = await fetch_article(
                u,
                st,
                allow_partial=bool(args.allow_partial),
                rule_version=ver,
                full_markdown=bool(args.full or args.out_dir),
                domain=domain,
                diagnostics=diagnostics_enabled,
                request_id=(f"{batch_request_id}-{index + 1}" if diagnostics_enabled else None),
                cookie_header=explicit_cookie_header,
                cloud_budget=cloud_budget,
            )
            r.pop("_image_urls", None)
            markdown = r.get("markdown") or ""
            if r.get("ok") and args.out_dir:
                # Batch output must be deterministic and collision-safe even
                # when two articles have the same headline.
                base_slug = _slugify(r.get("title") or domain)
                url_hash = hashlib.sha256(u.encode("utf-8")).hexdigest()[:8]
                slug = f"{base_slug[:71]}-{url_hash}"
                md_path = Path(args.out_dir) / slug / f"{slug}.md"
                md_path.parent.mkdir(parents=True, exist_ok=True)
                md_path.write_text(markdown, encoding="utf-8")
                r["path"] = str(md_path)
            if not args.full:
                r["markdown"], r["truncated"] = truncate_markdown(
                    markdown, BATCH_SUMMARY_CHARS
                )
            return r

    results = list(await aio.gather(*[one(index, u) for index, u in enumerate(urls)]))
    success = sum(1 for r in results if r.get("ok"))
    result: dict = {
        "ok": True,
        "total": len(urls),
        "success": success,
        "failed": len(urls) - success,
        "rule_version": ver,
        "warnings": warnings,
        "results": results,
    }
    if diagnostics_enabled:
        result["diagnostics"] = aggregate_diagnostics(
            request_id=batch_request_id,
            total_latency_ms=int((time.perf_counter() - started_at) * 1000),
            items=zip(urls, results),
        )
    return result


async def _cmd_feeds(args) -> dict:
    """Probe registered public feeds without credentials or article fetches."""
    from .feed_health import health_report_as_dict, validate_registered_feeds

    command = getattr(args, "feeds_cmd", "")
    if command != "health":
        return {
            "ok": False,
            "error_code": "USAGE",
            "failure_class": "config",
            "error": "usage: pac feeds health [--domains domain1,domain2]",
            "strategy_hit": [],
        }
    domains = tuple(
        value.strip().casefold().removeprefix("www.")
        for value in str(getattr(args, "domains", "")).split(",")
        if value.strip()
    )
    reports = await validate_registered_feeds(
        domains or None,
        concurrency=max(1, int(getattr(args, "concurrency", 4))),
    )
    payload = health_report_as_dict(reports)
    payload.update({"ok": True, "domains": list(domains)})
    out_path = getattr(args, "out", None)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        payload["path"] = str(out_path)
    return payload


def _cmd_cookies(args) -> dict:
    """Cookie vault management: list / store / delete / import."""
    from . import cookies as vault

    cmd = getattr(args, "cookies_cmd", "")
    try:
        if cmd == "list":
            return {
                "ok": True,
                "backend": vault.vault_backend(),
                "root": str(vault.vault_root()),
                "entries": vault.list_domains(),
            }
        if cmd == "store":
            header = getattr(args, "header", None)
            if not header and getattr(args, "file", None):
                header = _read_cookie_source(args.file)
            if not header:
                import sys as _sys

                header = _sys.stdin.read().strip()
            path = vault.store(args.domain, header)
            return {
                "ok": True,
                "domain": args.domain,
                "stored": True,
                "backend": vault.vault_backend(),
                "path": str(path) if path else None,
            }
        if cmd == "delete":
            removed = vault.delete(args.domain)
            return {"ok": removed, "domain": args.domain, "removed": removed}
        if cmd == "import":
            header, source = _import_browser_cookies(args.domain, getattr(args, "browser", "auto"))
            if not header:
                return {
                    "ok": False,
                    "error_code": "IMPORT_FAILED",
                    "failure_class": "config",
                    "error": source,
                    "strategy_hit": [],
                }
            vault.store(args.domain, header)
            names = [p.split("=", 1)[0] for p in header.split(";") if "=" in p]
            return {
                "ok": True,
                "domain": args.domain,
                "imported_from": source,
                "cookie_names": names,
            }
        return {
            "ok": False,
            "error_code": "USAGE",
            "failure_class": "config",
            "error": "usage: pac cookies {list|store|delete|import}",
            "strategy_hit": [],
        }
    except Exception as exc:
        return {
            "ok": False,
            "error_code": "VAULT_ERROR",
            "failure_class": "config",
            "error": str(exc),
            "strategy_hit": [],
        }


def _read_cookie_source(path: str) -> str:
    """Read a raw header line or Netscape cookies.txt into a header string."""
    from pathlib import Path

    text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    netscape = [ln for ln in lines if not ln.startswith("#")]
    if len(netscape) == 1 and "\t" not in netscape[0]:
        return netscape[0]
    pairs: list[str] = []
    for line in netscape:
        parts = line.split("\t")
        if len(parts) >= 7:
            name, value = parts[-2], parts[-1]
            if name and value:
                pairs.append(f"{name}={value}")
    if pairs:
        return "; ".join(pairs)
    raise SystemExit(f"cookie file not recognised: {path}")


_DIA_SAFE_STORAGE = "Dia Safe Storage"


def _chromium_candidates(browser: str) -> list[tuple[str, str]]:
    """Return (profile_dir, keychain_service) candidates for a Chromium browser."""
    home = os.path.expanduser("~")
    app_support = Path(home) / "Library" / "Application Support"
    if browser != "auto":
        return [(browser, _DIA_SAFE_STORAGE), (browser, "Chrome Safe Storage")]
    return [
        (str(app_support / "Dia" / "User Data"), _DIA_SAFE_STORAGE),
        (str(app_support / "Google" / "Chrome"), "Chrome Safe Storage"),
        (str(app_support / "Arc" / "User Data"), "Arc Safe Storage"),
        (str(app_support / "Microsoft Edge"), "Chrome Safe Storage"),
    ]


def _import_browser_cookies(domain: str, browser: str) -> tuple[str, str]:
    """Decrypt Chromium cookies for a domain from a local profile (macOS).

    Returns (header, source_description); header is empty on failure with the
    description carrying the reason.  Cookies are filtered to non-expired
    entries matching the target registrable domain.
    """
    import datetime
    import hashlib
    import shutil
    import sqlite3
    import tempfile
    import time as _time

    for profile_root, keychain_service in _chromium_candidates(browser):
        root = Path(profile_root)
        if not root.exists():
            continue
        cookie_dbs: list[Path] = []
        if (root / "Default" / "Cookies").exists():
            cookie_dbs.append(root / "Default" / "Cookies")
        cookie_dbs.extend(sorted(root.glob("Profile */Cookies")))
        if not cookie_dbs:
            continue

        key_result = os.popen(
            f'security find-generic-password -w -s "{keychain_service}" 2>/dev/null'
        ).read()
        key_b64 = key_result.strip()
        if not key_b64:
            continue
        aes_key = hashlib.pbkdf2_hmac("sha1", key_b64.encode(), b"saltysalt", 1003, dklen=16)

        for db in cookie_dbs:
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                shutil.copy(db, tmp_path)
                conn = sqlite3.connect(tmp_path)
                rows = conn.execute(
                    "SELECT host_key, name, encrypted_value, expires_utc "
                    "FROM cookies WHERE host_key LIKE ?",
                    (f"%{domain}%",),
                ).fetchall()
                conn.close()
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

            if not rows:
                continue

            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

            now_unix = _time.time()
            pairs: list[str] = []
            for host, name, enc, expires_utc in rows:
                if expires_utc:
                    exp_unix = expires_utc / 1_000_000 - 11_644_473_600
                    if exp_unix <= now_unix:
                        continue
                if not (enc[:3] in (b"v10", b"v20")):
                    continue
                try:
                    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(b" " * 16))
                    dec = cipher.decryptor()
                    plain = dec.update(enc[3:]) + dec.finalize()
                    pad = plain[-1]
                    if 0 < pad <= 16:
                        plain = plain[:-pad]
                    value = plain[32:].decode("utf-8", errors="replace")
                except Exception:
                    continue
                if not value:
                    continue
                key = f"{name}={value}"
                if key not in pairs:
                    pairs.append(key)

            if pairs:
                source = f"{root.name}/{'/'.join(db.parts[-2:-1])} ({keychain_service})"
                return "; ".join(pairs), source

    return "", f"no cookies for {domain} found in known Chromium profiles"


async def _cmd_discover(args) -> dict:
    from .discover import discover_articles

    diagnostics_enabled = bool(getattr(args, "diagnostics", False))
    return await discover_articles(
        args.target,
        limit=getattr(args, "limit", 20),
        search_query=getattr(args, "query", None),
        diagnostics=diagnostics_enabled,
        request_id=new_request_id() if diagnostics_enabled else None,
    )


def _cmd_install_browser(args) -> dict:
    import subprocess

    try:
        timeout = max(30, int(os.environ.get("PAC_INSTALL_BROWSER_TIMEOUT_S", "600")))
    except ValueError:
        timeout = 600
    try:
        r = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error_code": "BROWSER_UNAVAILABLE",
            "failure_class": "config",
            "error": f"browser installation timed out after {timeout}s",
            "strategy_hit": [],
        }
    if r.returncode == 0:
        return {"ok": True, "message": "Chromium installed"}
    return {
        "ok": False,
        "error_code": "BROWSER_UNAVAILABLE",
        "failure_class": "config",
        "error": (r.stderr or r.stdout)[-300:],
        "strategy_hit": [],
    }


async def _cmd_rules(args) -> dict:
    from .rules.store import get_sites_map_with_version, load_manifest
    from .rules.sync import sync_rules

    cmd = getattr(args, "rules_cmd", None)
    if cmd == "sync":
        return sync_rules(
            from_zip=getattr(args, "from_zip", None),
            offline=bool(getattr(args, "offline", False)),
        )
    if cmd == "version":
        man = load_manifest()
        sites, ver, warnings = get_sites_map_with_version(args.sites_js)
        return {
            "ok": True,
            "rule_version": ver,
            "site_count": len(sites),
            "manifest": man,
            "warnings": warnings,
        }
    if cmd == "show":
        sites, ver, warnings = get_sites_map_with_version(args.sites_js)
        domain = args.domain
        st = sites.get(domain)
        if not st:
            # try suffix match
            for d, s in sites.items():
                normalized = domain.casefold().removeprefix("www.").rstrip(".")
                rule_domain = d.casefold().removeprefix("www.").rstrip(".")
                if (
                    normalized == rule_domain
                    or normalized.endswith("." + rule_domain)
                    or rule_domain.endswith("." + normalized)
                ):
                    st = s
                    domain = d
                    break
        if not st:
            return {
                "ok": False,
                "error_code": "RULE_MISSING",
                "failure_class": "config",
                "error": f"no rule for {args.domain}",
                "strategy_hit": [],
                "rule_version": ver,
                "warnings": warnings,
            }
        return {
            "ok": True,
            "rule_version": ver,
            "domain": domain,
            "strategy": st.to_dict(),
            "warnings": warnings,
        }
    return {
        "ok": False,
        "error_code": "INTERNAL",
        "failure_class": "config",
        "error": "usage: pac rules sync|version|show <domain>",
        "strategy_hit": [],
    }


def _slugify(text: str) -> str:
    import re

    s = re.sub(r"[^\w\s-]", "", text.lower())
    s = re.sub(r"[-\s]+", "-", s).strip("-")
    return s[:80] or "article"
