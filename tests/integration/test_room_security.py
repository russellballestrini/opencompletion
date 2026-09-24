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
    from models import Message

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
    assert Message.query.get(target.id).content == "original"

    # In its own public room, our collaborative edit still works.
    client.emit(
        "update_message",
        {"message_id": target.id, "content": "edited", "room_name": "other"},
    )
    assert Message.query.get(target.id).content == "edited"
    client.emit("delete_message", {"message_id": target.id, "room_name": "other"})
    assert Message.query.get(target.id) is None


def test_private_room_messages_are_owner_only(test_app, app_module):
    from models import Message

    owner = make_user("owner@example.test", "owner")
    room = make_room("secret", owner=owner, private=True)
    target = add_message(room, "mine")

    guest = socket_client(app_module, http_client(test_app))
    guest.emit("delete_message", {"message_id": target.id, "room_name": "secret"})
    assert Message.query.get(target.id) is not None

    owner_socket = socket_client(app_module, http_client(test_app, owner))
    owner_socket.emit(
        "delete_message", {"message_id": target.id, "room_name": "secret"}
    )
    assert Message.query.get(target.id) is None
