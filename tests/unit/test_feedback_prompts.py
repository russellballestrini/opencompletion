#!/usr/bin/env python3
"""
Characterisation tests for activity.provide_feedback_prompts

Each feedback prompt becomes one generate_ai_feedback ask (patched here, so
no model is reached); these tests pin which tokens, metadata & response
each ask receives, which prompts are skipped & which replies are kept.
Tests whose docstring says "PINS CURRENT BEHAVIOUR" hold a surprising
behaviour steady through a refactor; they do not endorse it.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def run(prompts, metadata=None, transition=None, legacy="", replies=None, **kw):
    """Call provide_feedback_prompts; returns (messages, asks) where each
    ask is the kwargs-free argument tuple generate_ai_feedback received."""
    # Imported per call: some suites re-import activity under mocks, & a
    # patch must land on the module provide_feedback_prompts reads.
    import activity

    replies = list(replies) if replies is not None else None
    asks = []

    def fake(*args):
        asks.append(args)
        if replies is not None:
            return replies.pop(0)
        return f"reply {len(asks)}"

    with patch("activity.generate_ai_feedback", side_effect=fake):
        out = activity.provide_feedback_prompts(
            transition or {},
            kw.get("category", "correct"),
            kw.get("question", "Q?"),
            prompts,
            kw.get("user_response", "my answer"),
            kw.get("user_language", "English"),
            kw.get("username", "alice"),
            json.dumps(metadata or {}),
            kw.get("json_new_metadata", '{"n": 1}'),
            legacy,
            kw.get("model", "MODEL_5"),
        )
    return out, asks


def test_one_prompt_without_filter_gets_everything():
    out, (ask,) = run(
        [{"name": "coach", "tokens_for_ai": "Coach."}],
        metadata={"score": 3},
    )
    assert out == [{"name": "coach", "content": "reply 1"}]
    category, question, response, tokens, username, meta, new, model = ask
    assert (category, question, response, username, new, model) == (
        "correct",
        "Q?",
        "my answer",
        "alice",
        '{"n": 1}',
        "MODEL_5",
    )
    assert (
        tokens
        == "Coach. You must provide the feedback in the user's language: English."
    )
    # user_response rides along in the metadata a prompt sees.
    assert json.loads(meta) == {"score": 3, "user_response": "my answer"}


def test_legacy_tokens_language_and_transition_tokens_combine():
    _, (ask,) = run(
        [{"name": "coach", "tokens_for_ai": "Coach."}],
        transition={"ai_feedback": {"tokens_for_ai": "Cheer."}},
        legacy="Legacy.",
        user_language="Klingon",
    )
    assert ask[3] == (
        "Legacy. Coach. You must provide the feedback in the user's language: "
        "Klingon. Cheer."
    )


def test_ai_feedback_without_tokens_adds_a_space():
    _, (ask,) = run(
        [{"tokens_for_ai": "T."}],
        transition={"ai_feedback": {}},
    )
    assert ask[3].endswith("language: English. ")


def test_defaults_for_missing_name_and_tokens():
    out, (ask,) = run([{}])
    assert out == [{"name": "unnamed", "content": "reply 1"}]
    assert ask[3] == " You must provide the feedback in the user's language: English."


def test_metadata_filter_narrows_metadata_and_drops_response():
    _, (ask,) = run(
        [{"name": "judge", "metadata_filter": ["score", "missing"]}],
        metadata={"score": 3, "other": 1},
    )
    assert json.loads(ask[5]) == {"score": 3}
    assert ask[2] == ""


def test_metadata_filter_naming_user_response_keeps_it():
    _, (ask,) = run(
        [{"name": "judge", "metadata_filter": ["user_response"]}],
        metadata={"score": 3},
    )
    assert json.loads(ask[5]) == {"user_response": "my answer"}
    assert ask[2] == "my answer"


@pytest.mark.parametrize(
    "condition, values, skipped",
    [
        ("all_null", [None, "", "None"], True),
        ("all_null", [None, 0], False),
        ("all_false", [False, "False"], True),
        ("all_false", [False, 0], False),
        ("all_true", [True, "True"], True),
        ("all_true", [True, 1], False),
        ("unknown_condition", [None], False),
    ],
)
def test_skip_conditions(condition, values, skipped):
    metadata = {f"k{i}": v for i, v in enumerate(values)}
    out, asks = run(
        [
            {
                "name": "maybe",
                "metadata_filter": list(metadata),
                "skip_condition": condition,
            },
            {"name": "always"},
        ],
        metadata=metadata,
    )
    names = [m["name"] for m in out]
    assert names == (["always"] if skipped else ["maybe", "always"])
    assert len(asks) == len(names)


def test_skip_condition_with_nothing_filtered_skips():
    """PINS CURRENT BEHAVIOUR: a filter matching no key leaves no values &
    all() of nothing is True, so every skip condition skips the prompt."""
    out, asks = run(
        [{"name": "x", "metadata_filter": ["absent"], "skip_condition": "all_true"}],
        metadata={"present": 1},
    )
    assert out == [] and asks == []


def test_skip_condition_without_filter_is_ignored():
    """PINS CURRENT BEHAVIOUR: skip_condition is only read beside a
    metadata_filter; on its own it never skips."""
    out, _ = run(
        [{"name": "x", "skip_condition": "all_null"}],
        metadata={"a": None},
    )
    assert [m["name"] for m in out] == ["x"]


def test_empty_replies_are_dropped_and_others_stripped():
    out, asks = run(
        [{"name": "a"}, {"name": "b"}, {"name": "c"}, {"name": "d"}],
        replies=["  padded \n", "", "   ", None],
    )
    assert out == [{"name": "a", "content": "padded"}]
    assert len(asks) == 4


def test_battleship_debug_prompts_still_answer(capsys):
    """Ship Status & Game Over print debug lines (with & without a filter);
    the prompts themselves behave like any other."""
    meta = {
        "user_sunk_ship_this_round": "no",
        "ai_sunk_ship_this_round": "no",
        "game_over": False,
        "user_wins": False,
        "ai_wins": False,
    }
    prompts = [
        {"name": "Ship Status", "metadata_filter": ["user_sunk_ship_this_round"]},
        {"name": "Game Over", "metadata_filter": ["game_over"]},
        {"name": "Ship Status"},
        {"name": "Game Over"},
    ]
    out, asks = run(prompts, metadata=meta)
    assert [m["name"] for m in out] == [p["name"] for p in prompts]
    assert json.loads(asks[0][5]) == {"user_sunk_ship_this_round": "no"}
    assert json.loads(asks[1][5]) == {"game_over": False}
    printed = capsys.readouterr().out
    assert "DEBUG SHIP STATUS - filter_keys" in printed
    assert "DEBUG GAME OVER - filter_keys" in printed
    assert "DEBUG SHIP STATUS - NO metadata_filter" in printed
    assert "DEBUG GAME OVER - NO metadata_filter" in printed


def test_prompts_do_not_share_filtered_metadata():
    _, asks = run(
        [
            {"name": "a", "metadata_filter": ["x"]},
            {"name": "b"},
        ],
        metadata={"x": 1, "y": 2},
    )
    assert json.loads(asks[0][5]) == {"x": 1}
    assert json.loads(asks[1][5]) == {"x": 1, "y": 2, "user_response": "my answer"}
