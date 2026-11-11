"""Our validator's grading keys (unyaml's): bucket_criteria,
required_elements, allow_clarifying_questions & multiple choice; its
unknown-key warning; & a content step's own next_section_and_step."""

import copy

import pytest
import yaml

from activity_yaml_validator import ActivityYAMLValidator

QUESTION = {
    "step_id": "ask",
    "title": "Ask",
    "question": "What is 3 x 4?",
    "buckets": ["correct", "partial_understanding", "off_topic"],
    "transitions": {
        "correct": {"next_section_and_step": "s:end"},
        "partial_understanding": {},
        "off_topic": {},
    },
}

CHOICE = {
    "step_id": "pick",
    "title": "Pick",
    "question_type": "multiple_choice",
    "question": "Which gas do plants take in?",
    "choices": [
        {"id": "a", "text": "Carbon dioxide"},
        {"id": "b", "text": "Oxygen"},
    ],
    "answer": "a",
    "transitions": {
        "correct": {"next_section_and_step": "s:end"},
        "incorrect": {},
    },
}


def activity(*steps, **root):
    return {
        **root,
        "sections": [
            {
                "section_id": "s",
                "title": "S",
                "steps": [
                    *steps,
                    {"step_id": "end", "title": "End", "content_blocks": ["Bye"]},
                ],
            }
        ],
    }


def check(tmp_path, content):
    path = tmp_path / "activity-test.yaml"
    path.write_text(yaml.safe_dump(content, sort_keys=False))
    ok, errors, warnings = ActivityYAMLValidator().validate_file(str(path))
    return errors, warnings


def with_keys(step, **keys):
    made = copy.deepcopy(step)
    made.update(keys)
    return made


def test_criteria_and_elements_pass(tmp_path):
    step = with_keys(
        QUESTION,
        bucket_criteria={"correct": "Says 12.", "off_topic": "Not about it."},
        required_elements=["states 12"],
        allow_clarifying_questions=False,
    )
    assert check(tmp_path, activity(step)) == ([], [])


@pytest.mark.parametrize(
    "keys, says",
    [
        ({"bucket_criteria": {"right": "Says 12."}}, "not one of its buckets"),
        ({"bucket_criteria": {"correct": ""}}, "non-empty text"),
        ({"required_elements": "states 12"}, "list of non-empty text"),
        ({"required_elements": []}, "list of non-empty text"),
        ({"allow_clarifying_questions": "no"}, "true or false"),
    ],
)
def test_bad_criteria_are_errors(tmp_path, keys, says):
    errors, _ = check(tmp_path, activity(with_keys(QUESTION, **keys)))
    assert any(says in e for e in errors), errors


def test_a_multiple_choice_step_defaults_its_buckets(tmp_path):
    assert check(tmp_path, activity(CHOICE)) == ([], [])


def test_multi_select_answers_pass(tmp_path):
    step = with_keys(
        CHOICE,
        choices=CHOICE["choices"] + [{"id": "c", "text": "Water"}],
        answer=["a", "c"],
        answer_mode="all",
    )
    assert check(tmp_path, activity(step)) == ([], [])


@pytest.mark.parametrize(
    "keys, says",
    [
        ({"question_type": "quiz"}, "question_type must be one of"),
        ({"choices": [{"id": "a", "text": "Only"}]}, "2+ choices"),
        ({"choices": [{"id": "a"}, {"id": "b", "text": "B"}]}, "must be {id, text}"),
        (
            {"choices": [{"id": "a", "text": "A"}, {"id": "a", "text": "B"}]},
            "unique",
        ),
        ({"answer": "z"}, "not a choice id"),
        ({"answer": None}, "needs an answer"),
        ({"answer_mode": "some"}, "answer_mode must be one of"),
        ({"answer": ["a", "b"], "answer_mode": "one"}, "takes one answer"),
        ({"mc_correct": "right"}, "not one of its buckets"),
        ({"mc_ai_feedback": "yes"}, "true or false"),
    ],
)
def test_bad_multiple_choice_steps_are_errors(tmp_path, keys, says):
    errors, _ = check(tmp_path, activity(with_keys(CHOICE, **keys)))
    assert any(says in e for e in errors), errors


def test_a_multiple_choice_step_needs_a_transition_per_bucket(tmp_path):
    step = with_keys(CHOICE, transitions={"correct": {}})
    errors, _ = check(tmp_path, activity(step))
    assert any("Missing transition for bucket 'incorrect'" in e for e in errors)


def test_choice_keys_on_a_free_answer_step_warn(tmp_path):
    _, warnings = check(tmp_path, activity(with_keys(QUESTION, answer="12")))
    assert any("only on a multiple-choice step" in w for w in warnings)


def test_a_typo_anywhere_is_a_warning(tmp_path):
    step = copy.deepcopy(QUESTION)
    step["transitons"] = {}
    step["transitions"]["correct"]["metadata_ad"] = {"score": "n+1"}
    content = activity(step, titel="Oops")
    _, warnings = check(tmp_path, content)
    joined = " ".join(warnings)
    assert "unknown step key 'transitons'" in joined
    assert "unknown transition key 'metadata_ad'" in joined
    assert "unknown activity key 'titel'" in joined


def test_a_content_steps_own_navigation_is_checked(tmp_path):
    hub = {"step_id": "hub", "title": "Hub", "content_blocks": ["Hi"]}
    good = with_keys(hub, next_section_and_step="s:end")
    assert check(tmp_path, activity(good)) == ([], [])
    bad = with_keys(hub, next_section_and_step="s:nowhere")
    errors, _ = check(tmp_path, activity(bad))
    assert any("Invalid next_section_and_step 's:nowhere'" in e for e in errors)


def test_navigation_on_a_question_step_is_ignored_and_says_so(tmp_path):
    step = with_keys(QUESTION, next_section_and_step="s:end")
    _, warnings = check(tmp_path, activity(step))
    assert any("ignored; put it on each transition" in w for w in warnings)


def test_every_activity_we_ship_still_validates():
    import glob

    for path in sorted(glob.glob("research/activity*.yaml")):
        ok, errors, _ = ActivityYAMLValidator().validate_file(path)
        assert ok, (path, errors)
