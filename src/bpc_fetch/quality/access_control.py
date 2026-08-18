"""WAF, bot-challenge, and access-control shell classification."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .metrics import _meaningful_length, _normalize_space
from .paywall import _window

@dataclass(frozen=True)
class AccessControlResult:
    """Structural classification of a WAF, bot challenge, or HTTP block shell."""

    detected: bool
    challenge: bool
    provider: str = ""
    reason: str = ""
    score: float = 0.0

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
