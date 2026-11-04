"""The fade (fade.py): a room's history fitted to its model's context before
each ask, the way uncloseai-cli fades a long chain. fox, 2026-11-04: a reply
after ten searches & fetches stopped ~500 tokens in, its context full."""

import json

import fade

PAGE = "page text " * 3000  # ~30k chars


def call(cid, name, **arguments):
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def room():
    return [
        {"role": "system", "content": "Today is ... You can call these tools: ..."},
        {"role": "user", "content": "first prompt: research modbus devices"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [call("a", "web_fetch", url="https://a.example/devices")],
        },
        {"role": "tool", "tool_call_id": "a", "content": PAGE},
        {"role": "assistant", "content": "Devices, at length. " * 200},
        {"role": "user", "content": "now energy meters"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [call("b", "web_search", query="modbus energy meters")],
        },
        {"role": "tool", "tool_call_id": "b", "content": PAGE},
        {"role": "assistant", "content": "Meters, at length. " * 200},
        {"role": "user", "content": "and solar inverters?"},
        {
            "role": "assistant",
            "content": "Searching.",
            "tool_calls": [call("c", "web_search", query="sunspec inverters")],
        },
        {"role": "tool", "tool_call_id": "c", "content": PAGE},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [call("d", "web_fetch", url="https://sunspec.example")],
        },
        {"role": "tool", "tool_call_id": "d", "content": PAGE},
    ]


def paired(messages):
    """Every tool result follows the assistant turn that called it."""
    asked = set()
    for m in messages:
        for c in m.get("tool_calls") or []:
            asked.add(c["id"])
        if m.get("role") == "tool" and m["tool_call_id"] not in asked:
            return False
    return True


def test_a_prompt_that_fits_is_left_alone():
    messages = room()
    out, step = fade.fit(messages, None, 10**9, 3.0)
    assert step == 0 and out == messages


def test_middle_results_fade_first_naming_the_call():
    messages = room()
    full = fade.tokens(messages, None, 3.0)
    out, step = fade.fit(messages, None, full - 12000, 3.0)
    assert step == 1
    stubbed = [m["content"] for m in out if m.get("role") == "tool"]
    assert stubbed[0].startswith("[faded: web_fetch https://a.example/devices result")
    assert "call it again" in stubbed[0]
    # the first prompt & the current turn are whole
    assert out[1]["content"] == "first prompt: research modbus devices"
    assert out[-1]["content"] == PAGE and out[-3]["content"] == PAGE
    assert paired(out) and messages[3]["content"] == PAGE  # input untouched


def test_then_older_turns_go_with_a_line_saying_so():
    messages = room()
    # room for the head & the current turn, a little more: the middle must go
    kept = [messages[0], messages[1]] + messages[9:]
    out, step = fade.fit(messages, None, fade.tokens(kept, None, 3.0) + 200, 3.0)
    assert step >= 3
    assert out[1]["content"] == "first prompt: research modbus devices"
    current = [m for m in out if m.get("role") == "user"][-1]
    assert current["content"].endswith("and solar inverters?")
    assert "faded to fit" in json.dumps(out)
    assert paired(out)


def test_the_current_turns_older_rounds_fade_its_newest_stays():
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "find everything"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [call("x", "web_fetch", url="u1")],
        },
        {"role": "tool", "tool_call_id": "x", "content": PAGE},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [call("y", "web_fetch", url="u2")],
        },
        {"role": "tool", "tool_call_id": "y", "content": PAGE},
    ]
    out, step = fade.fit(messages, None, 14000, 3.0)
    assert step == 4
    assert out[3]["content"].startswith("[faded: web_fetch u1 result")
    assert out[5]["content"] == PAGE  # the newest round, being answered from
    assert paired(out)


def test_every_ask_is_faded_to_leave_its_answer_room(monkeypatch):
    from types import SimpleNamespace

    import app as app_module

    monkeypatch.setattr(app_module, "CONTEXT_WINDOWS", {"q": (65536, 3.0)})
    sent = []
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **k: sent.append(k) or "stream")
        )
    )
    messages = room() * 2  # ~80k tokens of history
    kwargs = {"model": "q", "messages": messages, "max_tokens": 24576}
    record = {}
    assert app_module.open_completion(client, kwargs, sent=record) == "stream"
    asked = sent[-1]
    prompt = fade.tokens(asked["messages"], None, 3.0)
    assert prompt + 8192 <= 65536  # an answer has its 8k at least
    assert prompt + asked["max_tokens"] <= 65536
    assert record["faded"] >= 1 and record["max_tokens"] == asked["max_tokens"]
