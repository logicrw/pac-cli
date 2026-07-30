"""§15.2.4 archive 条件触发 + 冷却。"""
import time

from bpc_fetch.sites import SiteStrategy
from bpc_fetch.strategy import (
    ARCHIVE_IS_COOLDOWN_S,
    _archive_is_fail_at,
    build_plan,
)


def test_archive_not_in_default_plan():
    s = SiteStrategy(domain="x.com", referer_custom="https://a.com/")
    assert "archive_is" not in build_plan(s)
    assert "archive_org" not in build_plan(s)


def test_archive_with_force():
    s = SiteStrategy(domain="x.com")
    plan = build_plan(s, force_archive=True)
    assert "archive_is" in plan and "archive_org" in plan


def test_archive_with_extra_hint():
    s = SiteStrategy(domain="x.com", extra={"archive": 1})
    assert "archive_is" in build_plan(s)


def test_cooldown_window():
    _archive_is_fail_at["y.com"] = time.monotonic()
    assert (time.monotonic() - _archive_is_fail_at["y.com"]) < ARCHIVE_IS_COOLDOWN_S
