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
    assert reply_of("hermes").content == "Hi!"


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
    assert trail == [
        {
            "tool": "web_search",
            "label": "elmo",
            "where": "browser",
            "status": "done",
            "sources": [{"url": "https://a.org/", "title": "A"}],
            "asker": "ada",
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


def test_a_reply_without_tools_keeps_no_trail(harness):
    harness.model.scripts = [[chunk(content="Hi!")]]
    harness.app.chat_gpt(
        "ada", "muppets", model_name="qwen", tool_context=tools.ToolContext("s")
    )
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
