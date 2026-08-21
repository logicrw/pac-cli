"""Lightweight article discovery via RSS, Sitemap, and Google News RSS.

Google News RSS commonly wraps publisher links in URL-safe Base64 protobuf
payloads.  PAC decodes the legacy/local form without network access and only
falls back to an SSRF-safe HTTP redirect resolution when local decoding cannot
produce a trustworthy publisher URL.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Iterable

import httpx

from .result import build_diagnostics, new_request_id
from .sites import domain_from_url
from .ssrf import SSRFBlocked, assert_public_url

COMMON_RSS_PATHS = (
    "/feed",
    "/rss",
    "/rss.xml",
    "/feed.xml",
    "/index.xml",
    "/news/feed",
    "/rss/news.xml",
)

COMMON_SITEMAP_PATHS = (
    "/sitemap.xml",
    "/news-sitemap.xml",
    "/sitemap_news.xml",
    "/sitemap-news.xml",
)

TIMEOUT = 15.0
DISCOVERY_REDIRECT_LIMIT = 5
DISCOVERY_MAX_BYTES = 2_000_000
DISCOVERY_MAX_PARSE_CHARS = 5_000_000
GOOGLE_NEWS_REDIRECT_LIMIT = 8
GOOGLE_NEWS_RESOLVE_CONCURRENCY = 8
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
    )
}
_GOOGLE_NEWS_HOSTS = frozenset({"news.google.com", "www.news.google.com"})
_GOOGLE_NEWS_PATH_MARKERS = frozenset({"articles", "read"})
_URL_BYTES_RE = re.compile(rb"https?://[^\x00-\x20\x7f\"'<>\\]+", re.IGNORECASE)


async def _safe_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int = DISCOVERY_MAX_BYTES,
) -> httpx.Response:
    """GET a bounded response while validating every redirect before I/O."""

    current = url
    for redirect_count in range(DISCOVERY_REDIRECT_LIMIT + 1):
        assert_public_url(current)
        async with client.stream("GET", current, follow_redirects=False) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise RuntimeError("redirect missing Location")
                if redirect_count >= DISCOVERY_REDIRECT_LIMIT:
                    raise RuntimeError("redirect limit exceeded")
                current = urllib.parse.urljoin(str(response.url), location)
                continue

            declared = response.headers.get("content-length")
            if declared:
                try:
                    declared_size = int(declared)
                except ValueError as exc:
                    raise RuntimeError("invalid Content-Length") from exc
                if declared_size < 0 or declared_size > max_bytes:
                    raise RuntimeError("discovery response too large")

            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > max_bytes:
                    raise RuntimeError("discovery response too large")
                body.extend(chunk)
            headers = dict(response.headers)
            headers.pop("content-encoding", None)
            headers.pop("Content-Encoding", None)
            headers.pop("content-length", None)
            headers.pop("Content-Length", None)
            return httpx.Response(
                response.status_code,
                headers=headers,
                content=bytes(body),
                request=response.request,
            )
    raise RuntimeError("redirect limit exceeded")


def _discovery_warning(stage: str, exc: BaseException) -> str:
    detail = str(exc).replace("\n", " ").strip()[:180]
    suffix = f":{detail}" if detail else ""
    return f"{stage}:{exc.__class__.__name__}{suffix}"


async def discover_articles(
    target: str,
    *,
    limit: int = 20,
    search_query: str | None = None,
    diagnostics: bool = False,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Discover articles from a domain, RSS feed URL, or Google News search query."""

    effective_limit = max(1, int(limit))
    articles: list[dict[str, str]] = []
    source_type = "unknown"
    source_url = target
    warnings: list[str] = []
    started_at = time.perf_counter()
    diagnostic_request_id = (request_id or new_request_id()) if diagnostics else ""
    diagnostic_attempts: list[dict[str, Any]] = []

    async def diagnostic_get(stage: str, client: httpx.AsyncClient, request_url: str) -> httpx.Response:
        attempt_started = time.perf_counter()
        try:
            response = await _safe_get(client, request_url)
        except Exception as exc:
            if diagnostics:
                diagnostic_attempts.append({
                    "handler": "Discovery",
                    "label": stage,
                    "engine": "discover_http",
                    "status": 0,
                    "elapsed_ms": int((time.perf_counter() - attempt_started) * 1000),
                    "error_code": "SSRF_BLOCKED" if isinstance(exc, SSRFBlocked) else "NETWORK",
                    "error": str(exc),
                    "quality_reason": "",
                })
            raise
        if diagnostics:
            diagnostic_attempts.append({
                "handler": "Discovery",
                "label": stage,
                "engine": "discover_http",
                "status": int(response.status_code),
                "elapsed_ms": int((time.perf_counter() - attempt_started) * 1000),
                "error_code": "" if 200 <= response.status_code < 400 else "HTTP_BLOCKED",
                "error": "",
                "quality_reason": "",
            })
        return response

    async with httpx.AsyncClient(
        timeout=TIMEOUT,
        headers=HEADERS,
        follow_redirects=False,
    ) as client:
        if search_query or not target.startswith("http"):
            domain = target.replace("https://", "").replace("http://", "").split("/")[0]
            domain = domain_from_url(target if target.startswith("http") else f"https://{target}")
            scoped = bool(search_query) or "." in target
            query = f"site:{domain} {search_query}".strip() if search_query else f"site:{domain}"

            # Plain keyword queries hit Bing News RSS first: item links embed
            # the publisher URL (apiclick.aspx?...&url=<encoded>), so no Google
            # News token decoding is needed at all.  Bing does not support the
            # ``site:`` operator, so domain-scoped queries go to Google News.
            bing_query = (search_query or target) if not scoped else search_query
            if bing_query:
                encoded_bing = urllib.parse.quote(bing_query)
                bing_news_url = f"https://www.bing.com/news/search?q={encoded_bing}&format=rss"
                source_url = bing_news_url
                try:
                    response = await diagnostic_get("bing_news", client, bing_news_url)
                    if response.status_code == 200:
                        bing_articles = _parse_rss(response.text, limit=effective_limit)
                        bing_articles = _extract_bing_publisher_urls(bing_articles)
                        if bing_articles:
                            articles = bing_articles
                            source_type = "bing_news_rss"
                except Exception as exc:
                    warnings.append(_discovery_warning("bing_news", exc))

            if not articles:
                encoded_query = urllib.parse.quote(query)
                google_news_url = (
                    "https://news.google.com/rss/search?"
                    f"q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
                )
                source_url = google_news_url
                try:
                    response = await diagnostic_get("google_news", client, google_news_url)
                    if response.status_code == 200:
                        articles = _parse_rss(response.text, limit=effective_limit)
                        if articles:
                            articles = await _resolve_google_news_articles(articles, client)
                            source_type = "google_news_rss"
                except Exception as exc:
                    warnings.append(_discovery_warning("google_news", exc))
        if not articles:
            if target.startswith("http://") or target.startswith("https://"):
                base_url = target.rstrip("/")
            else:
                base_url = f"https://{target}".rstrip("/")

            try:
                response = await diagnostic_get("base", client, base_url)
                if response.status_code == 200:
                    content_type = response.headers.get("content-type", "").lower()
                    if (
                        "xml" in content_type
                        or "rss" in content_type
                        or response.text.strip().startswith("<?xml")
                    ):
                        articles = _parse_rss(response.text, limit=effective_limit) or _parse_sitemap(
                            response.text,
                            limit=effective_limit,
                        )
                        if articles:
                            articles = await _resolve_google_news_articles(articles, client)
                            source_type = "rss_feed"
                            source_url = base_url
                    else:
                        rss_links = _extract_rss_links_from_html(response.text, base_url)
                        for rss_url in rss_links:
                            try:
                                rss_response = await diagnostic_get("rss_link", client, rss_url)
                                if rss_response.status_code == 200:
                                    parsed = _parse_rss(rss_response.text, limit=effective_limit)
                                    if parsed:
                                        articles = await _resolve_google_news_articles(parsed, client)
                                        source_type = "rss_link"
                                        source_url = rss_url
                                        break
                            except Exception as exc:
                                warnings.append(_discovery_warning("rss_link", exc))
                                continue
            except Exception as exc:
                warnings.append(_discovery_warning("base", exc))

            if not articles:
                for path in COMMON_RSS_PATHS:
                    probe_url = urllib.parse.urljoin(base_url, path)
                    try:
                        response = await diagnostic_get("rss_probe", client, probe_url)
                        if response.status_code == 200:
                            parsed = _parse_rss(response.text, limit=effective_limit)
                            if parsed:
                                articles = await _resolve_google_news_articles(parsed, client)
                                source_type = "rss_probe"
                                source_url = probe_url
                                break
                    except Exception as exc:
                        warnings.append(_discovery_warning("rss_probe", exc))
                        continue

            if not articles:
                for path in COMMON_SITEMAP_PATHS:
                    probe_url = urllib.parse.urljoin(base_url, path)
                    try:
                        response = await diagnostic_get("sitemap_probe", client, probe_url)
                        if response.status_code == 200:
                            parsed = _parse_sitemap(response.text, limit=effective_limit)
                            if parsed:
                                articles = parsed
                                source_type = "sitemap"
                                source_url = probe_url
                                break
                    except Exception as exc:
                        warnings.append(_discovery_warning("sitemap_probe", exc))
                        continue

    domain = domain_from_url(target if target.startswith("http") else f"https://{target}")
    urls = [article["url"] for article in articles if article.get("url")]
    next_command = f"pac batch {' '.join(urls[:3])} --compact" if urls else "pac fetch <url>"

    result: dict[str, Any] = {
        "ok": len(articles) > 0,
        "domain": domain,
        "source_type": source_type,
        "source_url": source_url,
        "count": len(articles),
        "articles": articles[:effective_limit],
        "next_command": next_command,
        "warnings": list(dict.fromkeys(warnings)),
    }
    if diagnostics:
        result["diagnostics"] = build_diagnostics(
            request_id=diagnostic_request_id,
            total_latency_ms=int((time.perf_counter() - started_at) * 1000),
            attempts=diagnostic_attempts,
            quality=None,
        )
    return result


def is_google_news_url(url: str) -> bool:
    """Return whether *url* is a Google News encoded article/read URL."""

    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold().rstrip(".")
    if host not in _GOOGLE_NEWS_HOSTS:
        return False
    parts = [part for part in parsed.path.split("/") if part]
    return len(parts) >= 2 and parts[-2].casefold() in _GOOGLE_NEWS_PATH_MARKERS


def _google_news_token(url: str) -> str:
    if not is_google_news_url(url):
        return ""
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    return urllib.parse.unquote(parts[-1]).strip()


def _urlsafe_b64decode(token: str) -> bytes:
    value = token.strip()
    if not value or len(value) > 64_000:
        return b""
    padding = "=" * ((4 - len(value) % 4) % 4)
    try:
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except (ValueError, UnicodeEncodeError, binascii.Error):
        return b""


def _read_varint(data: bytes, offset: int) -> tuple[int, int] | None:
    """Read one protobuf varint and return ``(value, next_offset)``."""

    value = 0
    shift = 0
    position = offset
    while position < len(data) and shift < 70:
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, position
        shift += 7
    return None


def _protobuf_length_fields(
    data: bytes,
    *,
    depth: int = 0,
    max_depth: int = 4,
) -> Iterable[bytes]:
    """Yield protobuf length-delimited fields from a best-effort wire parser.

    Google does not publish the Google News RSS token schema.  This parser does
    not depend on field numbers: it safely walks standard protobuf wire types
    and recursively examines nested length-delimited payloads.
    """

    if depth > max_depth or not data:
        return
    offset = 0
    while offset < len(data):
        key_result = _read_varint(data, offset)
        if key_result is None:
            return
        key, offset = key_result
        wire_type = key & 0x07
        field_number = key >> 3
        if field_number == 0:
            return

        if wire_type == 0:
            value_result = _read_varint(data, offset)
            if value_result is None:
                return
            _, offset = value_result
            continue
        if wire_type == 1:
            if offset + 8 > len(data):
                return
            offset += 8
            continue
        if wire_type == 2:
            length_result = _read_varint(data, offset)
            if length_result is None:
                return
            length, offset = length_result
            if length < 0 or offset + length > len(data):
                return
            payload = data[offset : offset + length]
            offset += length
            yield payload
            if payload and depth < max_depth:
                yield from _protobuf_length_fields(payload, depth=depth + 1, max_depth=max_depth)
            continue
        if wire_type == 5:
            if offset + 4 > len(data):
                return
            offset += 4
            continue
        return


def _sanitize_decoded_url(value: str) -> str:
    candidate = value.strip().strip("\x00\r\n\t ")
    candidate = candidate.rstrip("\x00\x01\x02\x03")
    try:
        parsed = urllib.parse.urlparse(candidate)
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.casefold().rstrip(".")
    if host in _GOOGLE_NEWS_HOSTS:
        return ""
    return candidate


def _decode_url_field(payload: bytes) -> str:
    for encoding in ("utf-8", "latin-1"):
        try:
            text = payload.decode(encoding)
        except UnicodeDecodeError:
            continue
        candidate = _sanitize_decoded_url(text)
        if candidate:
            return candidate
    return ""


def _url_candidates_from_payload(decoded: bytes) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    for field in _protobuf_length_fields(decoded):
        candidate = _decode_url_field(field)
        if candidate and candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)

    for match in _URL_BYTES_RE.finditer(decoded):
        candidate = _decode_url_field(match.group(0))
        if candidate and candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)

    return candidates


def _looks_like_amp_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold().rstrip("/")
    return (
        host.startswith("amp.")
        or path.endswith(".amp")
        or path.endswith("/amp")
        or "/amp/" in path
        or "output=amp" in parsed.query.casefold()
    )


def decode_google_news_url_local(source_url: str) -> str | None:
    """Decode a Google News URL locally without performing any network I/O.

    Returns the canonical publisher URL when the token contains an embedded
    URL in a protobuf length-delimited field; otherwise returns ``None``.
    """

    token = _google_news_token(source_url)
    if not token:
        return None
    decoded = _urlsafe_b64decode(token)
    if not decoded:
        return None
    candidates = _url_candidates_from_payload(decoded)
    if not candidates:
        return None
    non_amp = [candidate for candidate in candidates if not _looks_like_amp_url(candidate)]
    return non_amp[0] if non_amp else candidates[0]


def decode_google_news_url(source_url: str) -> str:
    """Backward-friendly local decoder that returns the input on a miss."""

    return decode_google_news_url_local(source_url) or source_url


async def resolve_google_news_url(
    source_url: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Resolve a Google News article URL, preferring zero-network local decode.

    Network fallback is only attempted when the local protobuf decoder cannot
    recover a publisher URL.  Redirects are followed manually so each hop can
    pass PAC's SSRF guard before any request is sent.
    """

    local = decode_google_news_url_local(source_url)
    if local:
        return local
    if not is_google_news_url(source_url):
        return source_url

    owns_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=TIMEOUT,
        headers=HEADERS,
        follow_redirects=False,
    )
    current = source_url
    try:
        for redirect_count in range(GOOGLE_NEWS_REDIRECT_LIMIT + 1):
            assert_public_url(current)
            response = await active_client.get(current, follow_redirects=False)
            status = int(response.status_code)
            if status not in {301, 302, 303, 307, 308}:
                final_url = str(response.url)
                if final_url and not is_google_news_url(final_url):
                    try:
                        assert_public_url(final_url)
                    except SSRFBlocked:
                        return source_url
                    return final_url
                return source_url

            location = response.headers.get("location")
            if not location or redirect_count >= GOOGLE_NEWS_REDIRECT_LIMIT:
                return source_url
            next_url = urllib.parse.urljoin(str(response.url), location)
            assert_public_url(next_url)
            if not is_google_news_url(next_url):
                return next_url
            current = next_url
    except (httpx.HTTPError, SSRFBlocked, ValueError):
        return source_url
    except Exception:
        return source_url
    finally:
        if owns_client:
            await active_client.aclose()
    return source_url


async def _resolve_google_news_articles(
    articles: list[dict[str, str]],
    client: httpx.AsyncClient,
) -> list[dict[str, str]]:
    encoded_indexes = [
        index
        for index, article in enumerate(articles)
        if is_google_news_url(article.get("url", ""))
    ]
    if not encoded_indexes:
        return articles

    output = [dict(article) for article in articles]
    semaphore = asyncio.Semaphore(GOOGLE_NEWS_RESOLVE_CONCURRENCY)

    async def resolve_one(index: int) -> None:
        original_url = output[index].get("url", "")
        local = decode_google_news_url_local(original_url)
        if local:
            output[index]["url"] = local
            return
        async with semaphore:
            output[index]["url"] = await resolve_google_news_url(original_url, client=client)

    await asyncio.gather(*(resolve_one(index) for index in encoded_indexes))
    return output


def _extract_bing_publisher_urls(
    articles: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Rewrite Bing News RSS links to their publisher URLs.

    Bing item links look like
    ``http://www.bing.com/news/apiclick.aspx?...&url=<percent-encoded target>&...``.
    The encoded ``url`` parameter points straight at the publisher article.
    Items without it (internal Bing pages) are dropped.
    """
    import urllib.parse as _up

    output: list[dict[str, str]] = []
    for article in articles:
        link = article.get("url", "")
        if "bing.com" not in _up.urlsplit(link).netloc:
            output.append(article)
            continue
        query = _up.urlsplit(link).query
        params = _up.parse_qs(query)
        candidates = params.get("url") or []
        publisher = candidates[0] if candidates else ""
        if publisher.startswith("http") and "bing.com" not in _up.urlsplit(publisher).netloc:
            rewritten = dict(article)
            rewritten["url"] = publisher
            output.append(rewritten)
    return output


def _parse_rss(xml_text: str, limit: int = 20) -> list[dict[str, str]]:
    """Parse RSS 2.0 or Atom feed."""

    articles: list[dict[str, str]] = []
    try:
        root = ET.fromstring((xml_text or "")[:DISCOVERY_MAX_PARSE_CHARS])

        def local_name(element: ET.Element) -> str:
            return str(element.tag).rsplit("}", 1)[-1].casefold()

        def child_text(element: ET.Element, *names: str) -> str:
            wanted = {name.casefold() for name in names}
            for child in list(element):
                if local_name(child) in wanted:
                    return (child.text or "").strip()
            return ""

        for item in (node for node in root.iter() if local_name(node) == "item"):
            title = child_text(item, "title")
            link = child_text(item, "link")
            published = child_text(item, "pubDate")
            if link and link.startswith("http"):
                articles.append({"title": title, "url": link, "published": published})
            if len(articles) >= limit:
                break

        if not articles:
            for entry in (node for node in root.iter() if local_name(node) == "entry"):
                title = child_text(entry, "title")
                link = ""
                for child in list(entry):
                    if local_name(child) != "link":
                        continue
                    rel = str(child.attrib.get("rel") or "alternate").casefold()
                    href = str(child.attrib.get("href") or "").strip()
                    if href and rel in {"", "alternate"}:
                        link = href
                        break
                published = child_text(entry, "published", "updated")
                if link and link.startswith("http"):
                    articles.append({"title": title, "url": link, "published": published})
                if len(articles) >= limit:
                    break
    except (ET.ParseError, ValueError, TypeError):
        return []
    return articles


def _parse_sitemap(xml_text: str, limit: int = 20) -> list[dict[str, str]]:
    """Parse XML sitemap for URL locations."""

    articles: list[dict[str, str]] = []
    try:
        root = ET.fromstring((xml_text or "")[:DISCOVERY_MAX_PARSE_CHARS])
        for url_element in root.iter():
            if str(url_element.tag).rsplit("}", 1)[-1].casefold() != "url":
                continue
            location = ""
            last_modified = ""
            for child in list(url_element):
                name = str(child.tag).rsplit("}", 1)[-1].casefold()
                if name == "loc":
                    location = (child.text or "").strip()
                elif name == "lastmod":
                    last_modified = (child.text or "").strip()
            if location and location.startswith("http"):
                articles.append({"title": "", "url": location, "published": last_modified})
            if len(articles) >= limit:
                break
    except (ET.ParseError, ValueError, TypeError):
        return []
    return articles


def _extract_rss_links_from_html(html_text: str, base_url: str) -> list[str]:
    """Find RSS/Atom link tags inside HTML."""

    from bs4 import BeautifulSoup

    links: list[str] = []
    soup = BeautifulSoup((html_text or "")[:DISCOVERY_MAX_PARSE_CHARS], "html.parser")
    for element in soup.find_all("link"):
        media_type = str(element.get("type") or "").casefold().strip()
        if media_type not in {"application/rss+xml", "application/atom+xml"}:
            continue
        href = str(element.get("href") or "").strip()
        if href:
            links.append(urllib.parse.urljoin(base_url, href))
    return links