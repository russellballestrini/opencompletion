"""activity_grading.py: unyaml's grading primitives in our engine."""

import copy

import pytest

import activity_engine
import activity_grading as grading

FREE = {
    "step_id": "ask",
    "title": "Ask",
    "question": "What is 3 x 4?",
    "tokens_for_ai": "Grade the product.",
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
        {"id": "c", "text": "Water vapour"},
    ],
    "answer": "a",
    "transitions": {
        "correct": {
            "content_blocks": ["Right."],
            "ai_feedback": {"tokens_for_ai": "Praise."},
            "next_section_and_step": "s:end",
        },
        "incorrect": {"content_blocks": ["Not quite."]},
    },
}


def test_a_free_step_gains_the_clarifying_bucket():
    assert grading.buckets_for({}, FREE)[-1] == grading.CLARIFYING
    assert grading.transition_for(FREE, grading.CLARIFYING) == (
        grading.CLARIFYING_TRANSITION
    )
    assert grading.CLARIFYING_TRANSITION["counts_as_attempt"] is False


@pytest.mark.parametrize(
    "content, step_keys",
    [
        ({"allow_clarifying_questions": False}, {}),
        ({}, {"allow_clarifying_questions": False}),
    ],
)
def test_clarifying_can_be_turned_off(content, step_keys):
    step = {**FREE, **step_keys}
    assert grading.CLARIFYING not in grading.buckets_for(content, step)


def test_a_step_level_yes_beats_an_activity_level_no():
    step = {**FREE, "allow_clarifying_questions": True}
    content = {"allow_clarifying_questions": False}
    assert grading.CLARIFYING in grading.buckets_for(content, step)


def test_a_steps_own_clarifying_bucket_is_kept_not_doubled():
    step = copy.deepcopy(FREE)
    step["buckets"].append("asking_clarifying_questions")
    step["transitions"]["asking_clarifying_questions"] = {"content_blocks": ["Ask"]}
    buckets = grading.buckets_for({}, step)
    assert buckets.count(grading.CLARIFYING) == 1
    assert grading.transition_for(step, grading.CLARIFYING) == {
        "content_blocks": ["Ask"]
    }


def test_instructions_carry_criteria_elements_and_clarifying():
    step = {
        **FREE,
        "bucket_criteria": {"correct": "Says 12.", "off_topic": "Not about it."},
        "required_elements": ["states 12", "shows 3 groups of 4"],
    }
    text = grading.grading_instructions({}, step)
    assert text.startswith("Grade the product.")
    assert "Category boundaries:\n- correct: Says 12." in text
    assert "- shows 3 groups of 4" in text
    assert "Missing any one element = partial_understanding, never correct." in text
    assert f"- {grading.CLARIFYING}:" in text


def test_instructions_without_extras_are_the_steps_own():
    content = {"allow_clarifying_questions": False}
    assert grading.grading_instructions(content, FREE) == "Grade the product."


@pytest.mark.parametrize(
    "said, bucket",
    [
        ("a", "correct"),
        ("A", "correct"),
        ("a)", "correct"),
        ("**a**", "correct"),
        ("carbon dioxide", "correct"),
        ("b", "incorrect"),
        ("a, b", "incorrect"),
        ("", "incorrect"),
        ("no idea", "incorrect"),
    ],
)
def test_one_answer_choices_are_graded_by_key(said, bucket):
    assert grading.grade_choice(CHOICE, said) == bucket


def test_pick_every_one_needs_the_exact_set():
    step = {**CHOICE, "answer": ["a", "c"]}
    assert grading.answer_mode(step) == "all"
    assert grading.grade_choice(step, "c, a") == "correct"
    assert grading.grade_choice(step, "a and c") == "correct"
    assert grading.grade_choice(step, "a") == "incorrect"
    assert grading.grade_choice(step, "a, b, c") == "incorrect"


def test_any_accepts_one_of_the_set():
    step = {**CHOICE, "answer": ["a", "c"], "answer_mode": "any"}
    assert grading.grade_choice(step, "c") == "correct"
    assert grading.grade_choice(step, "a, c") == "incorrect"


def test_named_buckets_follow_mc_correct_and_mc_incorrect():
    step = {
        **CHOICE,
        "buckets": ["spot_on", "nope", "off_topic"],
        "mc_correct": "spot_on",
        "mc_incorrect": "nope",
    }
    assert grading.grade_choice(step, "a") == "spot_on"
    assert grading.grade_choice(step, "b") == "nope"


def test_the_page_never_gets_the_answer():
    offer = grading.choices_for_page({**CHOICE, "answer": ["a", "c"]})
    assert offer == {
        "choices": [
            {"id": "a", "text": "Carbon dioxide"},
            {"id": "b", "text": "Oxygen"},
            {"id": "c", "text": "Water vapour"},
        ],
        "multiple": True,
    }
    assert "answer" not in str(offer)


class FakeIO(activity_engine.ActivityIO):
    def __init__(self):
        self.said = []
        self.offers = []
        self.categorized = []
        self.feedback_asked = 0
        self.done = False

    def say(self, content, username="System", shown_as=None):
        self.said.append((username, content))

    def notice(self, content, pause=None):
        pass

    def categorize(self, question, response, buckets, tokens_for_ai, model):
        self.categorized.append((buckets, tokens_for_ai))
        return self.next_bucket

    def feedback(self, *args):
        self.feedback_asked += 1
        return "model words"

    def offer_choices(self, offer):
        self.offers.append(offer)

    def complete(self, state, username, feedback_model):
        state.completed = True
        self.done = True


def activity(*steps):
    return {
        "sections": [
            {
                "section_id": "s",
                "title": "S",
                "steps": [
                    *steps,
                    {"step_id": "end", "title": "End", "content_blocks": ["Bye"]},
                ],
            }
        ]
    }


def start(content):
    first = content["sections"][0]
    state = activity_engine.MemoryState("s", first["steps"][0]["step_id"])
    io = FakeIO()
    activity_engine.show_until_question(content, state, io, "Ann")
    return state, io


def test_a_choice_turn_asks_no_model_and_offers_buttons():
    content = activity(CHOICE)
    state, io = start(content)
    question = io.said[-1][1]
    assert "- **a**: Carbon dioxide" in question
    assert io.offers == [grading.choices_for_page(CHOICE)]
    activity_engine.answer(content, state, "b", "Ann", io)
    assert io.categorized == []
    assert io.feedback_asked == 0
    assert state.step_id == "pick"  # incorrect stays & asks again
    activity_engine.answer(content, state, "a", "Ann", io)
    assert io.feedback_asked == 0  # its static content, no model words
    assert ("System", "Right.") in io.said
    assert io.done


def test_mc_ai_feedback_asks_a_model_for_words():
    content = activity({**CHOICE, "mc_ai_feedback": True})
    state, io = start(content)
    activity_engine.answer(content, state, "a", "Ann", io)
    assert io.feedback_asked == 1


def test_a_clarifying_question_costs_no_attempt_and_stays():
    content = activity(FREE)
    state, io = start(content)
    io.next_bucket = grading.CLARIFYING
    activity_engine.answer(content, state, "what is x?", "Ann", io)
    buckets, instructions = io.categorized[-1]
    assert grading.CLARIFYING in buckets
    assert grading.CLARIFYING in instructions
    assert state.step_id == "ask"
    assert state.attempts == 0
    assert io.feedback_asked == 1  # the built-in transition's answer


def test_a_content_step_goes_where_it_says():
    hub = {
        "step_id": "hub",
        "title": "Hub",
        "content_blocks": ["Hub"],
        "next_section_and_step": "s:end",
    }
    skipped = {"step_id": "skipped", "title": "No", "content_blocks": ["Never"]}
    state, io = start(activity(hub, skipped))
    assert all("Never" not in text for _, text in io.said)
    assert io.done


def test_a_content_step_follows_a_conditional_target():
    hub = {
        "step_id": "hub",
        "title": "Hub",
        "content_blocks": ["Hub"],
        "next_section_and_step": [
            {"if": {"score_gte": 5}, "goto": "s:end"},
            {"else": True, "goto": "s:low"},
        ],
    }
    low = {"step_id": "low", "title": "Low", "content_blocks": ["Low score"]}
    content = activity(
        hub, {"step_id": "gap", "title": "Gap", "content_blocks": ["Gap"]}, low
    )
    first = content["sections"][0]
    state = activity_engine.MemoryState("s", "hub", metadata={"score": 7})
    io = FakeIO()
    activity_engine.show_until_question(content, state, io, "Ann")
    texts = [text for _, text in io.said]
    assert "Low score" not in texts and "Gap" not in texts
    state = activity_engine.MemoryState("s", "hub", metadata={"score": 1})
    io = FakeIO()
    activity_engine.show_until_question(content, state, io, "Ann")
    texts = [text for _, text in io.said]
    assert "Low score" in texts and "Gap" not in texts
    assert first
