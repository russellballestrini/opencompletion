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
    import routes

    monkeypatch.setitem(routes.DEPS, "system_users", app_module.SYSTEM_USERS)
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


def client_for(test_app, app_module, joined_as="ada", room="lobby"):
    """A socket in `room` under the name `joined_as` (resend checks it)."""
    http = test_app.test_client()
    client = app_module.socketio.test_client(app_module.app, flask_test_client=http)
    client.emit("join", {"room_name": room, "username": joined_as})
    client.get_received()
    return client


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
    # Someone opening the room now sees only the edit.
    http = test_app.test_client()
    bob = app_module.socketio.test_client(app_module.app, flask_test_client=http)
    bob.emit("join", {"room_name": "lobby", "username": "bob"})
    history = [
        e["args"][0] for e in bob.get_received() if e["name"] == "previous_messages"
    ]
    assert "second try" in str(history)
    assert "first try" not in str(history) and "first answer" not in str(history)

    download = test_app.test_client().get(
        "/download_chat_history_md", query_string={"room_name": "lobby"}
    )
    assert download.status_code == 200
    assert b"second try" in download.data and b"first try" not in download.data


def test_only_its_author_resends_a_message(test_app, app_module):
    room = make_room("lobby")
    ask = say(room, "ada", "hi")
    say(room, "qwen", "hello")
    bob = client_for(test_app, app_module, joined_as="bob")
    bob.emit(
        "edit_and_resend",
        {"message_id": ask.id, "content": "vandalised", "room_name": "lobby"},
    )
    assert visible(room) == ["hi", "hello"]
    assert replied_models(app_module) == []
    refusals = [e for e in bob.get_received() if e["name"] == "edit_resend_refused"]
    assert refusals and "author" in refusals[0]["args"][0]["error"]


def test_a_replaced_message_cannot_be_forked(test_app, app_module):
    room = make_room("lobby")
    ask = say(room, "ada", "first try")
    client_for(test_app, app_module).emit(
        "edit_and_resend",
        {"message_id": ask.id, "content": "second try", "room_name": "lobby"},
    )
    response = test_app.test_client().post(f"/api/messages/{ask.id}/fork", json={})
    assert response.status_code == 404


def resend(client, message, content):
    client.emit(
        "edit_and_resend",
        {"message_id": message.id, "content": content, "room_name": "lobby"},
    )
    return next(
        e["args"][0]
        for e in client.get_received()
        if e["name"] == "chat_message" and e["args"][0].get("content") == content
    )


def test_an_edited_message_lists_each_earlier_version_with_what_followed(
    test_app, app_module
):
    from models import Message

    room = make_room("lobby")
    first = say(room, "ada", "list three muppets")
    say(room, "qwen", "Elmo, Grover, Oscar")
    client = client_for(test_app, app_module)

    # The edit arrives saying it is version 2 of 2.
    posted = resend(client, first, "list two muppets")
    assert posted["versions"] == 2
    second = Message.visible(room.id).one()
    say(room, "qwen", "Elmo, Grover")
    assert resend(client, second, "list one muppet")["versions"] == 3
    third = Message.visible(room.id).one()

    body = test_app.test_client().get(f"/api/messages/{third.id}/versions").json
    assert [[(m["username"], m["content"]) for m in v] for v in body["versions"]] == [
        [("ada", "list three muppets"), ("qwen", "Elmo, Grover, Oscar")],
        [("ada", "list two muppets"), ("qwen", "Elmo, Grover")],
    ]
    assert [m["from_model"] for m in body["versions"][0]] == [False, True]

    # Someone opening the room is told too; a message never edited has one.
    http = test_app.test_client()
    bob = app_module.socketio.test_client(app_module.app, flask_test_client=http)
    bob.emit("join", {"room_name": "lobby", "username": "bob"})
    history = [
        e["args"][0] for e in bob.get_received() if e["name"] == "previous_messages"
    ]
    assert [(m["content"], m["versions"]) for m in history] == [("list one muppet", 3)]
    assert say(room, "bob", "hi").version_count() == 1


def test_versions_answer_only_for_a_message_someone_may_see(test_app, app_module):
    from models import Message, db

    room = make_room("lobby")
    first = say(room, "ada", "first try")
    client = client_for(test_app, app_module)
    resend(client, first, "second try")
    revision = Message.visible(room.id).one()
    http = test_app.test_client()

    # A replaced message & a missing one are equally unknown.
    assert http.get(f"/api/messages/{first.id}/versions").status_code == 404
    assert http.get("/api/messages/99999/versions").status_code == 404
    assert http.get(f"/api/messages/{revision.id}/versions").status_code == 200

    # A solo room's earlier versions are its owner's alone.
    room.is_private, room.owner_id = True, 4242
    db.session.commit()
    assert http.get(f"/api/messages/{revision.id}/versions").status_code == 404


def test_an_earlier_version_whose_messages_were_deleted_is_not_counted(
    test_app, app_module
):
    from models import Message, db

    room = make_room("lobby")
    first = say(room, "ada", "first try")
    client = client_for(test_app, app_module)
    resend(client, first, "second try")
    revision = Message.visible(room.id).one()
    db.session.delete(db.session.get(Message, first.id))
    db.session.commit()

    assert revision.version_count() == 1
    body = test_app.test_client().get(f"/api/messages/{revision.id}/versions").json
    assert body == {"versions": []}
