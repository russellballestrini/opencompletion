"""Message.created_at: when a message was written, for our page's timeline."""

import time

import pytest

pytestmark = pytest.mark.integration


def test_a_message_knows_when_it_was_written(test_app):
    from models import Message, Room, db

    room = Room(name="times")
    db.session.add(room)
    db.session.commit()
    before = time.time()
    message = Message(username="ada", content="hi", room_id=room.id)
    db.session.add(message)
    db.session.commit()
    assert before <= message.created_at <= time.time()

    import app as app_module

    entry = app_module.history_entry(message, "times")
    assert entry["created_at"] == message.created_at


def test_an_older_message_has_no_time_and_says_none(test_app):
    from models import Message, Room, db

    room = Room(name="old-times")
    db.session.add(room)
    db.session.commit()
    message = Message(username="ada", content="hi", room_id=room.id)
    db.session.add(message)
    db.session.commit()
    message.created_at = None  # a row from before migration 2026112501
    db.session.commit()

    import app as app_module

    assert app_module.history_entry(message, "old-times")["created_at"] is None


def test_a_sent_message_carries_its_time_to_the_room(test_app, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "dispatch_message", lambda *a, **k: None)
    client = app_module.socketio.test_client(test_app)
    client.emit("join", {"username": "ada", "room_name": "times-live"})
    client.get_received()
    client.emit(
        "chat_message",
        {
            "username": "ada",
            "message": "hello",
            "model": "None",
            "room_name": "times-live",
        },
    )
    sent = [
        e["args"][0]
        for e in client.get_received()
        if e["name"] == "chat_message" and e["args"][0].get("content") == "hello"
    ]
    assert sent and sent[0]["created_at"] > 0


def test_a_fork_keeps_each_messages_time(test_app):
    from models import Message, Room, db

    room = Room(name="times-fork")
    db.session.add(room)
    db.session.commit()
    ids = []
    for who, text, at in (("ada", "q", 1000.0), ("hermes", "a", 1060.0)):
        message = Message(username=who, content=text, room_id=room.id)
        message.created_at = at
        db.session.add(message)
        db.session.commit()
        ids.append(message.id)
    client = test_app.test_client()
    response = client.post(f"/api/messages/{ids[1]}/fork", json={})
    assert response.status_code == 200, response.json
    fork = Room.query.filter_by(name=response.json["room"]["name"]).one()
    times = [
        m.created_at
        for m in Message.query.filter_by(room_id=fork.id).order_by(Message.id)
    ]
    assert times == [1000.0, 1060.0]
