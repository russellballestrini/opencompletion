"""tools/: which tools a reply is offered & where each runs, the calls a
model streams, our browser round trip & the public-address guard on
web_fetch. No network: every outside call is replaced, & the guard's own
tests dial loopback addresses it must refuse."""

# Patch as app.py does, before our threads exist: web_fetch then runs under
# the same gevent a server does, & a suite importing app after this one
# never finds a lock patched out from under a running thread.
from gevent import monkey

monkey.patch_all()

import asyncio  # noqa: E402
import http.server  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import aiohttp  # noqa: E402
import gevent  # noqa: E402
import pytest  # noqa: E402

import tools  # noqa: E402
from tools import browser, loop, policy, web_fetch  # noqa: E402

GUEST = tools.ToolContext(sid="sid-guest")
FOX = tools.ToolContext(sid="sid-fox", email="fox@unturf.com")


@pytest.fixture(autouse=True)
def tools_on(monkeypatch):
    """conftest pins tools off for the suite; these tests are about them."""
    monkeypatch.setenv("OPENCOMPLETION_WEB_SEARCH", "on")
    monkeypatch.setenv("OPENCOMPLETION_WEB_CRAWL", "on")
    monkeypatch.setenv("OPENCOMPLETION_SERVER_EGRESS_USERS", "fox@unturf.com")


# -- policy ----------------------------------------------------------------


def test_search_and_crawl_are_on_unless_turned_off(monkeypatch):
    monkeypatch.delenv("OPENCOMPLETION_WEB_SEARCH")
    monkeypatch.delenv("OPENCOMPLETION_WEB_CRAWL")
    assert policy.web_search_enabled() and policy.web_crawl_enabled()
    for word in ("off", "0", "false", "NO", " Off "):
        monkeypatch.setenv("OPENCOMPLETION_WEB_SEARCH", word)
        monkeypatch.setenv("OPENCOMPLETION_WEB_CRAWL", word)
        assert not policy.web_search_enabled() and not policy.web_crawl_enabled()


def test_nobody_has_our_network_unless_listed(monkeypatch):
    monkeypatch.delenv("OPENCOMPLETION_SERVER_EGRESS_USERS")
    assert not policy.server_egress_allowed("fox@unturf.com")
    monkeypatch.setenv(
        "OPENCOMPLETION_SERVER_EGRESS_USERS", " A@X.org , fox@unturf.com,"
    )
    assert policy.server_egress_allowed("fox@unturf.com")
    assert policy.server_egress_allowed("a@x.ORG")
    assert not policy.server_egress_allowed("eve@unturf.com")
    assert not policy.server_egress_allowed(None)
    assert not policy.server_egress_allowed("")


def test_rounds_and_timeout_knobs(monkeypatch):
    assert policy.max_rounds() == 4 and policy.browser_timeout() == 30
    monkeypatch.setenv("OPENCOMPLETION_TOOL_ROUNDS", "0")
    monkeypatch.setenv("OPENCOMPLETION_TOOL_TIMEOUT_S", "junk")
    assert policy.max_rounds() == 1 and policy.browser_timeout() == 30


# -- what a reply is offered -----------------------------------------------


def test_a_guest_searches_from_their_browser_and_never_fetches():
    assert tools.offered(GUEST) == {"web_search": "browser"}
    assert [s["function"]["name"] for s in tools.schemas(GUEST)] == ["web_search"]


def test_a_signed_in_person_off_the_list_is_a_guest_to_our_network():
    eve = tools.ToolContext(sid="sid-eve", email="eve@unturf.com")
    assert tools.offered(eve) == {"web_search": "browser"}


def test_an_egress_user_searches_and_fetches_on_our_server():
    assert tools.offered(FOX) == {"web_search": "server", "web_fetch": "server"}


def test_no_socket_and_no_egress_offers_nothing():
    assert tools.offered(tools.ToolContext()) == {}
    assert tools.offered(None) == {} and tools.schemas(None) == []


def test_flags_turn_each_tool_off(monkeypatch):
    monkeypatch.setenv("OPENCOMPLETION_WEB_CRAWL", "off")
    assert tools.offered(FOX) == {"web_search": "server"}
    monkeypatch.setenv("OPENCOMPLETION_WEB_SEARCH", "off")
    assert tools.offered(FOX) == {} and tools.offered(GUEST) == {}


def test_context_reads_the_sign_in():
    user = SimpleNamespace(email="fox@unturf.com")
    assert tools.context("s1", user) == tools.ToolContext("s1", "fox@unturf.com")
    assert tools.context("s1", None) == tools.ToolContext("s1", None)


# -- running a call --------------------------------------------------------


def test_a_tool_not_offered_is_refused(monkeypatch):
    called = []
    monkeypatch.setattr(web_fetch, "fetch_page", lambda url: called.append(url))
    assert (
        "not available"
        in tools.run(GUEST, "web_fetch", {"url": "https://x.org"})["error"]
    )
    assert "not available" in tools.run(FOX, "rm_rf", {})["error"]
    assert called == []


def test_a_guest_search_goes_to_their_browser(monkeypatch):
    from tools import keyless_search

    monkeypatch.setattr(
        keyless_search, "search", lambda *a, **k: pytest.fail("ran on our server")
    )
    asked = []

    def ask(sid, tool, arguments, timeout):
        asked.append((sid, tool, arguments, timeout))
        return {
            "success": True,
            "served_by": "exa",
            "results": [
                {
                    "url": "https://muppet.fandom.com/",
                    "title": "Muppets",
                    "description": "Wiki",
                }
            ],
        }

    monkeypatch.setattr(browser, "ask", ask)
    result = tools.run(GUEST, "web_search", {"query": " muppets "})
    assert asked == [("sid-guest", "web_search", {"query": "muppets", "limit": 5}, 30)]
    assert result == {
        "served_by": "exa",
        "results": [
            {"url": "https://muppet.fandom.com/", "title": "Muppets", "snippet": "Wiki"}
        ],
    }


def test_an_egress_users_search_runs_on_our_server(monkeypatch):
    from tools import keyless_search

    monkeypatch.setattr(browser, "ask", lambda *a, **k: pytest.fail("asked a browser"))
    monkeypatch.setattr(
        keyless_search,
        "search",
        lambda query, limit: {"success": True, "served_by": "keenable", "results": []},
    )
    assert tools.run(FOX, "web_search", {"query": "x"}) == {
        "results": [],
        "served_by": "keenable",
    }


def test_an_empty_query_never_leaves(monkeypatch):
    monkeypatch.setattr(browser, "ask", lambda *a, **k: pytest.fail("asked"))
    assert tools.run(GUEST, "web_search", {"query": "  "}) == {
        "error": "query required"
    }


def test_a_tool_that_raises_becomes_an_error_the_model_reads(monkeypatch):
    def boom(url):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(web_fetch, "fetch_page", boom)
    assert tools.run(FOX, "web_fetch", {"url": "https://x.org"}) == {
        "error": "web_fetch failed: disk on fire"
    }


def test_a_browsers_answer_is_cut_to_shape():
    """A browser is anyone's: its answer is trimmed & typed before a model
    reads it."""
    junk = {
        "success": True,
        "served_by": {"not": "a string"},
        "results": [
            "not a dict",
            {"url": "javascript:alert(1)", "title": "x"},
            {
                "url": "https://a.org/" + "a" * 3000,
                "title": "t" * 400,
                "description": "d" * 900,
            },
        ]
        + [{"url": f"https://{i}.org/"} for i in range(9)],
    }
    shaped = tools.shape_search(junk)
    assert "served_by" not in shaped
    assert len(shaped["results"]) == tools.SEARCH_LIMIT
    first = shaped["results"][0]
    assert len(first["url"]) == 2000 and len(first["title"]) == 300
    assert len(first["snippet"]) == tools.SNIPPET_CHARS
    assert all(r["url"].startswith("https://") for r in shaped["results"])


def test_failed_searches_are_errors():
    assert tools.shape_search(None) == {"error": "no result"}
    assert tools.shape_search({"success": False, "error": "all throttled"}) == {
        "error": "all throttled"
    }
    assert tools.shape_search({"error": "the asker's browser left"}) == {
        "error": "the asker's browser left"
    }


# -- the calls a model streams ---------------------------------------------


def piece(index, id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index, id=id, function=SimpleNamespace(name=name, arguments=arguments)
    )


def test_streamed_pieces_become_whole_calls():
    calls = loop.ToolCallCollector()
    calls.add([piece(0, id="call_a", name="web_search", arguments='{"que')])
    calls.add([piece(0, arguments='ry": "elmo"}'), piece(1, name="web_fetch")])
    calls.add([piece(1, arguments='{"url": "https://x.org"}')])
    assert calls.calls == [
        {"id": "call_a", "name": "web_search", "arguments": '{"query": "elmo"}'},
        {"id": "call_1", "name": "web_fetch", "arguments": '{"url": "https://x.org"}'},
    ]


def test_a_nameless_call_is_dropped_and_mock_attributes_are_ignored():
    from unittest.mock import MagicMock

    calls = loop.ToolCallCollector()
    calls.add([piece(0, arguments="{}")])
    calls.add([MagicMock()])  # a test double's fabricated attributes
    calls.add(None)
    assert calls.calls == []


def test_a_round_runs_each_call_and_tells_the_room(monkeypatch):
    monkeypatch.setattr(
        tools,
        "run",
        lambda context, name, arguments: (
            {"results": [{"url": "https://a.org/", "title": "A", "snippet": ""}]}
            if name == "web_search"
            else {"error": "nope"}
        ),
    )
    events = []
    calls = [
        {"id": "c1", "name": "web_search", "arguments": '{"query": "elmo"}'},
        {"id": "c2", "name": "web_fetch", "arguments": "not json"},
    ]
    messages = loop.run_round(FOX, calls, "Let me look.", events.append)

    assert messages[0] == {
        "role": "assistant",
        "content": "Let me look.",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "web_search", "arguments": '{"query": "elmo"}'},
            },
            {
                "id": "c2",
                "type": "function",
                "function": {"name": "web_fetch", "arguments": "not json"},
            },
        ],
    }
    assert [m["tool_call_id"] for m in messages[1:]] == ["c1", "c2"]
    assert json.loads(messages[1]["content"])["results"][0]["url"] == "https://a.org/"
    assert json.loads(messages[2]["content"]) == {
        "error": "arguments were not a JSON object"
    }
    assert [(e["tool"], e["status"], e["where"]) for e in events] == [
        ("web_search", "running", "server"),
        ("web_search", "done", "server"),
        ("web_fetch", "running", "server"),
        ("web_fetch", "error", "server"),
    ]
    assert events[1]["sources"] == [{"url": "https://a.org/", "title": "A"}]
    assert events[1]["label"] == "elmo"


# -- our browser round trip ------------------------------------------------


@pytest.fixture
def fake_socketio(monkeypatch):
    sent = []
    monkeypatch.setattr(
        browser, "_socketio", SimpleNamespace(emit=lambda *a, **k: sent.append((a, k)))
    )
    return sent


def test_only_the_asking_socket_can_answer(fake_socketio):
    asking = gevent.spawn(browser.ask, "sid-ada", "web_search", {"query": "q"}, 5)
    gevent.sleep(0)
    (event, payload), where = fake_socketio[0]
    assert event == "tool_request" and where == {"room": "sid-ada"}
    request_id = payload["request_id"]

    # Someone else in the room tries to answer (or poison) it: ignored.
    assert not browser.resolve(
        "sid-eve", {"request_id": request_id, "result": {"x": 1}}
    )
    assert not browser.resolve("sid-ada", {"request_id": "made-up", "result": {}})
    assert not browser.resolve("sid-ada", "not a dict")
    assert browser.resolve("sid-ada", {"request_id": request_id, "result": {"ok": 1}})
    assert asking.get(timeout=2) == {"ok": 1}
    assert browser._pending == {}


def test_an_answer_that_is_not_a_dict_is_an_error(fake_socketio):
    asking = gevent.spawn(browser.ask, "sid-ada", "web_search", {}, 5)
    gevent.sleep(0)
    request_id = fake_socketio[0][0][1]["request_id"]
    browser.resolve("sid-ada", {"request_id": request_id, "result": ["junk"]})
    assert asking.get(timeout=2) == {"error": "no result"}


def test_a_quiet_browser_times_out(fake_socketio):
    assert browser.ask("sid-ada", "web_search", {}, 0.01) == {
        "error": "the asker's browser did not answer within 0.01s"
    }
    assert browser._pending == {}


def test_a_browser_that_leaves_answers_at_once(fake_socketio):
    asking = gevent.spawn(browser.ask, "sid-ada", "web_search", {}, 30)
    other = gevent.spawn(browser.ask, "sid-bob", "web_search", {}, 0.2)
    gevent.sleep(0)
    browser.abandon("sid-ada")
    assert asking.get(timeout=2) == {"error": "the asker's browser left"}
    assert "did not answer" in other.get(timeout=2)["error"]


def test_no_server_or_no_socket_never_waits(monkeypatch):
    monkeypatch.setattr(browser, "_socketio", None)
    assert browser.ask("sid", "web_search", {}, 30) == {
        "error": "no browser to run this in"
    }


# -- web_fetch dials public addresses only --------------------------------


@pytest.mark.parametrize(
    "address,public",
    [
        ("93.184.215.14", True),
        ("2606:4700::6810:84e5", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("0.0.0.0", False),
        ("::1", False),
        ("fe80::1%eth0", False),
        ("fd00::1", False),
        ("::ffff:127.0.0.1", False),
        ("not an address", False),
    ],
)
def test_is_public(address, public):
    assert web_fetch.is_public(address) is public


def test_aiohttp_still_resolves_every_host_through_resolve_host():
    """PublicOnlyConnector rests on aiohttp's private _resolve_host; an
    aiohttp that stops calling it fails here, not in production."""

    async def go():
        connector = web_fetch.PublicOnlyConnector()
        async with aiohttp.ClientSession(connector=connector) as session:
            with pytest.raises(web_fetch.PrivateAddressError):
                await session.get("http://127.0.0.1:9/")
            with pytest.raises(web_fetch.PrivateAddressError):
                await session.get("http://[::1]:9/")

    asyncio.run(go())


def test_a_name_resolving_only_to_private_addresses_is_refused():
    class Resolver(aiohttp.abc.AbstractResolver):
        async def resolve(self, host, port=0, family=0):
            return [
                {
                    "hostname": host,
                    "host": "10.0.0.7",
                    "port": port,
                    "family": 2,
                    "proto": 0,
                    "flags": 0,
                }
            ]

        async def close(self):
            pass

    async def go():
        connector = web_fetch.PublicOnlyConnector(resolver=Resolver())
        async with aiohttp.ClientSession(connector=connector) as session:
            with pytest.raises(web_fetch.PrivateAddressError, match="intranet"):
                await session.get("http://intranet.example/")

    asyncio.run(go())


class _Handler(http.server.BaseHTTPRequestHandler):
    target = ""

    def do_GET(self):
        if self.path == "/robots.txt":
            self.send_response(404)
            self.end_headers()
            return
        if self.path == "/go":
            self.send_response(302)
            self.send_header("Location", self.target)
            self.end_headers()
            return
        body = b"<html><head><title>Inside</title></head><body>secret</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve(host):
    server = http.server.ThreadingHTTPServer((host, 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_a_redirect_to_a_private_address_is_refused(monkeypatch):
    """A public page answering 302 to our own network: aiohttp's redirect
    hook names the old url, so only the connector sees this hop. Here
    127.0.0.2 plays the public site & 127.0.0.3 our private network."""
    outside, inside = _serve("127.0.0.2"), _serve("127.0.0.3")
    try:
        _Handler.target = f"http://127.0.0.3:{inside.server_port}/page"
        monkeypatch.setattr(web_fetch, "is_public", lambda a: a == "127.0.0.2")
        base = f"http://127.0.0.2:{outside.server_port}"
        assert web_fetch.fetch_page(base + "/page")["title"] == "Inside"
        refused = web_fetch.fetch_page(base + "/go")
        assert "no public address" in refused["error"]
        assert "text" not in refused
    finally:
        outside.shutdown()
        inside.shutdown()


def test_fetch_page_refuses_other_schemes_and_cuts_long_pages(monkeypatch):
    assert "only http" in web_fetch.fetch_page("file:///etc/passwd")["error"]
    assert "only http" in web_fetch.fetch_page(None)["error"]

    async def fake(url):
        return {"title": "Long", "text": "x" * 50}

    monkeypatch.setattr(web_fetch, "_fetch", fake)
    page = web_fetch.fetch_page("https://a.org/", max_chars=10)
    assert page["text"].startswith("x" * 10) and "cut at 10" in page["text"]
