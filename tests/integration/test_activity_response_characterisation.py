#!/usr/bin/env python3
"""
Characterisation tests for activity.handle_activity_response

These pin what our activity engine does TODAY, branch by branch, so it can
be refactored with no behaviour change. Each test drives one small inline
YAML activity through a real (in-memory) database & a fake model that never
touches the network: categorize asks pop the next verdict from a queue,
feedback asks answer "FEEDBACK <n>", translations answer "[<language>] <text>".

Tests whose docstring says "PINS CURRENT BEHAVIOUR" record something that
looks surprising; they hold it steady, they do not endorse it. Eleven of
them pinned defects (2026-11-01), now fixed: each says "FIXED" & what is
right instead.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ── fake model ───────────────────────────────────────────────────────


def _completion(text, logprobs=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=text), logprobs=logprobs)
        ]
    )


class FakeModel:
    """Answers every chat.completions.create our activity engine makes,
    told apart by the system prompt each helper writes."""

    def __init__(self):
        self.verdicts = []
        self.feedback_count = 0
        self.calls = []  # (kind, messages)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        messages = kwargs["messages"]
        system = messages[0]["content"]
        if system.startswith("Translate the following text to"):
            self.calls.append(("translate", messages))
            lang = system[len("Translate the following text to ") :].split(".")[0]
            return _completion(f"[{lang}] {messages[1]['content']}")
        if "Generate a human-readable feedback message" in system:
            self.calls.append(("feedback", messages))
            self.feedback_count += 1
            return _completion(f"FEEDBACK {self.feedback_count}")
        if system.startswith("Using the following rubric"):
            self.calls.append(("grading", messages))
            return _completion("GRADE")
        self.calls.append(("categorize", messages))
        return _completion(self.verdicts.pop(0) if self.verdicts else "")

    def kinds(self):
        return [kind for kind, _ in self.calls]

    def feedback_calls(self):
        return [m for kind, m in self.calls if kind == "feedback"]


# ── harness ──────────────────────────────────────────────────────────


class Engine:
    """One room, one activity, one fake model & every socket emit kept."""

    def __init__(self, db, activity, model, socketio, get_client):
        self.db = db
        self.activity = activity
        self.model = model
        self.socketio = socketio
        self.get_client = get_client
        self.content = None
        self.room = None

    def load(self, activity_yaml, section="s1", step="q1", metadata=None, **state):
        from models import ActivityState, Room

        self.content = yaml.safe_load(activity_yaml)
        self.room = Room(name="engine_room")
        self.db.session.add(self.room)
        self.db.session.commit()
        st = ActivityState(
            room_id=self.room.id,
            section_id=section,
            step_id=step,
            s3_file_path="research/inline.yaml",
            max_attempts=self.content.get("default_max_attempts_per_step", 3),
            **state,
        )
        for key, value in (metadata or {}).items():
            st.add_metadata(key, value)
        self.db.session.add(st)
        self.db.session.commit()
        return st

    def respond(self, response, *verdicts, username="alice"):
        self.model.verdicts.extend(verdicts)
        self.activity.handle_activity_response(self.room.name, response, username)
        self.db.session.expire_all()

    def state(self):
        from models import ActivityState

        return ActivityState.query.filter_by(room_id=self.room.id).first()

    def emits(self, event=None):
        out = []
        for call in self.socketio.emit.call_args_list:
            name, payload = call.args[0], call.args[1]
            if event is None or name == event:
                out.append((name, payload))
        return out

    def said(self):
        """Every chat_message content, in order."""
        return [p["content"] for _, p in self.emits("chat_message")]

    def said_by(self, username):
        return [
            p["content"]
            for _, p in self.emits("chat_message")
            if p["username"] == username
        ]

    def stored(self):
        from models import Message

        return [
            (m.username, m.content)
            for m in Message.query.filter_by(room_id=self.room.id)
            .order_by(Message.id)
            .all()
        ]


@pytest.fixture
def engine():
    import app as app_module
    import activity
    from flask import Flask
    from models import db

    test_app = Flask(__name__)
    test_app.config["TESTING"] = True
    test_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    test_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    test_app.config["LOCAL_ACTIVITIES"] = True
    db.init_app(test_app)

    saved = (activity.app, activity.db, activity.get_room, activity.SYSTEM_USERS)
    activity.app = test_app
    activity.db = db
    activity.get_room = app_module.get_room
    activity.SYSTEM_USERS = app_module.SYSTEM_USERS

    model = FakeModel()
    ctx = test_app.app_context()
    ctx.push()
    db.create_all()
    socketio = MagicMock()
    eng = Engine(db, activity, model, socketio, None)
    with patch("activity.socketio", socketio), patch(
        "activity.get_openai_client_and_model",
        side_effect=lambda *a: (model, "fake-model"),
    ) as get_client, patch(
        "activity.get_activity_content", side_effect=lambda path: eng.content
    ):
        eng.get_client = get_client
        yield eng
    db.session.remove()
    db.drop_all()
    ctx.pop()
    activity.app, activity.db, activity.get_room, activity.SYSTEM_USERS = saved


# ── activities ───────────────────────────────────────────────────────

TWO_QUESTIONS = """
default_max_attempts_per_step: 3
sections:
  - section_id: s1
    title: One
    steps:
      - step_id: q1
        title: Q1
        question: "What is 2+2?"
        tokens_for_ai: "Grade it."
        buckets: [correct, incorrect, wrong, hint, partial_understanding]
        transitions:
          correct:
            content_blocks: ["Great job!"]
            next_section_and_step: "s1:q2"
          incorrect:
            content_blocks: ["Try again!"]
          wrong:
            content_blocks: ["Moving on."]
          hint:
            content_blocks: ["Think of fingers."]
            counts_as_attempt: false
          partial_understanding:
            content_blocks: ["Almost."]
            next_section_and_step: "s1:q1"
      - step_id: q2
        title: Q2
        question: "What is 3+3?"
        buckets: [correct, incorrect]
        transitions:
          correct:
            content_blocks: ["Done."]
          incorrect:
            content_blocks: ["Nope."]
"""


def one_step(transitions, step_extra="", buckets=None, after=""):
    """A single question step q1 whose transitions are given as YAML text,
    followed by a question step q2 (or `after`, a replacement tail)."""
    names = buckets or [
        line.strip()[:-1]
        for line in transitions.splitlines()
        if line.startswith("          ") and not line.startswith("           ")
    ]
    tail = after or """
      - step_id: q2
        title: Q2
        question: "Next?"
        buckets: [correct]
        transitions:
          correct:
            content_blocks: ["ok"]
"""
    return f"""
default_max_attempts_per_step: 3
sections:
  - section_id: s1
    title: One
    steps:
      - step_id: q1
        title: Q1
        question: "Answer?"
        buckets: {json.dumps(names)}
{step_extra}
        transitions:
{transitions}
{tail}
"""


# ── load state & step ────────────────────────────────────────────────


def test_no_activity_state_does_nothing(engine):
    from models import Room

    room = Room(name="engine_room")
    engine.db.session.add(room)
    engine.db.session.commit()
    engine.room = room
    engine.respond("hi")
    assert engine.emits() == []
    assert engine.model.calls == []


def test_unknown_section_emits_error(engine):
    engine.load(TWO_QUESTIONS, section="nowhere")
    engine.respond("4", "correct")
    (msg,) = engine.said()
    assert msg.startswith("Error processing activity response: ")
    assert "StopIteration" in msg
    assert engine.model.calls == []


def test_step_without_question_loops_to_next_question(engine):
    engine.load(
        """
sections:
  - section_id: s1
    title: One
    steps:
      - step_id: intro
        title: Intro
        content_blocks: ["Welcome {{username}}"]
      - step_id: q1
        title: Q1
        question: "Ready?"
        buckets: [yes]
        transitions:
          yes:
            content_blocks: ["ok"]
""",
        step="intro",
    )
    engine.respond("whatever", username="bob")
    assert engine.said() == ["Welcome bob", "Ready?"]
    assert "categorize" not in engine.model.kinds()
    assert engine.state().step_id == "q1"


# ── categorize, buckets & transitions ────────────────────────────────


def test_correct_answer_advances_and_asks_next_question(engine):
    engine.load(TWO_QUESTIONS, attempts=2)
    engine.respond("4", "correct")
    assert engine.said() == ["Category: correct", "Great job!", "What is 3+3?"]
    st = engine.state()
    assert (st.step_id, st.attempts) == ("q2", 0)
    assert engine.stored() == [
        ("System", "Great job!"),
        ("System (Question)", "What is 3+3?"),
    ]
    # The categorize ask carries the step's tokens & bucket list.
    kind, messages = engine.model.calls[0]
    assert kind == "categorize"
    # The step's instructions, then the clarifying bucket every question
    # step gains (activity_grading.py), then our categorizer's own words.
    assert messages[0]["content"].startswith(
        "Grade it.\n\n- asking_clarifying_questions: the learner asks"
    )
    assert "Categorize the following" in messages[0]["content"]
    assert messages[1]["content"] == "Question: What is 2+2?\nResponse: 4\n\nCategory:"


def test_incorrect_stays_and_counts_an_attempt(engine):
    engine.load(TWO_QUESTIONS)
    engine.respond("5", "incorrect")
    assert engine.said() == ["Category: incorrect", "Try again!", "What is 2+2?"]
    st = engine.state()
    assert (st.step_id, st.attempts) == ("q1", 1)


def test_counts_as_attempt_false_keeps_attempts(engine):
    engine.load(TWO_QUESTIONS, attempts=1)
    engine.respond("help", "hint")
    # "hint" is not one of the stay-put buckets & has no next_section_and_step,
    # so it advances like any other bucket; counts_as_attempt only matters
    # when the step is repeated.
    assert engine.state().step_id == "q2"


def test_counts_as_attempt_false_on_stay_bucket(engine):
    engine.load(
        one_step("""          set_language:
            content_blocks: ["noted"]
            counts_as_attempt: false
"""),
        attempts=1,
    )
    engine.respond("in english", "set_language")
    st = engine.state()
    assert (st.step_id, st.attempts) == ("q1", 1)
    assert engine.said()[-1] == "Answer?"


def test_stay_bucket_pointed_at_its_own_step_counts_like_staying(engine):
    # A partial_understanding transition naming its own step is staying
    # (2026-11-11): the answer counts, so max attempts moves it on. As a
    # fresh visit it reset attempts to 0 & a learner could never leave.
    engine.load(TWO_QUESTIONS, attempts=1)
    engine.respond("4ish", "partial_understanding")
    st = engine.state()
    assert (st.step_id, st.attempts) == ("q1", 2)
    assert engine.said() == [
        "Category: partial_understanding",
        "Almost.",
        "What is 2+2?",
    ]
    engine.respond("4ish", "partial_understanding")
    assert engine.state().step_id == "q2"


def test_non_stay_bucket_without_target_advances_in_order(engine):
    engine.load(TWO_QUESTIONS, attempts=1)
    engine.respond("eh", "wrong")
    st = engine.state()
    assert (st.step_id, st.attempts) == ("q2", 0)


def test_max_attempts_exhausted_advances(engine):
    """FIXED: this answer counts before the comparison, so max 3 means three
    answers: a third incorrect one moves on (it once allowed a 4th)."""
    engine.load(TWO_QUESTIONS)
    engine.respond("5", "incorrect")
    engine.respond("5", "incorrect")
    st = engine.state()
    assert (st.step_id, st.attempts) == ("q1", 2)
    engine.respond("5", "incorrect")
    st = engine.state()
    assert (st.step_id, st.attempts) == ("q2", 0)


def test_unrecognized_category_errors_and_keeps_state(engine):
    engine.load(TWO_QUESTIONS, attempts=1)
    engine.respond("4", "banana")
    assert engine.said() == [
        "Category: banana",
        "Error: Unrecognized category 'banana'. Please try again.",
    ]
    st = engine.state()
    assert (st.step_id, st.attempts) == ("q1", 1)


def test_categorize_resolves_wordy_verdict_onto_bucket(engine):
    engine.load(TWO_QUESTIONS)
    engine.respond("4", "Partial Understanding.")
    assert engine.said()[0] == "Category: partial_understanding"


def test_integer_buckets(engine):
    engine.load("""
sections:
  - section_id: s1
    title: One
    steps:
      - step_id: q1
        title: Q1
        question: "Pick"
        buckets: [1, 2]
        transitions:
          1:
            content_blocks: ["one"]
            next_section_and_step: "s1:q2"
          2:
            content_blocks: ["two"]
      - step_id: q2
        title: Q2
        question: "Next?"
        buckets: [a]
        transitions:
          a:
            content_blocks: ["ok"]
""")
    engine.respond("1", "1")
    assert engine.said() == ["Category: 1", "one", "Next?"]


def test_yes_no_buckets_parse_as_booleans(engine):
    """YAML reads `yes:` as True; a "yes" verdict is converted to True to
    find it. FIXED: it is still the first bucket, so no separator (one was
    printed because True != "yes")."""
    engine.load("""
sections:
  - section_id: s1
    title: One
    steps:
      - step_id: q1
        title: Q1
        question: "Ready?"
        buckets: [yes, no]
        transitions:
          yes:
            content_blocks: ["go"]
            next_section_and_step: "s1:q2"
          no:
            content_blocks: ["wait"]
      - step_id: q2
        title: Q2
        question: "Next?"
        buckets: [a]
        transitions:
          a:
            content_blocks: ["ok"]
""")
    engine.respond("sure", "yes")
    said = engine.said()
    assert said == ["Category: yes", "go", "Next?"]


def test_classifier_path_answers_with_confidence(engine):
    engine.load(TWO_QUESTIONS)
    decision = {
        "value": "correct",
        "p": 0.91,
        "dist": {"correct": 0.91, "incorrect": 0.09},
        "confidence": "high",
        "instance": "cls-0",
        "model": "jev-latest",
    }
    with patch("activity._classifier.available", return_value=True), patch(
        "activity._classifier.categorize", return_value=decision
    ) as cat:
        engine.respond("4")
    assert engine.said()[0] == "Category: correct (confidence 91%)"
    assert "categorize" not in engine.model.kinds()
    question, said, buckets, instructions = cat.call_args.args
    assert (question, said) == ("What is 2+2?", "4")
    assert buckets == [
        "correct",
        "incorrect",
        "wrong",
        "hint",
        "partial_understanding",
        "asking_clarifying_questions",
    ]
    assert instructions.startswith("Grade it.\n\n- asking_clarifying_questions:")
    assert engine.state().step_id == "q2"


def test_classifier_error_fails_open_onto_chat(engine):
    import classifier

    engine.load(TWO_QUESTIONS)
    with patch("activity._classifier.available", return_value=True), patch(
        "activity._classifier.categorize",
        side_effect=classifier.ClassifierError("HTTP 503"),
    ):
        engine.respond("4", "correct")
        assert engine.activity.last_readout()["classifier_error"] == "HTTP 503"
    assert engine.said()[0] == "Category: correct"
    assert "categorize" in engine.model.kinds()
    assert engine.state().step_id == "q2"


def test_chat_logprobs_show_confidence(engine):
    engine.load(TWO_QUESTIONS)
    top = [
        SimpleNamespace(token="correct", logprob=-0.105),
        SimpleNamespace(token="incorrect", logprob=-2.3),
    ]
    logprobs = SimpleNamespace(content=[SimpleNamespace(top_logprobs=top)])
    real = engine.model.create

    def create(**kwargs):
        if kwargs.get("logprobs"):
            return _completion("correct", logprobs)
        return real(**kwargs)

    engine.model.chat.completions.create = create
    engine.respond("4")
    assert engine.said()[0].startswith("Category: correct (confidence ")


def test_step_and_activity_model_overrides(engine):
    engine.load("""
classifier_model: "MODEL_7"
feedback_model: "MODEL_8"
sections:
  - section_id: s1
    title: One
    steps:
      - step_id: q1
        title: Q1
        question: "Pick"
        classifier_model: "MODEL_9"
        feedback_tokens_for_ai: "Be kind."
        buckets: [a]
        transitions:
          a:
            ai_feedback:
              tokens_for_ai: "Cheer."
            next_section_and_step: "s1:q1"
""")
    engine.respond("x", "a")
    models_asked = [c.args for c in engine.get_client.call_args_list]
    assert models_asked == [("MODEL_9",), ("MODEL_8",)]


# ── random buckets ───────────────────────────────────────────────────


def test_random_bucket_triggers_and_runs_after_the_verdict(engine):
    engine.load(
        one_step(
            """          correct:
            content_blocks: ["right"]
            metadata_add: {score: "n+1"}
            counts_as_attempt: false
          storm:
            content_blocks: ["A storm!"]
            metadata_add: {storms: "n+1"}
            next_section_and_step: "s1:q2"
""",
            step_extra="""        random_buckets:
          storm: {probability: 0.5}
          calm: {probability: 0.0}""",
            buckets=["correct"],
        )
    )
    with patch("activity.random.random", return_value=0.1):
        engine.respond("x", "correct")
    said = engine.said()
    assert said[0] == "🎲 [RANDOM EVENT] 'storm' triggered!"
    assert said[1] == "Category: correct"
    assert said[2] == "right"
    assert "Processing transition for bucket: 'storm'" in said[3]
    assert said[4:] == ["A storm!", "Next?"]
    st = engine.state()
    assert st.dict_metadata == {"score": 1, "storms": 1}
    assert st.step_id == "q2"


def test_random_bucket_not_triggered(engine):
    engine.load(
        one_step(
            """          correct:
            content_blocks: ["right"]
          storm:
            content_blocks: ["A storm!"]
""",
            step_extra="""        random_buckets:
          storm: {probability: 0.5}""",
            buckets=["correct"],
        )
    )
    with patch("activity.random.random", return_value=0.9):
        engine.respond("x", "correct")
    assert "A storm!" not in engine.said()


# ── metadata operations ──────────────────────────────────────────────


def test_metadata_add_operations(engine):
    engine.load(
        one_step("""          correct:
            metadata_add:
              name: "the-users-response"
              score: "n+5"
              lives: "n-2"
              fresh: "n+1"
              roll: "n+random(3,3)"
              tags: "n+,blue"
              newtags: "n+,red"
              colors: "n-,green"
              empty: "n-,x"
              bad: "n+abc"
              plain: "hello"
              number: 7
              later: "the-llms-response"
            next_section_and_step: "s1:q1"
"""),
        metadata={
            "score": 10,
            "lives": 3,
            "roll": 1,
            "tags": "red",
            "colors": "red,green,blue",
        },
    )
    engine.respond("Ada", "correct")
    assert engine.state().dict_metadata == {
        "score": 15,
        "lives": 1,
        "roll": 4,
        "tags": "red,blue",
        "colors": "red,blue",
        "name": "Ada",
        "fresh": 1,
        "newtags": "red",
        "empty": "",
        "bad": "n+abc",
        "plain": "hello",
        "number": 7,
    }


def test_metadata_tmp_add_lives_for_one_turn(engine):
    engine.load(
        one_step(
            """          correct:
            metadata_tmp_add:
              said: "the-users-response"
              bonus: "n+2"
              tag: "n+,x"
              mood: "happy"
              skip: "the-llms-response"
            ai_feedback:
              tokens_for_ai: "Cheer."
            next_section_and_step: "s1:q1"
""",
            step_extra='        feedback_tokens_for_ai: "Be kind."',
        ),
        metadata={"bonus": 1},
    )
    engine.respond("hey", "correct")
    (fb,) = engine.model.feedback_calls()
    user = fb[1]["content"]
    meta = json.loads(user.split("Metadata: ")[1].split("\n")[0])
    assert meta == {"bonus": 3, "said": "hey", "tag": "x", "mood": "happy"}
    new = json.loads(user.split("New Metadata: ")[1])
    assert new == {"said": "hey", "bonus": 3, "tag": "x", "mood": "happy"}
    # Gone by the end of the turn, including a key it overwrote.
    assert engine.state().dict_metadata == {}


def test_metadata_tmp_add_random_and_concat_paths(engine):
    engine.load(
        one_step(
            """          correct:
            metadata_tmp_add:
              roll: "n+random(2,2)"
              less: "n-1"
              drop: "n-,a"
              none: "n-,a"
              bad: "n-zz"
            next_section_and_step: "s1:q1"
""",
            step_extra='        feedback_tokens_for_ai: "x"',
        ),
        metadata={"roll": 1, "less": 5, "drop": "a,b"},
    )
    with patch("activity.provide_feedback", return_value="") as fb:
        engine.respond("x", "correct")
    meta = json.loads(fb.call_args.args[7])
    assert meta == {"roll": 3, "less": 4, "drop": "b", "none": "", "bad": "n-zz"}
    assert engine.state().dict_metadata == {}


def test_metadata_append_and_tmp_append(engine):
    engine.load(
        one_step("""          correct:
            metadata_append:
              answers: "the-users-response"
              wrapped: "new"
              many: [b, c]
              later: "the-llms-response"
            metadata_tmp_append:
              scratch: "the-users-response"
              tmplist: [y]
              skip: "the-llms-response"
            next_section_and_step: "s1:q1"
"""),
        metadata={"answers": ["first"], "wrapped": "old", "many": ["a"]},
    )
    engine.respond("second", "correct")
    assert engine.state().dict_metadata == {
        "answers": ["first", "second"],
        "wrapped": ["old", "new"],
        "many": ["a", "b", "c"],
    }


def test_metadata_remove_list(engine):
    engine.load(
        one_step("""          correct:
            metadata_remove: [a, missing]
            next_section_and_step: "s1:q1"
"""),
        metadata={"a": 1, "b": 2},
    )
    engine.respond("x", "correct")
    assert engine.state().dict_metadata == {"b": 2}


def test_metadata_remove_takes_one_key_as_a_string(engine):
    """FIXED: `metadata_remove: "ab"` (our SPEC allows one key) removes the
    key "ab", never "a" & "b" letter by letter."""
    engine.load(
        one_step("""          correct:
            metadata_remove: "ab"
            next_section_and_step: "s1:q1"
"""),
        metadata={"ab": 1, "a": 2, "b": 3, "c": 4},
    )
    engine.respond("x", "correct")
    assert engine.state().dict_metadata == {"a": 2, "b": 3, "c": 4}


def test_metadata_random_picks_one_item_of_a_list(engine):
    """FIXED: a key holding a list gets ONE item of it (the dice roll our
    docs describe); the whole list was stored."""
    engine.load(
        one_step(
            """          correct:
            metadata_random:
              dice: [1, 2, 3]
            metadata_tmp_random:
              coin: [heads, tails]
            next_section_and_step: "s1:q1"
""",
            step_extra='        feedback_tokens_for_ai: "x"',
        )
    )
    with patch("activity.provide_feedback", return_value="") as fb:
        engine.respond("x", "correct")
    picked = json.loads(fb.call_args.args[8])
    assert picked["dice"] in (1, 2, 3) and picked["coin"] in ("heads", "tails")
    assert engine.state().dict_metadata == {"dice": picked["dice"]}


def test_metadata_random_chooses_among_keys(engine):
    engine.load(one_step("""          correct:
            metadata_random:
              left: 1
              right: 2
            next_section_and_step: "s1:q1"
"""))
    with patch("activity.random.choice", side_effect=lambda keys: keys[-1]):
        engine.respond("x", "correct")
    assert engine.state().dict_metadata == {"right": 2}


def test_metadata_weighted_random(engine):
    engine.load(
        one_step(
            """          correct:
            metadata_weighted_random:
              loot:
                - {value: gold, weight: 1}
                - {value: dust, weight: 0}
            metadata_tmp_weighted_random:
              weather:
                - {value: rain, weight: 5}
            next_section_and_step: "s1:q1"
""",
            step_extra='        feedback_tokens_for_ai: "x"',
        )
    )
    with patch("activity.provide_feedback", return_value="") as fb:
        engine.respond("x", "correct")
    assert json.loads(fb.call_args.args[8]) == {"loot": "gold", "weather": "rain"}
    assert engine.state().dict_metadata == {"loot": "gold"}


def test_metadata_clear(engine):
    engine.load(
        one_step("""          correct:
            metadata_add: {kept: 1}
            metadata_tmp_add: {tmp: 1}
            metadata_clear: true
            next_section_and_step: "s1:q1"
"""),
        metadata={"old": 1},
    )
    engine.respond("x", "correct")
    assert engine.state().dict_metadata == {}


def test_metadata_clear_false_keeps(engine):
    engine.load(
        one_step("""          correct:
            metadata_clear: false
            next_section_and_step: "s1:q1"
"""),
        metadata={"old": 1},
    )
    engine.respond("x", "correct")
    assert engine.state().dict_metadata == {"old": 1}


def test_metadata_conditions_met_runs_transition(engine):
    engine.load(
        one_step("""          correct:
            metadata_conditions: {score_gte: 2}
            content_blocks: ["unlocked"]
            next_section_and_step: "s1:q2"
"""),
        metadata={"score": 3},
    )
    engine.respond("x", "correct")
    assert engine.said() == ["Category: correct", "unlocked", "Next?"]


def test_metadata_conditions_unmet_asks_again(engine):
    """FIXED: with every transition skipped by its conditions nothing
    applied, so the question is asked again, uncounted (it moved on to the
    next step in order)."""
    engine.load(
        one_step("""          correct:
            metadata_conditions: {score_gte: 2}
            content_blocks: ["unlocked"]
            next_section_and_step: "s1:q1"
"""),
        metadata={"score": 1},
    )
    engine.respond("x", "correct")
    assert engine.said() == [
        "Category: correct",
        "Skipping 'correct' - metadata conditions not met",
        "Answer?",
    ]
    st = engine.state()
    assert (st.step_id, st.attempts) == ("q1", 0)


def test_metadata_conditions_unmet_on_stay_bucket_asks_again(engine):
    """FIXED: every transition skipped on a stay-put bucket asks again in
    the learner's language (it raised UnboundLocalError on
    `user_language`); the attempt is not counted."""
    engine.load(
        one_step("""          incorrect:
            metadata_conditions: {score_gte: 2}
            content_blocks: ["unlocked"]
"""),
        metadata={"score": 1},
    )
    engine.respond("x", "incorrect")
    assert engine.said() == [
        "Category: incorrect",
        "Skipping 'incorrect' - metadata conditions not met",
        "Answer?",
    ]
    assert engine.state().attempts == 0


# ── scripts ──────────────────────────────────────────────────────────


def test_pre_script_sees_response_and_writes_metadata(engine):
    engine.load(
        one_step(
            """          correct:
            next_section_and_step: "s1:q1"
""",
            step_extra="""        pre_script: |
          script_result = {"metadata": {"echo": metadata["user_response"].upper(),
                                        "seen": sorted(metadata)}}""",
        ),
        metadata={"a": 1},
    )
    engine.respond("hi", "correct")
    assert engine.state().dict_metadata == {
        "a": 1,
        "echo": "HI",
        "seen": ["a", "user_response"],
    }


def test_pre_script_without_result(engine):
    engine.load(
        one_step(
            """          correct:
            next_section_and_step: "s1:q1"
""",
            step_extra="""        pre_script: |
          x = 1""",
        ),
        metadata={"a": 1},
    )
    engine.respond("hi", "correct")
    assert engine.state().dict_metadata == {"a": 1}


PROCESSING = """        processing_script: |
          metadata["count"] = metadata.get("count", 0) + 1
          script_result = {"metadata": {"doubled": metadata["count"] * 2},
                           "note": "ran"}"""


def test_processing_script_runs_when_transition_asks(engine):
    engine.load(
        one_step(
            """          correct:
            run_processing_script: true
            next_section_and_step: "s1:q1"
""",
            step_extra=PROCESSING + '\n        feedback_tokens_for_ai: "x"',
        ),
        metadata={"count": 1},
    )
    with patch("activity.provide_feedback", return_value="") as fb:
        engine.respond("x", "correct")
    meta = json.loads(fb.call_args.args[7])
    # FIXED (2026-11-02): metadata is read/write for a script, as SPEC.yaml
    # says, so its own write (count) is kept with script_result["metadata"],
    # plus the whole result as temporary processing_script_result (a
    # script's writes were lost on our server, kept in guarded_ai).
    assert meta == {
        "count": 2,
        "processing_script_result": {"metadata": {"doubled": 4}, "note": "ran"},
        "doubled": 4,
    }
    assert engine.state().dict_metadata == {"count": 2, "doubled": 4}


def test_processing_script_skipped_without_flag(engine):
    engine.load(
        one_step(
            """          correct:
            next_section_and_step: "s1:q1"
""",
            step_extra=PROCESSING,
        ),
        metadata={"count": 1},
    )
    engine.respond("x", "correct")
    assert engine.state().dict_metadata == {"count": 1}


def test_post_script_name_and_navigation_override(engine):
    engine.load(
        one_step(
            """          correct:
            run_post_script: true
""",
            step_extra="""        post_script: |
          script_result = {"next_section_and_step": "s1:q2"}""",
            after="""
      - step_id: q_skipped
        title: Skipped
        question: "Skipped?"
        buckets: [a]
        transitions:
          a: {}
      - step_id: q2
        title: Q2
        question: "Next?"
        buckets: [a]
        transitions:
          a: {}
""",
        )
    )
    engine.respond("x", "correct")
    assert engine.state().step_id == "q2"
    assert engine.said()[-1] == "Next?"


def test_script_target_beats_transition_target(engine):
    """FIXED: a script's next_section_and_step overrides its transition's
    (the transition's overwrote it, so a script only steered a transition
    naming no target)."""
    engine.load(
        one_step(
            """          correct:
            run_processing_script: true
            next_section_and_step: "s1:q1"
""",
            step_extra="""        processing_script: |
          script_result = {"next_section_and_step": "s1:q2"}""",
        )
    )
    engine.respond("x", "correct")
    assert engine.state().step_id == "q2"


def test_processing_script_plot_image_is_posted_as_the_user(engine):
    engine.load(
        one_step(
            """          correct:
            run_processing_script: true
            next_section_and_step: "s1:q1"
""",
            step_extra="""        processing_script: |
          script_result = {"plot_image": "QUJD"}""",
        )
    )
    engine.respond("x", "correct", username="carol")
    img = '<img alt="Plot Image" src="data:image/png;base64,QUJD">'
    assert engine.said_by("carol") == [img]
    assert ("carol", img) in engine.stored()
    # plot_image is popped before the result is kept.
    assert engine.state().dict_metadata == {}


def test_processing_script_plot_image_as_background(engine):
    engine.load(
        one_step(
            """          correct:
            run_processing_script: true
            next_section_and_step: "s1:q1"
""",
            step_extra="""        processing_script: |
          script_result = {"plot_image": "QUJD", "set_background": True}""",
        )
    )
    engine.respond("x", "correct", username="carol")
    assert engine.emits("set_background") == [
        ("set_background", {"image_data": "QUJD"})
    ]
    assert engine.said_by("carol") == []


# ── navigation & termination ─────────────────────────────────────────


def test_conditional_navigation_branches(engine):
    engine.load(
        one_step("""          correct:
            next_section_and_step:
              - if: {score_gte: 5}
                goto: "s1:q2"
              - else: true
                goto: "s1:q1"
"""),
        metadata={"score": 7},
    )
    engine.respond("x", "correct")
    assert engine.state().step_id == "q2"


def test_conditional_navigation_no_match_moves_in_order(engine):
    engine.load(
        one_step("""          incorrect:
            next_section_and_step:
              - if: {score_gte: 5}
                goto: "s1:q1"
"""),
        metadata={"score": 1},
    )
    engine.respond("x", "incorrect")
    st = engine.state()
    assert (st.step_id, st.attempts) == ("q2", 0)


def test_invalid_navigation_target_errors(engine):
    engine.load(one_step("""          correct:
            next_section_and_step: "s1:nowhere"
"""))
    engine.respond("x", "correct")
    assert engine.said()[-1].startswith("Error processing activity response: ")
    assert engine.state().step_id == "q1"


def test_navigation_into_terminal_step_completes_activity(engine):
    engine.load(
        one_step(
            """          correct:
            content_blocks: ["right"]
            metadata_tmp_add: {t: 1}
            next_section_and_step: "end:bye"
""",
            after="""
  - section_id: end
    title: End
    steps:
      - step_id: bye
        title: Bye
        content_blocks: ["Thanks!"]
""",
        )
    )
    engine.respond("x", "correct")
    said = engine.said()
    assert said[:3] == ["Category: correct", "right", "Thanks!"]
    assert said[3].startswith("Activity Info:\nCurrent Section: end\n")
    assert said[3].endswith("GRADE")
    assert said[4] == "Activity completed!"
    assert engine.emits("activity_status") == [("activity_status", {"active": False})]
    assert engine.state() is None


def test_last_question_answered_completes_the_activity(engine):
    """FIXED: a bucket that moves on from the activity's final question
    completes it (nothing happened: no completion, no question)."""
    engine.load(TWO_QUESTIONS, step="q2", attempts=1)
    engine.respond("6", "correct")
    said = engine.said()
    assert said[:2] == ["Category: correct", "Done."]
    assert said[-1] == "Activity completed!"
    assert engine.state() is None


def test_last_transition_target_wins(engine):
    engine.load(
        one_step(
            """          correct:
            next_section_and_step: "s1:q1"
          bonus:
            next_section_and_step: "s1:q2"
""",
            step_extra="""        random_buckets:
          bonus: {probability: 1.0}""",
            buckets=["correct"],
        )
    )
    with patch("activity.random.random", return_value=0.5):
        engine.respond("x", "correct")
    assert engine.state().step_id == "q2"


# ── feedback & language ──────────────────────────────────────────────


def test_ai_feedback_with_feedback_tokens(engine):
    engine.load(
        one_step(
            """          correct:
            content_blocks: ["right"]
            ai_feedback:
              tokens_for_ai: "Cheer."
            metadata_feedback_filter: [keep]
            next_section_and_step: "s1:q2"
""",
            step_extra='        feedback_tokens_for_ai: "Be kind."',
        ),
        metadata={"keep": 1, "drop": 2},
    )
    engine.respond("x", "correct", username="dee")
    assert engine.said() == [
        "Category: correct",
        "right",
        "\n\nFEEDBACK 1",
        "Next?",
    ]
    assert ("System (Feedback)", "\n\nFEEDBACK 1") in engine.stored()
    (fb,) = engine.model.feedback_calls()
    assert fb[0]["content"] == (
        "Be kind. You must provide the feedback in the user's language: "
        "English. Cheer.. Generate a human-readable feedback message based on "
        "the following:"
    )
    assert fb[1]["content"] == (
        "Username: dee\nQuestion: Answer?\nResponse: x\nCategory: correct\n"
        'Metadata: {"keep": 1}\n New Metadata: {}'
    )


def test_feedback_tokens_without_ai_feedback_say_nothing(engine):
    engine.load(
        one_step(
            """          correct:
            next_section_and_step: "s1:q2"
""",
            step_extra='        feedback_tokens_for_ai: "Be kind."',
        )
    )
    engine.respond("x", "correct")
    assert "feedback" not in engine.model.kinds()
    assert engine.said() == ["Category: correct", "Next?"]


def test_ai_feedback_without_feedback_tokens_runs(engine):
    """FIXED: a transition's ai_feedback runs on its own (it needed the
    step's feedback_tokens_for_ai or feedback_prompts beside it)."""
    engine.load(one_step("""          correct:
            ai_feedback:
              tokens_for_ai: "Cheer."
            next_section_and_step: "s1:q2"
"""))
    engine.respond("x", "correct")
    assert "feedback" in engine.model.kinds()
    assert "\n\nFEEDBACK 1" in engine.said()


def test_llms_response_lands_in_metadata(engine):
    engine.load(
        one_step(
            """          correct:
            ai_feedback:
              tokens_for_ai: "Cheer."
            metadata_add:
              last_feedback: "the-llms-response"
            metadata_append:
              history: "the-llms-response"
              plain: "the-llms-response"
            next_section_and_step: "s1:q1"
""",
            step_extra='        feedback_tokens_for_ai: "Be kind."',
        ),
        metadata={"history": ["old"], "plain": "solo"},
    )
    engine.respond("x", "correct")
    assert engine.state().dict_metadata == {
        "last_feedback": "\n\nFEEDBACK 1",
        "history": ["old", "\n\nFEEDBACK 1"],
        "plain": ["solo", "\n\nFEEDBACK 1"],
    }


def test_feedback_prompts_post_one_message_each(engine):
    engine.load(
        one_step(
            """          correct:
            ai_feedback:
              tokens_for_ai: "Cheer."
            next_section_and_step: "s1:q2"
""",
            step_extra="""        feedback_tokens_for_ai: "Legacy."
        feedback_prompts:
          - name: coach
            tokens_for_ai: "Coach."
          - name: judge
            tokens_for_ai: "Judge."
            metadata_filter: [score]""",
        ),
        metadata={"score": 2, "other": 1},
    )
    engine.respond("x", "correct")
    assert engine.said() == [
        "Category: correct",
        "FEEDBACK 1",
        "FEEDBACK 2",
        "Next?",
    ]
    assert ("System (Coach)", "FEEDBACK 1") in engine.stored()
    assert ("System (Judge)", "FEEDBACK 2") in engine.stored()
    coach, judge = engine.model.feedback_calls()
    assert coach[0]["content"].startswith("Legacy. Coach. You must provide")
    assert '"other": 1' in coach[1]["content"]
    assert 'Metadata: {"score": 2}' in judge[1]["content"]
    assert "Response: \n" in judge[1]["content"]


def test_feedback_prompts_with_llms_response_saves_it(engine):
    """FIXED: `the-llms-response` beside feedback_prompts saves the posted
    feedback (it raised UnboundLocalError after the first message)."""
    engine.load(
        one_step(
            """          correct:
            metadata_add: {saved: "the-llms-response"}
            next_section_and_step: "s1:q2"
""",
            step_extra="""        feedback_prompts:
          - name: coach
            tokens_for_ai: "Coach." """,
        )
    )
    engine.respond("x", "correct")
    said = engine.said()
    assert said[1] == "FEEDBACK 1"
    assert not [m for m in said if m.startswith("Error")]
    st = engine.state()
    assert st.step_id == "q2" and st.dict_metadata["saved"] == "FEEDBACK 1"


def test_progressive_hint_follows_a_counted_miss(engine):
    # A hint never undoes the miss before it (2026-11-11): one that did
    # held the learner on hint 1 forever, its attempt never moving.
    engine.load(
        one_step(
            """          incorrect:
            content_blocks: ["no"]
""",
            step_extra="""        hints:
          - attempt: 1
            text: "Hint for try {{current_attempt}}"
          - attempt: 2
            text: "Second hint"
            counts_as_attempt: true""",
        )
    )
    engine.respond("x", "incorrect")
    # Hint 1 follows the first miss & prepares the second try, so its
    # {{current_attempt}} reads 2 (the try the learner is on, from 1).
    assert engine.said() == ["Category: incorrect", "no", "Hint for try 2", "Answer?"]
    assert engine.state().attempts == 1
    assert ("System (Hint)", "Hint for try 2") in engine.stored()


def test_progressive_hint_that_counts(engine):
    engine.load(
        one_step(
            """          incorrect:
            content_blocks: ["no"]
""",
            step_extra="""        hints:
          - attempt: 2
            text: "Second hint"
            counts_as_attempt: true""",
        ),
        attempts=1,
    )
    engine.respond("x", "incorrect")
    assert "Second hint" in engine.said()
    assert engine.state().attempts == 2


def test_no_hint_for_this_attempt(engine):
    engine.load(
        one_step(
            """          incorrect:
            content_blocks: ["no"]
""",
            step_extra="""        hints:
          - attempt: 3
            text: "Later" """,
        )
    )
    engine.respond("x", "incorrect")
    assert engine.said() == ["Category: incorrect", "no", "Answer?"]
    assert engine.state().attempts == 1


def test_language_translates_everything_we_say(engine):
    """Content, hints & the re-asked question are translated; model
    feedback is not (it is asked for in the learner's language instead).
    FIXED: translate_text names the language as written (it sent a Python
    list repr, `['spanish']`); the fake echoes it back."""
    engine.load(
        one_step(
            """          incorrect:
            content_blocks: ["no {{metadata.language}}"]
            ai_feedback:
              tokens_for_ai: "Cheer."
""",
            step_extra="""        feedback_tokens_for_ai: "Be kind."
        hints:
          - attempt: 1
            text: "a hint" """,
        ),
        metadata={"language": "Spanish"},
    )
    engine.respond("x", "incorrect")
    assert engine.said() == [
        "Category: incorrect",
        "[Spanish] no Spanish",
        "\n\nFEEDBACK 1",
        "[Spanish] a hint",
        "[Spanish] Answer?",
    ]
    (fb,) = engine.model.feedback_calls()
    assert "user's language: Spanish." in fb[0]["content"]


def test_set_language_switches_this_turn(engine):
    engine.load(one_step("""          set_language:
            content_blocks: ["Language updated."]
            metadata_add: {language: "the-users-response"}
            counts_as_attempt: false
"""))
    engine.respond("French", "set_language")
    assert engine.said() == [
        "Category: set_language",
        "[French] Language updated.",
        "[French] Answer?",
    ]
    st = engine.state()
    assert st.dict_metadata == {"language": "French"}
    assert st.attempts == 0


def test_conditional_content_blocks(engine):
    engine.load(
        one_step(
            """          correct:
            content_blocks:
              - text: "shown"
                show_if: {vip: true}
              - text: "hidden"
                show_if: {vip: false}
            next_section_and_step: "s1:q2"
""",
        ),
        metadata={"vip": True},
    )
    engine.respond("x", "correct")
    assert engine.said() == ["Category: correct", "shown", "Next?"]


def test_all_content_blocks_filtered_posts_nothing(engine):
    engine.load(
        one_step(
            """          correct:
            content_blocks:
              - text: "hidden"
                show_if: {vip: true}
            next_section_and_step: "s1:q2"
""",
        )
    )
    engine.respond("x", "correct")
    assert engine.said() == ["Category: correct", "Next?"]


def test_empty_transition_is_a_transition(engine):
    """FIXED: a bucket whose transition is `{}` is one that does nothing &
    moves on (it was taken for a missing one: "Unrecognized category")."""
    engine.load(
        one_step(
            """          correct: {}
""",
            buckets=["correct"],
        )
    )
    engine.respond("x", "correct")
    assert engine.said() == ["Category: correct", "Next?"]
    assert engine.state().step_id == "q2"
