"""What a reply's tools did stays with the reply: in the history a model
reads (so it can't stand by "I fetched it" over the 404 it got) & in the
transcripts people download (fox, 2026-11-02: a model insisted a 404 repo
existed; the export showed only its claims)."""

import json

import pytest

from tools import web_fetch
from tools.loop import trail_lines

TRAIL = json.dumps(
    [
        {
            "tool": "web_fetch",
            "label": "https://github.com/rockrms/Rock",
            "status": "error",
            "error": "HTTP 404: no page at this address",
        },
        {
            "tool": "web_search",
            "label": "rock rms github",
            "status": "done",
            "sources": [{"url": "a"}, {"url": "b"}],
        },
        "not a call",
    ]
)


def test_trail_lines_say_what_each_call_did():
    assert trail_lines(TRAIL) == [
        "web_fetch https://github.com/rockrms/Rock → failed: HTTP 404: no page at this address",
        "web_search rock rms github → ok, 2 sources",
    ]
    assert trail_lines(None) == [] and trail_lines("{not json") == []
    # a delegated run's "offered" (the tools it was given) is no call
    assert trail_lines([{"tool": "offered", "status": "done"}]) == []


@pytest.fixture
def room(test_app, monkeypatch):
    import app as app_module
    from models import Message, Room, db

    monkeypatch.setattr(app_module, "SYSTEM_USERS", ["qwen"])
    room = Room(name="lobby")
    db.session.add(room)
    db.session.commit()
    for name, words in (("ada", "is rockrms/Rock up?"), ("qwen", "It is live.")):
        message = Message(username=name, content=words, room_id=room.id)
        message.tool_trail = TRAIL
        db.session.add(message)
    db.session.commit()
    return room


def test_a_models_history_keeps_its_past_tool_results(room):
    import app as app_module

    _, history = app_module.reply_history("lobby", "qwen")
    # the reply as it happened: its calls (real argument names, rebuilt
    # from the trail), their results, then its words
    roles = [m["role"] for m in history]
    assert roles == ["user", "assistant", "tool", "tool", "assistant"]
    calls = history[1]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["web_fetch", "web_search"]
    assert json.loads(calls[0]["function"]["arguments"]) == {
        "url": "https://github.com/rockrms/Rock"
    }
    assert json.loads(calls[1]["function"]["arguments"]) == {"query": "rock rms github"}
    assert [m["tool_call_id"] for m in history[2:4]] == [c["id"] for c in calls]
    assert json.loads(history[2]["content"]) == {
        "error": "HTTP 404: no page at this address"
    }
    assert json.loads(history[3]["content"]) == {"sources": ["a", "b"]}
    assert history[4] == {"role": "assistant", "content": "It is live."}
    assert history[0]["content"] == "is rockrms/Rock up?"


def test_a_reply_goes_back_split_where_its_calls_happened():
    # words, the calls made at that point, their results, the words after:
    # its words alone read as narrating work it never called (fox, 2026-11-04)
    from tools.loop import reply_turns

    words = (
        "Let me write it. Written; sharing. Here it is: ![c.png](/attachments/x/c.png)"
    )
    trail = [
        {"tool": "offered", "names": ["sandbox_shell"], "status": "done", "at": 0},
        {
            "tool": "sandbox_write_file",
            "label": "/root/work/c.py",
            "status": "done",
            "at": 16,
            "detail": {"language": "py", "input": "print(1)"},
        },
        {
            "tool": "sandbox_share_file",
            "label": "/root/work/c.png",
            "status": "done",
            "at": 34,
            "sources": [{"url": "/attachments/x/c.png"}],
        },
    ]
    turns = reply_turns(words, trail, 9)
    roles = [t["role"] for t in turns]
    assert roles == ["assistant", "tool", "assistant", "tool", "assistant"]
    assert turns[0]["content"] == "Let me write it."
    write = json.loads(turns[0]["tool_calls"][0]["function"]["arguments"])
    assert write == {"path": "/root/work/c.py", "content": "print(1)"}
    assert turns[2]["content"] == " Written; sharing."
    assert json.loads(turns[3]["content"]) == {
        "markdown": "![c.png](/attachments/x/c.png)"
    }
    assert turns[4]["content"].startswith(" Here it is")
    assert reply_turns("Just words.", None, 1) == [
        {"role": "assistant", "content": "Just words."}
    ]


def test_notes_a_model_copied_are_left_out_of_its_history():
    import app as app_module

    echoed = (
        "Net profit: ~$55/month.\n\n(tools in this reply: none)\n\n"
        "(tools in this reply: offered → ok)"
    )
    assert app_module.without_echoed_notes(echoed) == "Net profit: ~$55/month."
    # words that only mention it mid-line stay
    keep = "I wrote (tools in this reply: x) inline."
    assert app_module.without_echoed_notes(keep) == keep


def test_transcripts_carry_each_replys_tools(room, test_app):
    client = test_app.test_client()
    md = client.get(
        "/download_chat_history_md", query_string={"room_name": "lobby"}
    ).get_data(as_text=True)
    assert (
        "It is live.\n\n**Tools:**\n- web_fetch https://github.com/rockrms/Rock → failed: HTTP 404"
        in md
    )
    data = client.get(
        "/download_chat_history", query_string={"room_name": "lobby"}
    ).get_json()
    reply = [m for m in data["messages"] if m["content"] == "It is live."][0]
    assert reply["tools"][0] == {
        "tool": "web_fetch",
        "label": "https://github.com/rockrms/Rock",
        "status": "error",
        "error": "HTTP 404: no page at this address",
    }
    assert reply["tools"][1]["sources"] == [{"url": "a"}, {"url": "b"}]


def test_a_dead_link_is_said_plainly(monkeypatch):
    async def fake(url):
        web_fetch.crawler.LAST_FETCH_ERROR = {
            "type": "http_error",
            "details": "HTTP 404",
            "status_code": 404,
        }
        return None

    monkeypatch.setattr(web_fetch, "_fetch", fake)
    error = web_fetch.fetch_page("https://github.com/rockrms/Rock")["error"]
    assert error.startswith("HTTP 404: no page at this address")
    assert "never describe it as if you read it" in error


def test_a_transcript_carries_what_each_run_did_bounded(test_app, monkeypatch):
    """2026-11-12: a deleted room's export was the only copy left, & it
    held no code, output or exit code for any of its runs."""
    import app as app_module
    from models import Message, Room, db
    from tools.loop import DETAIL_CODE_CHARS, DETAIL_OUTPUT_CHARS

    monkeypatch.setattr(app_module, "SYSTEM_USERS", ["qwen"])
    run = {
        "tool": "run_code",
        "label": "python",
        "status": "error",
        "error": "exited with code 1: NameError: name 'qr' is not defined",
        "asker": "ada",
        "result_sha256": "x",
        "detail": {
            "language": "python",
            "input": "import qrcode\n" + "#" * (DETAIL_CODE_CHARS + 50),
            "stdout": "s" * (DETAIL_OUTPUT_CHARS + 50) + "END",
            "stderr": "Traceback ...\nNameError: name 'qr' is not defined",
            "exit_code": 1,
            "log": "a delegate's log stays out",
        },
    }
    for name, private in (("tacos", False), ("secret", True)):
        room = Room(name=name, is_private=private)
        db.session.add(room)
        db.session.commit()
        reply = Message(username="qwen", content="Here is your QR.", room_id=room.id)
        reply.tool_trail = json.dumps([run])
        db.session.add(reply)
    db.session.commit()

    client = test_app.test_client()
    data = client.get(
        "/download_chat_history", query_string={"room_name": "tacos"}
    ).get_json()
    (tool,) = data["messages"][0]["tools"]
    assert tool["status"] == "error" and "asker" not in tool
    detail = tool["detail"]
    assert set(detail) == {"language", "input", "stdout", "stderr", "exit_code"}
    assert detail["exit_code"] == 1 and detail["language"] == "python"
    assert detail["input"].startswith("import qrcode")
    assert len(detail["input"]) == DETAIL_CODE_CHARS
    assert len(detail["stdout"]) == DETAIL_OUTPUT_CHARS
    assert detail["stdout"].endswith("END")
    assert detail["stderr"].endswith("NameError: name 'qr' is not defined")
    # The room's access rule still holds: a private room is not found.
    hidden = client.get("/download_chat_history", query_string={"room_name": "secret"})
    assert hidden.status_code == 404


def test_a_transcript_says_a_run_a_restart_cut_off_was_interrupted(
    test_app, monkeypatch
):
    """2026-11-12: a deploy stopped a reply mid sandbox_share_file. Its
    page read "interrupted" (trail_as_of_now) while both downloads still
    said "running", the raw row, with no way to tell it would never end."""
    import app as app_module
    from models import Message, Room, db

    monkeypatch.setattr(app_module, "SYSTEM_USERS", ["qwen"])
    room = Room(name="cut-off")
    db.session.add(room)
    db.session.commit()
    reply = Message(username="qwen", content="", room_id=room.id)
    reply.tool_trail = json.dumps(
        [
            {"tool": "run_code", "label": "python", "status": "done"},
            {"tool": "sandbox_share_file", "label": "/tmp/qr.png", "status": "running"},
        ]
    )
    db.session.add(reply)
    db.session.commit()

    client = test_app.test_client()
    data = client.get(
        "/download_chat_history", query_string={"room_name": "cut-off"}
    ).get_json()
    done, cut = data["messages"][0]["tools"]
    assert done["status"] == "done"
    assert cut["status"] == "error"
    assert cut["error"] == "interrupted: the server restarted while it ran"
    md = client.get(
        "/download_chat_history_md", query_string={"room_name": "cut-off"}
    ).get_data(as_text=True)
    assert "sandbox_share_file /tmp/qr.png → failed: interrupted" in md

    # A reply streaming in its room right now is still running.
    monkeypatch.setitem(app_module.streaming_replies, "cut-off", object())
    data = client.get(
        "/download_chat_history", query_string={"room_name": "cut-off"}
    ).get_json()
    assert data["messages"][0]["tools"][1]["status"] == "running"
