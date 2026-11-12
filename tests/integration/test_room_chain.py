"""A room's history as an append-only, hash-chained, Merkle-rooted log
(chain.py, migration 2026110201). fox, 2026-11-02: a model said a 404 repo
was live; its tool's result is now chained before the reply that cites it,
& a room's chain outlives the room, for forensics."""

import json
import pytest

import chain
import provenance as P
import tools
from models import Message, Room, RoomEvent, db

from tests.integration.test_tools_chat import (  # noqa: F401  the harness
    Model,
    chunk,
    harness,
    search_call,
    tools_on,
)


def kinds(room):
    return [e.type for e in chain.events(chain.chain_of(room))]


def bodies(room):
    return [json.loads(e.line) for e in chain.events(chain.chain_of(room))]


def make_room(name, **fields):
    room = Room(name=name, **fields)
    db.session.add(room)
    db.session.commit()
    return room


def test_a_tool_result_is_chained_before_the_reply_that_cites_it(harness, monkeypatch):
    monkeypatch.setattr(
        tools,
        "run",
        lambda context, name, arguments: {"error": "HTTP 404: no page at this address"},
    )
    harness.model.scripts = [
        search_call("rockrms rock github"),
        [chunk(content="It is live.")],
    ]
    room = make_room("muppets")
    harness.app.chat_gpt(
        "ada", "muppets", model_name="qwen", tool_context=tools.ToolContext(sid="s")
    )

    assert kinds(room) == ["tool", "assistant"]
    tool, reply = bodies(room)
    assert tool["status"] == "error" and "HTTP 404" in tool["error"]
    assert tool["resultSha256"] == chain.sha256_text(
        json.dumps({"error": "HTTP 404: no page at this address"}, ensure_ascii=False)
    )
    assert reply["toolEvents"] == [chain.events(chain.chain_of(room))[0].hash]
    assert (
        reply["contentSha256"] == chain.sha256_text("It is live.")
        and reply["model"] == "qwen"
    )
    assert (
        "It is live." not in chain.events(chain.chain_of(room))[1].line
    )  # digests, never words
    report = chain.verify(chain.chain_of(room))
    assert report["ok"] and report["entries"] == 2


def test_a_run_that_crashed_is_chained_as_an_error(harness, monkeypatch):
    from tools import sandbox

    crashed = sandbox._finished(
        {"status": "completed", "exit_code": 1, "stderr": "NameError: qr"}
    )
    monkeypatch.setattr(tools, "run", lambda context, name, arguments: crashed)
    harness.model.scripts = [search_call("qr"), [chunk(content="Done.")]]
    room = make_room("tacos")
    harness.app.chat_gpt(
        "ada", "tacos", model_name="qwen", tool_context=tools.ToolContext(sid="s")
    )
    tool, _reply = bodies(room)
    assert tool["status"] == "error"
    assert tool["error"] == "exited with code 1: NameError: qr"


def socket_for(test_app, room_name="lobby", username="ada"):
    import app as app_module

    client = app_module.socketio.test_client(
        app_module.app, flask_test_client=test_app.test_client()
    )
    client.emit("join", {"room_name": room_name, "username": username})
    return client


@pytest.fixture
def quiet(monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module.gevent, "spawn", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "SYSTEM_USERS", ["qwen"])
    from routes import DEPS

    monkeypatch.setitem(DEPS, "system_users", ["qwen"])


def test_an_edit_or_delete_names_who_did_it_beside_the_author(test_app, quiet):
    """In a public chatroom anyone may edit or delete any message: the
    chain says who, by the name their socket joined under, never a name
    the event claims."""
    from models import Message

    room = make_room("lobby")
    ada = socket_for(test_app)
    bob = socket_for(test_app, username="bob")
    for words in ("first", "second"):
        ada.emit(
            "chat_message",
            {
                "room_name": "lobby",
                "username": "ada",
                "message": words,
                "model": "None",
            },
        )
    first = Message.query.filter_by(content="first").one()
    second = Message.query.filter_by(content="second").one()
    bob.emit(
        "update_message",
        {
            "room_name": "lobby",
            "message_id": first.id,
            "content": "vandalised",
            "username": "ada",
        },
    )
    bob.emit(
        "delete_message",
        {"room_name": "lobby", "message_id": second.id, "username": "ada"},
    )
    ada.emit("delete_message", {"room_name": "lobby", "message_id": first.id})

    edit, deleted, own = bodies(room)[2:]
    assert (edit["type"], edit["username"], edit["by"]) == ("edit", "ada", "bob")
    assert (deleted["type"], deleted["username"], deleted["by"]) == (
        "delete",
        "ada",
        "bob",
    )
    assert (own["username"], own["by"]) == ("ada", "ada")
    # Guests here: no account to name.
    assert edit["byUserId"] is None and deleted["byUserId"] is None
    assert chain.verify(chain.chain_of(room))["ok"]


def test_messages_edits_deletes_and_resends_are_chained(test_app, quiet):
    room = make_room("lobby")
    ada = socket_for(test_app)
    ada.emit(
        "chat_message",
        {"room_name": "lobby", "username": "ada", "message": "first", "model": "None"},
    )
    first = Message.query.filter_by(content="first").one()
    ada.emit(
        "update_message",
        {"room_name": "lobby", "message_id": first.id, "content": "first, fixed"},
    )
    ada.emit(
        "chat_message",
        {"room_name": "lobby", "username": "ada", "message": "second", "model": "None"},
    )
    second = Message.query.filter_by(content="second").one()
    ada.emit("delete_message", {"room_name": "lobby", "message_id": second.id})
    ada.emit(
        "edit_and_resend",
        {"room_name": "lobby", "message_id": first.id, "content": "again"},
    )

    assert kinds(room) == ["user", "edit", "user", "delete", "user", "supersede"]
    edit, resend = bodies(room)[1], bodies(room)[5]
    assert edit["contentSha256"] == chain.sha256_text("first, fixed")
    assert resend["revisionOf"] == first.id and first.id in resend["supersededIds"]
    assert chain.verify(chain.chain_of(room))["ok"]


def test_a_doctored_line_or_message_is_caught(test_app, quiet):
    room = make_room("lobby")
    ada = socket_for(test_app)
    for words in ("one", "two", "three"):
        ada.emit(
            "chat_message",
            {
                "room_name": "lobby",
                "username": "ada",
                "message": words,
                "model": "None",
            },
        )
    key = chain.chain_of(room)
    chain.checkpoint(room.id)
    assert chain.verify(key)["ok"]

    # Someone edits a message straight in the database: its digest disagrees
    two = Message.query.filter_by(content="two").one()
    two.content = "TWO, rewritten"
    db.session.commit()
    report = chain.verify(key)
    assert not report["ok"] and report["content_mismatches"] == [two.id]

    # Someone edits a chained line: the chain breaks there
    middle = chain.events(key)[1]
    middle.line = middle.line.replace('"seq": 1,', '"seq": 7,')
    assert '"seq": 7,' in middle.line
    db.session.commit()
    report = chain.verify(key)
    assert report["state"] == P.CORRUPTED and report["first_break"] is not None


def test_a_chain_outlives_its_room_and_a_reused_id_starts_afresh(
    test_app, quiet, monkeypatch
):
    import auth
    from models import User

    owner = User(email="ada@example.com", display_name="ada")
    db.session.add(owner)
    db.session.commit()
    monkeypatch.setattr(auth, "get_current_user", lambda: owner)
    room = make_room("gone", owner_id=None)
    room.owner_id = owner.id
    db.session.commit()
    ada = socket_for(test_app, "gone")
    ada.emit(
        "chat_message",
        {
            "room_name": "gone",
            "username": "ada",
            "message": "evidence",
            "model": "None",
        },
    )
    old_key = chain.chain_of(room)
    room_id = room.id

    from routes.rooms import delete_room

    with test_app.test_request_context():
        delete_room(room_id)
    assert db.session.get(Room, room_id) is None
    assert [e.type for e in chain.events(old_key)] == ["user", "room_deleted"]
    report = chain.verify(old_key)
    assert report["ok"] and not report["room_alive"]

    # A new room handed the same id writes its own chain
    reborn = Room(id=room_id, name="reborn")
    db.session.add(reborn)
    db.session.commit()
    chain.append(
        reborn.id, reborn.name, "user", messageId=1, username="bo", contentSha256="x"
    )
    assert chain.chain_of(reborn) != old_key
    assert [e.seq for e in chain.events(chain.chain_of(reborn))] == [0]
    assert old_key in chain.chains() and chain.chain_of(reborn) in chain.chains()


def test_a_fork_starts_its_chain_where_it_came_from(test_app, quiet):
    room = make_room("lobby")
    ada = socket_for(test_app)
    ada.emit(
        "chat_message",
        {"room_name": "lobby", "username": "ada", "message": "hello", "model": "None"},
    )
    reply = Message(username="qwen", content="hi there", room_id=room.id)
    db.session.add(reply)
    db.session.commit()
    head = chain.events(chain.chain_of(room))[-1].hash

    response = test_app.test_client().post(f"/api/messages/{reply.id}/fork")
    assert response.status_code == 200, response.get_data(as_text=True)
    fork = db.session.get(Room, response.get_json()["room"]["id"])
    (genesis,) = bodies(fork)
    assert (
        genesis["type"] == "fork"
        and genesis["fromHead"] == head
        and genesis["prevHash"] is None
    )
    assert {c["contentSha256"] for c in genesis["copied"]} == {
        chain.sha256_text("hello"),
        chain.sha256_text("hi there"),
    }
    assert chain.verify(chain.chain_of(fork))["ok"]


def test_the_chain_and_a_proof_are_served_under_the_rooms_access_rule(test_app, quiet):
    room = make_room("lobby")
    ada = socket_for(test_app)
    for words in ("a", "b", "c"):
        ada.emit(
            "chat_message",
            {
                "room_name": "lobby",
                "username": "ada",
                "message": words,
                "model": "None",
            },
        )
    chain.checkpoint(room.id)
    client = test_app.test_client()

    lines = client.get(f"/api/rooms/{room.id}/chain.jsonl").data.splitlines()
    assert len(lines) == 3 and P.verify_lines(lines)["state"] == P.OPEN
    second = chain.events(chain.chain_of(room))[1].hash
    got = client.get(f"/api/rooms/{room.id}/chain/proof/{second}").get_json()
    proof = P.proof_from_dict(got["proof"])
    assert P.verify_proof(proof) and proof.root.hex() == got["checkpoint"]["root"]
    assert client.get(f"/api/rooms/{room.id}/chain/proof/{'0' * 64}").status_code == 404

    solo = make_room("mine", is_private=True, owner_id=None)
    solo.owner_id = 99
    db.session.commit()
    assert client.get(f"/api/rooms/{solo.id}/chain.jsonl").status_code == 404


def test_an_upload_is_chained_and_lines_can_be_mirrored(
    test_app, quiet, monkeypatch, tmp_path
):
    monkeypatch.setenv("OPENCOMPLETION_CHAIN_MIRROR", str(tmp_path))
    room = make_room("lobby")
    from blob_store import save_attachment

    saved = save_attachment(room.id, b"hello", "note.txt", "text/plain", "ada")
    (event,) = bodies(room)
    assert event["type"] == "attachment" and event["sha256"] == saved.sha256
    (mirrored,) = list(tmp_path.glob("opencompletion-*.jsonl"))
    assert (
        mirrored.read_bytes().rstrip(b"\n")
        == chain.events(chain.chain_of(room))[0].line.encode()
    )
    assert oct(mirrored.stat().st_mode & 0o777) == "0o600"


def test_chaining_never_breaks_what_it_records(test_app, quiet, monkeypatch):
    room = make_room("lobby")
    monkeypatch.setattr(chain.P, "chain_line", lambda *a, **k: 1 / 0)
    ada = socket_for(test_app)
    ada.emit(
        "chat_message",
        {
            "room_name": "lobby",
            "username": "ada",
            "message": "still sent",
            "model": "None",
        },
    )
    assert Message.query.filter_by(content="still sent").count() == 1
    assert RoomEvent.query.count() == 0


def test_downloads_are_merkle_backed_and_check_without_us(test_app, quiet):
    """fox, 2026-11-02: a room's JSON & Markdown downloads carry, per
    message, its chain event & a merkle-v1 proof under the room's root, so
    a reader checks the words weren't changed with the shared provenance
    code alone."""
    room = make_room("lobby")
    ada = socket_for(test_app)
    for words in ("is rock rms on github?", "where is SparkDevNetwork/Rock?"):
        ada.emit(
            "chat_message",
            {
                "room_name": "lobby",
                "username": "ada",
                "message": words,
                "model": "None",
            },
        )
    reply = Message(
        username="qwen", content="github.com/SparkDevNetwork/Rock", room_id=room.id
    )
    db.session.add(reply)
    db.session.commit()
    chain.append(
        room.id,
        "lobby",
        "assistant",
        messageId=reply.id,
        model="qwen",
        contentSha256=chain.sha256_text(reply.content),
        toolEvents=[],
    )
    client = test_app.test_client()

    doc = client.get(
        "/download_chat_history", query_string={"room_name": "lobby"}
    ).get_json()
    assert (
        doc["$schema"] == "opencompletion-transcript/1"
        and doc["room"]["name"] == "lobby"
    )
    root = doc["chain"]["root"]
    assert [m["role"] for m in doc["messages"]] == ["user", "user", "assistant"]
    for message in doc["messages"]:
        assert chain.verify_message(message["content"], message["chain"], root) == (
            True,
            "verified",
        )
    # The same root the room's own chain checkpoint holds
    lines = client.get(doc["chain"]["lines"]).data.splitlines()
    assert P.session_root(json.loads(line)["hash"] for line in lines) == root

    # One changed word, a swapped event, or another root: each is caught
    answer = doc["messages"][2]
    assert (
        chain.verify_message("github.com/rockrms/Rock", answer["chain"], root)[0]
        is False
    )
    assert (
        chain.verify_message(answer["content"], doc["messages"][0]["chain"], root)[0]
        is False
    )
    assert (
        chain.verify_message(answer["content"], answer["chain"], "0" * 64)[0] is False
    )
    # New words with their digest written into the old line: the line no
    # longer hashes to the hash it carries, whatever its proof says
    forged = dict(answer["chain"], contentSha256=chain.sha256_text("forged"))
    forged["line"] = forged["line"].replace(
        answer["chain"]["contentSha256"], forged["contentSha256"]
    )
    assert chain.verify_message("forged", forged, root) == (
        False,
        "the event line does not hash to its event",
    )

    md = client.get(
        "/download_chat_history_md", query_string={"room_name": "lobby"}
    ).get_data(as_text=True)
    assert f"chain event `{answer['chain']['event']}`" in md
    assert f"root `{root}`" in md and "## Verification" in md


def test_an_unchained_message_says_so(test_app, quiet):
    room = make_room("lobby")
    db.session.add(Message(username="ada", content="before chains", room_id=room.id))
    db.session.commit()
    doc = (
        test_app.test_client()
        .get("/download_chat_history", query_string={"room_name": "lobby"})
        .get_json()
    )
    assert doc["chain"] is None and doc["messages"][0]["chain"] is None
    assert chain.verify_message("before chains", None) == (False, "not chained")
