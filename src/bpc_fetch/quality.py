"""Language-agnostic structural quality gate for extracted article content.

The public API remains compatible with the original implementation while the
internal evaluator combines lexical, structural, and DOM-derived signals.  The
quality gate is deliberately conservative around access-control shells and
navigation pages, but it preserves genuine short newsflashes when they exhibit
article-like structure.
"""
from __future__ import annotations

import json
import math
import re
import statistics
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from bs4 import BeautifulSoup, Tag

MIN_CONTENT_CHARS = 100
MIN_NEWSFLASH_CHARS = 45
TEASER_WINDOW = 1200
MAX_HTML_ANALYSIS_CHARS = 3_000_000
SHORT_NEWSFLASH_MAX_CHARS = 700
QUALITY_PASS_SCORE = 0.52

_TEASER_MARKERS = (
    "log in or create an account to continue",
    "subscribe to continue reading",
    "subscribe to continue",
    "subscribe to read",
    "subscription required",
    "sign in to continue",
    "sign in to read",
    "log in to continue",
    "login to continue",
    "create a free account to continue",
    "create a free account",
    "this article is for subscribers",
    "to read the full story",
    "continue reading this article",
    "register for free to continue reading",
    "register to continue",
    "already a subscriber? sign in",
    "already a subscriber",
    "become a subscriber",
    "want to read more?",
    "unlock this article",
    "premium content",
    "members only",
    "member-only",
    "subscribers only",
    "subscriber-only",
    "remaining free articles",
    "you've reached your limit",
    "you have reached your article limit",
    "to continue reading",
    "abonnez-vous pour continuer",
    "connectez-vous pour continuer",
    "suscríbete para continuar",
    "inicia sesión para continuar",
    "abonnieren sie, um weiterzulesen",
    "melden sie sich an, um weiterzulesen",
    "assine para continuar lendo",
    "accedi per continuare",
    "abbonati per continuare",
    "请登录后继续",
    "登录后继续阅读",
    "订阅后继续阅读",
    "本文为付费内容",
    "會員限定",
    "会員限定",
    "続きを読むには",
    "구독 후 계속",
    "로그인 후 계속",
    "اشترك لمتابعة القراءة",
    "سجل الدخول للمتابعة",
    "подпишитесь, чтобы продолжить",
    "войдите, чтобы продолжить",
)

_CLEAN_MARKERS = (
    "Enjoying our latest content?",
    "Log in or create an account to continue",
    "Subscribe to continue reading",
    "Already a subscriber?",
    "Sign in to continue",
    "Create a free account to continue",
    "Register for free to continue reading",
    "Want to read more?",
    "请登录后继续",
    "订阅后继续阅读",
    "本文为付费内容",
)

_NAVIGATION_SIGNALS = (
    "skip navigation",
    "pre-markets",
    "search quotes, news & videos",
    "investing club",
    "latest video",
)

_PAYWALL_ATTRIBUTE_RE = re.compile(
    r"(?:paywall|meter(?:ed)?|subscribe|subscription|subscriber|premium|"
    r"registration|register|login|sign[-_ ]?in|content[-_ ]?gate|hard[-_ ]?gate|"
    r"soft[-_ ]?gate|locked[-_ ]?content|overlay|modal)",
    re.IGNORECASE,
)

_CHALLENGE_FORM_RE = re.compile(
    r"(?:captcha|challenge|turnstile|verify|verification|human|bot[-_ ]?check)",
    re.IGNORECASE,
)

_PROVIDER_PATTERNS: Sequence[tuple[str, Sequence[re.Pattern[str]]]] = (
    (
        "cloudflare",
        (
            re.compile(r"(?:__cf_chl|cf-chl-|challenge-platform|cdn-cgi/challenge)", re.I),
            re.compile(r"(?:cf-turnstile|challenges\.cloudflare\.com|cloudflare ray id)", re.I),
        ),
    ),
    (
        "akamai",
        (
            re.compile(r"(?:errors\.edgesuite\.net|akamai[-_ ]?bot|akamai[-_ ]?bm)", re.I),
            re.compile(r"(?:_abck|bm_sz|bm_sv|sensor_data)", re.I),
        ),
    ),
    (
        "datadome",
        (
            re.compile(r"(?:datadome|captcha-delivery\.com|geo\.captcha-delivery\.com)", re.I),
        ),
    ),
    (
        "perimeterx",
        (
            re.compile(r"(?:perimeterx|px-captcha|_pxhd|px-cloud\.net|human-challenge)", re.I),
        ),
    ),
    (
        "imperva",
        (
            re.compile(r"(?:incapsula|imperva|visid_incap|incap_ses)", re.I),
        ),
    ),
    (
        "aws-waf",
        (
            re.compile(r"(?:awswaf|aws-waf-token|challenge\.js|captcha\.js)", re.I),
        ),
    ),
    (
        "f5",
        (
            re.compile(r"(?:f5[-_ ]?bot|bigip|ts[a-z0-9]{6,}=|support id is)", re.I),
        ),
    ),
)

_CHALLENGE_TITLE_RE = re.compile(
    r"^(?:just a moment|attention required|security verification|verify (?:you are|that you are) human|"
    r"checking your browser|one more step|robot check|captcha|request unsuccessful|"
    r"please wait|访问验证|安全验证|人机验证|잠시만 기다려|보안 확인)\b",
    re.IGNORECASE,
)

_ACCESS_DENIED_TITLE_RE = re.compile(
    r"^(?:access denied|forbidden|request blocked|service unavailable|"
    r"拒绝访问|访问被拒绝|アクセスが拒否されました|접근이 거부되었습니다)\b",
    re.IGNORECASE,
)

_TERMINAL_PUNCTUATION = frozenset(".!?。！？…؟।॥")
_CLOSING_PUNCTUATION = "\"'”’»）)]}】》」』"


@dataclass
class QualityResult:
    """Result returned by :func:`quality_check`.

    The first four fields preserve the original public contract.  ``score`` and
    ``metrics`` are additive diagnostics and therefore do not break existing
    callers that construct or inspect the legacy fields.
    """

    ok: bool
    paywall_suspected: bool
    reason: str = ""
    error_code: str = ""
    score: float = 0.0
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AccessControlResult:
    """Structural classification of a WAF, bot challenge, or HTTP block shell."""

    detected: bool
    challenge: bool
    provider: str = ""
    reason: str = ""
    score: float = 0.0


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _meaningful_length(value: str) -> int:
    count = 0
    for character in value or "":
        if character.isspace():
            continue
        category = unicodedata.category(character)
        if category.startswith("C"):
            continue
        count += 1
    return count


def _letter_number_ratio(value: str) -> float:
    meaningful = 0
    letters_or_numbers = 0
    for character in value or "":
        if character.isspace():
            continue
        category = unicodedata.category(character)
        if category.startswith("C"):
            continue
        meaningful += 1
        if category.startswith("L") or category.startswith("N"):
            letters_or_numbers += 1
    if meaningful == 0:
        return 0.0
    return letters_or_numbers / meaningful


def _split_paragraphs(text: str) -> list[str]:
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = [segment.strip() for segment in re.split(r"\n\s*\n+", raw) if segment.strip()]
    if len(blocks) <= 1:
        lines = [line.strip() for line in raw.split("\n") if line.strip()]
        if len(lines) > 1:
            blocks = lines
    return blocks or ([raw.strip()] if raw.strip() else [])


def _paragraph_metrics(text: str) -> dict[str, Any]:
    paragraphs = _split_paragraphs(text)
    lengths = [_meaningful_length(paragraph) for paragraph in paragraphs if paragraph]
    lengths = [length for length in lengths if length > 0]
    count = len(lengths)
    mean = statistics.fmean(lengths) if lengths else 0.0
    variance = statistics.pvariance(lengths) if len(lengths) > 1 else 0.0
    standard_deviation = math.sqrt(variance)
    coefficient_of_variation = standard_deviation / mean if mean > 0 else 0.0
    long_count = sum(length >= 80 for length in lengths)
    short_count = sum(length <= 40 for length in lengths)
    return {
        "paragraph_count": count,
        "paragraph_lengths": lengths,
        "paragraph_mean": mean,
        "paragraph_variance": variance,
        "paragraph_cv": coefficient_of_variation,
        "long_paragraph_count": long_count,
        "short_paragraph_ratio": short_count / count if count else 0.0,
    }


def _terminal_count(text: str) -> int:
    return sum((text or "").count(mark) for mark in _TERMINAL_PUNCTUATION)


def _has_terminal_ending(text: str) -> bool:
    candidate = (text or "").rstrip()
    while candidate and candidate[-1] in _CLOSING_PUNCTUATION:
        candidate = candidate[:-1].rstrip()
    return bool(candidate and candidate[-1] in _TERMINAL_PUNCTUATION)


def _repeated_line_ratio(text: str) -> float:
    lines = [
        _normalize_space(line).casefold()
        for line in (text or "").replace("\r", "\n").split("\n")
        if _meaningful_length(line) >= 3
    ]
    if len(lines) < 4:
        return 0.0
    unique = len(set(lines))
    return max(0.0, 1.0 - (unique / len(lines)))


def _extract_title_from_soup(soup: BeautifulSoup) -> str:
    if soup.title and soup.title.string:
        return _normalize_space(str(soup.title.string))
    heading = soup.find("h1")
    if heading is not None:
        return _normalize_space(heading.get_text(" ", strip=True))
    return ""


def _walk_article_body(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() == "articlebody" and isinstance(item, str):
                yield item
            else:
                yield from _walk_article_body(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            yield from _walk_article_body(item)


def _jsonld_article_body_length(soup: BeautifulSoup) -> int:
    maximum = 0
    for script in soup.find_all("script"):
        script_type = str(script.get("type") or "").casefold()
        if "ld+json" not in script_type:
            continue
        raw = script.string or script.get_text("", strip=False)
        if not raw or len(raw) > 1_000_000:
            continue
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        for body in _walk_article_body(parsed):
            maximum = max(maximum, _meaningful_length(body))
    return maximum


def _attribute_blob(tag: Tag) -> str:
    values: list[str] = []
    for key in ("id", "class", "role", "aria-label", "data-testid", "data-test", "data-component"):
        value = tag.get(key)
        if isinstance(value, (list, tuple)):
            values.extend(str(item) for item in value)
        elif value is not None:
            values.append(str(value))
    return " ".join(values)


def _maximum_dom_depth(root: Tag | BeautifulSoup, cap: int = 80) -> int:
    maximum = 0
    stack: list[tuple[Tag | BeautifulSoup, int]] = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        maximum = max(maximum, depth)
        if depth >= cap:
            continue
        for child in getattr(node, "children", ()):  # pragma: no branch - defensive
            if isinstance(child, Tag):
                stack.append((child, depth + 1))
    return maximum


def _select_article_container(soup: BeautifulSoup) -> Tag | None:
    selectors = (
        "article",
        "main article",
        "[role='main'] article",
        "[itemprop='articleBody']",
        "[data-article]",
        "[data-component='body']",
        ".article-body",
        ".article__body",
        ".story-body",
        ".story-text",
        ".post-content",
        ".entry-content",
    )
    candidates: list[Tag] = []
    for selector in selectors:
        try:
            candidates.extend(tag for tag in soup.select(selector) if isinstance(tag, Tag))
        except Exception:
            continue
    if not candidates:
        main = soup.find("main")
        if isinstance(main, Tag):
            candidates.append(main)
    if not candidates and isinstance(soup.body, Tag):
        candidates.append(soup.body)
    if not candidates:
        return None
    return max(candidates, key=lambda tag: _meaningful_length(tag.get_text(" ", strip=True)))


def _analyze_html(html: str) -> dict[str, Any]:
    if not html:
        return {}
    sample = html[:MAX_HTML_ANALYSIS_CHARS]
    try:
        soup = BeautifulSoup(sample, "html.parser")
    except Exception:
        return {
            "html_chars": len(sample),
            "html_parse_failed": True,
        }

    title = _extract_title_from_soup(soup)
    jsonld_body_length = _jsonld_article_body_length(soup)
    all_tags = list(soup.find_all(True))
    tag_count = len(all_tags)
    script_count = len(soup.find_all("script"))
    form_count = len(soup.find_all("form"))
    button_count = len(soup.find_all(["button"]))
    input_count = len(soup.find_all("input"))
    password_input_count = len(soup.find_all("input", attrs={"type": re.compile(r"password", re.I)}))
    meta_refresh_count = len(
        soup.find_all("meta", attrs={"http-equiv": re.compile(r"^refresh$", re.I)})
    )

    paywall_attribute_count = 0
    modal_count = 0
    hidden_prose_count = 0
    challenge_form_count = 0
    for tag in all_tags:
        attributes = _attribute_blob(tag)
        if attributes and _PAYWALL_ATTRIBUTE_RE.search(attributes):
            paywall_attribute_count += 1
        if str(tag.get("role") or "").casefold() in {"dialog", "alertdialog"}:
            modal_count += 1
        style = str(tag.get("style") or "").casefold()
        if (
            _meaningful_length(tag.get_text(" ", strip=True)) >= 200
            and (
                "overflow:hidden" in style.replace(" ", "")
                or "max-height:" in style.replace(" ", "")
                or "clip-path:" in style.replace(" ", "")
            )
        ):
            hidden_prose_count += 1
        if tag.name == "form":
            action_and_attributes = f"{tag.get('action') or ''} {attributes}"
            if _CHALLENGE_FORM_RE.search(action_and_attributes):
                challenge_form_count += 1

    schema_metrics = {
        "jsonld_article_body_chars": jsonld_body_length,
        "raw_title": title,
        "script_count": script_count,
        "form_count": form_count,
        "button_count": button_count,
        "input_count": input_count,
        "password_input_count": password_input_count,
        "meta_refresh_count": meta_refresh_count,
        "paywall_attribute_count": paywall_attribute_count,
        "modal_count": modal_count,
        "hidden_prose_count": hidden_prose_count,
        "challenge_form_count": challenge_form_count,
    }

    for removable in soup.find_all(["script", "style", "noscript", "template", "svg"]):
        removable.decompose()

    container = _select_article_container(soup)
    visible_text = _normalize_space(soup.get_text(" ", strip=True))
    visible_chars = _meaningful_length(visible_text)
    html_chars = max(len(sample), 1)

    if container is None:
        container_text = ""
        paragraph_tags: list[Tag] = []
        link_tags: list[Tag] = []
        list_item_count = 0
        heading_count = 0
        container_tag_count = 0
    else:
        container_text = _normalize_space(container.get_text(" ", strip=True))
        paragraph_tags = [tag for tag in container.find_all("p") if isinstance(tag, Tag)]
        link_tags = [tag for tag in container.find_all("a") if isinstance(tag, Tag)]
        list_item_count = len(container.find_all("li"))
        heading_count = len(container.find_all(re.compile(r"^h[1-6]$")))
        container_tag_count = len(container.find_all(True))

    container_chars = _meaningful_length(container_text)
    paragraph_lengths = [
        _meaningful_length(tag.get_text(" ", strip=True))
        for tag in paragraph_tags
        if _meaningful_length(tag.get_text(" ", strip=True)) > 0
    ]
    link_text_chars = sum(_meaningful_length(tag.get_text(" ", strip=True)) for tag in link_tags)
    cta_count = sum(
        1
        for tag in list(link_tags) + [tag for tag in soup.find_all("button") if isinstance(tag, Tag)]
        if 0 < _meaningful_length(tag.get_text(" ", strip=True)) <= 80
    )

    navigation_regions = soup.find_all(["nav", "aside", "header", "footer"])
    navigation_chars = sum(
        _meaningful_length(tag.get_text(" ", strip=True))
        for tag in navigation_regions
        if isinstance(tag, Tag)
    )

    paragraph_mean = statistics.fmean(paragraph_lengths) if paragraph_lengths else 0.0
    paragraph_variance = statistics.pvariance(paragraph_lengths) if len(paragraph_lengths) > 1 else 0.0
    paragraph_cv = math.sqrt(paragraph_variance) / paragraph_mean if paragraph_mean > 0 else 0.0

    metrics: dict[str, Any] = {
        "html_chars": len(sample),
        "html_truncated_for_analysis": len(html) > len(sample),
        "tag_count": tag_count,
        "container_tag_count": container_tag_count,
        "dom_depth": _maximum_dom_depth(soup),
        "visible_text_chars": visible_chars,
        "container_text_chars": container_chars,
        "text_to_html_ratio": visible_chars / html_chars,
        "article_text_ratio": container_chars / visible_chars if visible_chars else 0.0,
        "link_text_chars": link_text_chars,
        "link_density": link_text_chars / container_chars if container_chars else 0.0,
        "link_count": len(link_tags),
        "paragraph_count_dom": len(paragraph_lengths),
        "paragraph_mean_dom": paragraph_mean,
        "paragraph_variance_dom": paragraph_variance,
        "paragraph_cv_dom": paragraph_cv,
        "long_paragraph_count_dom": sum(length >= 80 for length in paragraph_lengths),
        "list_item_count": list_item_count,
        "heading_count": heading_count,
        "cta_count": cta_count,
        "navigation_text_ratio": navigation_chars / visible_chars if visible_chars else 0.0,
        "article_container_present": bool(container is not None and container.name != "body"),
        "interactive_density": (len(link_tags) + button_count + input_count) / max(tag_count, 1),
    }
    metrics.update(schema_metrics)
    return metrics


def _merge_dom_metrics(metrics: dict[str, Any], dom_metrics: Mapping[str, Any] | None) -> None:
    if not dom_metrics:
        return
    source: Mapping[str, Any]
    nested = dom_metrics.get("metrics") if isinstance(dom_metrics, Mapping) else None
    if isinstance(nested, Mapping):
        source = nested
    else:
        source = dom_metrics
    aliases = {
        "text_chars": "container_text_chars",
        "html_chars": "html_chars",
        "paragraph_count": "paragraph_count_dom",
        "paragraph_mean": "paragraph_mean_dom",
        "paragraph_variance": "paragraph_variance_dom",
        "paragraph_cv": "paragraph_cv_dom",
        "link_density": "link_density",
        "link_text_chars": "link_text_chars",
        "link_count": "link_count",
        "dom_depth": "dom_depth",
        "tag_count": "tag_count",
        "article_text_ratio": "article_text_ratio",
        "text_to_html_ratio": "text_to_html_ratio",
        "article_container_present": "article_container_present",
        "form_count": "form_count",
        "button_count": "button_count",
        "input_count": "input_count",
        "paywall_attribute_count": "paywall_attribute_count",
        "hidden_prose_count": "hidden_prose_count",
    }
    for source_key, destination_key in aliases.items():
        value = source.get(source_key)
        if value is None:
            continue
        if destination_key not in metrics or not metrics[destination_key]:
            metrics[destination_key] = value


def _window(title: str, text: str) -> str:
    head = (text or "")[:TEASER_WINDOW]
    return f"{title or ''}\n{head}".casefold()


def is_teaser(title: str, text: str) -> bool:
    """Return ``True`` for explicit subscription/login teaser language."""

    window = _window(title, text)
    if not window.strip():
        return False
    return any(marker.casefold() in window for marker in _TEASER_MARKERS)


def is_navigation_shell(title: str, text: str) -> bool:
    """Detect publisher navigation mistakenly extracted as article content."""

    window = _window(title, text)
    if "do not delete" in (title or "").casefold():
        if sum(signal in window for signal in _NAVIGATION_SIGNALS) >= 3:
            return True

    paragraphs = _split_paragraphs(text)
    if len(paragraphs) >= 8:
        lengths = [_meaningful_length(paragraph) for paragraph in paragraphs]
        short_ratio = sum(length <= 35 for length in lengths) / max(len(lengths), 1)
        if short_ratio >= 0.75 and _repeated_line_ratio(text) >= 0.2:
            return True
    return False


def classify_access_control_page(
    html: str,
    title: str = "",
    status: int = 0,
) -> AccessControlResult:
    """Classify an access-control response using status, DOM, and provider signals.

    The function distinguishes interactive bot challenges from plain HTTP
    blocking.  It intentionally does not attempt to solve or bypass a CAPTCHA.
    """

    raw = (html or "")[:400_000]
    lower = raw.casefold()
    has_markup = bool(
        re.search(r"<(?:html|head|body|script|form|title|div|meta|iframe)\b", raw, re.I)
    )
    normalized_title = _normalize_space(title).casefold()
    if not normalized_title and raw:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
        if title_match:
            normalized_title = _normalize_space(re.sub(r"<[^>]+>", " ", title_match.group(1))).casefold()

    provider = ""
    provider_hits = 0
    for candidate, patterns in _PROVIDER_PATTERNS:
        hits = sum(bool(pattern.search(raw)) for pattern in patterns)
        if hits > provider_hits:
            provider = candidate
            provider_hits = hits

    title_is_challenge = bool(_CHALLENGE_TITLE_RE.search(normalized_title))
    title_is_denied = bool(_ACCESS_DENIED_TITLE_RE.search(normalized_title))
    captcha_signal = bool(
        re.search(
            r"(?:g-recaptcha|hcaptcha|cf-turnstile|captcha-container|captcha-delivery|"
            r"verify.{0,40}human|human.{0,40}verification|robot.{0,30}check)",
            raw,
            re.I | re.S,
        )
    )
    if not has_markup and not title_is_challenge:
        captcha_signal = bool(
            _meaningful_length(raw) < 1800
            and re.search(
                r"(?:verify.{0,30}human|human.{0,30}verification|robot.{0,20}check)",
                raw,
                re.I | re.S,
            )
        )
    challenge_form = bool(
        re.search(
            r"<form[^>]*(?:captcha|challenge|turnstile|verify|human|bot)[^>]*>",
            raw,
            re.I | re.S,
        )
    )
    challenge_script = bool(
        re.search(
            r"(?:challenge-platform|cdn-cgi/challenge|awswaf|px-captcha|captcha-delivery|"
            r"/challenge\.js|/captcha\.js)",
            raw,
            re.I,
        )
    )
    denied_text = bool(
        re.search(
            r"(?:permission to access|access has been denied|request was blocked|"
            r"reference (?:number|id)|support id is|errors\.edgesuite\.net)",
            lower,
            re.I,
        )
    )

    visible_text = _normalize_space(re.sub(r"<[^>]+>", " ", raw))
    visible_chars = _meaningful_length(visible_text)
    script_count = len(re.findall(r"<script\b", raw, re.I))
    paragraph_count = len(re.findall(r"<p\b", raw, re.I))

    score = 0.0
    if status in {401, 403, 407, 429, 451, 503}:
        score += 1.2
    if title_is_challenge:
        score += 3.0
    if title_is_denied:
        score += 1.8
    if provider_hits:
        score += 1.2 + min(provider_hits, 2) * 0.6
    if captcha_signal:
        score += 2.5
    if challenge_form:
        score += 2.0
    if challenge_script:
        score += 1.8
    if denied_text:
        score += 1.4
    if visible_chars < 900 and script_count >= 2 and paragraph_count <= 2:
        score += 0.7
    if re.search(r"<meta[^>]+http-equiv=[\"']?refresh", raw, re.I):
        score += 0.5

    plain_challenge_body = bool(
        re.search(
            r"(?:waiting for|status code|request id|checking your browser|"
            r"enable javascript|verify.{0,30}human|one more step)",
            raw,
            re.I | re.S,
        )
    )
    title_challenge_evidence = bool(
        title_is_challenge
        and (
            has_markup
            or status in {401, 403, 407, 429, 451, 503}
            or plain_challenge_body
        )
    )
    challenge = bool(
        title_challenge_evidence
        or captcha_signal
        or challenge_form
        or challenge_script
        or (
            has_markup
            and provider in {"cloudflare", "datadome", "perimeterx", "imperva", "aws-waf"}
            and score >= 3.2
        )
        or (has_markup and provider == "akamai" and provider_hits >= 2 and score >= 3.6)
    )
    denied_evidence = bool(
        title_is_denied
        and visible_chars < 5000
        and (
            has_markup
            or status in {401, 403, 407, 429, 451}
            or denied_text
        )
    )
    blocked = bool(
        challenge
        or denied_evidence
        or (denied_text and (has_markup or visible_chars < 1800))
        or (status in {401, 403, 407, 429, 451} and visible_chars < 5000)
        or (has_markup and score >= 4.0)
    )

    if not blocked:
        return AccessControlResult(False, False, provider="", reason="", score=score)
    if challenge:
        reason = f"{provider or 'generic'}_challenge"
    else:
        reason = f"{provider or 'generic'}_http_block"
    return AccessControlResult(True, challenge, provider=provider, reason=reason, score=score)


def is_challenge_shell(title: str, text: str) -> bool:
    """Detect access-control or challenge pages extracted as article content."""

    title_lower = _normalize_space(title).casefold()
    window = _window(title, text)
    if title_lower == "security verification":
        return "challenge" in window and ("status code" in window or "waiting for" in window)
    if title_lower == "access denied":
        return "permission to access" in window or "errors.edgesuite.net" in window
    return classify_access_control_page(text, title=title).detected


def _structural_navigation(metrics: Mapping[str, Any], text: str) -> bool:
    link_density = float(metrics.get("link_density") or 0.0)
    link_count = int(metrics.get("link_count") or 0)
    paragraph_count = int(metrics.get("paragraph_count_dom") or 0)
    list_items = int(metrics.get("list_item_count") or 0)
    navigation_ratio = float(metrics.get("navigation_text_ratio") or 0.0)
    article_ratio = float(metrics.get("article_text_ratio") or 0.0)
    interactive_density = float(metrics.get("interactive_density") or 0.0)

    if link_count >= 8 and link_density >= 0.55 and paragraph_count <= 2:
        return True
    if navigation_ratio >= 0.58 and article_ratio < 0.35 and paragraph_count <= 3:
        return True
    if list_items >= 10 and list_items >= max(4, paragraph_count * 4) and link_density >= 0.35:
        return True
    if interactive_density >= 0.25 and paragraph_count <= 1 and _meaningful_length(text) < 1000:
        return True
    return False


def _structural_teaser(
    text: str,
    title: str,
    metrics: Mapping[str, Any],
) -> tuple[bool, str]:
    if is_teaser(title, text):
        return True, "teaser_markers"

    content_chars = _meaningful_length(text)
    paragraph_count = int(metrics.get("paragraph_count_dom") or 0)
    if not paragraph_count:
        paragraph_count = int(_paragraph_metrics(text).get("paragraph_count") or 0)
    link_density = float(metrics.get("link_density") or 0.0)
    form_count = int(metrics.get("form_count") or 0)
    password_inputs = int(metrics.get("password_input_count") or 0)
    modal_count = int(metrics.get("modal_count") or 0)
    paywall_attributes = int(metrics.get("paywall_attribute_count") or 0)
    hidden_prose = int(metrics.get("hidden_prose_count") or 0)
    jsonld_chars = int(metrics.get("jsonld_article_body_chars") or 0)
    cta_count = int(metrics.get("cta_count") or 0)

    schema_mismatch = bool(
        jsonld_chars >= 800
        and content_chars < 1600
        and jsonld_chars >= max(content_chars * 2.2, content_chars + 500)
    )
    access_ui = bool(
        password_inputs
        or (modal_count and form_count)
        or hidden_prose
        or (paywall_attributes >= 1 and schema_mismatch)
    )
    sparse_shell = bool(content_chars < 1200 and paragraph_count <= 3)
    abrupt = bool(
        content_chars < 1200
        and (
            (text or "").rstrip().endswith("…") or bool(re.search(r"\.{3}$", (text or "").rstrip()))
            or (not _has_terminal_ending(text) and content_chars >= 180)
        )
    )

    if schema_mismatch and (access_ui or hidden_prose or abrupt):
        return True, "teaser_schema_mismatch"
    if sparse_shell and access_ui and (cta_count >= 1 or link_density >= 0.2):
        return True, "teaser_access_shell"
    if sparse_shell and paywall_attributes >= 1 and hidden_prose >= 1:
        return True, "teaser_clipped_content"
    if content_chars < 700 and form_count >= 1 and cta_count >= 2 and link_density >= 0.25:
        return True, "teaser_login_shell"
    return False, ""


def _is_short_newsflash(
    text: str,
    title: str,
    metrics: Mapping[str, Any],
    score: float,
) -> bool:
    content_chars = _meaningful_length(text)
    if not (MIN_NEWSFLASH_CHARS <= content_chars <= SHORT_NEWSFLASH_MAX_CHARS):
        return False
    if _meaningful_length(title) < 3:
        return False
    paragraph_count = int(_paragraph_metrics(text).get("paragraph_count") or 0)
    if paragraph_count == 0 or paragraph_count > 5:
        return False
    if float(metrics.get("link_density") or 0.0) > 0.22:
        return False
    if int(metrics.get("form_count") or 0) > 0 or int(metrics.get("password_input_count") or 0) > 0:
        return False
    terminal = _has_terminal_ending(text) or _terminal_count(text) >= 1
    numeric_chars = sum(character.isdigit() for character in text)
    numeric_density = numeric_chars / max(content_chars, 1)
    time_or_market_signal = bool(
        re.search(
            r"(?:\b\d{1,2}:\d{2}\b|\b\d+(?:\.\d+)?%\b|[$€£¥₹₩₽]|"
            r"\b[A-Z]{2,6}\b|\bQ[1-4]\b)",
            text,
        )
    )
    sentence_count = _terminal_count(text)
    return bool(
        terminal
        and score >= 0.46
        and (
            numeric_density >= 0.01
            or time_or_market_signal
            or sentence_count >= 2
            or content_chars >= 220
        )
    )


def _quality_score(text: str, title: str, metrics: Mapping[str, Any]) -> float:
    content_chars = _meaningful_length(text)
    paragraph = _paragraph_metrics(text)
    paragraph_count = int(paragraph["paragraph_count"])
    long_paragraph_count = int(paragraph["long_paragraph_count"])
    paragraph_cv = float(paragraph["paragraph_cv"])
    repeated_ratio = _repeated_line_ratio(text)
    terminal_count = _terminal_count(text)
    letter_number_ratio = _letter_number_ratio(text)

    score = 0.35
    if content_chars >= 1800:
        score += 0.22
    elif content_chars >= 900:
        score += 0.18
    elif content_chars >= 450:
        score += 0.12
    elif content_chars >= 220:
        score += 0.07
    elif content_chars >= MIN_CONTENT_CHARS:
        score += 0.03

    if paragraph_count >= 5:
        score += 0.12
    elif paragraph_count >= 3:
        score += 0.09
    elif paragraph_count == 2:
        score += 0.06
    elif paragraph_count == 1:
        score += 0.03

    if long_paragraph_count >= 3:
        score += 0.08
    elif long_paragraph_count >= 1:
        score += 0.04

    if paragraph_count >= 3:
        if 0.12 <= paragraph_cv <= 1.8:
            score += 0.04
        elif paragraph_cv < 0.04 and float(paragraph["short_paragraph_ratio"]) >= 0.7:
            score -= 0.05
        elif paragraph_cv > 2.5:
            score -= 0.02

    if _meaningful_length(title) >= 3:
        score += 0.05
    if terminal_count >= 2:
        score += 0.06
    elif _has_terminal_ending(text):
        score += 0.04
    if letter_number_ratio >= 0.55:
        score += 0.05
    elif letter_number_ratio < 0.35:
        score -= 0.12
    if repeated_ratio >= 0.45:
        score -= 0.15
    elif repeated_ratio >= 0.25:
        score -= 0.08

    if metrics:
        text_to_html = float(metrics.get("text_to_html_ratio") or 0.0)
        link_density = float(metrics.get("link_density") or 0.0)
        article_ratio = float(metrics.get("article_text_ratio") or 0.0)
        dom_paragraphs = int(metrics.get("paragraph_count_dom") or 0)
        dom_depth = int(metrics.get("dom_depth") or 0)
        article_present = bool(metrics.get("article_container_present"))
        form_count = int(metrics.get("form_count") or 0)
        interactive_density = float(metrics.get("interactive_density") or 0.0)

        if article_present:
            score += 0.08
        if article_ratio >= 0.55:
            score += 0.06
        elif article_ratio and article_ratio < 0.15:
            score -= 0.07
        if link_density <= 0.12:
            score += 0.07
        elif link_density >= 0.55:
            score -= 0.25
        elif link_density >= 0.35:
            score -= 0.12
        if text_to_html >= 0.03:
            score += 0.04
        elif 0 < text_to_html < 0.001:
            score -= 0.05
        if dom_paragraphs >= 4:
            score += 0.05
        if 3 <= dom_depth <= 60:
            score += 0.02
        if form_count >= 2 and content_chars < 1000:
            score -= 0.08
        if interactive_density >= 0.25:
            score -= 0.08

    if content_chars < 1200 and not _has_terminal_ending(text):
        score -= 0.05
    return max(0.0, min(1.0, score))


def html_looks_paywalled(html: str) -> bool:
    """Conservatively detect a paywall/login shell in raw HTML."""

    if not html or len(html) < 200:
        return True
    metrics = _analyze_html(html)
    try:
        soup = BeautifulSoup(html[:MAX_HTML_ANALYSIS_CHARS], "html.parser")
        for removable in soup.find_all(["script", "style", "noscript", "template", "svg"]):
            removable.decompose()
        text = _normalize_space(soup.get_text(" ", strip=True))
        title = _extract_title_from_soup(soup)
    except Exception:
        text = _normalize_space(re.sub(r"<[^>]+>", " ", html))
        title = ""
    teaser, _ = _structural_teaser(text, title, metrics)
    return teaser


def html_has_content(html: str) -> bool:
    """Return whether HTML contains enough article-like material to attempt extraction."""

    if not html or len(html) < 300:
        return False
    access = classify_access_control_page(html)
    if access.detected:
        return False
    lower = html.casefold()
    if "<article" in lower or "articlebody" in lower or "article-body" in lower:
        return True
    paragraph_count = len(re.findall(r"<p\b", html, re.I))
    if paragraph_count >= 2:
        text = _normalize_space(re.sub(r"<[^>]+>", " ", html[:MAX_HTML_ANALYSIS_CHARS]))
        return _meaningful_length(text) >= MIN_CONTENT_CHARS
    metrics = _analyze_html(html)
    return bool(
        int(metrics.get("container_text_chars") or 0) >= 250
        and float(metrics.get("link_density") or 0.0) < 0.45
    )


def quality_check(
    text: str,
    title: str = "",
    *,
    allow_partial: bool = False,
    html: str = "",
    dom_metrics: Mapping[str, Any] | None = None,
) -> QualityResult:
    """Evaluate extracted content using language-agnostic structural signals.

    ``text`` and ``title`` retain their original positional semantics.  ``html``
    and ``dom_metrics`` are optional additive inputs used for DOM density,
    text-to-HTML ratio, paragraph variance, and link-density analysis.
    """

    candidate = (text or "").strip()
    metrics = _paragraph_metrics(candidate)
    html_metrics = _analyze_html(html) if html else {}
    metrics.update(html_metrics)
    _merge_dom_metrics(metrics, dom_metrics)
    metrics["content_chars"] = _meaningful_length(candidate)
    metrics["title_chars"] = _meaningful_length(title)
    metrics["repeated_line_ratio"] = _repeated_line_ratio(candidate)
    metrics["terminal_count"] = _terminal_count(candidate)
    metrics["letter_number_ratio"] = _letter_number_ratio(candidate)

    access_source = html if html else candidate
    access = classify_access_control_page(access_source, title=title)
    if access.detected:
        metrics["access_control_provider"] = access.provider
        metrics["access_control_challenge"] = access.challenge
        metrics["access_control_score"] = access.score
        return QualityResult(
            ok=False,
            paywall_suspected=False,
            reason="challenge_shell",
            error_code="EXTRACT_FAILED",
            score=0.0,
            metrics=metrics,
        )

    if is_navigation_shell(title, candidate) or _structural_navigation(metrics, candidate):
        return QualityResult(
            ok=False,
            paywall_suspected=False,
            reason="navigation_shell",
            error_code="EXTRACT_FAILED",
            score=0.0,
            metrics=metrics,
        )

    teaser, teaser_reason = _structural_teaser(candidate, title, metrics)
    content_chars = int(metrics["content_chars"])
    if content_chars < MIN_CONTENT_CHARS:
        if teaser:
            return QualityResult(
                ok=False,
                paywall_suspected=True,
                reason=teaser_reason or "teaser_markers",
                error_code="PAYWALL_REMAINING",
                score=0.0,
                metrics=metrics,
            )
        provisional_score = _quality_score(candidate, title, metrics)
        metrics["quality_score"] = provisional_score
        if _is_short_newsflash(candidate, title, metrics, provisional_score):
            return QualityResult(
                ok=True,
                paywall_suspected=False,
                reason="pass",
                error_code="",
                score=provisional_score,
                metrics=metrics,
            )
        return QualityResult(
            ok=False,
            paywall_suspected=False,
            reason=f"content_chars={content_chars} < {MIN_CONTENT_CHARS}",
            error_code="EXTRACT_FAILED",
            score=provisional_score,
            metrics=metrics,
        )

    if teaser and not allow_partial:
        return QualityResult(
            ok=False,
            paywall_suspected=True,
            reason=teaser_reason or "teaser_markers",
            error_code="PAYWALL_REMAINING",
            score=0.0,
            metrics=metrics,
        )
    if teaser and allow_partial:
        score = _quality_score(candidate, title, metrics)
        return QualityResult(
            ok=True,
            paywall_suspected=True,
            reason="teaser_allowed_partial",
            error_code="",
            score=score,
            metrics=metrics,
        )

    score = _quality_score(candidate, title, metrics)
    metrics["quality_score"] = score
    if score >= QUALITY_PASS_SCORE or _is_short_newsflash(candidate, title, metrics, score):
        return QualityResult(
            ok=True,
            paywall_suspected=False,
            reason="pass",
            error_code="",
            score=score,
            metrics=metrics,
        )

    return QualityResult(
        ok=False,
        paywall_suspected=False,
        reason=f"structural_score={score:.3f} < {QUALITY_PASS_SCORE:.3f}",
        error_code="EXTRACT_FAILED",
        score=score,
        metrics=metrics,
    )


def clean_paywall_text(text: str) -> str:
    """Trim trailing paywall/login prompts while preserving the article prefix."""

    if not text:
        return ""
    folded = text.casefold()
    earliest: int | None = None
    for marker in _CLEAN_MARKERS:
        index = folded.find(marker.casefold())
        if index > 0 and (earliest is None or index < earliest):
            earliest = index
    if earliest is not None:
        return text[:earliest].rstrip()
    return text
