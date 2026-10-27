"""RepetitionGuard catches a model stuck repeating itself mid-stream, and
chat_gpt hangs up on it; chat_sampling never decodes greedily."""

import itertools
from types import SimpleNamespace

import pytest

from repetition_guard import RepetitionGuard, repeat_limit


def feed(guard, text, size=7):
    """Stream `text` in small deltas, as a model does; True once looping."""
    return any(guard.add(text[i : i + size]) for i in range(0, len(text), size))


def test_a_line_that_keeps_coming_back_is_a_loop():
    guard = RepetitionGuard(limit=8)
    thinking = "Let me list them.\n- Big Bird\n- Elmo\n" + "- Mr. Snuffleupagus?\n" * 8
    assert feed(guard, thinking)
    assert guard.looping == "- Mr. Snuffleupagus?"


def test_one_endless_line_keeps_little_and_still_counts_repeats():
    guard = RepetitionGuard(limit=8)
    assert not feed(guard, "".join(f"{n} " for n in range(50_000)), size=50)
    assert len(guard._partial) <= 1000
    long_line = "Elmo " * 400 + "\n"  # past our 1000-char cut
    guard = RepetitionGuard(limit=8)
    assert feed(guard, long_line * 8)


def test_a_cycle_of_lines_is_a_loop():
    guard = RepetitionGuard(limit=8)
    assert feed(guard, "- Oscar?\n- Grover?\n" * 8)


def test_a_phrase_repeating_without_newlines_is_a_loop():
    guard = RepetitionGuard(limit=8)
    assert feed(guard, "Elmo loves you! " * 40)
    # The loop is reported from wherever its unit lines up: any rotation.
    assert guard.looping in "Elmo loves you! " * 3


def test_ordinary_answers_code_and_tables_are_left_alone():
    guard = RepetitionGuard(limit=8)
    muppets = "\n".join(f"- Muppet number {n}: a friendly monster" for n in range(60))
    code = "def f():\n    if x:\n        return 1\n    }\n" + "    }\n" * 20
    table = "| name | role |\n" + "|---|---|\n" * 20
    assert not feed(guard, muppets + "\n" + code + table)
    assert guard.looping is None


def test_zero_turns_the_guard_off(monkeypatch):
    monkeypatch.setenv("OPENCOMPLETION_REPEAT_LIMIT", "0")
    assert repeat_limit() == 0
    assert not feed(RepetitionGuard(), "- Mr. Snuffleupagus?\n" * 50)
    monkeypatch.setenv("OPENCOMPLETION_REPEAT_LIMIT", "junk")
    assert repeat_limit() == 8


def test_chat_never_decodes_greedily(monkeypatch):
    import app as app_module

    for name in (
        "OPENCOMPLETION_TEMPERATURE",
        "OPENCOMPLETION_TOP_P",
        "OPENCOMPLETION_PRESENCE_PENALTY",
        "OPENCOMPLETION_MAX_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)
    sampling = app_module.chat_sampling("turboderp/Qwen3.8-27B-exl3")
    # max_tokens bounds a loop even where hanging up can't cancel it.
    assert sampling == {"temperature": 0.6, "top_p": 0.95, "max_tokens": 8192}
    monkeypatch.setenv("OPENCOMPLETION_PRESENCE_PENALTY", "1.5")
    monkeypatch.setenv("OPENCOMPLETION_MAX_TOKENS", "8192")
    sampling = app_module.chat_sampling("hermes")
    assert sampling["presence_penalty"] == 1.5 and sampling["max_tokens"] == 8192
    monkeypatch.setenv("OPENCOMPLETION_MAX_TOKENS", "0")
    assert "max_tokens" not in app_module.chat_sampling("hermes")
    assert app_module.chat_sampling("o1-mini") == {"temperature": 1}
    assert app_module.chat_sampling("o3-mini") == {}


class LoopingStream:
    """A model stuck in its thinking: yields the same line forever."""

    def __init__(self):
        self.closed = False
        self.sent = 0

    def __iter__(self):
        for _ in itertools.count():
            self.sent += 1
            delta = SimpleNamespace(
                reasoning_content="- Mr. Snuffleupagus?\n", reasoning=None, content=None
            )
            yield SimpleNamespace(choices=[SimpleNamespace(delta=delta)])

    def close(self):
        self.closed = True


def test_chat_hangs_up_on_a_looping_model(test_app, monkeypatch):
    import app as app_module
    from models import Message

    monkeypatch.delenv("OPENCOMPLETION_REPEAT_LIMIT", raising=False)
    stream = LoopingStream()
    sent = {}

    class Completions:
        def create(self, **kwargs):
            sent.update(kwargs)
            return stream

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr(
        app_module, "get_openai_client_and_model", lambda name: (client, name)
    )
    monkeypatch.setattr(app_module.socketio, "emit", lambda *a, **k: None)
    monkeypatch.setattr(app_module.socketio, "sleep", lambda s: None)
    monkeypatch.setattr(app_module, "title_untitled_room", lambda name: None)

    app_module.chat_gpt("ada", "muppets", model_name="qwen-thinker")

    assert stream.closed and stream.sent < 50
    assert sent["temperature"] == 0.6 and sent["max_tokens"] > 0
    reply = Message.query.filter_by(username="qwen-thinker").one()
    assert "kept repeating" in reply.content and "Snuffleupagus" in reply.content


def test_a_reply_cut_at_our_token_cap_says_so(test_app, monkeypatch):
    import app as app_module
    from models import Message

    monkeypatch.delenv("OPENCOMPLETION_MAX_TOKENS", raising=False)

    def stream():
        for text, reason in (("Big Bird is tall", None), (" and yel", "length")):
            delta = SimpleNamespace(
                reasoning_content=None, reasoning=None, content=text
            )
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=delta, finish_reason=reason)]
            )

    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: stream())
        )
    )
    monkeypatch.setattr(
        app_module, "get_openai_client_and_model", lambda name: (client, name)
    )
    monkeypatch.setattr(app_module.socketio, "emit", lambda *a, **k: None)
    monkeypatch.setattr(app_module.socketio, "sleep", lambda s: None)
    monkeypatch.setattr(app_module, "title_untitled_room", lambda name: None)

    app_module.chat_gpt("ada", "muppets", model_name="qwen-thinker")

    reply = Message.query.filter_by(username="qwen-thinker").one().content
    assert reply.startswith("Big Bird is tall and yel")
    assert "8192-token limit" in reply
