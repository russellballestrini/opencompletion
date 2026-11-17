"""harvest.py: tool trails into repair lanes, & the report."""

import json
import os
import stat

import harvest


def trail(*events):
    return json.dumps(list(events))


MESSAGES = [
    (
        1,
        10,
        "qwen",
        trail(
            {"tool": "offered", "status": "done"},
            {"tool": "web_search", "status": "done"},
            {
                "tool": "web_search",
                "status": "done",
                "repaired": ["trailing_comma"],
                "repaired_from": '{"query": "elmo",}',
            },
            {"tool": "web_fetch", "status": "error", "unparsed": '{"url": "x",}'},
            {"tool": "web_fetch", "status": "error", "unparsed": '{"url",, 7}'},
        ),
    ),
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


def test_a_harvest_writes_owner_only_lanes_and_reports(tmp_path):
    summary = harvest.harvest(MESSAGES, str(tmp_path))
    lanes = {
        lane: [json.loads(line) for line in open(tmp_path / "repair" / f"{lane}.jsonl")]
        for lane in harvest.LANES
    }
    assert [
        (r["message"], r["call"], r["repairs"], r["raw"]) for r in lanes["repaired"]
    ] == [
        (1, 2, ["trailing_comma"], '{"query": "elmo",}'),
        (3, 0, ["fence"], None),  # kept before repaired_from existed
    ]
    assert [r["raw"] for r in lanes["unparsed"]] == ['{"url": "x",}', '{"url",, 7}']
    # an unparsed call's error is not filed twice
    assert [(r["tool"], r["error"], r["model"]) for r in lanes["errors"]] == [
        ("sandbox_run", "boom", "hermes")
    ]
    for lane in harvest.LANES:
        mode = os.stat(tmp_path / "repair" / f"{lane}.jsonl").st_mode
        assert stat.S_IMODE(mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "repair").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(tmp_path).st_mode) == 0o700

    # kept before a trailing-comma repair existed: today's repair reads it
    assert [r["raw"] for r in summary["salvaged"]] == ['{"url": "x",}']
    assert [r["raw"] for r in summary["still"]] == ['{"url",, 7}']
    text = "\n".join(harvest.report(summary))
    assert "repaired 2, unparsed 2, errors 1 call(s)" in text
    assert "1  trailing_comma" in text and "1  fence" in text
    assert "1 read by today's repair, 1 still unreadable" in text
    assert "web_fetch: object, not JSON  (models: qwen)" in text
    assert "sandbox_run: boom" in text

    # a second run rewrites the lanes, never appends
    harvest.harvest(MESSAGES[1:2], str(tmp_path))
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
