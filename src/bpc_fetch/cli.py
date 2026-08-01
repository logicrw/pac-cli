"""CLI entrypoint for pac (and bpc-fetch alias)."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from .result import BATCH_SUMMARY_CHARS, truncate_markdown
from .sites import SITES_JS_DEFAULT, domain_from_url


def main():
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
    p_fetch.add_argument("--no-rule-sync", action="store_true", help="Skip lazy rule sync")

    p_batch = sub.add_parser("batch", help="Batch fetch (default summary only)", parents=[_common])
    p_batch.add_argument("urls", nargs="*", help="URLs")
    p_batch.add_argument("--file", type=Path, default=None)
    p_batch.add_argument("--out-dir", type=Path, default=None)
    p_batch.add_argument("--concurrency", type=int, default=2)
    p_batch.add_argument("--max", type=int, default=10, help="Default cap 10, hard 25")
    p_batch.add_argument("--allow-partial", action="store_true")
    p_batch.add_argument("--full", action="store_true")
    p_batch.add_argument("--no-rule-sync", action="store_true", help="Skip lazy rule sync")

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

    try:
        result = asyncio.run(_dispatch(args))
        print(json.dumps(result, ensure_ascii=False, indent=None if args.compact else 2))
        if not result.get("ok", True) and args.command in ("fetch",):
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
    if args.command == "install-browser":
        return _cmd_install_browser(args)
    if args.command == "rules":
        return await _cmd_rules(args)
    return {
        "ok": False,
        "error_code": "INTERNAL",
        "failure_class": "config",
        "error": f"unknown command: {args.command}",
        "strategy_hit": [],
    }


async def _cmd_doctor(args) -> dict:
    from .rules.store import get_sites_map_with_version, load_manifest

    issues = []
    sites, ver, warnings = get_sites_map_with_version(args.sites_js)
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
        import importlib.metadata

        from .browser import ensure_browser

        pw_ok = True
        pw_ver = importlib.metadata.version("playwright")
        browser_status = await ensure_browser()
        chromium_ok = bool(browser_status.get("ok"))
        if not chromium_ok:
            issues.append(browser_status.get("error") or "Chromium could not be launched")
            if browser_status.get("install_cmd"):
                issues.append(browser_status["install_cmd"])
    except Exception:
        issues.append("playwright not installed")

    if man.get("stale") or man.get("using_bundled_base"):
        warnings = list(warnings) + ["base_stale_or_bundled"]

    return {
        "ok": len(issues) == 0,
        "rule_version": ver,
        "site_count": len(sites),
        "manifest": {k: man.get(k) for k in ("rule_version", "sources", "stale", "site_count") if man},
        "trafilatura": traf_ok,
        "httpx": httpx_ok,
        "playwright": pw_ok,
        "playwright_version": pw_ver,
        "chromium_installed": chromium_ok,
        "warnings": warnings,
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


async def _cmd_fetch(args) -> dict:
    from .extract import download_images
    from .rules.store import get_sites_map_with_version
    from .rules.sync import maybe_sync_rules
    from .strategy import fetch_article

    t0 = time.perf_counter()
    sites_js = getattr(args, "sites_js", None)
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

    return result


async def _cmd_batch(args) -> dict:
    import asyncio as aio
    import os

    from .rules.store import get_sites_map_with_version
    from .rules.sync import maybe_sync_rules
    from .strategy import fetch_article

    urls = list(args.urls or [])
    if args.file and args.file.exists():
        urls.extend(
            ln.strip()
            for ln in args.file.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")
        )
    hard_cap = min(int(os.environ.get("PAC_BATCH_HARD_CAP", "25")), 25)
    max_n = min(args.max or 10, hard_cap)
    if len(urls) > max_n:
        return {
            "ok": False,
            "error_code": "LIMIT_EXCEEDED",
            "failure_class": "config",
            "error": f"url count {len(urls)} > max {max_n}",
            "strategy_hit": [],
        }
    if not urls:
        return {
            "ok": False,
            "error_code": "INTERNAL",
            "failure_class": "config",
            "error": "no URLs",
            "strategy_hit": [],
        }

    sites_js = getattr(args, "sites_js", None)
    sync_result = maybe_sync_rules(
        sites_js=sites_js, disabled=bool(getattr(args, "no_rule_sync", False))
    )
    sites, ver, warnings = get_sites_map_with_version(sites_js)
    warnings = list(dict.fromkeys(list(sync_result.get("warnings") or []) + list(warnings)))
    sem = aio.Semaphore(args.concurrency or 2)
    results = []

    async def one(u: str) -> dict:
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
            )
            r.pop("_image_urls", None)
            markdown = r.get("markdown") or ""
            if r.get("ok") and args.out_dir:
                slug = _slugify(r.get("title") or domain)
                md_path = Path(args.out_dir) / slug / f"{slug}.md"
                md_path.parent.mkdir(parents=True, exist_ok=True)
                md_path.write_text(markdown, encoding="utf-8")
                r["path"] = str(md_path)
            if not args.full:
                r["markdown"], _ = truncate_markdown(markdown, BATCH_SUMMARY_CHARS)
                r["truncated"] = True
            return r

    results = list(await aio.gather(*[one(u) for u in urls]))
    success = sum(1 for r in results if r.get("ok"))
    return {
        "ok": True,
        "total": len(urls),
        "success": success,
        "failed": len(urls) - success,
        "rule_version": ver,
        "warnings": warnings,
        "results": results,
    }


def _cmd_install_browser(args) -> dict:
    import subprocess

    r = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True,
        text=True,
    )
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
                if domain.endswith(d) or d.endswith(domain):
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
