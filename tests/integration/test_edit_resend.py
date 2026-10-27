"""Edit & resend: a person's edited message is answered again for everyone
in the room, & the version it replaced (with everything after it) is
hidden, never deleted."""

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def app_module(test_app, monkeypatch):
    import app as app_module

    # Record replies instead of running them; "qwen" & "hermes" are models.
    spawned = []
    monkeypatch.setattr(
        app_module.gevent, "spawn", lambda fn, *a, **k: spawned.append((fn, a, k))
    )
    monkeypatch.setattr(app_module, "SYSTEM_USERS", ["qwen", "hermes", "system"])
    app_module.spawned = spawned
    return app_module


def make_room(name):
    from models import Room, db

    room = Room(name=name)
    db.session.add(room)
    db.session.commit()
    return room


def say(room, username, content):
    from models import Message, db

    message = Message(username=username, content=content, room_id=room.id)
    db.session.add(message)
    db.session.commit()
    return message


def client_for(test_app, app_module):
    http = test_app.test_client()
    return app_module.socketio.test_client(app_module.app, flask_test_client=http)


def replied_models(app_module):
    return [
        k["model_name"]
        for fn, a, k in app_module.spawned
        if fn is app_module.stoppable_reply
    ]


def visible(room):
    from models import Message

    return [m.content for m in Message.visible(room.id).order_by(Message.id)]


def test_an_edit_is_resent_and_the_old_branch_is_hidden_not_deleted(
    test_app, app_module
):
    from models import Message, db

    room = make_room("lobby")
    ask = say(room, "ada", "list three muppets")
    say(room, "qwen", "Elmo, Grover, Oscar")
    say(room, "bob", "more please")
    say(room, "qwen", "Big Bird")

    client = client_for(test_app, app_module)
    client.emit("join", {"room_name": "lobby", "username": "ada"})
    client.get_received()
    client.emit(
        "edit_and_resend",
        {"message_id": ask.id, "content": "list two muppets", "room_name": "lobby"},
    )

    # Everyone sees only the edit now; the old branch is still stored.
    assert visible(room) == ["list two muppets"]
    revision = Message.visible(room.id).one()
    assert revision.username == "ada" and revision.revision_of_id == ask.id
    hidden = Message.query.filter_by(superseded_by_id=revision.id).all()
    assert [m.content for m in hidden] == [
        "list three muppets",
        "Elmo, Grover, Oscar",
        "more please",
        "Big Bird",
    ]
    assert db.session.get(Message, ask.id).content == "list three muppets"

    events = {e["name"]: e["args"][0] for e in client.get_received()}
    assert set(events["messages_hidden"]["ids"]) == {m.id for m in hidden}
    # The model that answered the old message answers the edit.
    assert replied_models(app_module) == ["qwen"]


def test_with_no_earlier_answer_our_picked_model_answers(test_app, app_module):
    room = make_room("lobby")
    ask = say(room, "ada", "hello?")
    client = client_for(test_app, app_module)
    client.emit(
        "edit_and_resend",
        {
            "message_id": ask.id,
            "content": "hello!",
            "room_name": "lobby",
            "model": "hermes",
        },
    )
    assert visible(room) == ["hello!"]
    assert replied_models(app_module) == ["hermes"]


def test_a_model_reply_is_never_resent(test_app, app_module):
    room = make_room("lobby")
    say(room, "ada", "hi")
    reply = say(room, "qwen", "hello")
    client = client_for(test_app, app_module)
    client.emit(
        "edit_and_resend",
        {"message_id": reply.id, "content": "rewritten", "room_name": "lobby"},
    )
    assert visible(room) == ["hi", "hello"]
    assert replied_models(app_module) == []


def test_resend_waits_for_a_streaming_reply(test_app, app_module, monkeypatch):
    room = make_room("lobby")
    ask = say(room, "ada", "hi")
    monkeypatch.setitem(app_module.streaming_replies, "lobby", 1)
    client = client_for(test_app, app_module)
    client.emit(
        "edit_and_resend",
        {"message_id": ask.id, "content": "hey", "room_name": "lobby"},
    )
    assert visible(room) == ["hi"]
    names = [e["name"] for e in client.get_received()]
    assert "edit_resend_refused" in names


def test_hidden_messages_stay_out_of_history_forks_and_downloads(test_app, app_module):
    room = make_room("lobby")
    ask = say(room, "ada", "first try")
    say(room, "qwen", "first answer")
    client = client_for(test_app, app_module)
    client.emit(
        "edit_and_resend",
        {"message_id": ask.id, "content": "second try", "room_name": "lobby"},
    )
    client.get_received()
    client.emit("join", {"room_name": "lobby", "username": "ada"})
    history = [
        e["args"][0] for e in client.get_received() if e["name"] == "previous_messages"
    ]
    assert "second try" in str(history)
    assert "first try" not in str(history) and "first answer" not in str(history)

    download = test_app.test_client().get(
        "/download_chat_history_md", query_string={"room_name": "lobby"}
    )
    assert download.status_code == 200
    assert b"second try" in download.data and b"first try" not in download.data
