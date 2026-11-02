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
    reply = [m for m in history if m["role"] == "assistant"][0]["content"]
    assert reply.startswith("It is live.\n\n(tools in this reply: ")
    assert "rockrms/Rock → failed: HTTP 404" in reply
    person = [m for m in history if m["role"] == "user"][0]["content"]
    assert person == "is rockrms/Rock up?"  # only a model's replies carry a note


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
