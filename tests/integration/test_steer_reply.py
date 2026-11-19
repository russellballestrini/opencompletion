"""A message sent while a model is already replying in its room steers that
reply instead of starting a second one beside it: the model reads it after
its current tool round. One it can no longer read (its reply was already
answering) gets one reply after it ends. A sender who could not call the
reply's tools themselves never steers it mid-reply (nobody steers another
person's sandbox); theirs waits for that next reply."""

import json
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.integration


def delta(content=None, calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    reasoning_content=None,
                    reasoning=None,
                    content=content,
                    tool_calls=calls,
                ),
                finish_reason=None,
            )
        ]
    )


def search_call():
    function = SimpleNamespace(name="web_search", arguments=json.dumps({"query": "x"}))
    return delta(calls=[SimpleNamespace(index=0, id="c1", function=function)])


@pytest.fixture
def harness(test_app, monkeypatch):
    import app as app_module
    import tools
    from models import Room, db

    db.session.add(Room(name="lobby"))
    db.session.commit()

    asks, scripts = [], []

    def create(**kwargs):
        asks.append(kwargs)
        script = scripts.pop(0)

        def stream():
            for piece in script:
                if callable(piece):
                    piece()  # someone sends a message at this instant
                    continue
                yield piece

        return stream()

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(
        app_module, "get_openai_client_and_model", lambda name: (client, name)
    )
    emitted, spawned = [], []
    monkeypatch.setattr(
        app_module.socketio, "emit", lambda *a, **k: emitted.append((a, k))
    )
    monkeypatch.setattr(app_module.socketio, "sleep", lambda s: None)
    monkeypatch.setattr(app_module, "title_untitled_room", lambda name: None)
    monkeypatch.setattr(
        app_module.gevent, "spawn", lambda *a, **k: spawned.append((a, k))
    )

    # Who may call what: fox signed in gets the sandbox, a guest search only.
    def offered(context):
        if context is None:
            return {}
        names = {"web_search": "browser"}
        if context.email:
            names["sandbox_shell"] = "sandbox"
        return names

    monkeypatch.setattr(tools, "offered", offered)
    monkeypatch.setattr(tools, "schemas", lambda context: [tools.SCHEMAS["web_search"]])
    during_round = []

    def run_round(context, calls, content, notify=None, resume=None):
        notify({"tool": "web_search", "label": "x", "status": "running"})
        for send in during_round:
            send()
        return [
            tools.loop.assistant_turn(content, calls),
            {"role": "tool", "tool_call_id": "c1", "content": "found it"},
        ]

    monkeypatch.setattr(tools, "run_round", run_round)

    def send(words, message_id, model="qwen", context=None):
        context = context or tools.context("sid-bob")
        return lambda: app_module.steer_running_reply(
            "lobby", model, message_id, words, "bob", context
        )

    return SimpleNamespace(
        app=app_module,
        tools=tools,
        asks=asks,
        scripts=scripts,
        emitted=emitted,
        spawned=spawned,
        during_round=during_round,
        send=send,
    )


def steered(harness):
    return [
        (a[1]["message_id"], a[1]["status"])
        for a, _ in harness.emitted
        if a[0] == "message_steered"
    ]


def follow_ups(harness):
    return [(a, k) for a, k in harness.spawned if harness.app.chat_gpt in a]


def test_a_message_sent_mid_round_is_read_by_that_reply(harness):
    harness.scripts += [[search_call()], [delta("Done, and noted.")]]
    harness.during_round.append(harness.send("also check y", 41))
    harness.app.chat_gpt(
        "ada", "lobby", "qwen", tool_context=harness.tools.context("sid-ada")
    )

    after_round = harness.asks[1]["messages"]
    assert after_round[-1] == {"role": "user", "content": "also check y"}
    assert after_round[-2]["role"] == "tool"  # after its round's results
    assert steered(harness) == [(41, "queued"), (41, "read")]
    assert follow_ups(harness) == []  # one reply, steered, not two


def test_a_message_its_reply_can_no_longer_read_gets_the_next_reply(harness):
    late = harness.tools.context("sid-bob")
    harness.scripts.append(
        [delta("All "), harness.send("and z?", 42, context=late), delta("done.")]
    )
    harness.app.chat_gpt(
        "ada", "lobby", "qwen", tool_context=harness.tools.context("sid-ada")
    )

    assert steered(harness) == [(42, "queued"), (42, "answering")]
    ((args, kwargs),) = follow_ups(harness)
    assert args[1:] == (harness.app.chat_gpt, "bob", "lobby")
    assert kwargs["model_name"] == "qwen" and kwargs["tool_context"] is late
    assert harness.app.live_replies == {}


def test_a_guest_never_steers_a_reply_holding_someones_sandbox(harness):
    harness.scripts += [[search_call()], [delta("Done.")]]
    harness.during_round.append(harness.send("rm -rf it", 43))  # a guest's
    fox = harness.tools.context("sid-fox", SimpleNamespace(email="fox@example.test"))
    harness.app.chat_gpt("fox", "lobby", "qwen", tool_context=fox)

    assert all(m.get("content") != "rm -rf it" for m in harness.asks[1]["messages"])
    assert steered(harness) == [(43, "queued"), (43, "answering")]
    ((args, kwargs),) = follow_ups(harness)
    assert args[2] == "bob" and kwargs["tool_context"].email is None


def test_another_model_gets_a_reply_of_its_own(harness):
    harness.scripts += [[search_call()], [delta("Done.")]]
    taken = []
    harness.during_round.append(
        lambda: taken.append(harness.send("hi", 44, model="llama")())
    )
    harness.app.chat_gpt(
        "ada", "lobby", "qwen", tool_context=harness.tools.context("sid-ada")
    )
    assert taken == [None] and steered(harness) == []


def test_stop_drops_what_was_waiting(harness):
    def stop():
        harness.app.stop_requests.add("lobby")

    harness.scripts.append(
        [delta("All "), harness.send("and z?", 45), stop, delta("x")]
    )
    try:
        harness.app.chat_gpt(
            "ada", "lobby", "qwen", tool_context=harness.tools.context("sid-ada")
        )
    finally:
        harness.app.stop_requests.discard("lobby")
    assert follow_ups(harness) == []


def aside_harness(harness, monkeypatch, answer="It is searching."):
    asked = []

    def skip_thinking(client, **kwargs):
        asked.append(kwargs)
        message = SimpleNamespace(content=answer)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(harness.app, "create_completion_skip_thinking", skip_thinking)
    return asked


def asides(harness):
    return [
        a[1] for a, _ in harness.emitted if a[0] == "chat_message" and a[1].get("aside")
    ]


def test_an_aside_reads_the_running_reply_and_never_steers_it(harness, monkeypatch):
    asked = aside_harness(harness, monkeypatch)
    harness.scripts += [[search_call()], [delta("Done.")]]
    harness.during_round.append(
        lambda: harness.app.answer_aside("lobby", "what are you doing?", "qwen")
    )
    harness.app.chat_gpt(
        "ada", "lobby", "qwen", tool_context=harness.tools.context("sid-ada")
    )

    (ask,) = asked
    assert ask["model"] == "qwen" and "tools" not in ask
    body = ask["messages"][-1]["content"]
    assert "Running now: web_search" in body
    assert body.endswith("Aside question: what are you doing?")
    (said,) = asides(harness)
    assert said["username"] == "qwen (aside)" and said["content"] == "It is searching."
    # The running reply's next ask holds no trace of it.
    assert "what are you doing?" not in json.dumps(harness.asks[1]["messages"])
    assert steered(harness) == []


def test_a_btw_message_is_shown_and_never_stored(harness, monkeypatch, test_app):
    from models import Message

    aside_harness(harness, monkeypatch, answer="Nothing is running.")
    monkeypatch.setattr(harness.app.gevent, "spawn", lambda f, *a, **k: f(*a, **k))
    socket = harness.app.socketio.test_client(
        harness.app.app, flask_test_client=test_app.test_client()
    )
    socket.emit(
        "chat_message",
        {
            "room_name": "lobby",
            "username": "bob",
            "message": "/btw status?",
            "model": "qwen",
        },
    )
    question, answer = asides(harness)
    assert question["username"] == "bob" and question["content"] == "/btw status?"
    assert question["id"].startswith("aside-")
    assert answer["content"] == "Nothing is running."
    assert Message.query.count() == 0


def test_an_aside_knows_the_activity_but_never_its_hidden_state(harness, monkeypatch):
    from models import ActivityState, Room, Sandbox, db

    asked = aside_harness(harness, monkeypatch, answer="Step two.")
    room = Room.query.filter_by(name="lobby").one()
    state = ActivityState(
        room_id=room.id,
        section_id="vault",
        step_id="guess",
        attempts=1,
        max_attempts=3,
        s3_file_path="research/activity52-vault-heist.yaml",
    )
    state.dict_metadata = {"vault_code": "4417"}
    db.session.add(state)
    db.session.add(
        Sandbox(room_id=room.id, service_id="svc", name="sbx", state="frozen")
    )
    db.session.commit()
    content = {
        "sections": [
            {
                "section_id": "vault",
                "title": "The Vault",
                "steps": [
                    {
                        "step_id": "guess",
                        "question": "What is the four-digit code?",
                        "tokens_for_ai": "The code is 4417.",
                    }
                ],
            }
        ]
    }
    monkeypatch.setattr(harness.app.activity, "get_activity_content", lambda p: content)

    harness.app.answer_aside("lobby", "where are we?", "qwen")
    body = asked[0]["messages"][-1]["content"]
    assert "section vault, step guess; attempts on this step: 1 of 3" in body
    assert "What is the four-digit code?" in body
    assert "4417" not in body  # neither its metadata nor its grading notes
    assert "This chat has a sandbox (state: frozen" in body
    assert "a public chatroom" in body


def test_an_aside_sees_what_waits_for_a_running_reply(harness, monkeypatch):
    asked = aside_harness(harness, monkeypatch)
    harness.scripts.append(
        [
            delta("All "),
            harness.send("and z?", 46),
            lambda: harness.app.answer_aside("lobby", "status?", "qwen"),
            delta("done."),
        ]
    )
    harness.app.chat_gpt(
        "ada", "lobby", "qwen", tool_context=harness.tools.context("sid-ada")
    )
    body = asked[0]["messages"][-1]["content"]
    assert "Sent to it while it works, not yet read:\n- bob: and z?" in body
    assert "It has written this round so far:\nAll" in body
