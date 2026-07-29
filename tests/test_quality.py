"""A4 quality gate."""
from pathlib import Path

from bpc_fetch.quality import quality_check
from bpc_fetch.result import truncate_markdown, MARKDOWN_MAX_CHARS


def test_full_article_passes():
    text = "This is a long enough article body. " * 20
    q = quality_check(text, "A real title")
    assert q.ok
    assert not q.paywall_suspected


def test_teaser_blocked():
    text = "Subscribe to continue reading the rest of this story. " + ("word " * 50)
    q = quality_check(text, "Locked")
    assert not q.ok
    assert q.error_code == "PAYWALL_REMAINING"


def test_short_extract_failed():
    q = quality_check("too short", "t")
    assert not q.ok
    assert q.error_code == "EXTRACT_FAILED"


def test_allow_partial():
    text = "Subscribe to continue reading. " + ("word " * 50)
    q = quality_check(text, "x", allow_partial=True)
    assert q.ok
    assert q.paywall_suspected


def test_truncate_20k():
    s = "a" * 50_000
    out, trunc = truncate_markdown(s)
    assert trunc
    assert len(out) == MARKDOWN_MAX_CHARS


def test_quality_fixtures_if_present():
    root = Path(__file__).parent / "fixtures" / "quality"
    full_dir = root / "full"
    teaser_dir = root / "teaser"
    if not full_dir.exists():
        return
    for p in full_dir.glob("*.txt"):
        q = quality_check(p.read_text(encoding="utf-8"), p.stem)
        assert q.ok, p.name
    if teaser_dir.exists():
        blocked = 0
        total = 0
        for p in teaser_dir.glob("*.txt"):
            total += 1
            q = quality_check(p.read_text(encoding="utf-8"), p.stem)
            if not q.ok and q.error_code == "PAYWALL_REMAINING":
                blocked += 1
        if total:
            assert blocked / total >= 0.9
