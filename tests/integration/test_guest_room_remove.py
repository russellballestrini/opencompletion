"""A guest's chatroom can be archived or deleted by the browser that made
it, & by nobody else. A guest's session holds a random key & the room keeps
its sha256 (Room.creator_key); signing in claims the rooms that browser
made; joining a room never claims it (it once did, so any signed-in
passerby became a guest room's owner & could delete it).

Uses Flask's & Flask-SocketIO's test clients over an in-memory database.
"""

import hashlib

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


def signed_in(test_app, user):
    client = test_app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = user.id
    return client


def create(client, name, private=False):
    response = client.post(
        "/api/rooms/create", json={"name": name, "is_private": private}
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["room"]


def room_named(name):
    from models import Room

    return Room.query.filter_by(name=name).first()


def test_a_guest_room_keeps_a_digest_of_its_browsers_key(test_app):
    maker = test_app.test_client()
    made = create(maker, "guest-room")
    assert made["may_remove"] is True
    room = room_named("guest-room")
    assert room.owner_id is None
    with maker.session_transaction() as session:
        raw = session["guest_room_key"]
    # Only a digest reaches our database, never the key itself.
    assert room.creator_key == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in room.creator_key


def test_the_maker_may_delete_a_guest_room_and_nobody_else(test_app):
    from models import Room

    maker = test_app.test_client()
    room_id = create(maker, "guest-room")["id"]

    stranger = test_app.test_client()
    create(stranger, "strangers-room")  # a guest key of their own
    assert stranger.delete(f"/api/rooms/{room_id}/delete").status_code == 403
    passerby = signed_in(test_app, make_user("bob@example.test", "bob"))
    assert passerby.delete(f"/api/rooms/{room_id}/delete").status_code == 403
    assert passerby.post(f"/api/rooms/{room_id}/archive").status_code == 403
    keyless = test_app.test_client()
    assert keyless.delete(f"/api/rooms/{room_id}/delete").status_code == 403

    assert maker.delete(f"/api/rooms/{room_id}/delete").status_code == 200
    assert Room.query.get(room_id) is None


def test_the_maker_may_archive_a_guest_room(test_app):
    maker = test_app.test_client()
    room_id = create(maker, "guest-room")["id"]
    assert maker.post(f"/api/rooms/{room_id}/archive").status_code == 200
    assert room_named("guest-room").is_archived is True


def test_a_room_made_before_creator_keys_stays_unremovable(test_app):
    from models import Room, db

    db.session.add(Room(name="old-room"))
    db.session.commit()
    room_id = room_named("old-room").id
    guest = test_app.test_client()
    create(guest, "mine")  # this browser has a key; it never matches None
    assert guest.delete(f"/api/rooms/{room_id}/delete").status_code == 403


def test_joining_a_guest_room_never_claims_it(test_app, app_module):
    maker = test_app.test_client()
    create(maker, "guest-room")
    passerby = signed_in(test_app, make_user("bob@example.test", "bob"))
    socket = app_module.socketio.test_client(app_module.app, flask_test_client=passerby)
    socket.emit("join", {"room_name": "guest-room", "username": "bob"})
    room = room_named("guest-room")
    assert room.owner_id is None and room.creator_key
    socket.disconnect()


def test_a_room_a_join_makes_is_its_makers(test_app, app_module):
    ada = make_user("ada@example.test", "ada")
    socket = app_module.socketio.test_client(
        app_module.app, flask_test_client=signed_in(test_app, ada)
    )
    socket.emit("join", {"room_name": "typed-in", "username": "ada"})
    assert room_named("typed-in").owner_id == ada.id
    socket.disconnect()

    guest = test_app.test_client()
    assert guest.get("/chat/guest-typed").status_code == 200  # its page, then its join
    socket = app_module.socketio.test_client(app_module.app, flask_test_client=guest)
    socket.emit("join", {"room_name": "guest-typed", "username": "someone"})
    socket.disconnect()
    room = room_named("guest-typed")
    assert room.owner_id is None and room.creator_key
    assert guest.delete(f"/api/rooms/{room.id}/delete").status_code == 200


def test_signing_in_claims_the_rooms_this_browser_made(test_app):
    import auth

    maker = test_app.test_client()
    create(maker, "guest-room")
    other = test_app.test_client()
    create(other, "someone-elses")
    ada = make_user("ada@example.test", "ada")
    with test_app.test_request_context():
        with maker.session_transaction() as saved:
            raw = saved["guest_room_key"]
        from flask import session

        session["guest_room_key"] = raw
        auth.login_user(ada)
        assert "guest_room_key" not in session
    assert room_named("guest-room").owner_id == ada.id
    assert room_named("guest-room").creator_key is None
    assert room_named("someone-elses").owner_id is None


def test_the_makers_page_offers_delete_and_a_strangers_does_not(test_app):
    maker = test_app.test_client()
    create(maker, "guest-room")
    page = maker.get("/chat/guest-room").get_data(as_text=True)
    assert 'onclick="deleteRoom(' in page and "mayRemove: true" in page

    stranger = test_app.test_client()
    page = stranger.get("/chat/guest-room").get_data(as_text=True)
    assert 'onclick="deleteRoom(' not in page and "mayRemove: false" in page
    # Its sidebar row is still renameable by anyone, never removable.
    assert 'data-room-name="guest-room"' in page
    assert 'data-can-remove="true"' not in page
