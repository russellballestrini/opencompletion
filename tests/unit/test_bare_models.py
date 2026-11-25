"""Bare models (app.bare_model): a small model reads the conversation alone,
no tools, no tool guidance & no follow-up asks."""

from types import SimpleNamespace

import pytest

import app as app_module
import fade


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.delenv("OPENCOMPLETION_BARE_CONTEXT", raising=False)
    learned = {"andrea-v10-460m-4k": (4096, 3.0), "qwen": (65536, 3.0)}
    monkeypatch.setattr(app_module, "CONTEXT_WINDOWS", learned)
    return learned


def test_a_small_window_is_bare_a_large_one_is_not(windows):
    assert app_module.bare_model("andrea-v10-460m-4k")
    assert not app_module.bare_model("qwen")
    assert not app_module.bare_model("unknown")  # window not learned yet
    assert not app_module.bare_model(None)


def test_bare_by_size_turned_off(windows, monkeypatch):
    monkeypatch.setenv("OPENCOMPLETION_BARE_CONTEXT", "0")
    assert not app_module.bare_model("andrea-v10-460m-4k")


def test_a_bare_reply_offers_no_tools_and_no_guidance(windows, monkeypatch):
    monkeypatch.setattr(
        app_module.tools, "schemas", lambda context: [{"name": "web_search"}]
    )
    history = [{"role": "user", "content": "hello i am looking for food"}]

    def prepared(model):
        reply = app_module.ReplyStream(
            "ada", "lobby", model, 1, (1, "", []), None, False, None, list(history)
        )
        reply.prepare(None, None)
        return reply

    bare = prepared("andrea-v10-460m-4k")
    assert bare.bare and not bare.tool_schemas and bare.tool_calls is None
    assert "tools" not in bare.create_kwargs
    assert bare.chat_history == history  # no system prompt added

    full = prepared("qwen")
    assert not full.bare and full.create_kwargs["tools"]


def test_a_4k_window_keeps_half_for_the_conversation(windows):
    sent = []
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **k: sent.append(k) or "stream")
        )
    )
    messages = [
        {"role": "user", "content": "hello i am looking for food"},
        {"role": "assistant", "content": "What kind of food?"},
        {"role": "user", "content": "noodles"},
    ]
    kwargs = {
        "model": "andrea-v10-460m-4k",
        "messages": messages,
        "max_tokens": 24576,
    }
    assert app_module.open_completion(client, kwargs) == "stream"
    asked = sent[-1]
    assert asked["messages"] == messages  # 8k answer room no longer fades it all
    prompt = fade.tokens(asked["messages"], None, 3.0)
    assert prompt + asked["max_tokens"] <= 4096


def test_asking_bare_offers_nothing_whoever_asks(windows, monkeypatch):
    from tools import ToolContext, offered

    monkeypatch.setenv("OPENCOMPLETION_WEB_SEARCH", "on")
    asker = ToolContext(sid="s1", email="ada@example.com")
    assert offered(asker)  # a browser search at least
    assert offered(ToolContext(sid="s1", email="ada@example.com", bare=True)) == {}


def test_chat_s_bare_toggle_makes_a_large_model_reply_bare(windows):
    from tools import ToolContext

    history = [{"role": "user", "content": "hi"}]
    reply = app_module.ReplyStream(
        "ada", "lobby", "qwen", 1, (1, "", []), None, False, None, list(history)
    )
    reply.prepare(ToolContext(sid="s1", bare=True), None)
    assert reply.bare and not reply.tool_schemas
    assert reply.chat_history == history


def test_a_socket_message_carries_bare_into_its_tool_context(test_app, monkeypatch):
    import tools

    seen = []
    real = tools.context
    monkeypatch.setattr(
        tools, "context", lambda *a, **k: seen.append(k.get("bare")) or real(*a, **k)
    )
    monkeypatch.setattr(app_module, "dispatch_message", lambda *a, **k: None)
    client = app_module.socketio.test_client(test_app)
    client.emit("join", {"username": "ada", "room_name": "bare-room"})
    for bare in (True, "yes", None):
        client.emit(
            "chat_message",
            {
                "username": "ada",
                "message": "hello",
                "model": "qwen",
                "room_name": "bare-room",
                "bare": bare,
            },
        )
    assert seen == [True, False, False]  # only a real true asks bare
