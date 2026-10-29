"""A chat reply that calls tools: chat_gpt offers them, runs what the model
asks for, hands back what they found & streams the answer into the same
reply; a guest's search makes a real round trip through their socket."""

import json
from types import SimpleNamespace

import gevent
import pytest

import tools

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def tools_on(monkeypatch):
    monkeypatch.setenv("OPENCOMPLETION_WEB_SEARCH", "on")
    monkeypatch.setenv("OPENCOMPLETION_WEB_CRAWL", "on")
    monkeypatch.setenv("OPENCOMPLETION_SERVER_EGRESS_USERS", "fox@unturf.com")
    monkeypatch.delenv("OPENCOMPLETION_TOOL_ROUNDS", raising=False)


def chunk(content=None, calls=None):
    delta = SimpleNamespace(
        reasoning_content=None, reasoning=None, content=content, tool_calls=calls
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)])


def search_call(query, id="call_1"):
    """A model asking for web_search, its arguments in two pieces."""
    half = len(query) // 2
    function = SimpleNamespace
    return [
        chunk(
            calls=[
                SimpleNamespace(
                    index=0,
                    id=id,
                    function=function(
                        name="web_search", arguments='{"query": "' + query[:half]
                    ),
                )
            ]
        ),
        chunk(
            calls=[
                SimpleNamespace(
                    index=0,
                    id=None,
                    function=function(name=None, arguments=query[half:] + '"}'),
                )
            ]
        ),
    ]


class Model:
    """A fake model: each ask streams the next script in `scripts`."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.asks = []

    def create(self, **kwargs):
        self.asks.append(kwargs)
        script = self.scripts.pop(0)
        if isinstance(script, Exception):
            raise script
        return iter(script)


@pytest.fixture
def harness(test_app, monkeypatch):
    import app as app_module

    model = Model()
    client = SimpleNamespace(chat=SimpleNamespace(completions=model))
    monkeypatch.setattr(
        app_module, "get_openai_client_and_model", lambda name: (client, name)
    )
    emitted = []
    monkeypatch.setattr(
        app_module.socketio, "emit", lambda *a, **k: emitted.append((a, k))
    )
    monkeypatch.setattr(app_module.socketio, "sleep", lambda s: None)
    monkeypatch.setattr(app_module, "title_untitled_room", lambda name: None)
    return SimpleNamespace(app=app_module, model=model, emitted=emitted)


def reply_of(model_name):
    from models import Message

    return Message.query.filter_by(username=model_name).one()


def test_a_reply_searches_then_answers_with_what_it_found(harness, monkeypatch):
    found = []

    def run(context, name, arguments):
        found.append((context, name, arguments))
        return {
            "results": [
                {
                    "url": "https://muppet.fandom.com/wiki/Elmo",
                    "title": "Elmo",
                    "snippet": "a red monster",
                }
            ]
        }

    monkeypatch.setattr(tools, "run", run)
    harness.model.scripts = [
        [chunk(content="Let me check. ")] + search_call("who is elmo"),
        [
            chunk(content="Elmo is a red monster "),
            chunk(content="(muppet.fandom.com)."),
        ],
    ]
    asker = tools.ToolContext(sid="sid-ada")

    harness.app.chat_gpt("ada", "muppets", model_name="qwen", tool_context=asker)

    first, second = harness.model.asks
    assert [t["function"]["name"] for t in first["tools"]] == ["web_search"]
    # The context gains this chat & the model answering (sandbox, artifacts).
    ((context, name, arguments),) = found
    assert (context.sid, context.room_id, context.model) == ("sid-ada", 1, "qwen")
    assert (name, arguments) == ("web_search", {"query": "who is elmo"})
    # The second ask carries the model's tool turn & what the tool found.
    turn, result = second["messages"][-2:]
    assert turn["role"] == "assistant" and turn["content"] == "Let me check. "
    assert turn["tool_calls"][0]["function"] == {
        "name": "web_search",
        "arguments": '{"query": "who is elmo"}',
    }
    assert result["role"] == "tool" and result["tool_call_id"] == "call_1"
    assert json.loads(result["content"])["results"][0]["title"] == "Elmo"
    # One reply holds both rounds; tool messages are never saved as chat.
    assert reply_of("qwen").content == (
        "Let me check. Elmo is a red monster (muppet.fandom.com)."
    )
    activity = [a[1] for a, k in harness.emitted if a[0] == "tool_activity"]
    # The reply's first line names what it may call.
    offered = activity.pop(0)
    assert (offered["tool"], offered["names"]) == ("offered", ["web_search"])
    assert [(e["status"], e["where"], e["asker"]) for e in activity] == [
        ("running", "browser", "ada"),
        ("done", "browser", "ada"),
    ]
    assert activity[1]["sources"][0]["url"] == "https://muppet.fandom.com/wiki/Elmo"
    assert all(
        k == {"room": "muppets"} for a, k in harness.emitted if a[0] == "tool_activity"
    )


def test_the_last_round_has_no_tools_so_a_reply_always_answers(harness, monkeypatch):
    monkeypatch.setenv("OPENCOMPLETION_TOOL_ROUNDS", "2")
    monkeypatch.setattr(tools, "run", lambda *a: {"results": []})
    harness.model.scripts = [
        search_call("a", "c1"),
        search_call("b", "c2"),
        [chunk(content="Nothing turned up.")],
    ]
    harness.app.chat_gpt(
        "ada", "muppets", model_name="qwen", tool_context=tools.ToolContext("s")
    )
    assert ["tools" in ask for ask in harness.model.asks] == [True, True, False]
    # Its last ask says why the tools went, so it reports instead of stopping.
    last = harness.model.asks[-1]["messages"][-1]
    assert last["role"] == "user" and "used all 2 tool rounds" in last["content"]
    assert "continue" in last["content"]
    assert all(
        "tool rounds" not in str(m.get("content"))
        for m in harness.model.asks[1]["messages"]
    )
    assert reply_of("qwen").content == "Nothing turned up."


def test_a_model_that_refuses_tools_answers_plainly(harness):
    import httpx
    import openai

    refusal = openai.BadRequestError(
        "tools are not supported",
        response=httpx.Response(400, request=httpx.Request("POST", "http://m/")),
        body=None,
    )
    harness.model.scripts = [refusal, [chunk(content="Hi!")]]
    harness.app.chat_gpt(
        "ada", "muppets", model_name="hermes", tool_context=tools.ToolContext("s")
    )
    assert "tools" in harness.model.asks[0] and "tools" not in harness.model.asks[1]
    # Its retry is not told of tools it lacks, & the reply says it went without.
    assert not any(tools.is_guidance(m) for m in harness.model.asks[1]["messages"])
    reply = reply_of("hermes").content
    assert reply.startswith("Hi!")
    assert "No tools this reply: hermes refused them" in reply
    assert "nothing here was run or checked" in reply


def test_a_400_about_anything_else_never_strips_the_tools(harness):
    """A prompt past the model's context is a 400 too; it once fell through
    to dropping tools, & the model answered as plain chat, claiming runs it
    never made."""
    import httpx
    import openai

    def refusal(message):
        return openai.BadRequestError(
            message,
            response=httpx.Response(400, request=httpx.Request("POST", "http://m/")),
            body=None,
        )

    harness.model.scripts = [
        refusal("This model's maximum context length is 32768 tokens"),
        refusal("This model's maximum context length is 32768 tokens"),
    ]
    harness.app.chat_gpt(
        "ada", "muppets", model_name="qwen", tool_context=tools.ToolContext("s")
    )
    assert all("tools" in ask for ask in harness.model.asks)
    reply = reply_of("qwen").content
    assert "maximum context length" in reply and "No tools this reply" not in reply


def test_without_a_tool_context_a_reply_is_offered_nothing(harness):
    harness.model.scripts = [[chunk(content="Hi!")]]
    harness.app.chat_gpt("ada", "muppets", model_name="qwen")
    assert "tools" not in harness.model.asks[0]


def test_with_tools_off_a_reply_is_offered_nothing(harness, monkeypatch):
    monkeypatch.setenv("OPENCOMPLETION_WEB_SEARCH", "off")
    monkeypatch.setenv("OPENCOMPLETION_WEB_CRAWL", "off")
    harness.model.scripts = [[chunk(content="Hi!")]]
    harness.app.chat_gpt(
        "ada",
        "muppets",
        model_name="qwen",
        tool_context=tools.ToolContext("s", "fox@unturf.com"),
    )
    assert "tools" not in harness.model.asks[0]


def test_stop_ends_a_reply_between_tool_rounds(harness, monkeypatch):
    def run(*args):
        harness.app.stop_requests.add("muppets")
        return {"results": []}

    monkeypatch.setattr(tools, "run", run)
    harness.model.scripts = [search_call("q")]
    try:
        harness.app.chat_gpt(
            "ada", "muppets", model_name="qwen", tool_context=tools.ToolContext("s")
        )
    finally:
        harness.app.stop_requests.discard("muppets")
    assert len(harness.model.asks) == 1


# -- who asked: the socket & sign in a reply's tools run for ---------------


@pytest.fixture
def spawned(test_app, monkeypatch):
    import app as app_module

    calls = []
    monkeypatch.setattr(
        app_module.gevent, "spawn", lambda fn, *a, **k: calls.append((fn, a, k))
    )
    return calls


def test_a_message_carries_its_senders_socket_to_the_reply(test_app, spawned):
    import app as app_module

    client = app_module.socketio.test_client(
        app_module.app, flask_test_client=test_app.test_client()
    )
    client.emit("join", {"room_name": "lobby", "username": "ada"})
    client.emit(
        "chat_message",
        {"room_name": "lobby", "username": "ada", "message": "hi", "model": "qwen"},
    )
    ((fn, args, kwargs),) = [c for c in spawned if c[0] is app_module.stoppable_reply]
    context = kwargs["tool_context"]
    assert context.sid and context.email is None
    assert tools.offered(context) == {"web_search": "browser"}


# -- a guest's search, through their own socket ----------------------------


def test_a_guest_search_round_trips_through_their_browser(test_app, monkeypatch):
    """The real wiring: tool_request goes only to the asker's socket, their
    tool_result comes back, & the model answers with it."""
    import app as app_module
    from tools import keyless_search

    monkeypatch.setattr(
        keyless_search, "search", lambda *a, **k: pytest.fail("ran on our server")
    )
    model = Model(
        search_call("elmo"),
        [chunk(content="Elmo is red.")],
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=model))
    monkeypatch.setattr(
        app_module, "get_openai_client_and_model", lambda name: (client, name)
    )
    monkeypatch.setattr(app_module, "title_untitled_room", lambda name: None)

    http = test_app.test_client()
    ada = app_module.socketio.test_client(app_module.app, flask_test_client=http)
    bob = app_module.socketio.test_client(
        app_module.app, flask_test_client=test_app.test_client()
    )
    for socket, name in ((ada, "ada"), (bob, "bob")):
        socket.emit("join", {"room_name": "lobby", "username": name})
        socket.get_received()
    ada.emit(
        "chat_message",
        {
            "room_name": "lobby",
            "username": "ada",
            "message": "who is elmo?",
            "model": "qwen",
        },
    )

    request = None
    for _ in range(200):
        gevent.sleep(0.01)
        received = ada.get_received()
        asks = [e for e in received if e["name"] == "tool_request"]
        if asks:
            request = asks[0]["args"][0]
            break
    assert request and request["tool"] == "web_search"
    assert request["arguments"] == {"query": "elmo", "limit": 5}
    assert not [e for e in bob.get_received() if e["name"] == "tool_request"]

    # Bob can't answer Ada's search; Ada's browser does.
    bob.emit(
        "tool_result",
        {
            "request_id": request["request_id"],
            "result": {
                "success": True,
                "results": [{"url": "https://evil.example/", "title": "x"}],
            },
        },
    )
    ada.emit(
        "tool_result",
        {
            "request_id": request["request_id"],
            "result": {
                "success": True,
                "served_by": "exa",
                "results": [
                    {
                        "url": "https://muppet.fandom.com/",
                        "title": "Elmo",
                        "description": "red",
                    }
                ],
            },
        },
    )
    for _ in range(200):
        if len(model.asks) == 2 and not app_module.streaming_replies.get("lobby"):
            break
        gevent.sleep(0.01)

    found = json.loads(model.asks[1]["messages"][-1]["content"])
    assert found == {
        "served_by": "exa",
        "results": [
            {"url": "https://muppet.fandom.com/", "title": "Elmo", "snippet": "red"}
        ],
    }
    from models import Message

    assert Message.query.filter_by(username="qwen").one().content == "Elmo is red."


# -- a reply keeps its tool lines (Message.tool_trail) ---------------------


def test_a_reply_keeps_its_tool_lines_for_history_and_forks(
    harness, monkeypatch, test_app
):
    monkeypatch.setattr(
        tools,
        "run",
        lambda *a: {
            "results": [{"url": "https://a.org/", "title": "A", "snippet": ""}]
        },
    )
    harness.model.scripts = [search_call("elmo"), [chunk(content="Elmo is red.")]]
    harness.app.chat_gpt(
        "ada", "muppets", model_name="qwen", tool_context=tools.ToolContext("s")
    )
    reply = reply_of("qwen")
    trail = json.loads(reply.tool_trail)
    assert trail[0] == {
        "tool": "offered",
        "names": ["web_search"],
        "status": "done",
        "asker": "ada",
        "at": 0,  # where in the reply's text it happened
    }
    assert trail[1:] == [
        {
            "tool": "web_search",
            "label": "elmo",
            "where": "browser",
            "status": "done",
            "sources": [{"url": "https://a.org/", "title": "A"}],
            "asker": "ada",
            "at": 0,
        }
    ]

    # History hands the trail to whoever joins later.
    monkeypatch.undo()
    import app as app_module

    client = app_module.socketio.test_client(
        app_module.app, flask_test_client=test_app.test_client()
    )
    client.emit("join", {"room_name": "muppets", "username": "bob"})
    history = [
        e["args"][0] for e in client.get_received() if e["name"] == "previous_messages"
    ]
    saved = {m["id"]: m for m in history}[reply.id]
    assert saved["tools"] == trail
    assert all(m["tools"] is None for m in history if m["id"] != reply.id)

    # A fork copies it with the reply.
    from models import Message, Room
    from routes.rooms import fork_conversation

    source = Room.query.filter_by(name="muppets").one()
    with test_app.test_request_context():
        fork_conversation(source, up_to=reply.id)
    copies = Message.query.filter(
        Message.username == "qwen", Message.room_id != source.id
    ).all()
    assert [json.loads(c.tool_trail) for c in copies] == [trail]


def test_each_tool_line_remembers_where_in_the_text_it_happened(harness, monkeypatch):
    monkeypatch.setattr(tools, "run", lambda *a: {"results": []})
    harness.model.scripts = [
        [chunk(content="Let me look. ")] + search_call("elmo"),
        [chunk(content="Found it.")],
    ]
    harness.app.chat_gpt(
        "ada", "muppets", model_name="qwen", tool_context=tools.ToolContext("s")
    )
    reply = reply_of("qwen")
    trail = json.loads(reply.tool_trail)
    assert [(e["tool"], e["at"]) for e in trail] == [("offered", 0), ("web_search", 13)]
    assert reply.content[:13] == "Let me look. "
    live = [a[1] for a, k in harness.emitted if a[0] == "tool_activity"]
    assert all(e["model_name"] == "qwen" for e in live)


def test_a_reply_that_calls_no_tool_keeps_only_what_it_was_offered(harness):
    harness.model.scripts = [[chunk(content="Hi!")]]
    harness.app.chat_gpt(
        "ada", "muppets", model_name="qwen", tool_context=tools.ToolContext("s")
    )
    assert [e["tool"] for e in json.loads(reply_of("qwen").tool_trail)] == ["offered"]


def test_a_reply_offered_no_tools_keeps_no_trail(harness):
    harness.model.scripts = [[chunk(content="Hi!")]]
    harness.app.chat_gpt("ada", "muppets", model_name="qwen")
    assert reply_of("qwen").tool_trail is None


def test_a_signed_in_message_carries_its_email_so_egress_users_get_our_network(
    test_app, spawned
):
    import app as app_module
    from models import User, db

    user = User(email="fox@unturf.com", display_name="fox")
    db.session.add(user)
    db.session.commit()
    http = test_app.test_client()
    with http.session_transaction() as session:
        session["user_id"] = user.id
    client = app_module.socketio.test_client(app_module.app, flask_test_client=http)
    client.emit("join", {"room_name": "lobby", "username": "fox"})
    client.emit(
        "chat_message",
        {"room_name": "lobby", "username": "fox", "message": "hi", "model": "qwen"},
    )
    ((fn, args, kwargs),) = [c for c in spawned if c[0] is app_module.stoppable_reply]
    context = kwargs["tool_context"]
    assert context.email == "fox@unturf.com"
    assert tools.offered(context) == {"web_search": "server", "web_fetch": "server"}


def test_a_reply_with_tools_is_told_them_and_never_to_claim_a_run(harness):
    harness.model.scripts = [[chunk(content="Hi!")]]
    harness.app.chat_gpt(
        "ada", "muppets", model_name="qwen", tool_context=tools.ToolContext("s")
    )
    first = harness.model.asks[0]["messages"][0]
    assert first["role"] == "system"
    assert "You can call these tools: web_search." in first["content"]
    assert "Never say code ran" in first["content"]
    assert "not available here" in first["content"]
    assert "delegate" not in first["content"]


def test_a_reply_without_tools_gets_no_tool_note(harness):
    harness.model.scripts = [[chunk(content="Hi!")]]
    harness.app.chat_gpt("ada", "muppets", model_name="qwen")
    assert all(m["role"] != "system" for m in harness.model.asks[0]["messages"])


@pytest.mark.parametrize(
    "message,named",
    [
        ("use the web_search tool: who is elmo", "web_search"),
        ("please call web search for elmo", "web_search"),
        ("web_search: elmo", "web_search"),
        ("search the web for elmo", None),  # asked for the doing, not the tool
    ],
)
def test_a_tool_asked_for_by_name_is_forced_on_the_first_ask(
    harness, monkeypatch, message, named
):
    from models import Message, Room, db

    room = Room(name="muppets")
    db.session.add(room)
    db.session.commit()
    db.session.add(Message(username="ada", content=message, room_id=room.id))
    db.session.commit()
    monkeypatch.setattr(tools, "run", lambda *a: {"results": []})
    harness.model.scripts = [search_call("elmo"), [chunk(content="Found it.")]]
    harness.app.chat_gpt(
        "ada", "muppets", model_name="qwen", tool_context=tools.ToolContext("s")
    )
    first, second = harness.model.asks
    if named:
        assert first["tool_choice"] == {
            "type": "function",
            "function": {"name": named},
        }
    else:
        assert "tool_choice" not in first
    assert "tool_choice" not in second  # only the first ask is forced


def test_a_server_that_cannot_force_a_tool_still_offers_them(harness):
    import httpx
    import openai
    from models import Message, Room, db

    room = Room(name="muppets")
    db.session.add(room)
    db.session.commit()
    db.session.add(
        Message(username="ada", content="use the web_search tool", room_id=room.id)
    )
    db.session.commit()
    harness.model.scripts = [
        openai.BadRequestError(
            "tool_choice named functions are not supported",
            response=httpx.Response(400, request=httpx.Request("POST", "http://m/")),
            body=None,
        ),
        [chunk(content="Hi!")],
    ]
    harness.app.chat_gpt(
        "ada", "muppets", model_name="qwen", tool_context=tools.ToolContext("s")
    )
    retry = harness.model.asks[1]
    assert "tool_choice" not in retry and "tools" in retry
    assert "No tools this reply" not in reply_of("qwen").content


def test_a_reply_is_saved_as_it_goes_so_a_refresh_mid_tool_shows_it(
    harness, monkeypatch
):
    from models import Message, db

    seen = {}

    def run(context, name, arguments):
        # Mid-tool: what a page opened now would be sent.
        db.session.expire_all()
        reply = Message.query.filter_by(username="qwen").one()
        seen["content"] = reply.content
        seen["trail"] = json.loads(reply.tool_trail)
        return {"results": []}

    monkeypatch.setattr(tools, "run", run)
    harness.model.scripts = [
        [chunk(content="Let me look. ")] + search_call("elmo"),
        [chunk(content="Done.")],
    ]
    harness.app.chat_gpt(
        "ada", "muppets", model_name="qwen", tool_context=tools.ToolContext("s")
    )
    assert seen["content"] == "Let me look. "
    assert [(e["tool"], e["status"]) for e in seen["trail"]] == [
        ("offered", "done"),
        ("web_search", "running"),
    ]
    final = json.loads(reply_of("qwen").tool_trail)
    assert [(e["tool"], e["status"]) for e in final] == [
        ("offered", "done"),
        ("web_search", "done"),
    ]
