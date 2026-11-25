"""Bare models (app.bare_model): a small model reads the conversation alone,
no tools, no tool guidance & no follow-up asks."""

from types import SimpleNamespace

import pytest

import app as app_module
import fade


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.delenv("OPENCOMPLETION_BARE_MODELS", raising=False)
    monkeypatch.delenv("OPENCOMPLETION_BARE_CONTEXT", raising=False)
    learned = {"andrea-v10-460m-4k": (4096, 3.0), "qwen": (65536, 3.0)}
    monkeypatch.setattr(app_module, "CONTEXT_WINDOWS", learned)
    return learned


def test_a_small_window_is_bare_a_large_one_is_not(windows):
    assert app_module.bare_model("andrea-v10-460m-4k")
    assert not app_module.bare_model("qwen")
    assert not app_module.bare_model("unknown")  # window not learned yet
    assert not app_module.bare_model(None)


def test_bare_by_name_and_by_size_turned_off(windows, monkeypatch):
    monkeypatch.setenv("OPENCOMPLETION_BARE_MODELS", "^unknown")
    assert app_module.bare_model("unknown")
    monkeypatch.setenv("OPENCOMPLETION_BARE_CONTEXT", "0")
    assert not app_module.bare_model("andrea-v10-460m-4k")
    monkeypatch.setenv("OPENCOMPLETION_BARE_MODELS", "(")  # not a regex
    assert not app_module.bare_model("unknown")


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
