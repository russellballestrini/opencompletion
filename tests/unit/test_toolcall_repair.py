"""toolcall_repair.py is vendored from uncloseai-cli (its tool-call argument
repair, stdlib only). These replay the known-answer table shared with
uncloseai-cli, copied verbatim into tests/fixtures/toolcall_repair/, so a
drifted copy fails here instead of agreeing with itself."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import toolcall_repair as R  # noqa: E402

CASES = ROOT / "tests" / "fixtures" / "toolcall_repair" / "cases.json"


def test_the_vendored_copy_gives_uncloseai_clis_answers():
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    assert len(cases) >= 16
    for c in cases:
        args, repairs = R.repair_arguments(
            c["raw"], primary_key=c["primary_key"], aliases=c["aliases"]
        )
        assert (args, repairs) == (c["args"], c["repairs"]), c["case"]


def test_a_tool_call_a_model_broke_is_mended_and_says_so():
    from tools.loop import parse_arguments

    # a file body with raw newlines: json.loads refuses it (2026-11-06)
    raw = '{"path": "/root/work/a.py", "content": "print(1)\nprint(2)"}'
    assert parse_arguments(raw, "sandbox_write_file") == (
        {"path": "/root/work/a.py", "content": "print(1)\nprint(2)"},
        ["json_repair"],
    )
    # misnamed keys & a bare shell command
    assert parse_arguments('{"file_path": "a", "text": "b"}', "sandbox_write_file")[
        0
    ] == {
        "path": "a",
        "content": "b",
    }
    assert parse_arguments("ls -la", "sandbox_shell") == ({"command": "ls -la"}, [])
    # junk is never a URL; nothing at all is no arguments
    assert parse_arguments("not json", "web_fetch") == (None, [])
    assert parse_arguments("", "web_fetch") == ({}, [])
