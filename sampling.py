"""What a chat ask is sent & how much it may spend: sampling, token budgets
& the pure helpers that size a prompt. Everything here reads our
environment or its arguments only, no Flask, database or socket; app.py
imports these names, & what needs a model's learned context
(`CONTEXT_WINDOWS`: `fit_max_tokens`, `room_from_refusal`, `fade_to_fit`)
stays there."""

import json
import os


def _env_float(name, default):
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# One request's thinking & answer together; a loop that a hang-up can't
# stop ends here. Qwen suggests 32k for thinking mode; ours is a thinking
# budget plus an answer.
DEFAULT_MAX_TOKENS = 24576
# Thinking past this many tokens with no answer begun: hang up & have the
# model answer from its notes (chat_gpt), in up to DEFAULT_ANSWER_TOKENS.
DEFAULT_THINKING_BUDGET = 16384
DEFAULT_ANSWER_TOKENS = 8192
# Qwen's thinking-mode top_k; sent as an extra body field, which servers
# that don't know it reject (open_completion then drops it).
DEFAULT_TOP_K = 20


def _env_int(name, default):
    return int(_env_float(name, default))


def chat_sampling(model_name):
    """Sampling for a chat reply. Greedy decoding (temperature 0, our old
    default) sends reasoning models such as Qwen3 into endless repetition
    in their thinking, so chat samples at Qwen's thinking-mode settings:
    OPENCOMPLETION_TEMPERATURE (0.6), OPENCOMPLETION_TOP_P (0.95) &
    OPENCOMPLETION_TOP_K (20; 0 = not sent), plus
    OPENCOMPLETION_PRESENCE_PENALTY when set (Qwen suggests up to 1.5 for
    quantized models that repeat).

    OPENCOMPLETION_MAX_TOKENS (24576; 0 = no cap) bounds every reply in the
    request itself: hanging up on a looping model (RepetitionGuard) stops
    the generation only where a disconnect reaches the server & aborts it,
    which not every GPU host or proxy does, while a server always stops at
    max_tokens. OpenAI's o-series take temperature 1 only & no top_p (o3:
    neither)."""
    if any(tag in model_name for tag in ("o1-", "o3", "o4-")):
        return {} if "o3" in model_name else {"temperature": 1}
    sampling = {
        "temperature": _env_float("OPENCOMPLETION_TEMPERATURE", 0.6),
        "top_p": _env_float("OPENCOMPLETION_TOP_P", 0.95),
    }
    top_k = _env_int("OPENCOMPLETION_TOP_K", DEFAULT_TOP_K)
    if top_k > 0:
        sampling["extra_body"] = {"top_k": top_k}
    presence = _env_float("OPENCOMPLETION_PRESENCE_PENALTY", None)
    if presence is not None:
        sampling["presence_penalty"] = presence
    max_tokens = _env_int("OPENCOMPLETION_MAX_TOKENS", DEFAULT_MAX_TOKENS)
    if max_tokens > 0:
        sampling["max_tokens"] = max_tokens
    return sampling


def thinking_budget():
    """OPENCOMPLETION_THINKING_BUDGET in tokens (16384; 0 = no budget)."""
    return max(0, _env_int("OPENCOMPLETION_THINKING_BUDGET", DEFAULT_THINKING_BUDGET))


DEFAULT_STREAM_RETRIES = 2


def stream_retries():
    """OPENCOMPLETION_STREAM_RETRIES (2; 0 = off): how often a reply whose
    stream breaks while it is read asks again, carrying on where it
    stopped (ReplyStream.carry_on)."""
    return max(0, _env_int("OPENCOMPLETION_STREAM_RETRIES", DEFAULT_STREAM_RETRIES))


def answer_tokens():
    """OPENCOMPLETION_ANSWER_TOKENS (8192; 0 = no cap): the answer a model
    writes from its notes once its thinking ran past its budget."""
    return max(0, _env_int("OPENCOMPLETION_ANSWER_TOKENS", DEFAULT_ANSWER_TOKENS))


def prompt_chars(kwargs):
    """How long an ask's prompt is: its messages & tool schemas as sent."""
    return len(json.dumps(kwargs.get("messages") or [], ensure_ascii=False)) + len(
        json.dumps(kwargs.get("tools") or [], ensure_ascii=False)
    )


def shrink_history(kwargs):
    """Make an ask's prompt smaller when it alone is past the model's
    context: drop the oldest whole turn of history (from the first message
    after the system ones up to the next message from a person), keeping
    the last person's message & all after it; with no older turn left,
    halve the longest tool result. False when nothing more can go."""
    messages = list(kwargs.get("messages") or [])
    people = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    first = next((i for i, m in enumerate(messages) if m.get("role") != "system"), None)
    if first is not None and people and first < people[-1]:
        cut = next(i for i in people if i > first)
        kwargs["messages"] = messages[:first] + messages[cut:]
        return True
    results = [
        i
        for i, m in enumerate(messages)
        if m.get("role") == "tool" and len(str(m.get("content") or "")) > 2000
    ]
    if not results:
        return False
    longest = max(results, key=lambda i: len(str(messages[i]["content"])))
    text = str(messages[longest]["content"])
    messages[longest] = dict(
        messages[longest],
        content=text[: len(text) // 2] + " …(cut to fit the model's context)",
    )
    kwargs["messages"] = messages
    return True


DEFAULT_ANSWER_ROOM = 8192


def answer_room():
    """OPENCOMPLETION_ANSWER_ROOM (8192; 0 = off): the tokens an ask keeps
    free for its answer; past that the conversation fades (fade.py)."""
    return max(0, _env_int("OPENCOMPLETION_ANSWER_ROOM", DEFAULT_ANSWER_ROOM))
