"""Golden quality-gate layouts that freeze release-critical classifications."""
from __future__ import annotations

from bs4 import BeautifulSoup

from bpc_fetch.quality import QUALITY_PASS_SCORE, classify_access_control_page, quality_check


FULL_ARTICLE_HTML = """<!doctype html>
<html><head><title>City launches a new transit plan</title></head><body>
<header><a href="/home">Home</a></header><main><article>
<h1>City launches a new transit plan</h1>
<p>City officials approved a ten year transit plan after months of public hearings. The program adds frequent bus routes, repairs aging stations, and sets measurable reliability targets for riders across the region.</p>
<p>The first phase begins next spring with new service on three crowded corridors. Transportation staff said the schedule was chosen using passenger counts, travel time data, and feedback collected from neighborhood meetings.</p>
<p>Funding will come from an existing capital program and a newly approved state grant. Officials said construction contracts will be published online and reviewed quarterly to keep costs and deadlines visible.</p>
<p>Rider groups welcomed the frequency targets but asked the city to preserve late night service. The final plan includes a yearly review that can shift vehicles when demand changes or construction causes delays.</p>
<p>The council approved the measure by an eight to two vote. The transit agency will publish the first implementation dashboard in January and begin reporting reliability figures every month.</p>
</article></main></body></html>"""

NEWSFLASH_PARAGRAPH = """The central bank left its benchmark rate unchanged on Tuesday after officials reviewed the latest inflation, employment, and consumer spending data. Policymakers said price growth has continued to cool, while hiring remains positive and household demand is expanding at a moderate pace. The statement repeated that future decisions will depend on incoming evidence rather than a preset schedule."""
NEWSFLASH_HTML = (
    "<!doctype html><html><head><title>Central bank holds rates as inflation cools</title></head>"
    "<body><main><article>"
    + "".join(f"<p>{NEWSFLASH_PARAGRAPH}</p>" for _ in range(5))
    + "</article></main></body></html>"
)

CHALLENGE_HTML = """<!doctype html><html><head><title>Security Verification</title></head><body>
<form id="challenge-form" action="/cdn-cgi/challenge-platform">
<div class="cf-turnstile"></div>
<p>Checking your browser before accessing the site. Verify you are human.</p>
<script src="/cdn-cgi/challenge-platform/h/g/orchestrate"></script>
</form></body></html>"""

PAYWALL_TEASER_HTML = """<!doctype html><html><head><title>Markets rally after earnings</title>
<script type="application/ld+json">{"@type":"NewsArticle","articleBody":"Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body. Full subscriber article body."}</script>
</head><body><main><article><h1>Markets rally after earnings</h1>
<p>Stocks rose sharply after several large companies reported stronger quarterly results.</p>
<p>Investors also watched fresh inflation data and comments from central bank officials.</p>
</article><div class="paywall-modal" role="dialog"><form>
<button>Subscribe to continue reading</button><input type="password">
</form></div></main></body></html>"""

NAVIGATION_SHELL_HTML = """<!doctype html><html><head><title>Latest News</title></head><body>
<header><nav>
<a href="/world">World</a><a href="/business">Business</a><a href="/technology">Technology</a>
<a href="/markets">Markets</a><a href="/politics">Politics</a><a href="/science">Science</a>
<a href="/culture">Culture</a><a href="/sports">Sports</a><a href="/opinion">Opinion</a>
<a href="/video">Video</a><a href="/podcasts">Podcasts</a><a href="/newsletters">Newsletters</a>
</nav></header><main><ul>
<li><a href="/story/1">Latest story headline one</a></li><li><a href="/story/2">Latest story headline two</a></li>
<li><a href="/story/3">Latest story headline three</a></li><li><a href="/story/4">Latest story headline four</a></li>
<li><a href="/story/5">Latest story headline five</a></li><li><a href="/story/6">Latest story headline six</a></li>
<li><a href="/story/7">Latest story headline seven</a></li><li><a href="/story/8">Latest story headline eight</a></li>
<li><a href="/story/9">Latest story headline nine</a></li><li><a href="/story/10">Latest story headline ten</a></li>
</ul><button>Menu</button><button>Search</button></main><footer><a href="/privacy">Privacy</a></footer>
</body></html>"""


def _title_and_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    article = soup.find("article")
    source = article if article is not None else soup
    return title, source.get_text("\n", strip=True)


def test_golden_full_article_passes_above_threshold() -> None:
    title, text = _title_and_text(FULL_ARTICLE_HTML)
    result = quality_check(text, title, html=FULL_ARTICLE_HTML)
    assert result.ok is True
    assert result.error_code == ""
    assert result.paywall_suspected is False
    assert result.score >= QUALITY_PASS_SCORE
    assert result.metrics["article_container_present"] is True


def test_golden_newsflash_passes_as_article_content() -> None:
    title, text = _title_and_text(NEWSFLASH_HTML)
    result = quality_check(text, title, html=NEWSFLASH_HTML)
    assert result.ok is True
    assert result.error_code == ""
    assert result.score >= QUALITY_PASS_SCORE
    assert result.metrics["paragraph_count_dom"] == 5


def test_golden_403_challenge_is_never_article_content() -> None:
    access = classify_access_control_page(CHALLENGE_HTML, status=403)
    assert access.detected is True
    assert access.challenge is True
    assert access.provider == "cloudflare"
    title, text = _title_and_text(CHALLENGE_HTML)
    result = quality_check(text, title, html=CHALLENGE_HTML)
    assert result.ok is False
    assert result.error_code == "EXTRACT_FAILED"
    assert result.reason == "challenge_shell"
    assert result.paywall_suspected is False


def test_golden_paywall_teaser_is_rejected() -> None:
    title, text = _title_and_text(PAYWALL_TEASER_HTML)
    result = quality_check(text, title, html=PAYWALL_TEASER_HTML)
    assert result.ok is False
    assert result.error_code == "PAYWALL_REMAINING"
    assert result.paywall_suspected is True
    assert result.reason in {"teaser_markers", "teaser_schema_mismatch", "teaser_access_shell"}


def test_golden_navigation_shell_is_rejected() -> None:
    title, text = _title_and_text(NAVIGATION_SHELL_HTML)
    result = quality_check(text, title, html=NAVIGATION_SHELL_HTML)
    assert result.ok is False
    assert result.error_code == "EXTRACT_FAILED"
    assert result.reason == "navigation_shell"
    assert result.paywall_suspected is False
    assert result.metrics["link_density"] >= 0.55
