"""harvest.py: tool trails & room chains into repair lanes, & the report."""

import json
import os
import stat

import harvest


def trail(*events):
    return json.dumps(list(events))


def tool_event(chain, room, message, **body):
    return (chain, room, message, "tool", json.dumps(dict(type="tool", **body)))


def reply_event(chain, room, message, model):
    return (chain, room, message, "assistant", json.dumps({"model": model}))


LIVE = {"10@a", "11@b"}

MESSAGES = [
    (
        1,
        10,
        "qwen",
        trail(
            {"tool": "offered", "status": "done"},
            {"tool": "web_search", "status": "done", "result_sha256": "r1"},
            {
                "tool": "web_search",
                "status": "done",
                "repaired": ["trailing_comma"],
                "repaired_from": '{"query": "elmo",}',
                "result_sha256": "r2",
            },
            # kept before a trailing-comma repair existed: today's reads it
            {"tool": "web_fetch", "status": "error", "unparsed": '{"url": "x",}'},
            {"tool": "web_fetch", "status": "error", "unparsed": '{"url",, 7}'},
            # filed by its chain too: one row, not two; the identical call
            # after it the chain missed (a chain write failed) still counts
            {
                "tool": "run_code",
                "status": "error",
                "error": "exit 1",
                "result_sha256": "r5",
            },
            {
                "tool": "run_code",
                "status": "error",
                "error": "exit 1",
                "result_sha256": "r5",
            },
        ),
    ),
    # older than our chains: its trail is the only record of the error
    (
        2,
        11,
        "hermes",
        trail({"tool": "sandbox_run", "status": "error", "error": "boom"}),
    ),
    (3, 11, "hermes", trail({"tool": "web_search", "repaired": ["fence"]})),
    (4, 12, "qwen", "not json"),
    (5, 12, "qwen", json.dumps({"not": "a list"})),
]

EVENTS = [
    reply_event("10@a", 10, 1, "qwen"),
    tool_event(
        "10@a",
        10,
        1,
        tool="web_search",
        status="done",
        resultSha256="r2",
        repaired=["trailing_comma"],
    ),  # live: its trail has it, raw too
    tool_event(
        "10@a",
        10,
        1,
        tool="run_code",
        status="error",
        error="exit 1",
        resultSha256="r5",
    ),
    # a deleted room (its id reused by room 10's chain "10@a"): chain only
    reply_event("10@old", 10, 1, "hermes"),
    tool_event(
        "10@old",
        10,
        1,
        tool="sandbox_shell",
        status="error",
        error="exited with code 1",
        resultSha256="d1",
    ),
    tool_event(
        "10@old",
        10,
        1,
        tool="sandbox_write_file",
        status="done",
        resultSha256="d2",
        repaired=["raw_newlines"],
    ),
    tool_event(
        "10@old",
        10,
        1,
        tool="web_fetch",
        status="error",
        error="arguments were not a JSON object",
        unparsed=True,
        resultSha256="d3",
    ),
    (None, None, None, "tool", "not json"),
]


def lanes_in(path):
    return {
        lane: [json.loads(line) for line in open(path / "repair" / f"{lane}.jsonl")]
        for lane in harvest.LANES
    }


def test_a_harvest_reads_trails_and_chains_one_row_a_call(tmp_path):
    summary = harvest.harvest(MESSAGES, iter(EVENTS), LIVE, str(tmp_path))
    lanes = lanes_in(tmp_path)
    assert sorted(
        (r["room_alive"], r["message"], r["repairs"], r["raw"])
        for r in lanes["repaired"]
    ) == [
        (False, 1, ["raw_newlines"], None),  # a deleted room keeps no arguments
        (True, 1, ["trailing_comma"], '{"query": "elmo",}'),
        (True, 3, ["fence"], None),  # kept before repaired_from existed
    ]
    assert sorted((r["room_alive"], r["raw"] or "") for r in lanes["unparsed"]) == [
        (False, ""),
        (True, '{"url",, 7}'),
        (True, '{"url": "x",}'),
    ]
    # the chain's errors, the deleted room's included; the trail's only
    # where no chain filed that call; an unparsed call's error never twice
    assert sorted(
        (r["tool"], r["error"], r["model"], r["room_alive"]) for r in lanes["errors"]
    ) == [
        ("run_code", "exit 1", "qwen", True),
        ("run_code", "exit 1", "qwen", True),
        ("sandbox_run", "boom", "hermes", True),
        ("sandbox_shell", "exited with code 1", "hermes", False),
    ]
    for lane in harvest.LANES:
        mode = os.stat(tmp_path / "repair" / f"{lane}.jsonl").st_mode
        assert stat.S_IMODE(mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "repair").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(tmp_path).st_mode) == 0o700

    # replay reads what kept its arguments, & skips what kept none
    assert [r["raw"] for r in summary["salvaged"]] == ['{"url": "x",}']
    assert [r["raw"] for r in summary["still"]] == ['{"url",, 7}']
    text = "\n".join(harvest.report(summary))
    assert "repaired 3, unparsed 3, errors 4 call(s)" in text
    assert "from deleted rooms (their chains): repaired 1, unparsed 1, errors 1" in text
    assert (
        "1 read by today's repair, 1 still unreadable, 1 with no arguments kept" in text
    )
    assert "1  trailing_comma" in text and "1  raw_newlines" in text
    assert "web_fetch: object, not JSON  (models: qwen)" in text
    assert "sandbox_shell: exited with code 1" in text

    # a second run rewrites the lanes, never appends
    harvest.harvest(MESSAGES[1:2], [], LIVE, str(tmp_path))
    assert open(tmp_path / "repair" / "repaired.jsonl").read() == ""


def test_shapes():
    assert [
        harvest.shape(raw)
        for raw in (
            "",
            "  ",
            "<parameter=x>1</parameter>",
            '{"a": {',
            '{"a" 1}',
            "[1]",
            "hi",
        )
    ] == [
        "empty",
        "empty",
        "markup tags",
        "object cut off",
        "object, not JSON",
        "a list",
        "not an object",
    ]
