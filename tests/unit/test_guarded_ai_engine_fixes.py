"""research/guarded_ai.py, our thin client, carries the same activity fixes
as activity.py (2026-11-01): metadata_remove takes one key as a string,
metadata_random picks one item of a list, an empty transition is one,
ai_feedback runs on its own & a script's target beats its transition's.
simulate_activity runs for real; only input & the model are replaced."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "research"))

import guarded_ai  # noqa: E402
from activity_utils import metadata_keys, pick_metadata_random  # noqa: E402


def test_metadata_keys_never_split_a_name():
    assert metadata_keys("coin") == ["coin"]
    assert metadata_keys(["a", "b"]) == ["a", "b"]
    assert metadata_keys(None) == []


def test_pick_metadata_random_picks_a_key_and_an_item():
    with patch("activity_utils.random.choice", side_effect=lambda xs: xs[-1]):
        assert pick_metadata_random({"dice": [1, 2, 3]}) == ("dice", 3)
        assert pick_metadata_random({"amulet": True, "scroll": True}) == (
            "scroll",
            True,
        )
    assert pick_metadata_random({"empty": []}) == ("empty", [])


def run(tmp_path, activity, verdicts, answers=None, feedback="FB"):
    """simulate_activity over `activity`; each answer is categorized as the
    next verdict. Returns (printed lines, feedback asks, each with
    the metadata it was sent)."""
    path = tmp_path / "a.yaml"
    path.write_text(activity)
    verdicts = list(verdicts)
    answers = list(answers or ["x"] * len(verdicts))
    printed, asks = [], []

    def fake_feedback(*args, **kwargs):
        asks.append(args[:4] + (dict(args[4]),))  # metadata as it was asked
        return feedback

    with patch("builtins.input", side_effect=answers), patch.object(
        guarded_ai, "categorize_response", side_effect=lambda *a, **k: verdicts.pop(0)
    ), patch.object(
        guarded_ai, "generate_ai_feedback", side_effect=fake_feedback
    ), patch(
        "builtins.print",
        side_effect=lambda *a, **k: printed.append(" ".join(map(str, a))),
    ):
        guarded_ai.simulate_activity(str(path))
    return printed, asks


def step_with(transition, extra="", buckets="[go]"):
    return f"""
sections:
  - section_id: s1
    title: One
    steps:
      - step_id: q1
        title: Q1
        question: "Answer?"
        tokens_for_ai: "Sort it."
        buckets: {buckets}
{extra}
        transitions:
{transition}
      - step_id: q2
        title: Q2
        content_blocks: ["Q2 reached"]
      - step_id: end
        title: End
        content_blocks: ["The end"]
"""


def test_an_empty_transition_moves_on(tmp_path):
    printed, _ = run(tmp_path, step_with("          go: {}"), ["go"])
    assert any("Q2 reached" in line for line in printed)
    assert not any("No valid transition" in line for line in printed)


def test_ai_feedback_runs_without_feedback_tokens(tmp_path):
    printed, asks = run(
        tmp_path,
        step_with("""          go:
            ai_feedback: {tokens_for_ai: "Cheer."}"""),
        ["go"],
    )
    assert len(asks) == 1 and any("FB" in line for line in printed)


def test_metadata_remove_string_and_random_list(tmp_path):
    activity = step_with(
        """          go:
            metadata_add: {ab: 1, a: 2}
            metadata_tmp_random:
              dice: [4, 5, 6]
            ai_feedback: {tokens_for_ai: "Say."}
            next_section_and_step: "s1:q3"
          stop:
            metadata_remove: "ab"
            ai_feedback: {tokens_for_ai: "Say."}""",
        buckets="[go, stop]",
    ).replace(
        "      - step_id: q2\n",
        """      - step_id: q3
        title: Q3
        question: "Again?"
        tokens_for_ai: "Sort it."
        buckets: [go, stop]
        transitions:
          stop:
            metadata_remove: "ab"
            ai_feedback: {tokens_for_ai: "Say."}
          go: {}
      - step_id: q2
""",
    )
    printed, asks = run(tmp_path, activity, ["go", "stop"])
    first, second = asks[0][4], asks[1][4]  # the metadata the model saw
    assert first["dice"] in (4, 5, 6)
    assert "ab" not in second and second["a"] == 2


def test_a_script_target_beats_its_transition(tmp_path):
    printed, _ = run(
        tmp_path,
        step_with(
            """          go:
            run_processing_script: true
            next_section_and_step: "s1:q1\"""",
            extra="""        processing_script: |
          script_result = {"next_section_and_step": "s1:end"}""",
        ),
        ["go"],
    )
    assert any("The end" in line for line in printed)
    assert not any("Q2 reached" in line for line in printed)
