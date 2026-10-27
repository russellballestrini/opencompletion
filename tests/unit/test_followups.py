"""Follow-up suggestions: after a reply, the model that answered offers a
few next questions, sent to the room as chips under its reply."""

from types import SimpleNamespace

import app as app_module


def test_suggestions_are_cleaned_and_capped():
    text = '1. What about Elmo?\n- "Who is Grover?"\n\n* What about Elmo?\n4) More?\n5) Extra'
    assert app_module.parse_followups(text) == [
        "What about Elmo?",
        "Who is Grover?",
        "More?",
    ]
    assert app_module.parse_followups("x" * 500) == []
    assert app_module.parse_followups(None) == []


def test_suggestions_go_to_the_room(monkeypatch):
    sent = []
    asked = {}

    def create(**kwargs):
        asked.update(kwargs)
        message = SimpleNamespace(content="<think>hmm</think>Who is Oscar?\nWhy green?")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(
        app_module.socketio, "emit", lambda *a, **k: sent.append((a, k))
    )
    app_module.suggest_followups(
        client, "qwen", "lobby", 7, "list muppets", "Elmo, Oscar"
    )

    assert sent == [
        (
            (
                "followup_suggestions",
                {"message_id": 7, "suggestions": ["Who is Oscar?", "Why green?"]},
            ),
            {"room": "lobby"},
        )
    ]
    assert asked["model"] == "qwen" and asked["max_tokens"] == 120
    assert "Elmo, Oscar" in asked["messages"][1]["content"]


def test_a_failing_model_suggests_nothing(monkeypatch):
    sent = []

    def create(**kwargs):
        raise RuntimeError("down")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(app_module.socketio, "emit", lambda *a, **k: sent.append(a))
    app_module.suggest_followups(client, "qwen", "lobby", 7, "q", "a")
    assert sent == []


def test_followups_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("OPENCOMPLETION_FOLLOWUPS", "off")
    assert not app_module.followups_enabled()
    monkeypatch.setenv("OPENCOMPLETION_FOLLOWUPS", "on")
    assert app_module.followups_enabled()
