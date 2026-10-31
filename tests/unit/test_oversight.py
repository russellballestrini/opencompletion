"""tools/oversight.py: when a delegate is getting somewhere, and reading
its overseer's call."""

import pytest

from tools import oversight

pytestmark = pytest.mark.unit


def test_only_a_different_action_is_news():
    a = oversight.activity_key("[bash] pytest -q  ↳ ok · 433 bytes")
    b = oversight.activity_key("[bash] pytest -q ↳ ok · 1.4k bytes")
    assert a == b  # same command, new timings: no news
    assert a != oversight.activity_key("[bash] git diff")


def test_a_run_moves_on_plan_files_or_new_activity(monkeypatch):
    monkeypatch.setenv("OPENCOMPLETION_DELEGATE_STALL_S", "600")
    watch = oversight.Watch(0)
    assert watch.observe({"plan": {"done": 1}}, "[bash] git clone", 15)
    assert not watch.observe({"plan": {"done": 1}}, "[bash] git clone", 30)
    assert watch.observe({"plan": {"done": 2}}, "[bash] git clone", 45)
    assert watch.observe({"files": {"count": 1, "changed": ["a.py"]}}, "", 60)
    assert not watch.observe({"files": {"count": 1, "changed": ["a.py"]}}, "", 75)
    assert watch.observe({}, "[write] b.py", 90)
    assert watch.last_progress == 90
    assert not watch.stalled(600) and watch.stalled(691)


def test_a_loop_is_a_stall(monkeypatch):
    monkeypatch.setenv("OPENCOMPLETION_DELEGATE_STALL_S", "60")
    watch = oversight.Watch(0)
    loop = "[bash] python -c 'print(x)'\n↳ bash ok · 89 bytes"
    watch.observe({"turn": 1}, loop, 0)
    for t in range(15, 120, 15):
        watch.observe({"turn": t}, loop, t)  # turns climb, nothing new
    assert watch.stalled(120)


def test_no_stall_limit_never_stalls(monkeypatch):
    monkeypatch.setenv("OPENCOMPLETION_DELEGATE_STALL_S", "0")
    assert not oversight.Watch(0).stalled(10**9)


def test_the_overseers_call_is_read_safely():
    assert oversight.parse_decision("CONTINUE") == ("continue", "")
    assert oversight.parse_decision("Steer: use 30-day months.") == (
        "steer",
        "use 30-day months.",
    )
    assert oversight.parse_decision("STOP — it cannot reach the repo") == (
        "stop",
        "it cannot reach the repo",
    )
    # unreadable, or a steer/stop with nothing said: let it go on
    assert oversight.parse_decision("I think it's fine?")[0] == "continue"
    assert oversight.parse_decision("STOP")[0] == "continue"
    assert oversight.parse_decision(None)[0] == "continue"
