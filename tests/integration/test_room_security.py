"""Who may read, post, edit & delete where: socket events & downloads apply
our same private-room rule as the /chat page, and a posting name comes from
the server, never from whatever a client sends.

Uses Flask-SocketIO's test client over an in-memory database; no network.
"""

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def app_module(test_app):
    import app as app_module

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


def add_message(room, content, username="someone"):
    from models import Message, db

    message = Message(username=username, content=content, room_id=room.id)
    db.session.add(message)
    db.session.commit()
    return message


def http_client(test_app, user=None):
    client = test_app.test_client()
    if user:
        with client.session_transaction() as session:
            session["user_id"] = user.id
    return client


def socket_client(app_module, http):
    """A socket client sharing `http`'s cookie session (so its sign-in)."""
    return app_module.socketio.test_client(app_module.app, flask_test_client=http)


def received(client, event):
    return [msg["args"][0] for msg in client.get_received() if msg["name"] == event]


# --- private rooms --------------------------------------------------------


def test_socket_join_to_someone_elses_private_room_is_refused(test_app, app_module):
    owner = make_user("owner@example.test", "owner")
    room = make_room("secret", owner=owner, private=True)
    add_message(room, "the vault code is 1234")

    guest = socket_client(app_module, http_client(test_app))
    guest.emit("join", {"room_name": "secret", "username": "guest"})
    events = guest.get_received()
    names = [e["name"] for e in events]
    assert "access_denied" in names
    assert "the vault code" not in str(events)

    owner_socket = socket_client(app_module, http_client(test_app, owner))
    owner_socket.emit("join", {"room_name": "secret", "username": "whatever"})
    assert "the vault code is 1234" in str(owner_socket.get_received())


def test_guest_cannot_post_into_a_private_room(test_app, app_module):
    from models import Message

    owner = make_user("owner@example.test", "owner")
    room = make_room("secret", owner=owner, private=True)
    guest = socket_client(app_module, http_client(test_app))
    guest.emit(
        "chat_message",
        {"room_name": "secret", "username": "guest", "message": "hi", "model": "None"},
    )
    assert Message.query.filter_by(room_id=room.id).count() == 0


@pytest.mark.parametrize(
    "path", ["/download_chat_history", "/download_chat_history_md"]
)
def test_history_download_hides_private_rooms_and_creates_nothing(test_app, path):
    from models import Room

    owner = make_user("owner@example.test", "owner")
    room = make_room("secret", owner=owner, private=True)
    add_message(room, "private words")

    guest = http_client(test_app)
    response = guest.get(f"{path}?room_name=secret")
    assert response.status_code == 404
    assert b"private words" not in response.data

    assert (
        http_client(test_app, owner).get(f"{path}?room_name=secret").status_code == 200
    )

    guest.get(f"{path}?room_name=never-made")
    assert Room.query.filter_by(name="never-made").first() is None


# --- names ----------------------------------------------------------------


def test_signed_in_user_always_posts_as_themselves(test_app, app_module):
    from models import Message

    alice = make_user("alice@example.test", "alice")
    room = make_room("lobby")
    client = socket_client(app_module, http_client(test_app, alice))
    client.emit(
        "chat_message",
        {"room_name": "lobby", "username": "bob", "message": "hi", "model": "None"},
    )
    assert Message.query.filter_by(room_id=room.id).one().username == "alice"


@pytest.mark.parametrize(
    "claimed, posted",
    [
        ("alice", "alice (guest)"),  # a registered display name
        ("ALICE", "ALICE (guest)"),
        ("system", "system (guest)"),  # would become a system turn in prompts
        ("ada,lovelace", "ada lovelace"),  # room user lists are CSV
        ("", "guest"),
        ("wanderer", "wanderer"),
    ],
)
def test_guest_names_never_impersonate(test_app, app_module, claimed, posted):
    from models import Message

    make_user("alice@example.test", "alice")
    room = make_room("lobby")
    client = socket_client(app_module, http_client(test_app))
    client.emit("join", {"room_name": "lobby", "username": claimed})
    assert received(client, "your_username") == [{"username": posted}]
    client.emit(
        "chat_message",
        {"room_name": "lobby", "username": claimed, "message": "hi", "model": "None"},
    )
    assert Message.query.filter_by(room_id=room.id).one().username == posted


def test_guest_cannot_take_a_model_name(test_app, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "SYSTEM_USERS", ["hermes-3"])
    with test_app.test_request_context():
        assert app_module.resolve_username("Hermes-3") == "Hermes-3 (guest)"


# --- edits & deletes ------------------------------------------------------


def test_edit_and_delete_stay_inside_their_room(test_app, app_module):
    from models import Message, db

    make_room("lobby")
    other = make_room("other")
    target = add_message(other, "original")
    client = socket_client(app_module, http_client(test_app))

    # Naming a room the message is not in does nothing.
    client.emit(
        "update_message",
        {"message_id": target.id, "content": "vandalised", "room_name": "lobby"},
    )
    client.emit("delete_message", {"message_id": target.id, "room_name": "lobby"})
    assert db.session.get(Message, target.id).content == "original"

    # In its own public room, our collaborative edit still works.
    client.emit(
        "update_message",
        {"message_id": target.id, "content": "edited", "room_name": "other"},
    )
    assert db.session.get(Message, target.id).content == "edited"
    client.emit("delete_message", {"message_id": target.id, "room_name": "other"})
    assert db.session.get(Message, target.id) is None


def test_private_room_messages_are_owner_only(test_app, app_module):
    from models import Message, db

    owner = make_user("owner@example.test", "owner")
    room = make_room("secret", owner=owner, private=True)
    target = add_message(room, "mine")

    guest = socket_client(app_module, http_client(test_app))
    guest.emit("delete_message", {"message_id": target.id, "room_name": "secret"})
    assert db.session.get(Message, target.id) is not None

    owner_socket = socket_client(app_module, http_client(test_app, owner))
    owner_socket.emit(
        "delete_message", {"message_id": target.id, "room_name": "secret"}
    )
    assert db.session.get(Message, target.id) is None


def test_activity_status_reveals_nothing_about_private_rooms(test_app, app_module):
    from models import ActivityState, Room, db

    owner = make_user("owner@example.test", "owner")
    room = make_room("secret", owner=owner, private=True)
    db.session.add(
        ActivityState(
            room_id=room.id,
            section_id="s",
            step_id="t",
            s3_file_path="research/activity29-battleship.yaml",
        )
    )
    db.session.commit()

    guest = socket_client(app_module, http_client(test_app))
    guest.emit("get_activity_status", {"room_name": "secret"})
    assert received(guest, "activity_status") == [{"active": False}]

    guest.emit("get_activity_status", {"room_name": "never-made"})
    assert Room.query.filter_by(name="never-made").first() is None

    # The owner is handed on to activity.handle_get_activity_status (other
    # tests replace activity's socketio, so assert the hand-off itself).
    from unittest.mock import patch

    owner_socket = socket_client(app_module, http_client(test_app, owner))
    with patch("activity.handle_get_activity_status") as handler:
        owner_socket.emit("get_activity_status", {"room_name": "secret"})
        guest.emit("get_activity_status", {"room_name": "secret"})
    assert handler.call_count == 1
    assert handler.call_args.args[0] == {"room_name": "secret"}


# --- room names -----------------------------------------------------------

SCANNER_NAME = "bs4' UNION ALL SELECT NULL,NULL,NULL-- 88ojyu"


@pytest.mark.parametrize(
    "name, ok",
    [
        ("lobby", True),
        ("my-room_2", True),
        ("a" * 64, True),
        ("a" * 65, False),
        ("Lobby", False),
        ("-lobby", False),
        ("two words", False),
        ("", False),
        (None, False),
        (SCANNER_NAME, False),
    ],
)
def test_valid_room_name(name, ok):
    from routes.rooms import valid_room_name

    assert valid_room_name(name) is ok


def test_chat_page_refuses_to_offer_a_room_nobody_could_name(test_app, app_module):
    from models import Room

    client = http_client(test_app)
    assert client.get("/chat/bs4%27%20UNION%20ALL%20SELECT%20NULL--").status_code == 404
    assert client.get("/chat/lobby").status_code == 200

    # A room from before our rule still opens.
    make_room("Old Room")
    assert client.get("/chat/Old Room").status_code == 200
    assert Room.query.count() == 1


def test_socket_join_and_post_create_no_room_under_a_junk_name(test_app, app_module):
    from models import Message, Room

    guest = socket_client(app_module, http_client(test_app))
    guest.emit("join", {"room_name": SCANNER_NAME, "username": "guest"})
    guest.emit(
        "chat_message",
        {"room_name": SCANNER_NAME, "username": "g", "message": "x", "model": "None"},
    )
    assert Room.query.count() == 0
    assert Message.query.count() == 0

    guest.emit("join", {"room_name": "lobby", "username": "guest"})
    assert Room.query.filter_by(name="lobby").count() == 1


def test_room_create_api_refuses_a_junk_name(test_app):
    from models import Room

    client = http_client(test_app)
    response = client.post("/api/rooms/create", json={"name": SCANNER_NAME})
    assert response.status_code == 400
    assert Room.query.count() == 0
    assert client.post("/api/rooms/create", json={"name": "fine"}).status_code == 200


def test_prune_rooms_lists_then_deletes_only_empty_junk(test_app):
    import io

    import prune_rooms
    from models import Room

    make_room("lobby")
    make_room(SCANNER_NAME)
    chatty = make_room("Old Room")
    add_message(chatty, "keep me")

    out = io.StringIO()
    assert prune_rooms.prune(out=out) == 0
    assert "UNION" in out.getvalue()
    assert Room.query.count() == 3

    assert prune_rooms.prune(delete=True, out=io.StringIO()) == 1
    assert {r.name for r in Room.query.all()} == {"lobby", "Old Room"}

    assert prune_rooms.prune(delete=True, include_messages=True, out=io.StringIO()) == 1
    assert {r.name for r in Room.query.all()} == {"lobby"}


def test_prune_keeps_a_room_running_an_activity(test_app):
    import io

    import prune_rooms
    from models import ActivityState, Room, db

    room = make_room(SCANNER_NAME)
    db.session.add(
        ActivityState(room_id=room.id, section_id="s", step_id="t", s3_file_path="a")
    )
    db.session.commit()

    assert prune_rooms.prune(delete=True, out=io.StringIO()) == 0
    assert Room.query.count() == 1


def test_prune_rechecks_for_messages_when_it_deletes(test_app):
    import prune_rooms
    from models import Message, Room

    room = make_room(SCANNER_NAME)
    assert prune_rooms.junk_rooms() == [(room, 0)]
    add_message(room, "posted after our listing")

    assert prune_rooms.delete_room(room) is False
    assert Room.query.count() == 1
    assert Message.query.count() == 1


@pytest.mark.parametrize("body", [{"name": None}, {"name": 7}, ["lobby"]])
def test_room_create_api_refuses_a_name_that_is_not_text(test_app, body):
    response = http_client(test_app).post("/api/rooms/create", json=body)
    assert response.status_code == 400
