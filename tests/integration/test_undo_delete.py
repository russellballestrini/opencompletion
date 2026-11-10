"""Undo for deletes: in a public chatroom anyone may delete any message,
so a delete keeps its row, hidden from every reader, & anyone in the room
may restore it (migration 2026111001). Its words stay off the wire until
then; the room's chain records `delete` then `restore`, each with `by`."""

import time

import pytest

import chain
from models import Message, Room, User, db

from tests.integration.test_room_chain import (  # noqa: F401  shared fixtures
    bodies,
    kinds,
    quiet,
    socket_for,
)

pytestmark = pytest.mark.integration


def make_room(name="lobby", **fields):
    room = Room(name=name, **fields)
    db.session.add(room)
    db.session.commit()
    return room


def say(client, words, room="lobby", username="ada"):
    client.emit(
        "chat_message",
        {"room_name": room, "username": username, "message": words, "model": "None"},
    )
    return Message.query.filter_by(content=words).one()


def heard(client, event):
    return [e["args"][0] for e in client.get_received() if e["name"] == event]


def conversation(room):
    return [m.content for m in Message.visible(room.id).order_by(Message.id)]


def three_messages(test_app):
    """A lobby where ada said three things, bob beside her; both caught up."""
    room = make_room()
    ada = socket_for(test_app)
    bob = socket_for(test_app, username="bob")
    said = [say(ada, words) for words in ("first", "second", "third")]
    ada.get_received()
    bob.get_received()
    return room, ada, bob, said


def test_a_deleted_message_is_kept_but_gone_for_every_reader(test_app, quiet):
    import app as app_module

    room, ada, bob, (first, second, third) = three_messages(test_app)
    bob.emit("delete_message", {"room_name": "lobby", "message_id": second.id})

    row = db.session.get(Message, second.id)
    assert row.content == "second" and row.deleted_by == "bob"
    assert abs(row.deleted_at - time.time()) < 60
    assert conversation(room) == ["first", "third"]

    # Everyone hears who wrote it & who deleted it, never its words
    for client in (ada, bob):
        (marker,) = heard(client, "message_deleted")
        assert marker == {
            "message_id": second.id,
            "username": "ada",
            "deleted_by": "bob",
            "restorable": True,
        }

    # What a model reads
    _, history = app_module.reply_history("lobby", "qwen")
    assert "second" not in str(history) and "third" in str(history)

    # Downloads
    http = test_app.test_client()
    doc = http.get(
        "/download_chat_history", query_string={"room_name": "lobby"}
    ).get_json()
    assert [m["content"] for m in doc["messages"]] == ["first", "third"]
    md = http.get(
        "/download_chat_history_md", query_string={"room_name": "lobby"}
    ).get_data(as_text=True)
    assert "second" not in md and "third" in md

    # Forks: of the room, & never from the deleted message itself
    forked = http.post(f"/api/rooms/{room.id}/fork", json={}).get_json()["room"]
    assert conversation(db.session.get(Room, forked["id"])) == ["first", "third"]
    assert http.post(f"/api/messages/{second.id}/fork", json={}).status_code == 404
    assert http.get(f"/api/messages/{second.id}/versions").status_code == 404

    # Search
    from routes.rooms import search_messages

    assert search_messages("second") == [] and search_messages("third")

    # It can be neither edited nor deleted again while deleted
    bob.emit(
        "update_message",
        {"room_name": "lobby", "message_id": second.id, "content": "vandalised"},
    )
    bob.emit("delete_message", {"room_name": "lobby", "message_id": second.id})
    assert db.session.get(Message, second.id).content == "second"
    assert kinds(room) == ["user", "user", "user", "delete"]
    assert chain.verify(chain.chain_of(room))["ok"]


def test_restore_puts_it_back_in_order_for_everyone(test_app, quiet):
    room, ada, bob, (first, second, third) = three_messages(test_app)
    bob.emit("delete_message", {"room_name": "lobby", "message_id": second.id})
    ada.get_received()
    bob.get_received()

    ada.emit("restore_message", {"room_name": "lobby", "message_id": second.id})

    row = db.session.get(Message, second.id)
    assert row.deleted_at is None and row.deleted_by is None
    assert conversation(room) == ["first", "second", "third"]
    for client in (ada, bob):
        (restored,) = heard(client, "message_restored")
        assert (restored["id"], restored["username"], restored["content"]) == (
            second.id,
            "ada",
            "second",
        )
        # Its id says where it goes: between its neighbours
        assert first.id < restored["id"] < third.id

    # Someone who joins now reads it in place, with no deleted line
    carol = socket_for(test_app, username="carol")
    events = carol.get_received()
    assert [
        e["args"][0]["content"] for e in events if e["name"] == "previous_messages"
    ] == [
        "first",
        "second",
        "third",
    ]
    assert not [e for e in events if e["name"] == "message_deleted"]

    # Restoring what is not deleted does nothing
    ada.emit("restore_message", {"room_name": "lobby", "message_id": second.id})
    assert not heard(ada, "message_restored")
    assert kinds(room).count("restore") == 1


def test_the_chain_says_who_deleted_and_who_restored_and_still_verifies(
    test_app, quiet
):
    room, ada, bob, (first, second, third) = three_messages(test_app)
    bob.emit("delete_message", {"room_name": "lobby", "message_id": second.id})
    key = chain.chain_of(room)
    assert chain.verify(key)["ok"]
    assert second.id not in chain.evidence(room)["messages"]

    ada.emit("restore_message", {"room_name": "lobby", "message_id": second.id})

    assert kinds(room) == ["user", "user", "user", "delete", "restore"]
    deleted, restored = bodies(room)[3:]
    assert (deleted["messageId"], deleted["username"], deleted["by"]) == (
        second.id,
        "ada",
        "bob",
    )
    assert (restored["messageId"], restored["username"], restored["by"]) == (
        second.id,
        "ada",
        "ada",
    )
    assert restored["contentSha256"] == chain.sha256_text("second")
    assert restored["byUserId"] is None  # a guest
    assert chain.verify(key)["ok"]

    # Words changed under a restored message still show
    db.session.get(Message, second.id).content = "doctored"
    db.session.commit()
    report = chain.verify(key)
    assert not report["ok"] and report["content_mismatches"] == [second.id]


def test_a_restored_message_verifies_in_our_downloads(test_app, quiet):
    room, ada, bob, (first, second, third) = three_messages(test_app)
    bob.emit("delete_message", {"room_name": "lobby", "message_id": second.id})
    ada.emit("restore_message", {"room_name": "lobby", "message_id": second.id})

    http = test_app.test_client()
    doc = http.get(
        "/download_chat_history", query_string={"room_name": "lobby"}
    ).get_json()
    root = doc["chain"]["root"]
    assert [m["content"] for m in doc["messages"]] == ["first", "second", "third"]
    for message in doc["messages"]:
        assert chain.verify_message(message["content"], message["chain"], root) == (
            True,
            "verified",
        )
    back = doc["messages"][1]["chain"]
    assert '"type":"restore"' in back["line"].replace(" ", "")
    assert chain.verify_message("vandalised", back, root)[0] is False

    md = http.get(
        "/download_chat_history_md", query_string={"room_name": "lobby"}
    ).get_data(as_text=True)
    assert f"chain event `{back['event']}`" in md


def test_a_message_an_edit_and_resend_replaced_since_is_not_restored(test_app, quiet):
    room, ada, bob, (first, second, third) = three_messages(test_app)
    bob.emit("delete_message", {"room_name": "lobby", "message_id": second.id})
    ada.emit(
        "edit_and_resend",
        {"room_name": "lobby", "message_id": first.id, "content": "first, again"},
    )
    revision = Message.visible(room.id).one()
    # The deleted message went with its branch, & its line leaves the page
    assert db.session.get(Message, second.id).superseded_by_id == revision.id
    assert second.id in heard(bob, "messages_hidden")[0]["ids"]
    # ... but an earlier version never shows a deleted message
    assert [m.content for m in revision.earlier_versions()[0]] == ["first", "third"]

    bob.emit("restore_message", {"room_name": "lobby", "message_id": second.id})

    (refused,) = heard(bob, "restore_refused")
    assert refused["message_id"] == second.id and "Edit & resend" in refused["error"]
    assert db.session.get(Message, second.id).deleted_at is not None
    assert conversation(room) == ["first, again"]
    assert "restore" not in kinds(room)

    # A superseded message is read only: deleting one does nothing
    bob.emit("delete_message", {"room_name": "lobby", "message_id": third.id})
    assert db.session.get(Message, third.id).deleted_at is None


def test_restore_works_while_a_reply_streams(test_app, quiet, monkeypatch):
    """Delete is not refused mid-reply, so neither is its undo: a streaming
    reply read its history when it began."""
    import app as app_module

    room, ada, bob, (first, second, third) = three_messages(test_app)
    bob.emit("delete_message", {"room_name": "lobby", "message_id": second.id})
    monkeypatch.setitem(app_module.streaming_replies, "lobby", 1)

    ada.emit("restore_message", {"room_name": "lobby", "message_id": second.id})

    assert conversation(room) == ["first", "second", "third"]


def test_a_message_from_another_room_or_a_missing_one_is_ignored(test_app, quiet):
    room, ada, bob, (first, second, third) = three_messages(test_app)
    make_room("elsewhere")
    bob.emit("delete_message", {"room_name": "lobby", "message_id": second.id})
    bob.get_received()

    bob.emit("restore_message", {"room_name": "elsewhere", "message_id": second.id})
    bob.emit("restore_message", {"room_name": "lobby", "message_id": 999999})
    bob.emit("restore_message", {"room_name": "nowhere", "message_id": second.id})
    bob.emit("restore_message", {"room_name": "lobby"})
    bob.emit("restore_message", {})

    assert db.session.get(Message, second.id).deleted_at is not None
    assert not heard(bob, "message_restored")


def test_only_its_owner_restores_in_a_solo_room(test_app, quiet):
    import app as app_module

    owner = User(email="owner@example.test", display_name="owner")
    db.session.add(owner)
    db.session.commit()
    room = make_room("secret", is_private=True, owner_id=owner.id)
    mine = Message(username="owner", content="mine", room_id=room.id)
    db.session.add(mine)
    db.session.commit()

    signed_in = test_app.test_client()
    with signed_in.session_transaction() as session:
        session["user_id"] = owner.id
    owner_socket = app_module.socketio.test_client(
        app_module.app, flask_test_client=signed_in
    )
    owner_socket.emit("join", {"room_name": "secret", "username": "owner"})
    owner_socket.emit("delete_message", {"room_name": "secret", "message_id": mine.id})
    assert db.session.get(Message, mine.id).deleted_at is not None

    guest = app_module.socketio.test_client(
        app_module.app, flask_test_client=test_app.test_client()
    )
    guest.emit("join", {"room_name": "secret", "username": "mallory"})
    events = guest.get_received()
    assert not [e for e in events if e["name"] == "message_deleted"]
    guest.emit("restore_message", {"room_name": "secret", "message_id": mine.id})
    assert db.session.get(Message, mine.id).deleted_at is not None

    owner_socket.get_received()
    owner_socket.emit("restore_message", {"room_name": "secret", "message_id": mine.id})
    assert db.session.get(Message, mine.id).deleted_at is None
    (restored,) = heard(owner_socket, "message_restored")
    assert restored["content"] == "mine"
    restore = bodies(room)[-1]
    assert (restore["type"], restore["by"], restore["byUserId"]) == (
        "restore",
        "owner",
        owner.id,
    )
    assert not heard(guest, "message_restored")


def test_someone_joining_later_sees_lines_for_recent_deletes_only(
    test_app, quiet, monkeypatch
):
    room, ada, bob, (first, second, third) = three_messages(test_app)
    bob.emit("delete_message", {"room_name": "lobby", "message_id": first.id})
    bob.emit("delete_message", {"room_name": "lobby", "message_id": second.id})
    # `first` was deleted two days ago
    db.session.get(Message, first.id).deleted_at = int(time.time()) - 48 * 3600
    db.session.commit()

    carol = socket_for(test_app, username="carol")
    events = carol.get_received()
    assert [
        e["args"][0]["content"] for e in events if e["name"] == "previous_messages"
    ] == ["third"]
    assert [e["args"][0] for e in events if e["name"] == "message_deleted"] == [
        {
            "message_id": second.id,
            "username": "ada",
            "deleted_by": "bob",
            "restorable": True,
        }
    ]
    assert "second" not in str(events) and "first" not in str(events)

    # Past its window a delete is hidden for good here (its row is kept)
    carol.emit("restore_message", {"room_name": "lobby", "message_id": first.id})
    (refused,) = heard(carol, "restore_refused")
    assert "too long ago" in refused["error"]
    assert db.session.get(Message, first.id).deleted_at is not None

    # A longer window shows both again
    monkeypatch.setenv("OPENCOMPLETION_DELETED_RETENTION_HOURS", "72")
    dave = socket_for(test_app, username="dave")
    assert [m["message_id"] for m in heard(dave, "message_deleted")] == [
        first.id,
        second.id,
    ]

    # 0: never shown, never offered, still kept
    monkeypatch.setenv("OPENCOMPLETION_DELETED_RETENTION_HOURS", "0")
    erin = socket_for(test_app, username="erin")
    assert not heard(erin, "message_deleted")
    bob.get_received()
    bob.emit("delete_message", {"room_name": "lobby", "message_id": third.id})
    (gone,) = heard(bob, "message_deleted")
    assert gone["restorable"] is False
    bob.emit("restore_message", {"room_name": "lobby", "message_id": third.id})
    assert heard(bob, "restore_refused")
    assert db.session.get(Message, third.id).content == "third"


def test_deleting_a_room_still_deletes_everything(test_app, quiet):
    owner = User(email="owner@example.test", display_name="owner")
    db.session.add(owner)
    db.session.commit()
    room = make_room(owner_id=owner.id)
    ada = socket_for(test_app)
    kept, deleted = say(ada, "kept"), say(ada, "deleted")
    ada.emit("delete_message", {"room_name": "lobby", "message_id": deleted.id})
    ids = [kept.id, deleted.id]

    http = test_app.test_client()
    with http.session_transaction() as session:
        session["user_id"] = owner.id
    assert http.delete(f"/api/rooms/{room.id}/delete").status_code == 200

    db.session.expire_all()
    assert Message.query.filter(Message.id.in_(ids)).count() == 0
