import asyncio

from bpc_fetch.browser import _compile_general_block_regexes, _handle_general_block_route
from bpc_fetch.sites import SiteStrategy
from bpc_fetch.strategy import build_plan


def test_compile_general_block_regexes_compiles_materialized_input_and_skips_invalid():
    strategy = SiteStrategy(
        domain="target.example.com",
        general_block_regexes=[
            r"tracker/source\.example/paywall\.js",
            "[invalid",
            r"literal-{domain}",
        ],
    )

    patterns = _compile_general_block_regexes(strategy)

    assert len(patterns) == 2
    assert patterns[0].search("https://cdn.test/tracker/source.example/paywall.js")
    assert not patterns[0].search("https://cdn.test/tracker/target.example.com/paywall.js")
    assert patterns[1].pattern == r"literal-{domain}"


def test_general_block_route_aborts_only_script_xhr_and_fetch():
    class Request:
        def __init__(self, resource_type: str, url: str = "https://cdn.test/paywall.js"):
            self.resource_type = resource_type
            self.url = url

    class Route:
        def __init__(self, resource_type: str):
            self.request = Request(resource_type)
            self.action = ""

        async def abort(self):
            self.action = "abort"

        async def continue_(self):
            self.action = "continue"

    pattern = _compile_general_block_regexes(
        SiteStrategy(domain="example.com", general_block_regexes=["paywall"])
    )[0]

    async def exercise(resource_type: str) -> str:
        route = Route(resource_type)
        await _handle_general_block_route(route, [pattern])
        return route.action

    for resource_type in ("script", "xhr", "fetch"):
        assert asyncio.run(exercise(resource_type)) == "abort"
    for resource_type in ("document", "image"):
        assert asyncio.run(exercise(resource_type)) == "continue"


def test_general_blockers_do_not_enable_automatic_browser_fallback():
    strategy = SiteStrategy(
        domain="example.com",
        general_block_regexes=["paywall"],
    )

    assert "browser_cleanup" not in build_plan(strategy)


def test_resource_route_blocks_private_document_before_continue():
    from bpc_fetch.browser import _handle_resource_route

    class Request:
        resource_type = "document"
        url = "http://127.0.0.1/private"

    class Route:
        request = Request()

        def __init__(self):
            self.action = ""

        async def abort(self):
            self.action = "abort"

        async def continue_(self):
            self.action = "continue"

    async def exercise():
        route = Route()
        state = {}
        await _handle_resource_route(
            route,
            general_patterns=[],
            strategy_patterns=[],
            block_images=False,
            ssrf_state=state,
        )
        return route.action, state

    action, state = asyncio.run(exercise())
    assert action == "abort"
    assert "private_ip" in state["document"]
