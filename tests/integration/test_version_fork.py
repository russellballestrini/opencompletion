"""Continue from an earlier version: a new chat holding a room's
conversation as it stood in one earlier version of an edited & resent
message (Message.as_it_stood), under Fork's rules; its source untouched."""

import json

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def fresh_limit():
    from routes import rooms

    rooms.FORK_THROTTLE.reset()
    yield
    rooms.FORK_THROTTLE.reset()


@pytest.fixture
def app_module(test_app, monkeypatch):
    import app as app_module
    import routes

    # No replies run: each test says what a model answered.
    monkeypatch.setattr(app_module.gevent, "spawn", lambda fn, *a, **k: None)
    monkeypatch.setattr(app_module, "SYSTEM_USERS", ["qwen", "system"])
    monkeypatch.setitem(routes.DEPS, "system_users", app_module.SYSTEM_USERS)
    return app_module


def make_user(email, name):
    from models import User, db

    user = User(email=email, display_name=name)
    db.session.add(user)
    db.session.commit()
    return user


def make_room(name, owner=None, private=False):
    from models import Room, db

    room = Room(name=name, is_private=private, owner_id=owner.id if owner else None)
    db.session.add(room)
    db.session.commit()
    return room


def say(room, username, content):
    from models import Message, db

    message = Message(username=username, content=content, room_id=room.id)
    db.session.add(message)
    db.session.commit()
    return message


def http_for(test_app, user=None, ip="203.0.113.7"):
    client = test_app.test_client()
    client.environ_base["HTTP_X_FORWARDED_FOR"] = ip
    if user:
        with client.session_transaction() as session:
            session["user_id"] = user.id
    return client


def resend(test_app, app_module, room, message, content, user=None):
    """Edit & resend `message` as its author, through our real handler;
    the revision it made."""
    from models import Message

    socket = app_module.socketio.test_client(
        app_module.app, flask_test_client=http_for(test_app, user)
    )
    socket.emit("join", {"room_name": room.name, "username": message.username})
    socket.emit(
        "edit_and_resend",
        {"message_id": message.id, "content": content, "room_name": room.name},
    )
    socket.disconnect()
    return Message.query.filter_by(revision_of_id=message.id).one()


def snapshot(room):
    from models import Message

    return [
        (m.id, m.username, m.content, m.superseded_by_id, m.revision_of_id)
        for m in Message.query.filter_by(room_id=room.id).order_by(Message.id)
    ]


def continued(response):
    """What the new chat holds, in order."""
    from models import Message, Room

    assert response.status_code == 200, response.get_data(as_text=True)
    new_room = Room.query.filter_by(name=response.json["room"]["name"]).one()
    return [
        m.content
        for m in Message.query.filter_by(room_id=new_room.id).order_by(Message.id)
    ]


def path(revision, version):
    return f"/api/messages/{revision.id}/versions/{version}/fork"


def test_a_new_chat_holds_what_came_before_and_that_versions_branch(
    test_app, app_module
):
    from models import Message, Room

    room = make_room("lobby")
    say(room, "ada", "hello")
    say(room, "qwen", "hi ada")
    ask = say(room, "ada", "list three muppets")
    say(room, "qwen", "Elmo, Grover, Oscar")
    revision = resend(test_app, app_module, room, ask, "list two muppets")
    say(room, "qwen", "Elmo, Grover")
    before = snapshot(room)

    response = http_for(test_app).post(path(revision, 1), json={})
    assert continued(response) == [
        "hello",
        "hi ada",
        "list three muppets",
        "Elmo, Grover, Oscar",
    ]
    new_room = Room.query.filter_by(name=response.json["room"]["name"]).one()
    assert new_room.forked_from_id == room.id and not new_room.is_private
    # Every copy shows in its new chat: nothing arrives hidden or as a revision.
    assert Message.visible(new_room.id).count() == 4
    assert not Message.query.filter(
        Message.room_id == new_room.id, Message.revision_of_id.isnot(None)
    ).count()
    # The room it came from is as it was.
    assert snapshot(room) == before


def test_a_message_edited_twice_continues_from_either_earlier_version(
    test_app, app_module
):
    room = make_room("lobby")
    say(room, "ada", "hello")
    say(room, "qwen", "hi ada")
    first = say(room, "ada", "three muppets")
    say(room, "qwen", "Elmo, Grover, Oscar")
    second = resend(test_app, app_module, room, first, "two muppets")
    say(room, "qwen", "Elmo, Grover")
    say(room, "ada", "thanks")
    third = resend(test_app, app_module, room, second, "one muppet")
    say(room, "qwen", "Elmo")
    before = snapshot(room)
    assert third.version_count() == 3

    guest = http_for(test_app)
    assert continued(guest.post(path(third, 1), json={})) == [
        "hello",
        "hi ada",
        "three muppets",
        "Elmo, Grover, Oscar",
    ]
    # Version 2 never holds version 1's branch, nor our current one.
    assert continued(guest.post(path(third, 2), json={})) == [
        "hello",
        "hi ada",
        "two muppets",
        "Elmo, Grover",
        "thanks",
    ]
    # Our current version is no earlier one (Fork continues from it), & a
    # version that was itself replaced is as unknown as a missing message.
    for missing in (path(third, 3), path(third, 0), path(second, 1)):
        assert guest.post(missing, json={}).status_code == 404
    assert guest.post("/api/messages/999999/versions/1/fork").status_code == 404
    # A message never edited has no earlier version.
    hello = snapshot(room)[0][0]
    assert (
        guest.post(f"/api/messages/{hello}/versions/1/fork", json={}).status_code == 404
    )
    assert snapshot(room) == before


def test_an_edit_inside_a_branch_that_an_earlier_edit_then_hid(test_app, app_module):
    """ada edits her second question, then her first: the first edit's
    whole world (the second question's revision included, never the words
    it replaced) is version 1 of the first question."""
    from models import Message, db

    room = make_room("lobby")
    opening = say(room, "ada", "name a muppet")
    say(room, "qwen", "Kermit")
    follow_up = say(room, "ada", "what colour is he?")
    say(room, "qwen", "Red")
    inner = resend(test_app, app_module, room, follow_up, "what colour is Kermit?")
    say(room, "qwen", "Green")
    outer = resend(test_app, app_module, room, opening, "name a monster")
    say(room, "qwen", "Cookie Monster")
    before = snapshot(room)

    # The inner revision is hidden now, yet it is what the room showed.
    assert db.session.get(Message, inner.id).superseded_by_id == outer.id
    guest = http_for(test_app)
    assert continued(guest.post(path(outer, 1), json={})) == [
        "name a muppet",
        "Kermit",
        "what colour is Kermit?",
        "Green",
    ]
    # Hidden with its room's past, the inner edit offers no versions itself.
    assert guest.post(path(inner, 1), json={}).status_code == 404
    assert snapshot(room) == before


def test_a_later_edit_keeps_what_an_earlier_messages_edit_hid_since(
    test_app, app_module
):
    """The other way round: a late message is edited, then an EARLIER one
    twice. Each of the earlier message's versions holds the room as it
    stood then, the late edit's revision with it."""
    room = make_room("lobby")
    opening = say(room, "ada", "name a muppet")
    say(room, "qwen", "Kermit")
    follow_up = say(room, "ada", "colour?")
    say(room, "qwen", "Red")
    resend(test_app, app_module, room, follow_up, "what colour is Kermit?")
    say(room, "qwen", "Green")
    second = resend(test_app, app_module, room, opening, "name a monster")
    say(room, "qwen", "Cookie Monster")
    third = resend(test_app, app_module, room, second, "name a bird")
    say(room, "qwen", "Big Bird")

    guest = http_for(test_app)
    assert continued(guest.post(path(third, 1), json={})) == [
        "name a muppet",
        "Kermit",
        "what colour is Kermit?",
        "Green",
    ]
    assert continued(guest.post(path(third, 2), json={})) == [
        "name a monster",
        "Cookie Monster",
    ]


def test_an_earlier_edit_hides_a_message_whose_versions_still_reach_back(
    test_app, app_module
):
    """A message edited after an earlier one was: what that earlier edit
    replaced is long hidden & stays out; what shows before it stays in."""
    room = make_room("lobby")
    opening = say(room, "ada", "hi")
    say(room, "qwen", "hello")
    greeting = resend(test_app, app_module, room, opening, "good morning")
    say(room, "qwen", "morning, ada")
    ask = say(room, "ada", "three muppets")
    say(room, "qwen", "Elmo, Grover, Oscar")
    revision = resend(test_app, app_module, room, ask, "two muppets")

    assert continued(http_for(test_app).post(path(revision, 1), json={})) == [
        "good morning",
        "morning, ada",
        "three muppets",
        "Elmo, Grover, Oscar",
    ]
    assert greeting.version_count() == 2


def test_thinking_tool_trails_and_files_are_copied_as_fork_copies_them(
    test_app, app_module
):
    import blob_store
    from models import Attachment, Message, Room, db

    room = make_room("lobby")
    kept = blob_store.save_attachment(
        room.id, b"muppet notes", "notes.txt", "text/plain", "ada"
    )
    link = blob_store.attachment_markdown(kept)
    ask = say(room, "ada", f"read this {link}")
    reply = say(room, "qwen", "Read it.")
    reply.thinking = "ada sent notes"
    reply.tool_trail = json.dumps([{"tool": "run_code", "label": "cat", "at": 0}])
    db.session.commit()
    revision = resend(test_app, app_module, room, ask, "never mind")

    response = http_for(test_app).post(path(revision, 1), json={})
    assert response.status_code == 200, response.get_data(as_text=True)
    new_room = Room.query.filter_by(name=response.json["room"]["name"]).one()
    asked, answered = Message.query.filter_by(room_id=new_room.id).order_by(Message.id)
    assert answered.thinking == "ada sent notes"
    assert json.loads(answered.tool_trail)[0]["tool"] == "run_code"
    # Its file is the new chat's own row (shared bytes), linked from the copy.
    (copy,) = Attachment.query.filter_by(room_id=new_room.id)
    assert copy.public_id != kept.public_id and copy.public_id in asked.content
    assert kept.public_id not in asked.content
    assert Attachment.query.filter_by(room_id=room.id).count() == 1


def test_signed_in_it_is_solo_and_a_guest_cannot_ask_for_solo(test_app, app_module):
    from models import Room

    me = make_user("me@example.test", "me")
    room = make_room("lobby")
    ask = say(room, "ada", "hi")
    revision = resend(test_app, app_module, room, ask, "hello")

    response = http_for(test_app, me).post(path(revision, 1), json={"private": True})
    assert response.status_code == 200, response.get_data(as_text=True)
    new_room = Room.query.filter_by(name=response.json["room"]["name"]).one()
    assert new_room.is_private and new_room.owner_id == me.id
    assert (
        http_for(test_app).post(path(revision, 1), json={"private": True}).status_code
        == 401
    )


def test_nobody_continues_someone_elses_solo_chat(test_app, app_module):
    from models import Room

    owner = make_user("owner@example.test", "owner")
    stranger = make_user("x@example.test", "x")
    room = make_room("secret", owner=owner, private=True)
    ask = say(room, "owner", "my plan")
    say(room, "qwen", "noted")
    revision = resend(test_app, app_module, room, ask, "my other plan", user=owner)
    assert revision.version_count() == 2

    for client in (http_for(test_app), http_for(test_app, stranger)):
        assert client.post(path(revision, 1), json={}).status_code == 404
    assert Room.query.count() == 1
    assert continued(http_for(test_app, owner).post(path(revision, 1), json={})) == [
        "my plan",
        "noted",
    ]


def test_it_shares_forks_limit_per_address(test_app, app_module):
    from models import Room
    from routes import rooms

    room = make_room("busy")
    ask = say(room, "ada", "hi")
    reply = say(room, "qwen", "hello")
    revision = resend(test_app, app_module, room, ask, "hey")
    newest = say(room, "qwen", "hey yourself")

    bot = http_for(test_app, ip="198.51.100.9")
    # One count for both: a plain fork spends it too.
    assert bot.post(f"/api/messages/{newest.id}/fork", json={}).status_code == 200
    for _ in range(rooms.FORK_THROTTLE.count - 1):
        assert bot.post(path(revision, 1), json={}).status_code == 200
    blocked = bot.post(path(revision, 1), json={})
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0
    assert "Too many forks" in blocked.json["error"]
    assert Room.query.count() == 1 + rooms.FORK_THROTTLE.count
    # A hidden reply still can't be forked by its own id.
    other = http_for(test_app, ip="192.0.2.44")
    assert other.post(f"/api/messages/{reply.id}/fork", json={}).status_code == 404
    assert other.post(path(revision, 1), json={}).status_code == 200


def test_its_chain_says_which_version_it_came_from(test_app, app_module):
    import chain
    from models import Room

    room = make_room("lobby")
    ask = say(room, "ada", "three muppets")
    answer = say(room, "qwen", "Elmo, Grover, Oscar")
    revision = resend(test_app, app_module, room, ask, "two muppets")
    head = chain.events(chain.chain_of(room))[-1].hash

    response = http_for(test_app).post(path(revision, 1), json={})
    assert response.status_code == 200, response.get_data(as_text=True)
    new_room = Room.query.filter_by(name=response.json["room"]["name"]).one()
    (genesis,) = [json.loads(e.line) for e in chain.events(chain.chain_of(new_room))]
    assert genesis["type"] == "fork" and genesis["fromHead"] == head
    assert genesis["revisionId"] == revision.id and genesis["version"] == 1
    assert genesis["uptoMessageId"] == answer.id
    assert [c["fromMessageId"] for c in genesis["copied"]] == [ask.id, answer.id]
    assert chain.verify(chain.chain_of(new_room))["ok"]
    # Nothing was chained into the room it came from.
    assert chain.events(chain.chain_of(room))[-1].hash == head
