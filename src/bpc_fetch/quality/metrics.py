"""Structural metrics and quality scoring primitives."""
from __future__ import annotations

import json
import math
import re
import statistics
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

from bs4 import BeautifulSoup, Tag

MIN_CONTENT_CHARS = 100

MIN_NEWSFLASH_CHARS = 45

TEASER_WINDOW = 1200

MAX_HTML_ANALYSIS_CHARS = 3_000_000

SHORT_NEWSFLASH_MAX_CHARS = 700

QUALITY_PASS_SCORE = 0.52

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

_TERMINAL_PUNCTUATION = frozenset(".!?。！？…؟।॥")

_CLOSING_PUNCTUATION = "\"'”’»）)]}】》」』"

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
