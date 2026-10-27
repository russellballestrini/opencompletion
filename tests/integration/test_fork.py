"""Forking a conversation at a model's reply: what gets copied, who may
fork what, and our per-address limit that stops a bot from forking every
message of every chat in a loop."""

import pytest

from throttle import Throttle, parse_limit

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def fresh_limit():
    from routes import rooms

    rooms.FORK_THROTTLE.reset()
    yield
    rooms.FORK_THROTTLE.reset()


def make_user(email, name):
    from models import User, db

    user = User(email=email, display_name=name)
    db.session.add(user)
    db.session.commit()
    return user


def make_room(name, owner=None, private=False, lines=()):
    from models import Message, Room, db

    room = Room(name=name, is_private=private, owner_id=owner.id if owner else None)
    db.session.add(room)
    db.session.commit()
    ids = []
    for who, text in lines:
        message = Message(username=who, content=text, room_id=room.id)
        db.session.add(message)
        db.session.commit()
        ids.append(message.id)
    return room, ids


def client_for(test_app, user=None, ip="203.0.113.7"):
    client = test_app.test_client()
    client.environ_base["HTTP_X_FORWARDED_FOR"] = ip
    if user:
        with client.session_transaction() as session:
            session["user_id"] = user.id
    return client


CONVERSATION = [
    ("ada", "List some muppets"),
    ("hermes", "Big Bird, Elmo"),
    ("ada", "More please"),
    ("hermes", "Oscar, Grover"),
]


def test_forking_at_a_reply_copies_everything_up_to_it(test_app):
    from models import Message, Room

    room, ids = make_room("muppets", lines=CONVERSATION)
    response = client_for(test_app).post(f"/api/messages/{ids[1]}/fork", json={})
    assert response.status_code == 200, response.json
    fork = Room.query.filter_by(name=response.json["room"]["name"]).one()
    copied = [
        (m.username, m.content)
        for m in Message.query.filter_by(room_id=fork.id).order_by(Message.id)
    ]
    assert copied == CONVERSATION[:2]
    assert fork.forked_from_id == room.id and not fork.is_private
    assert fork.name.startswith("muppets-fork-")
    # The source chat stays as it was.
    assert Message.query.filter_by(room_id=room.id).count() == 4


def test_a_signed_in_fork_can_be_private(test_app):
    from models import Room

    me = make_user("me@example.test", "me")
    _, ids = make_room("mine", owner=me, private=True, lines=CONVERSATION)
    response = client_for(test_app, me).post(
        f"/api/messages/{ids[3]}/fork", json={"private": True}
    )
    assert response.status_code == 200, response.json
    fork = Room.query.filter_by(name=response.json["room"]["name"]).one()
    assert fork.is_private and fork.owner_id == me.id


def test_nobody_forks_someone_elses_private_chat(test_app):
    owner = make_user("owner@example.test", "owner")
    _, ids = make_room("secret", owner=owner, private=True, lines=CONVERSATION)
    stranger = make_user("x@example.test", "x")
    for client in (client_for(test_app), client_for(test_app, stranger)):
        assert client.post(f"/api/messages/{ids[1]}/fork", json={}).status_code == 404
    assert client_for(test_app).post("/api/messages/999999/fork").status_code == 404


def test_a_guest_cannot_make_a_private_fork(test_app):
    _, ids = make_room("open", lines=CONVERSATION)
    response = client_for(test_app).post(
        f"/api/messages/{ids[1]}/fork", json={"private": True}
    )
    assert response.status_code == 401


def test_forks_are_limited_per_address(test_app):
    from models import Room
    from routes import rooms

    _, ids = make_room("busy", lines=CONVERSATION)
    bot = client_for(test_app, ip="198.51.100.9")
    for _ in range(rooms.FORK_THROTTLE.count):
        assert bot.post(f"/api/messages/{ids[1]}/fork", json={}).status_code == 200
    blocked = bot.post(f"/api/messages/{ids[3]}/fork", json={})
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0
    assert "Too many forks" in blocked.json["error"]
    forks = Room.query.filter(Room.name.like("busy-fork-%")).count()
    assert forks == rooms.FORK_THROTTLE.count
    # Someone else, at another address, still forks.
    other = client_for(test_app, ip="192.0.2.44")
    assert other.post(f"/api/messages/{ids[1]}/fork", json={}).status_code == 200


def test_a_forged_forwarded_for_does_not_dodge_the_limit(test_app):
    from routes import rooms

    _, ids = make_room("spoof", lines=CONVERSATION)
    for n in range(rooms.FORK_THROTTLE.count + 1):
        client = test_app.test_client()
        # Our proxy appends the real address; the bot's own claim comes first.
        client.environ_base["HTTP_X_FORWARDED_FOR"] = f"10.0.0.{n}, 198.51.100.9"
        status = client.post(f"/api/messages/{ids[1]}/fork", json={}).status_code
    assert status == 429


def test_whole_room_fork_still_copies_every_message(test_app):
    from models import Message, Room

    room, _ = make_room("whole", lines=CONVERSATION)
    response = client_for(test_app).post(f"/api/rooms/{room.id}/fork", json={})
    assert response.status_code == 200
    fork = Room.query.filter_by(name=response.json["room"]["name"]).one()
    assert Message.query.filter_by(room_id=fork.id).count() == 4


def test_throttle_counts_a_sliding_window():
    throttle = Throttle("UNSET_FOR_TEST", default=(2, 60))
    assert throttle.hit("a", now=0) == 0 and throttle.hit("a", now=1) == 0
    assert throttle.hit("a", now=2) == 58
    assert throttle.hit("b", now=2) == 0
    assert throttle.hit("a", now=61) == 0  # the first hit has expired


def test_limit_settings_parse_or_fall_back():
    assert parse_limit("5/30", (10, 600)) == (5, 30)
    assert parse_limit("0/30", (10, 600)) == (0, 30)
    assert parse_limit("0", (10, 600)) == (0, 600)
    for junk in (None, "", "ten", "5/0", "-1/30"):
        assert parse_limit(junk, (10, 600)) == (10, 600)
    off = Throttle("UNSET_FOR_TEST", default=(0, 60))
    assert all(off.hit("a", now=0) == 0 for _ in range(50))


def test_history_marks_model_replies(test_app, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "SYSTEM_USERS", ["hermes", "system"])
    # Joining an untitled room titles it in the background; not this test's job.
    monkeypatch.setattr(app_module.gevent, "spawn", lambda *a, **k: None)
    make_room("marked", lines=CONVERSATION)
    socket = app_module.socketio.test_client(
        app_module.app, flask_test_client=test_app.test_client()
    )
    socket.emit("join", {"room_name": "marked", "username": "ada"})
    history = [
        e["args"][0] for e in socket.get_received() if e["name"] == "previous_messages"
    ]
    assert [m["from_model"] for m in history] == [False, True, False, True]
