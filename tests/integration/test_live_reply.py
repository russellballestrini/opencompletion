"""A reply as a page meets it mid-stream & at its end: a join is handed a
streaming reply's words so far from memory (not our last save), told how
many replies stream in its room (zero included), & a reply's end carries
its whole text, so no page keeps a short reply marked complete."""

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.integration


def chunk(content=None, reasoning=None):
    delta = SimpleNamespace(
        reasoning_content=reasoning, reasoning=None, content=content, tool_calls=None
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)])


@pytest.fixture
def room(test_app):
    from models import Room, db

    room = Room(name="lobby")
    db.session.add(room)
    db.session.commit()
    return room


def join(test_app, room_name="lobby"):
    import app as app_module

    socket = app_module.socketio.test_client(
        app_module.app, flask_test_client=test_app.test_client()
    )
    socket.emit("join", {"room_name": room_name, "username": "ada"})
    return socket, socket.get_received()


def named(events, name):
    return [e["args"][0] for e in events if e["name"] == name]


def test_a_join_always_hears_how_many_replies_stream(test_app, room, monkeypatch):
    """Zero too: a page back after its reply ended would keep Stop forever."""
    import app as app_module

    _, heard = join(test_app)
    assert named(heard, "replies_streaming") == [{"count": 0}]

    monkeypatch.setitem(app_module.streaming_replies, "lobby", 2)
    _, heard = join(test_app)
    assert named(heard, "replies_streaming") == [{"count": 2}]


def live_reply(app_module, room, saved="", buffer="", thinking=()):
    from models import Message, db

    message = Message(username="qwen", content=saved, room_id=room.id)
    db.session.add(message)
    db.session.commit()
    reply = app_module.ReplyStream(
        "ada", "lobby", "qwen", room.id, (message.id, saved, []), None, True, None, []
    )
    reply.buffer = buffer
    reply.thinking_parts = list(thinking)
    return message, reply


def test_a_join_mid_reply_gets_its_words_so_far_from_memory(
    test_app, room, monkeypatch
):
    import app as app_module
    from models import Message, db

    db.session.add(Message(username="ada", content="hi", room_id=room.id))
    message, reply = live_reply(
        app_module,
        room,
        saved="",  # nothing saved yet: a reply is saved at its tools & its end
        buffer="Elmo is red, and",
        thinking=["Who is ", "Elmo?"],
    )
    reply.shown_trail = [{"tool": "web_search", "status": "running", "at": 4}]
    monkeypatch.setitem(app_module.live_replies, message.id, reply)

    _, heard = join(test_app)
    history = {m["id"]: m for m in named(heard, "previous_messages")}
    mine = history[message.id]
    assert mine["streaming"] is True
    assert mine["content"] == "Elmo is red, and"
    assert mine["thinking"] == "Who is Elmo?"
    assert mine["tools"] == [{"tool": "web_search", "status": "running", "at": 4}]
    # Everyone else's message is as stored, & says it is not streaming.
    others = [m for m in history.values() if m["id"] != message.id]
    assert others and all(m["streaming"] is False for m in others)
    # Subscribed before the snapshot was taken: no chunk falls between.
    names = [e["name"] for e in heard]
    assert names.index("joined") < names.index("previous_messages")


def test_a_resumed_reply_keeps_the_thinking_it_saved(test_app, room, monkeypatch):
    import app as app_module
    from models import db

    message, reply = live_reply(app_module, room, saved="So far", buffer="So far")
    message.thinking = "earlier thoughts"
    db.session.commit()
    monkeypatch.setitem(app_module.live_replies, message.id, reply)
    _, heard = join(test_app)
    mine = [m for m in named(heard, "previous_messages") if m["id"] == message.id][0]
    assert mine["streaming"] is True and mine["thinking"] == "earlier thoughts"


@pytest.fixture
def harness(test_app, room, monkeypatch):
    import app as app_module

    scripts = []
    seen_live = []

    def create(**kwargs):
        script = scripts.pop(0)
        if isinstance(script, Exception):
            raise script

        def stream():
            for piece in script:
                # What a join at this instant would be handed.
                seen_live.append(
                    {i: r.snapshot() for i, r in app_module.live_replies.items()}
                )
                yield piece

        return stream()

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(
        app_module, "get_openai_client_and_model", lambda name: (client, name)
    )
    emitted = []
    monkeypatch.setattr(
        app_module.socketio, "emit", lambda *a, **k: emitted.append((a, k))
    )
    monkeypatch.setattr(app_module.socketio, "sleep", lambda s: None)
    monkeypatch.setattr(app_module, "title_untitled_room", lambda name: None)
    return SimpleNamespace(
        app=app_module, scripts=scripts, emitted=emitted, seen_live=seen_live
    )


def chunks_of(emitted):
    return [a[1] for a, _ in emitted if a[0] == "message_chunk"]


def test_a_replys_end_carries_its_whole_text(harness):
    from models import Message

    harness.scripts.append(
        [
            chunk(reasoning="Let me "),
            chunk(reasoning="think."),
            chunk(content="Elmo "),
            chunk(content="is "),
            chunk(content="red."),
        ]
    )
    harness.app.chat_gpt("ada", "lobby", "qwen")
    sent = chunks_of(harness.emitted)
    end = sent[-1]
    assert end["is_complete"] is True and end["content"] == ""
    assert end["final"] == "Elmo is red."
    assert end["thinking"] == "Let me think."
    assert end["model_name"] == "qwen"
    # What the end carries is what we stored & what the chunks added up to.
    stored = Message.query.filter_by(username="qwen").one()
    assert stored.content == end["final"]
    assert "".join(c.get("content") or "" for c in sent[:-1]) == end["final"]


def test_a_reply_is_live_only_while_it_streams(harness):
    harness.scripts.append([chunk(content="one "), chunk(content="two")])
    harness.app.chat_gpt("ada", "lobby", "qwen")
    # While it streamed, a join would have been handed its words so far.
    (first,), (second,) = [list(s.values()) for s in harness.seen_live]
    assert first["streaming"] and first["content"] == ""
    assert second["content"] == "one "
    assert harness.app.live_replies == {}


def test_a_reply_that_fails_is_live_no_more(harness):
    harness.scripts.append(RuntimeError("endpoint down"))
    harness.app.chat_gpt("ada", "lobby", "qwen")
    assert harness.app.live_replies == {}
    assert not [c for c in chunks_of(harness.emitted) if c.get("is_complete")]
