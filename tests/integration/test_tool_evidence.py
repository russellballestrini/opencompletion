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
    roles = [m["role"] for m in history]
    # its tools as tool turns (one call each, then each result), then its words
    assert roles == ["user", "assistant", "tool", "tool", "assistant"]
    calls = history[1]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["web_fetch", "web_search"]
    assert [m["tool_call_id"] for m in history[2:4]] == [c["id"] for c in calls]
    assert json.loads(history[2]["content"]) == {
        "status": "failed",
        "error": "HTTP 404: no page at this address",
    }
    assert json.loads(history[3]["content"]) == {"status": "ok", "sources": 2}
    # the reply's own words carry no note a model could learn to copy
    assert history[4]["content"] == "It is live."
    assert history[0]["content"] == "is rockrms/Rock up?"  # a person's: no tools


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


def test_offered_is_no_call():
    # a delegated run's "offered" entry lists the tools it was given; nothing
    # called it, so neither a model's history nor a transcript shows it
    from tools.loop import trail_turns

    trail = [{"tool": "offered", "label": "web_fetch, sandbox_shell", "status": "done"}]
    assert trail_turns(trail, 1) == [] and trail_lines(trail) == []


def test_backfill_takes_copied_notes_out_and_the_chain_still_holds(test_app):
    import app as app_module
    import chain
    from models import Message, Room, db

    room = Room(name="mining")
    db.session.add(room)
    db.session.commit()
    words = "Net: ~$55/month.\n\n(tools in this reply: offered → ok)"
    reply = Message(username="qwen", content=words, room_id=room.id)
    person = Message(username="ada", content="how much?", room_id=room.id)
    db.session.add_all([reply, person])
    db.session.commit()
    for msg, kind in ((person, "user"), (reply, "assistant")):
        chain.append(
            room.id,
            room.name,
            kind,
            messageId=msg.id,
            username=msg.username,
            contentSha256=chain.sha256_text(msg.content),
        )
    key = chain.chain_of(room)
    assert chain.verify(key)["ok"]

    assert app_module.backfill_tool_notes() == 1
    db.session.expire_all()  # the backfill wrote in its own app context
    assert db.session.get(Message, reply.id).content == "Net: ~$55/month."
    assert db.session.get(Message, person.id).content == "how much?"
    last = json.loads(chain.events(key)[-1].line)
    assert last["type"] == "edit" and last["messageId"] == reply.id
    assert chain.verify(key)["ok"]  # chained as an edit: no content mismatch
    assert app_module.backfill_tool_notes() == 0  # nothing left to clean
