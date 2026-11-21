"""Limits per address on what costs us something: messages a model
answers, sign-in code emails, new rooms & code runs. Plus two commands:
a bare `/activity` no longer crashes its handler, & `/activity metadata`
(a game's hidden state) is for a room's owner.

tests/conftest.py pins every limit off; each test here sets the count it
wants. Flask's & Flask-SocketIO's test clients, an in-memory database.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

KEYS = {"UNSANDBOX_PUBLIC_KEY": "unsb-pk-test", "UNSANDBOX_SECRET_KEY": "unsb-sk-test"}


@pytest.fixture
def app_module(test_app, monkeypatch):
    import app as app_module

    # Record what a message sets off instead of running it.
    spawned = []
    monkeypatch.setattr(
        app_module.gevent, "spawn", lambda fn, *a, **k: spawned.append((fn, a, k))
    )
    app_module.spawned = spawned
    return app_module


def limit(monkeypatch, throttle, count, seconds=600):
    throttle.reset()
    monkeypatch.setattr(throttle, "count", count)
    monkeypatch.setattr(throttle, "seconds", seconds)


def make_room(name, **fields):
    from models import Room, db

    room = Room(name=name, **fields)
    db.session.add(room)
    db.session.commit()
    return room


def socket_for(test_app, app_module, room="lobby", name="ada", http=None, **kw):
    http = http or test_app.test_client()
    client = app_module.socketio.test_client(
        app_module.app, flask_test_client=http, **kw
    )
    client.emit("join", {"room_name": room, "username": name})
    client.get_received()
    return client


def send(client, message, model="None", room="lobby"):
    client.emit(
        "chat_message",
        {"room_name": room, "username": "ada", "message": message, "model": model},
    )
    return [
        event["args"][0]
        for event in client.get_received()
        if event["name"] == "chat_message"
    ]


def stored(room_name="lobby"):
    from models import Message, Room

    room = Room.query.filter_by(name=room_name).first()
    return [m.content for m in Message.query.filter_by(room_id=room.id)]


def replies(app_module):
    return [fn for fn, a, k in app_module.spawned if fn is app_module.stoppable_reply]


# ── Messages ──────────────────────────────────────────────────────────────


def test_a_message_past_the_limit_is_neither_stored_nor_answered(
    test_app, app_module, monkeypatch
):
    limit(monkeypatch, app_module.MESSAGE_THROTTLE, 2)
    make_room("lobby")
    ada = socket_for(test_app, app_module)
    bob = socket_for(test_app, app_module, name="bob")

    send(ada, "one", "test-model")
    send(ada, "two", "test-model")
    bob.get_received()
    heard = send(ada, "three", "test-model")

    assert stored() == ["one", "two"]
    assert len(replies(app_module)) == 2
    # One quiet notice, saying how long to wait, to that socket alone.
    assert len(heard) == 1
    assert "Too many messages to models" in heard[0]["content"]
    assert "2 per 10 minutes" in heard[0]["content"]
    assert "Try again in 10 minutes" in heard[0]["content"]
    assert not isinstance(heard[0]["id"], int)  # a notice, never a row
    assert bob.get_received() == []


def test_people_talking_to_people_are_never_limited(test_app, app_module, monkeypatch):
    limit(monkeypatch, app_module.MESSAGE_THROTTLE, 1)
    make_room("lobby")
    ada = socket_for(test_app, app_module)
    for n in range(5):
        send(ada, f"hello {n}")
    assert len(stored()) == 5
    # & none of them used up the one message a model may answer.
    send(ada, "now a model", "test-model")
    assert len(replies(app_module)) == 1


def test_an_aside_counts(test_app, app_module, monkeypatch):
    limit(monkeypatch, app_module.MESSAGE_THROTTLE, 1)
    make_room("lobby")
    ada = socket_for(test_app, app_module)
    with patch.object(app_module, "ask_aside") as ask:
        send(ada, "/btw what is it doing?")
        heard = send(ada, "/btw and now?")
    assert ask.call_count == 1
    assert "Too many messages to models" in heard[0]["content"]


def test_answers_to_a_running_activity_are_never_limited(
    test_app, app_module, monkeypatch
):
    """A class plays one activity from a single address."""
    from models import ActivityState, db

    limit(monkeypatch, app_module.MESSAGE_THROTTLE, 1)
    room = make_room("lobby")
    db.session.add(
        ActivityState(
            room_id=room.id,
            section_id="s",
            step_id="q",
            max_attempts=3,
            s3_file_path="research/x.yaml",
        )
    )
    db.session.commit()
    ada = socket_for(test_app, app_module)
    for answer in ("my answer", "another answer", "a third"):
        send(ada, answer, model="qwen")  # even with a model picked
    assert stored() == ["my answer", "another answer", "a third"]


def test_addresses_are_counted_apart_by_what_our_proxy_appended(
    test_app, app_module, monkeypatch
):
    limit(monkeypatch, app_module.MESSAGE_THROTTLE, 1)
    make_room("lobby")
    # The left entry is a client's own claim: only the right one counts.
    ada = socket_for(
        test_app, app_module, headers={"X-Forwarded-For": "9.9.9.9, 10.0.0.1"}
    )
    eve = socket_for(
        test_app,
        app_module,
        name="eve",
        headers={"X-Forwarded-For": "1.1.1.1, 10.0.0.1"},
    )
    bob = socket_for(
        test_app, app_module, name="bob", headers={"X-Forwarded-For": "10.0.0.2"}
    )
    send(ada, "one", "test-model")
    assert "Too many" in send(eve, "two", "test-model")[-1]["content"]
    send(bob, "three", "test-model")
    assert stored() == ["one", "three"]


def test_zero_turns_the_limit_off(test_app, app_module, monkeypatch):
    limit(monkeypatch, app_module.MESSAGE_THROTTLE, 0)
    make_room("lobby")
    ada = socket_for(test_app, app_module)
    for n in range(5):
        send(ada, f"m{n}", "test-model")
    assert len(stored()) == 5


def test_defaults(monkeypatch):
    from throttle import Throttle

    for name in ("MESSAGE", "OTP", "ROOM", "CODE"):
        monkeypatch.delenv(f"OPENCOMPLETION_{name}_LIMIT", raising=False)
    import app as app_module
    from routes import accounts, code, rooms

    # Each throttle is built from its own knob; unset, these defaults hold.
    assert Throttle("OPENCOMPLETION_MESSAGE_LIMIT", (60, 600)).count == 60
    assert app_module.MESSAGE_THROTTLE.seconds == 600
    assert accounts.OTP_THROTTLE.seconds == 600
    assert rooms.ROOM_THROTTLE.seconds == 600
    assert code.CODE_THROTTLE.seconds == 600


def test_edit_and_resend_past_the_limit_is_refused(test_app, app_module, monkeypatch):
    from models import Message, db

    limit(monkeypatch, app_module.MESSAGE_THROTTLE, 1)
    room = make_room("lobby")
    first = Message(username="ada", content="first", room_id=room.id)
    db.session.add(first)
    db.session.commit()
    ada = socket_for(test_app, app_module)

    def resend(content):
        ada.emit(
            "edit_and_resend",
            {
                "message_id": first.id if content == "second" else revision_id(),
                "room_name": "lobby",
                "content": content,
                "model": "test-model",
            },
        )
        return ada.get_received()

    def revision_id():
        return Message.visible(room.id).order_by(Message.id.desc()).first().id

    resend("second")
    events = resend("third")
    assert [e["name"] for e in events] == ["edit_resend_refused"]
    assert "Too many messages to models" in events[0]["args"][0]["error"]
    # Nothing was hidden or posted by the refused resend.
    assert [m.content for m in Message.visible(room.id)] == ["second"]


# ── Sign-in codes ─────────────────────────────────────────────────────────


def test_sign_in_codes_are_limited_per_address(test_app, monkeypatch):
    from routes import accounts

    limit(monkeypatch, accounts.OTP_THROTTLE, 2)
    client = test_app.test_client()
    with patch("auth.send_otp_email", return_value="email") as sent:
        for _ in range(2):
            assert (
                client.post("/auth/send-otp", json={"email": "a@b.org"}).status_code
                == 200
            )
        # An address that fails validation costs nothing.
        assert client.post("/auth/send-otp", json={"email": "nope"}).status_code == 400
        refused = client.post("/auth/send-otp", json={"email": "c@d.org"})
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) > 0
    assert "Too many sign-in codes" in refused.get_json()["error"]
    assert sent.call_count == 2


# ── Rooms ─────────────────────────────────────────────────────────────────


def test_new_rooms_are_limited_per_address(test_app, app_module, monkeypatch):
    from models import Room
    from routes import rooms

    limit(monkeypatch, rooms.ROOM_THROTTLE, 2)
    client = test_app.test_client()
    assert client.post("/api/rooms/create", json={"name": "one"}).status_code == 200
    # A name already taken makes nothing & costs nothing.
    assert client.post("/api/rooms/create", json={"name": "one"}).status_code == 400
    assert client.post("/api/rooms/create", json={"name": "two"}).status_code == 200
    refused = client.post("/api/rooms/create", json={"name": "three"})
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) > 0
    assert "Too many new rooms" in refused.get_json()["error"]

    # A socket joining, or posting to, a name nobody has makes a room too.
    socket = app_module.socketio.test_client(app_module.app, flask_test_client=client)
    socket.emit("join", {"room_name": "four", "username": "ada"})
    events = socket.get_received()
    assert [e["name"] for e in events] == ["chat_message"]
    assert "Too many new rooms" in events[0]["args"][0]["content"]
    assert "Too many new rooms" in send(socket, "hi", room="five")[0]["content"]
    assert sorted(room.name for room in Room.query) == ["one", "two"]

    # Rooms that exist still open.
    socket.emit("join", {"room_name": "one", "username": "ada"})
    assert "joined" in [e["name"] for e in socket.get_received()]


# ── Code runs ─────────────────────────────────────────────────────────────


def test_code_runs_are_limited_per_address(test_app, monkeypatch):
    from routes import code

    limit(monkeypatch, code.CODE_THROTTLE, 2)
    client = test_app.test_client()
    body = {"language": "python", "code": "print(1)"}
    with (
        patch.dict("os.environ", KEYS),
        patch("un._make_request", return_value={"job_id": "job-1"}) as call,
    ):
        for _ in range(2):
            assert client.post("/api/code/execute", json=body).status_code == 200
        refused = client.post("/api/code/execute", json=body)
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) > 0
    assert "Too many code runs" in refused.get_json()["error"]
    assert call.call_count == 2


# ── Commands ──────────────────────────────────────────────────────────────


def test_a_bare_activity_command_says_how_it_is_used(test_app, app_module):
    make_room("lobby")
    ada = socket_for(test_app, app_module)
    for bare in ("/activity", "/activity   "):
        heard = send(ada, bare)
        assert heard[-1]["content"] == "_Usage: `/activity <path>`_"
    assert app_module.spawned == []
    send(ada, "/activity research/activity0.yaml")
    assert [(fn, a) for fn, a, k in app_module.spawned] == [
        (
            app_module.activity.start_activity,
            ("lobby", "research/activity0.yaml", "ada"),
        )
    ]


def shown_metadata(app_module):
    return [
        fn
        for fn, a, k in app_module.spawned
        if fn is app_module.activity.display_activity_metadata
    ]


def test_activity_metadata_is_for_a_signed_in_owner(test_app, app_module):
    from models import User, db

    owner = User(email="o@example.org", display_name="owner")
    other = User(email="p@example.org", display_name="passerby")
    db.session.add_all([owner, other])
    db.session.commit()
    make_room("lobby", owner_id=owner.id)

    def signed_in(user):
        http = test_app.test_client()
        with http.session_transaction() as session:
            session["user_id"] = user.id
        return socket_for(test_app, app_module, name=user.display_name, http=http)

    for client in (signed_in(other), socket_for(test_app, app_module, name="guest")):
        heard = send(client, "/activity metadata")
        assert (
            heard[-1]["content"] == "_`/activity metadata` is for this room's owner._"
        )
        assert shown_metadata(app_module) == []

    send(signed_in(owner), "/activity metadata")
    assert len(shown_metadata(app_module)) == 1


def test_activity_metadata_in_a_guest_room_is_for_the_browser_that_made_it(
    test_app, app_module
):
    maker = test_app.test_client()
    assert maker.post("/api/rooms/create", json={"name": "lobby"}).status_code == 200

    send(socket_for(test_app, app_module, name="eve"), "/activity metadata")
    assert shown_metadata(app_module) == []
    send(socket_for(test_app, app_module, http=maker), "/activity metadata")
    assert len(shown_metadata(app_module)) == 1


def test_a_room_nobody_owns_shows_its_metadata_to_nobody(test_app, app_module):
    make_room("lobby")
    send(socket_for(test_app, app_module), "/activity metadata")
    assert shown_metadata(app_module) == []
