import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from bpc_fetch import cli
from bpc_fetch.interactive import (
    ARTICLE_READY_JS,
    BACKEND_DRISSION,
    BACKEND_EGO,
    ENGINE,
    READ_ONLY_EXTRACT_JS,
    DrissionPageBackend,
    EgoLiteBackend,
    _ego_extract_script,
    default_backend,
    ensure_ego_lite_running,
    find_ego_lite_app,
    ForbiddenCdpError,
    ForbiddenProfileError,
    InteractiveSettings,
    PageSnapshot,
    assert_dedicated_profile,
    assert_loopback_cdp,
    assert_process_uses_profile,
    assert_tab_isolation,
    fetch_interactive,
    parse_cdp_endpoint,
    parse_user_data_dir,
    provenance,
    redact_secrets,
    reject_cookie_header,
)


FULL_ARTICLE = "This is a long enough article body with real reporting and names. " * 20
TEASER = "Subscribe to continue reading the rest of this story. " + ("word " * 50)
NAV_SHELL = (
    "Skip Navigation Markets Pre-Markets U.S. Markets Europe Markets "
    "Investing Club Latest Video Search quotes, news & videos "
    + ("Watchlist Menu " * 20)
)


class FakeBackend:
    name = BACKEND_DRISSION

    def __init__(self, snapshot: PageSnapshot):
        self.snapshot_value = snapshot
        self.calls = []

    def snapshot(self, url: str, settings: InteractiveSettings) -> PageSnapshot:
        self.calls.append((url, settings))
        return self.snapshot_value


def _profile(tmp_path: Path) -> Path:
    return tmp_path / "pac-interactive-chromium"


def _snapshot(**overrides) -> PageSnapshot:
    data = dict(
        title="A real title",
        final_url="https://www.ft.com/content/abc",
        html="<html><body><article><p>body</p></article></body></html>",
        text=FULL_ARTICLE,
        images=[],
        metrics={"paragraph_count": 8, "container_text_chars": 800, "body_text_chars": 900},
        preexisting_tab_ids=("other",),
        owned_tab_id="owned",
        closed_tab_ids=("owned",),
        remaining_tab_ids=("other",),
    )
    data.update(overrides)
    return PageSnapshot(**data)


def test_extract_script_is_read_only_and_mentions_no_cookies():
    ego_script = _ego_extract_script("https://www.ft.com/content/abc", 45)
    for script in (READ_ONLY_EXTRACT_JS, ARTICLE_READY_JS, ego_script):
        folded = script.casefold()
        assert "cookie" not in folded
        assert "document.cookie" not in folded
        assert "unhide" not in folded
        assert "display = 'none'" not in folded
        assert "remove()" not in folded
    assert "import --browser" not in ego_script
    assert "createTab" in ego_script
    assert "closeTab" in ego_script
    assert "ensureRealTab" not in ego_script


def test_default_backend_is_ego(monkeypatch):
    monkeypatch.delenv("PAC_INTERACTIVE_BACKEND", raising=False)
    assert isinstance(default_backend(), EgoLiteBackend)


def test_find_ego_lite_app_uses_explicit_bundle(tmp_path, monkeypatch):
    app = tmp_path / "ego lite.app"
    app.mkdir()
    monkeypatch.setenv("PAC_INTERACTIVE_EGO_APP", str(app))
    assert find_ego_lite_app() == app.resolve()


def test_ensure_ego_lite_running_launches_when_needed(monkeypatch):
    launches = []
    probes = {"n": 0}

    def fake_probe(binary):
        probes["n"] += 1
        return probes["n"] >= 2

    monkeypatch.setattr("bpc_fetch.interactive._ego_ready_probe", fake_probe)
    monkeypatch.setattr("bpc_fetch.interactive.ego_lite_is_running", lambda: False)
    monkeypatch.setattr("bpc_fetch.interactive.launch_ego_lite", lambda: launches.append("open") or Path("/Applications/ego lite.app"))
    monkeypatch.setattr("bpc_fetch.interactive.time.sleep", lambda _s: None)
    ensure_ego_lite_running(binary="/bin/ego-browser", timeout_s=5)
    assert launches == ["open"]


def test_ensure_ego_lite_running_skips_launch_when_ready(monkeypatch):
    monkeypatch.setattr("bpc_fetch.interactive._ego_ready_probe", lambda binary: True)

    def boom():
        raise AssertionError("must not launch Ego lite when it is already ready")

    monkeypatch.setattr("bpc_fetch.interactive.launch_ego_lite", boom)
    ensure_ego_lite_running(binary="/bin/ego-browser", timeout_s=5)


def test_daily_chrome_profiles_are_rejected(tmp_path):
    banned = [
        tmp_path / "Library/Application Support/Google/Chrome",
        tmp_path / "Library/Application Support/Google/Chrome/Default",
        tmp_path / "Library/Application Support/Microsoft Edge/Default",
        tmp_path / "Library/Application Support/BraveSoftware/Brave-Browser",
        tmp_path / "Library/Application Support/Arc",
        tmp_path / ".config/google-chrome",
        tmp_path / "AppData/Local/Google/Chrome/User Data",
    ]
    for path in banned:
        path.mkdir(parents=True, exist_ok=True)
        with pytest.raises(ForbiddenProfileError):
            assert_dedicated_profile(path)


def test_missing_profile_is_rejected(monkeypatch):
    monkeypatch.delenv("PAC_INTERACTIVE_PROFILE", raising=False)
    with pytest.raises(ForbiddenProfileError):
        assert_dedicated_profile(None)


def test_dedicated_profile_is_accepted(tmp_path):
    profile = _profile(tmp_path)
    profile.mkdir()
    assert assert_dedicated_profile(profile) == profile.resolve()


@pytest.mark.parametrize(
    "endpoint",
    ["0.0.0.0:9222", "192.168.1.20:9222", "8.8.8.8:9222", "[::]:9222"],
)
def test_non_loopback_cdp_is_rejected(endpoint):
    with pytest.raises(ForbiddenCdpError):
        parse_cdp_endpoint(endpoint)


def test_loopback_cdp_is_normalized():
    assert parse_cdp_endpoint("127.0.0.1:9222") == ("127.0.0.1", 9222)
    assert parse_cdp_endpoint("9222") == ("127.0.0.1", 9222)
    assert assert_loopback_cdp("localhost", 9333)[1] == 9333
    host, port = assert_loopback_cdp("localhost", 9333)
    assert host in {"127.0.0.1", "::1"}
    assert port == 9333


def test_command_line_without_user_data_dir_is_treated_as_default(tmp_path):
    profile = _profile(tmp_path)
    profile.mkdir()
    with pytest.raises(ForbiddenProfileError):
        assert_process_uses_profile(
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9222",
            profile,
        )


def test_command_line_must_match_dedicated_profile(tmp_path):
    profile = _profile(tmp_path)
    profile.mkdir()
    other = tmp_path / "someone-else-profile"
    other.mkdir()
    assert_process_uses_profile(f'chrome --user-data-dir="{profile}" --remote-debugging-port=9222', profile)
    with pytest.raises(ForbiddenProfileError):
        assert_process_uses_profile(
            '--user-data-dir="/Users/logicrw/Library/Application Support/Google/Chrome"',
            profile,
        )
    with pytest.raises(ForbiddenProfileError):
        assert_process_uses_profile(f"--user-data-dir={other}", profile)


def test_parse_user_data_dir_handles_quotes():
    path = parse_user_data_dir('foo --user-data-dir="/tmp/pac-interactive" --bar')
    assert path == Path("/tmp/pac-interactive")


def test_cookie_header_is_rejected():
    with pytest.raises(Exception) as excinfo:
        reject_cookie_header("session=secret")
    assert "cookie" in str(excinfo.value).casefold()


def test_result_cannot_smuggle_cookies():
    with pytest.raises(Exception):
        redact_secrets({"ok": True, "cookie": "session=secret"})
    with pytest.raises(Exception):
        redact_secrets({"ok": True, "nested": {"cookie_header": "a=b"}})
    assert redact_secrets({"ok": True, "title": "x"})["ok"] is True


def test_provenance_never_copies_cookies(tmp_path):
    settings = InteractiveSettings(profile_dir=_profile(tmp_path), cdp_host="127.0.0.1", cdp_port=9222)
    payload = provenance(settings, BACKEND_DRISSION)
    assert payload["interactive"]["cookie_copied"] is False
    assert payload["interactive"]["paywall_cleanup"] is False
    assert payload["interactive"]["route_interception"] is False
    assert payload["interactive"]["concurrency"] == 1


def test_tab_guard_rejects_closing_unrelated_tabs():
    with pytest.raises(Exception):
        assert_tab_isolation(_snapshot(
            preexisting_tab_ids=("user-tab", "owned"),
            owned_tab_id="owned",
            closed_tab_ids=("user-tab", "owned"),
            remaining_tab_ids=(),
        ))


def test_ego_backend_does_not_need_chrome_profile():
    class FakeEgo(FakeBackend):
        name = BACKEND_EGO

    backend = FakeEgo(_snapshot())
    result = asyncio.run(fetch_interactive(
        "https://www.ft.com/content/abc",
        backend=backend,
    ))
    assert result["ok"] is True
    assert result["interactive"]["backend"] == BACKEND_EGO
    assert result["interactive"]["profile_kind"] == "ego-lite"
    assert result["interactive"]["cookie_copied"] is False
    assert backend.calls


def test_ego_backend_parses_owned_tab_result(monkeypatch):
    import json
    from subprocess import CompletedProcess

    payload = {
        "error": "",
        "owned": "owned",
        "preexisting": ["other"],
        "remaining": ["other"],
        "closed": ["owned"],
        "payload": {
            "title": "A real title",
            "url": "https://www.ft.com/content/abc",
            "text": FULL_ARTICLE,
            "html": "<html><body><article><p>body</p></article></body></html>",
            "images": [],
            "paragraph_count": 8,
            "container_text_chars": 800,
            "body_text_chars": 900,
        },
    }

    def fake_run(script, timeout_s):
        assert "createTab" in script
        assert "closeTab" in script
        assert "cookie" not in script.casefold()
        return CompletedProcess(["ego-browser", "nodejs"], 0, stdout="PAC_RESULT " + json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr("bpc_fetch.interactive._run_ego_browser", fake_run)
    snapshot = EgoLiteBackend().snapshot("https://www.ft.com/content/abc", InteractiveSettings())
    assert snapshot.owned_tab_id == "owned"
    assert snapshot.closed_tab_ids == ("owned",)
    assert snapshot.remaining_tab_ids == ("other",)
    assert snapshot.preexisting_tab_ids == ("other",)
    assert_tab_isolation(snapshot)


def test_authorized_article_passes_quality_gate(tmp_path):
    backend = FakeBackend(_snapshot())
    result = asyncio.run(fetch_interactive(
        "https://www.ft.com/content/abc",
        profile=_profile(tmp_path),
        cdp="127.0.0.1:9222",
        backend=backend,
    ))
    assert result["ok"] is True
    assert result["engine"] == ENGINE
    assert f"{ENGINE}:{BACKEND_DRISSION}" in result["strategy_hit"]
    assert "final_quality_pass" in result["strategy_hit"]
    assert result["interactive"]["cookie_copied"] is False
    assert "cookie" not in result
    assert backend.calls


def test_teaser_is_paywall_remaining_not_fulltext(tmp_path):
    backend = FakeBackend(_snapshot(title="Locked", text=TEASER, html="<html><body>Subscribe</body></html>"))
    result = asyncio.run(fetch_interactive(
        "https://www.ft.com/content/teaser",
        profile=_profile(tmp_path),
        cdp="127.0.0.1:9222",
        backend=backend,
    ))
    assert result["ok"] is False
    assert result["error_code"] == "PAYWALL_REMAINING"
    assert result["paywall_suspected"] is True


def test_navigation_shell_is_not_false_fulltext(tmp_path):
    backend = FakeBackend(_snapshot(title="DO NOT DELETE", text=NAV_SHELL))
    result = asyncio.run(fetch_interactive(
        "https://www.bloomberg.com/news/articles/nav",
        profile=_profile(tmp_path),
        cdp="127.0.0.1:9222",
        backend=backend,
    ))
    assert result["ok"] is False
    assert result["error_code"] == "EXTRACT_FAILED"
    assert result["paywall_suspected"] is False


def test_explicit_cookie_header_fails_closed(tmp_path):
    backend = FakeBackend(_snapshot())
    result = asyncio.run(fetch_interactive(
        "https://www.ft.com/content/abc",
        profile=_profile(tmp_path),
        cookie_header="session=publisher",
        backend=backend,
    ))
    assert result["ok"] is False
    assert backend.calls == []
    assert "cookie" not in result
    assert "session=publisher" not in str(result)


def test_ssrf_still_applies(tmp_path):
    backend = FakeBackend(_snapshot())
    result = asyncio.run(fetch_interactive(
        "http://127.0.0.1/private",
        profile=_profile(tmp_path),
        backend=backend,
    ))
    assert result["ok"] is False
    assert result["error_code"] == "SSRF_BLOCKED"
    assert backend.calls == []


def test_redirected_final_url_ssrf_is_rejected(tmp_path):
    backend = FakeBackend(_snapshot(final_url="http://127.0.0.1/metadata"))
    result = asyncio.run(fetch_interactive(
        "https://www.ft.com/content/abc",
        profile=_profile(tmp_path),
        cdp="127.0.0.1:9222",
        backend=backend,
    ))
    assert result["ok"] is False
    assert result["error_code"] == "SSRF_BLOCKED"
    assert backend.calls


def test_drission_backend_opens_and_closes_only_owned_tab(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    profile.mkdir()
    settings = InteractiveSettings(profile_dir=profile.resolve(), cdp_host="127.0.0.1", cdp_port=9222)

    class FakeTab:
        def __init__(self, browser):
            self.browser = browser
            self.tab_id = "owned"
            self._url = "https://www.ft.com/content/abc"
            self.closed = False

        def get(self, url, timeout=None):
            self._url = url

        def run_js(self, script):
            assert "cookie" not in script.casefold()
            if "selectors.some" in script:
                return True
            return {
                "title": "A real title",
                "url": self._url,
                "text": FULL_ARTICLE,
                "html": "<html><body><article><p>body</p></article></body></html>",
                "images": [],
                "paragraph_count": 8,
                "container_text_chars": 800,
                "body_text_chars": 900,
            }

        def close(self):
            self.closed = True
            self.browser.tab_ids.remove(self.tab_id)

        @property
        def title(self):
            raise AssertionError("must not read title after the owned tab may be closed")

        @property
        def url(self):
            raise AssertionError("must not read url after the owned tab may be closed")

    class FakeChromium:
        def __init__(self, addr_or_opts=None):
            self.addr = addr_or_opts
            self.user_data_path = str(profile.resolve())
            self.tab_ids = ["user-tab"]

        def new_tab(self, url=None, new_window=False, background=True):
            assert new_window is False
            assert background is True
            self.tab_ids.append("owned")
            return FakeTab(self)

        def quit(self):
            raise AssertionError("must not quit the dedicated browser")

    monkeypatch.setattr(
        "bpc_fetch.interactive.assert_cdp_already_listening",
        lambda host, port: (4321, f"chrome --user-data-dir={profile.resolve()} --remote-debugging-address=127.0.0.1"),
    )
    monkeypatch.setattr("bpc_fetch.interactive.fetch_cdp_version", lambda host, port, timeout_s=2.0: {})
    monkeypatch.setattr("bpc_fetch.interactive._load_drissionpage", lambda: FakeChromium)

    snapshot = DrissionPageBackend().snapshot("https://www.ft.com/content/abc", settings)
    assert snapshot.owned_tab_id == "owned"
    assert snapshot.closed_tab_ids == ("owned",)
    assert snapshot.remaining_tab_ids == ("user-tab",)
    assert snapshot.preexisting_tab_ids == ("user-tab",)
    assert_tab_isolation(snapshot)


def _interactive_args(tmp_path, **overrides):
    args = dict(
        url="https://www.ft.com/content/abc",
        sites_js=None,
        out_dir=None,
        allow_partial=False,
        archive=False,
        full=True,
        no_browser=False,
        use_browser=None,
        images=False,
        interactive=True,
        interactive_profile=_profile(tmp_path),
        cdp="127.0.0.1:9222",
        cookie=None,
        cookie_file=None,
        diagnostics=False,
        no_rule_sync=True,
    )
    args.update(overrides)
    return SimpleNamespace(**args)


def test_fetch_interactive_bypasses_default_pipeline(monkeypatch, tmp_path):
    calls = []

    async def fake_interactive(url, **kwargs):
        calls.append(("interactive", url, kwargs))
        return {"ok": True, "title": "FT", "markdown": "body", "warnings": [], "engine": ENGINE}

    async def fake_fetch_article(*args, **kwargs):
        raise AssertionError("default fetch_article must not run for --interactive")

    monkeypatch.setattr("bpc_fetch.interactive.fetch_interactive", fake_interactive)
    monkeypatch.setattr("bpc_fetch.strategy.fetch_article", fake_fetch_article)
    monkeypatch.setattr(
        "bpc_fetch.rules.store.get_sites_map_with_version",
        lambda sites_js: ({}, "test-version", []),
    )
    monkeypatch.setattr(
        "bpc_fetch.rules.sync.maybe_sync_rules",
        lambda **kwargs: {"warnings": []},
    )

    result = asyncio.run(cli._cmd_fetch(_interactive_args(tmp_path)))
    assert result["ok"] is True
    assert result["engine"] == ENGINE
    assert calls and calls[0][0] == "interactive"
    assert calls[0][2]["cookie_header"] == ""


def test_fetch_interactive_rejects_pac_cookie(monkeypatch, tmp_path):
    called = []
    real = fetch_interactive

    async def fake_interactive(url, **kwargs):
        called.append(kwargs["cookie_header"])
        return await real(url, **kwargs)

    monkeypatch.setattr("bpc_fetch.interactive.fetch_interactive", fake_interactive)
    monkeypatch.setattr(
        "bpc_fetch.rules.store.get_sites_map_with_version",
        lambda sites_js: ({}, "test-version", []),
    )
    monkeypatch.setattr(
        "bpc_fetch.rules.sync.maybe_sync_rules",
        lambda **kwargs: {"warnings": []},
    )
    result = asyncio.run(cli._cmd_fetch(_interactive_args(tmp_path, cookie="session=secret")))
    assert result["ok"] is False
    assert called == ["session=secret"]
    assert "session=secret" not in str(result)


def test_batch_parser_has_no_interactive_flag():
    source = Path(cli.__file__).read_text(encoding="utf-8")
    batch_block = source.split('sub.add_parser("batch"', 1)[1].split("p_cookies = sub.add_parser", 1)[0]
    fetch_block = source.split('sub.add_parser("fetch"', 1)[1].split('sub.add_parser("batch"', 1)[0]
    assert "--interactive" in fetch_block
    assert "--interactive" not in batch_block
    assert "--interactive-profile" not in batch_block
