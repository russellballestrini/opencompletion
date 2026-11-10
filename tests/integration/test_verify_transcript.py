"""Verify a transcript (/verify): static/js/verify.js is chain.verify_message
ported to a browser. Proved here against the real thing: a room built
through our app, its real JSON download, our JavaScript run under node on
it, & each kind of tampering caught with the reason the Python gives."""

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import chain
from models import Message, Room, db

ROOT = Path(__file__).resolve().parents[2]
VERIFIER = ROOT / "static" / "js" / "verify.js"
pytestmark = pytest.mark.integration

# Accents, CJK, an emoji past the BMP (a surrogate pair in JavaScript), a
# joined emoji, a combining mark, quotes, backslashes & line endings: where
# a UTF-8 or JSON port goes wrong first.
WORDS = [
    "café déjà vu, naïve façade, Ærøskøbing",
    "日本語のテキスト 🦊 & 👩🏽‍🚀 é  ",
    'She said "hi" \\ back\\slash\ttab\r\nnext line\n\n  ',
    "plain words",
]
REPLY = "Voilà: 2 × 3 = 6 ✅ 𝛑 ≈ 3.14159"

# Reads {name: transcript text} & prints {name: report or {"error"}}.
RUNNER = """
const fs = require('fs');
const verify = require(process.argv[2]);
(async () => {
    const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
    const out = {};
    for (const [name, text] of Object.entries(cases)) {
        try {
            const report = await verify.verifyTranscriptText(text);
            report.says = verify.verdictText(report);
            out[name] = report;
        } catch (error) {
            out[name] = {error: error.message};
        }
    }
    process.stdout.write(JSON.stringify(out));
})();
"""


def require_node():
    node = shutil.which("node")
    if not node:
        if os.environ.get("GITHUB_ACTIONS"):
            pytest.fail("node is required to run static/js/verify.js")
        pytest.skip("node is required to run static/js/verify.js")
    return node


def run_verifier(tmp_path, cases):
    """Our JavaScript's report for each {name: transcript text}."""
    node = require_node()
    runner = tmp_path / "run_verify.js"
    runner.write_text(RUNNER)
    given = tmp_path / "cases.json"
    given.write_text(json.dumps(cases), encoding="utf-8")
    result = subprocess.run(
        [node, str(runner), str(VERIFIER), str(given)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def states(report):
    return [(r["state"], r["why"]) for r in report["results"]]


@pytest.fixture
def quiet(monkeypatch):
    import app as app_module
    from routes import DEPS

    monkeypatch.setattr(app_module.gevent, "spawn", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "SYSTEM_USERS", ["qwen"])
    monkeypatch.setitem(DEPS, "system_users", ["qwen"])


def say(test_app, room_name, words, username="ada"):
    import app as app_module

    socket = app_module.socketio.test_client(
        app_module.app, flask_test_client=test_app.test_client()
    )
    socket.emit("join", {"room_name": room_name, "username": username})
    for message in words:
        socket.emit(
            "chat_message",
            {
                "room_name": room_name,
                "username": username,
                "message": message,
                "model": "None",
            },
        )
    return socket


def reply(room, words):
    message = Message(username="qwen", content=words, room_id=room.id)
    db.session.add(message)
    db.session.commit()
    chain.append(
        room.id,
        room.name,
        "assistant",
        messageId=message.id,
        model="qwen",
        contentSha256=chain.sha256_text(words),
        toolEvents=[],
    )
    return message


def download(test_app, room_name):
    """A room's JSON download, exactly the bytes a browser saves."""
    response = test_app.test_client().get(
        "/download_chat_history", query_string={"room_name": room_name}
    )
    assert response.status_code == 200
    return response.get_data(as_text=True)


@pytest.fixture
def transcript(test_app, quiet):
    """A real room: people's messages (one edited in place), a model's
    reply, an odd number of events so the tree duplicates a node."""
    room = Room(name="lobby")
    db.session.add(room)
    db.session.commit()
    socket = say(test_app, "lobby", WORDS)
    edited = Message.query.filter_by(content="plain words").one()
    socket.emit(
        "update_message",
        {"room_name": "lobby", "message_id": edited.id, "content": "plain wörds ✏️"},
    )
    reply(room, REPLY)
    return download(test_app, "lobby")


def test_our_javascript_verifies_a_real_download(transcript, tmp_path):
    doc = json.loads(transcript)
    root = doc["chain"]["root"]
    assert len(doc["messages"]) == 5 and doc["chain"]["uptoSeq"] == 5
    # Python, our ground truth, agrees first
    for message in doc["messages"]:
        assert chain.verify_message(message["content"], message["chain"], root) == (
            True,
            "verified",
        )
    report = run_verifier(tmp_path, {"real": transcript})["real"]
    assert report["verdict"] == "verified"
    assert (report["total"], report["verified"], report["failed"]) == (5, 5, 0)
    assert states(report) == [("verified", "verified")] * 5
    assert report["root"] == root and report["chain"] == doc["chain"]["chain"]
    assert report["says"].startswith("Every message verified: 5 messages")


def test_every_kind_of_tampering_is_caught_with_its_reason(transcript, tmp_path):
    doc = json.loads(transcript)
    root = doc["chain"]["root"]

    def changed(change):
        other = copy.deepcopy(doc)
        change(other)
        return other

    def words(d):  # (a) one message's words, one accent
        d["messages"][0]["content"] = d["messages"][0]["content"].replace("é", "e", 1)

    def step(d):  # (b) one proof step
        sibling = d["messages"][1]["chain"]["proof"]["siblings"][0]
        sibling["hash"] = sibling["hash"][:-1] + (
            "0" if sibling["hash"][-1] != "0" else "1"
        )

    def side(d):  # (b) a proof step on its other side
        sibling = d["messages"][1]["chain"]["proof"]["siblings"][0]
        sibling["is_left"] = not sibling["is_left"]

    def top(d):  # (c) the root
        d["chain"]["root"] = "0" * 64

    def swapped(d):  # another message's chain block, whole
        d["messages"][4]["chain"] = d["messages"][0]["chain"]

    def relabelled(d):  # another message's event, under this one's id
        block = dict(d["messages"][0]["chain"])
        block["messageId"] = d["messages"][4]["chain"]["messageId"]
        d["messages"][4]["chain"] = block

    def rewritten(d):  # new words, their digest written into the old line
        message = d["messages"][4]
        old = message["chain"]["contentSha256"]
        message["content"] = "It is live."
        new = chain.sha256_text("It is live.")
        message["chain"]["contentSha256"] = new
        message["chain"]["line"] = message["chain"]["line"].replace(old, new)

    def leaf(d):  # a valid proof, of another event
        d["messages"][2]["chain"]["proof"] = d["messages"][3]["chain"]["proof"]

    def junk(d):
        d["messages"][2]["chain"]["proof"]["root"] = "not hex"

    cases = {
        "words": words,
        "step": step,
        "side": side,
        "top": top,
        "swapped": swapped,
        "relabelled": relabelled,
        "rewritten": rewritten,
        "leaf": leaf,
        "junk": junk,
    }
    docs = {name: changed(change) for name, change in cases.items()}
    reports = run_verifier(
        tmp_path,
        {name: json.dumps(d, ensure_ascii=False) for name, d in docs.items()},
    )
    expected = {
        "words": (0, "the words do not match the digest the chain recorded"),
        "step": (1, "the proof does not lead to its root"),
        "side": (1, "the proof does not lead to its root"),
        "swapped": (4, "the words do not match the digest the chain recorded"),
        "relabelled": (4, "the event line is about another message"),
        "rewritten": (4, "the event line does not hash to its event"),
        "leaf": (2, "the proof is for another event"),
        "junk": (2, "the chain block is malformed"),
    }
    for name, (index, why) in expected.items():
        report = reports[name]
        assert report["verdict"] == "failed" and report["failed"] == 1, name
        assert report["results"][index]["state"] == "failed", name
        assert report["results"][index]["why"] == why, name
        assert report["says"].startswith("1 message of 5 failed"), name
        # Where the Python decides too, it says the same
        if name != "junk":
            message = docs[name]["messages"][index]
            assert chain.verify_message(message["content"], message["chain"], root) == (
                False,
                why,
            ), name
    # Another root: no message leads to it
    assert reports["top"]["verdict"] == "failed" and reports["top"]["failed"] == 5
    assert set(states(reports["top"])) == {
        ("failed", "the proof leads to another root")
    }
    message = doc["messages"][0]
    assert chain.verify_message(message["content"], message["chain"], "0" * 64) == (
        False,
        "the proof leads to another root",
    )


def test_a_forks_copies_verify_under_its_fork_event(test_app, quiet, tmp_path):
    """A forked room's first event records every copied message; each
    copy is checked against its own digest inside that one line."""
    from routes import rooms

    rooms.FORK_THROTTLE.reset()
    room = Room(name="source")
    db.session.add(room)
    db.session.commit()
    say(test_app, "source", WORDS[:2])
    reply(room, REPLY)
    client = test_app.test_client()
    response = client.post(f"/api/rooms/{room.id}/fork", json={})
    rooms.FORK_THROTTLE.reset()
    assert response.status_code == 200, response.json
    text = download(test_app, response.json["room"]["name"])
    doc = json.loads(text)
    kinds = {json.loads(m["chain"]["line"])["type"] for m in doc["messages"]}
    assert kinds == {"fork"} and len(doc["messages"]) == 3

    tampered = copy.deepcopy(doc)
    tampered["messages"][1]["content"] += "!"
    reports = run_verifier(
        tmp_path, {"fork": text, "tampered": json.dumps(tampered, ensure_ascii=False)}
    )
    assert reports["fork"]["verdict"] == "verified" and reports["fork"]["verified"] == 3
    assert states(reports["tampered"]) == [
        ("verified", "verified"),
        ("failed", "the words do not match the digest the chain recorded"),
        ("verified", "verified"),
    ]


def test_a_transcript_without_proofs_says_so(test_app, quiet, tmp_path, transcript):
    old = Room(name="old")
    db.session.add(old)
    db.session.commit()
    db.session.add(Message(username="ada", content="before chains", room_id=old.id))
    db.session.commit()
    unproven = download(test_app, "old")
    assert json.loads(unproven)["chain"] is None
    # Some messages chained, one not: nothing failed, & it is not "every"
    partial = json.loads(transcript)
    partial["messages"][1]["chain"] = None
    reports = run_verifier(
        tmp_path, {"unproven": unproven, "partial": json.dumps(partial)}
    )
    assert reports["unproven"]["verdict"] == "unproven"
    assert states(reports["unproven"]) == [("unchained", "not chained")]
    assert "carries no proofs" in reports["unproven"]["says"]
    assert reports["partial"]["verdict"] == "partial"
    assert (reports["partial"]["verified"], reports["partial"]["unchained"]) == (4, 1)


def test_junk_is_named_for_what_it_is(test_app, quiet, tmp_path, transcript):
    markdown = (
        test_app.test_client()
        .get("/download_chat_history_md", query_string={"room_name": "lobby"})
        .get_data(as_text=True)
    )
    reports = run_verifier(
        tmp_path,
        {
            "markdown": markdown,
            "text": "hello there",
            "empty": "  \n",
            "list": "[1, 2]",
            "other": json.dumps({"$schema": "something-else/2", "messages": []}),
            "none": json.dumps({"messages": []}),
            "shape": json.dumps({"$schema": "opencompletion-transcript/1"}),
        },
    )
    assert "Markdown transcript" in reports["markdown"]["error"]
    assert "Download the JSON one" in reports["markdown"]["error"]
    assert "not JSON" in reports["text"]["error"]
    assert "empty" in reports["empty"]["error"]
    assert "not a transcript" in reports["list"]["error"]
    assert '"something-else/2"' in reports["other"]["error"]
    assert "no $schema" in reports["none"]["error"]
    assert "lists no messages" in reports["shape"]["error"]
