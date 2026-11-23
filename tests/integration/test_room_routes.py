"""Rooms' smaller edges: a download's filename can't break its header,
`is_private` is a real boolean, & Refresh title (a model call) is limited
per address. Flask's test client, an in-memory database, no model."""

import pytest

pytestmark = pytest.mark.integration


def make_room(name, **fields):
    from models import Message, Room, db

    room = Room(name=name, **fields)
    db.session.add(room)
    db.session.commit()
    db.session.add(Message(username="ada", content="hello", room_id=room.id))
    db.session.commit()
    return room


@pytest.mark.parametrize(
    "name",
    ["lobby", 'quote"d', "two words", "café", "semi;colon", "line\rbreak"],
)
def test_a_download_names_any_room_safely(test_app, name):
    from routes.rooms import download_as

    room = make_room(name)
    header = download_as(room, ".json")
    plain = header.split('filename="')[1].split('"')[0]
    assert all(c.isalnum() or c in "._-" for c in plain)
    assert "\r" not in header and "\n" not in header
    assert header.count('"') == 2 and header.endswith(".json")


@pytest.mark.parametrize(
    "path", ["/download_chat_history", "/download_chat_history_md"]
)
def test_downloads_carry_a_quoted_name(test_app, path):
    make_room("two words")
    response = test_app.test_client().get(path, query_string={"room_name": "two words"})
    assert response.status_code == 200
    disposition = response.headers["Content-Disposition"]
    assert 'filename="two_words.' in disposition
    assert "filename*=UTF-8''two%20words." in disposition


@pytest.mark.parametrize("flag", ["true", 1, "yes", ["x"]])
def test_only_a_real_true_makes_a_solo_chat(test_app, flag):
    from models import Room, User, db

    me = User(email="ada@example.org", display_name="ada")
    db.session.add(me)
    db.session.commit()
    http = test_app.test_client()
    with http.session_transaction() as session:
        session["user_id"] = me.id
    response = http.post("/api/rooms/create", json={"name": "r1", "is_private": flag})
    assert response.status_code == 200, response.json
    assert Room.query.filter_by(name="r1").one().is_private is False
    response = http.post("/api/rooms/create", json={"name": "r2", "is_private": True})
    assert Room.query.filter_by(name="r2").one().is_private is True


def test_refresh_title_is_limited_per_address(test_app, monkeypatch):
    from routes import DEPS, rooms

    asked = []
    monkeypatch.setitem(
        DEPS, "retitle_room", lambda room: asked.append(room.id) or "A title"
    )
    rooms.TITLE_THROTTLE.reset()
    monkeypatch.setattr(rooms.TITLE_THROTTLE, "count", 2)
    room = make_room("lobby")
    http = test_app.test_client()
    path = f"/api/rooms/{room.id}/title"
    for _ in range(2):
        assert http.post(path, json={"generate": True}).status_code == 200
    refused = http.post(path, json={"generate": True})
    assert refused.status_code == 429 and refused.headers["Retry-After"]
    assert len(asked) == 2
    # Typing a title asks no model & is never refused.
    assert http.post(path, json={"title": "Mine"}).status_code == 200
