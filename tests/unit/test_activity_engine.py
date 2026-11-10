"""activity_engine.py: our one activity engine, run by our server
(activity.RoomIO) & a terminal (research/guarded_ai.TerminalIO). Driven
here with MemoryState & a recording io: no Flask, database or socket."""

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import activity_engine  # noqa: E402
from activity_engine import ActivityIO, MemoryState  # noqa: E402


class RecordingIO(ActivityIO):
    """Says into a list; categorizes from a queue of verdicts."""

    def __init__(self, *verdicts):
        self.verdicts = list(verdicts)
        self.said = []
        self.completed = False

    def say(self, content, username="System", shown_as=None):
        self.said.append(content)

    def notice(self, content, pause=None):
        self.said.append(content)

    def categorize(self, question, response, buckets, tokens_for_ai, model):
        return self.verdicts.pop(0)

    def feedback(self, *args):
        return ""

    def run_script(self, metadata, script):
        env = {"metadata": metadata, "script_result": None}
        exec(script, env, env)
        return env["script_result"]

    def complete(self, state, username, feedback_model):
        state.completed = self.completed = True


ACTIVITY = yaml.safe_load("""
sections:
  - section_id: s1
    title: One
    steps:
      - step_id: hello
        title: Hello
        content_blocks: ["Welcome"]
      - step_id: q1
        title: Q1
        question: "A number?"
        buckets: [number, other]
        processing_script: |
          metadata["doubled"] = int(metadata["user_response"]) * 2
          del metadata["scratch"]
          script_result = {"metadata": {"seen": True}}
        transitions:
          number:
            run_processing_script: true
            content_blocks: ["Doubled: {{metadata.doubled}}"]
          other:
            content_blocks: ["A number, please."]
      - step_id: bye
        title: Bye
        content_blocks: ["Bye"]
""")


def start(**metadata):
    state = MemoryState("s1", "hello", metadata=metadata)
    io = RecordingIO()
    activity_engine.show_until_question(ACTIVITY, state, io, "ada")
    return state, io


def test_it_imports_nothing_of_our_server():
    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, activity_engine; "
            "print(sorted(m for m in ('flask', 'flask_socketio', 'sqlalchemy', 'models', 'app') if m in sys.modules))",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert loaded.stdout.strip() == "[]"


def test_content_steps_walk_to_the_first_question():
    state, io = start()
    assert io.said == ["Welcome", "A number?"]
    assert (state.section_id, state.step_id) == ("s1", "q1")


def test_a_post_script_reads_the_answer_and_its_writes_stick():
    """SPEC.yaml: a script's metadata holds user_response & is read/write;
    our server's scripts once lost their own writes."""
    state, io = start(scratch=1)
    io.verdicts = ["number"]
    activity_engine.answer(ACTIVITY, state, "21", "ada", io)
    assert "Doubled: 42" in io.said
    assert state.dict_metadata["doubled"] == 42 and state.dict_metadata["seen"]
    assert "scratch" not in state.dict_metadata
    assert "user_response" not in state.dict_metadata


def test_a_script_changing_a_list_or_dict_in_place_keeps_its_change():
    """A script that appends to a list in metadata wrote nothing: its
    metadata shared that list with our before-copy, so no change showed."""
    activity = yaml.safe_load("""
sections:
  - section_id: s1
    title: One
    steps:
      - step_id: q1
        title: Q1
        question: "A guess?"
        buckets: [guess]
        processing_script: |
          metadata["log"].append(metadata["user_response"])
          metadata["tally"]["guesses"] += 1
        transitions:
          guess:
            run_processing_script: true
            next_section_and_step: "s1:q1"
""")
    state = MemoryState("s1", "q1", metadata={"log": [], "tally": {"guesses": 0}})
    io = RecordingIO("guess", "guess")
    activity_engine.answer(activity, state, "123", "ada", io)
    activity_engine.answer(activity, state, "456", "ada", io)
    assert state.dict_metadata == {"log": ["123", "456"], "tally": {"guesses": 2}}


def test_the_last_step_completes():
    state, io = start(scratch=1)
    io.verdicts = ["number"]
    activity_engine.answer(ACTIVITY, state, "1", "ada", io)
    assert io.said[-1] == "Bye" and io.completed


def test_an_answer_that_stays_asks_again_and_counts():
    activity = json.loads(json.dumps(ACTIVITY))
    activity["sections"][0]["steps"][1]["transitions"]["other"][
        "next_section_and_step"
    ] = "s1:q1"
    state, io = start()
    io.verdicts = ["other"]
    activity_engine.answer(activity, state, "eh", "ada", io)
    assert io.said[-2:] == ["A number, please.", "A number?"]
    assert state.step_id == "q1"
