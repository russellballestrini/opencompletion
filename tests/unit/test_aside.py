"""aside.py: what a /btw aside reads of a running reply, as plain text."""

import aside


def test_tool_turns_become_plain_text_and_system_is_left_out():
    turns = [
        {"role": "system", "content": "you may call tools"},
        {"role": "user", "content": "fix the tests"},
        {
            "role": "assistant",
            "content": "Looking.",
            "tool_calls": [
                {
                    "function": {
                        "name": "sandbox_shell",
                        "arguments": '{"command": "make test"}',
                    }
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "x" * 5000},
    ]
    text = aside.transcript(turns)
    assert "you may call tools" not in text
    assert "[user] fix the tests" in text
    assert '[assistant called sandbox_shell] {"command": "make test"}' in text
    assert "[tool result] " + "x" * aside.RESULT_CHARS + " …" in text


def test_a_long_room_keeps_its_newest_part():
    turns = [{"role": "user", "content": f"turn {i} " + "y" * 1000} for i in range(100)]
    text = aside.transcript(turns)
    assert len(text) <= aside.TRANSCRIPT_CHARS + 2
    assert "turn 99" in text and "turn 0 " not in text


def test_a_vision_turn_gives_its_words_never_its_image():
    turns = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                },
            ],
        }
    ]
    assert aside.transcript(turns) == "[user] what is this"


def test_running_now_names_the_tool_and_its_progress():
    trail = [
        {"tool": "web_search", "status": "done"},
        {
            "tool": "delegate",
            "label": "make tests pass",
            "status": "running",
            "summary": "It is fixing the parser.",
            "progress": "line 1\nline 2",
        },
    ]
    now = aside.running_now("qwen", 95.4, "Working on it", trail)
    assert "qwen is still working on this reply (95s so far)" in now
    assert "Working on it" in now
    assert "Running now: delegate make tests pass" in now
    assert "It is fixing the parser." in now and "line 2" in now


def test_with_no_tool_running_it_says_so():
    assert "No tool is running" in aside.running_now("qwen", 3, "", [])


def test_the_ask_ends_with_the_question():
    asked = aside.messages("what now?", [{"role": "user", "content": "hi"}])
    assert asked[0]["role"] == "system"
    assert asked[-1]["content"].endswith("Aside question: what now?")
    assert "No reply is running" in asked[-1]["content"]
