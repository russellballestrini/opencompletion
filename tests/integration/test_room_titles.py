"""Rename a chat or have a model title it afresh, from our chat actions
menu: POST /api/rooms/<id>/title."""

import pytest

pytestmark = pytest.mark.integration


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


def client_for(test_app, user=None):
    client = test_app.test_client()
    if user:
        with client.session_transaction() as session:
            session["user_id"] = user.id
    return client


@pytest.fixture
def emitted(test_app, monkeypatch):
    import app as app_module

    sent = []
    monkeypatch.setattr(
        app_module.socketio, "emit", lambda event, data, **k: sent.append((event, data))
    )
    return sent


def test_an_owner_renames_their_chat(test_app, emitted):
    from models import Room, db

    ada = make_user("ada@example.test", "ada")
    room = make_room("plans", owner=ada, private=True)
    response = client_for(test_app, ada).post(
        f"/api/rooms/{room.id}/title", json={"title": "  Garden\n plans  "}
    )
    assert response.status_code == 200
    assert response.json["title"] == "Garden plans"
    assert db.session.get(Room, room.id).title == "Garden plans"
    assert ("update_room_title", {"title": "Garden plans"}) in emitted


def test_someone_else_cannot_rename_an_owned_chat(test_app, emitted):
    ada = make_user("ada@example.test", "ada")
    bob = make_user("bob@example.test", "bob")
    public = make_room("open", owner=ada)
    solo = make_room("mine", owner=ada, private=True)
    as_bob = client_for(test_app, bob)
    assert (
        as_bob.post(f"/api/rooms/{public.id}/title", json={"title": "x"}).status_code
        == 403
    )
    # A solo chat is as unknown as a missing one.
    assert (
        as_bob.post(f"/api/rooms/{solo.id}/title", json={"title": "x"}).status_code
        == 404
    )
    assert (
        client_for(test_app)
        .post(f"/api/rooms/{public.id}/title", json={"title": "x"})
        .status_code
        == 403
    )


def test_anyone_may_retitle_an_unowned_public_room(test_app, emitted):
    room = make_room("lobby")
    guest = client_for(test_app)
    assert (
        guest.post(f"/api/rooms/{room.id}/title", json={"title": "Lobby"}).status_code
        == 200
    )
    assert (
        guest.post(f"/api/rooms/{room.id}/title", json={"title": "  "}).status_code
        == 400
    )


def test_refresh_title_asks_a_model(test_app, emitted, monkeypatch):
    import app as app_module
    from models import Room, db

    room = make_room("lobby")
    monkeypatch.setattr(
        app_module, "gpt_generate_room_title", lambda msgs: "Muppet census"
    )
    response = client_for(test_app).post(
        f"/api/rooms/{room.id}/title", json={"generate": True}
    )
    assert response.json == {"title": "Muppet census"}
    assert db.session.get(Room, room.id).title == "Muppet census"

    monkeypatch.setattr(app_module, "gpt_generate_room_title", lambda msgs: None)
    response = client_for(test_app).post(
        f"/api/rooms/{room.id}/title", json={"generate": True}
    )
    assert response.status_code == 502
    assert db.session.get(Room, room.id).title == "Muppet census"


def test_room_cards_and_sidebar_rows_say_what_the_menu_may_offer(test_app):
    ada = make_user("ada@example.test", "ada")
    make_room("adas-room", owner=ada)
    make_room("lobby")
    page = client_for(test_app, ada).get("/browse").get_data(as_text=True)
    assert 'data-room-name="adas-room"' in page
    assert 'data-can-remove="true"' in page
    assert page.count("data-chat-actions") >= 2


def test_we_are_installable_as_an_app(test_app, monkeypatch):
    monkeypatch.setenv("OPENCOMPLETION_BRAND", "uncloseai")
    client = client_for(test_app)
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.mimetype == "application/manifest+json"
    manifest = response.json
    assert manifest["name"] == "uncloseai"
    assert manifest["display"] == "standalone" and manifest["start_url"] == "/"
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200
    page = client.get("/").get_data(as_text=True)
    assert 'rel="manifest"' in page and "apple-touch-icon" in page
