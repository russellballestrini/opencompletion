"""Walk activities with no model: our engine (activity_engine.py) driven by
dice, to find what reading the YAML (activity_yaml_validator.py) cannot.

    python activity_walk.py research/activity*.yaml
    python activity_walk.py --walks 40 --turns 200 research/activity52-vault-heist.yaml

Each walk starts an activity & answers every question: the classifier's
place is taken by a bucket picked at random, the learner's by words one
might type (numbers, tiles, yes/no, a word from the question). Step scripts
really run; feedback & translation are stubs. Reported per file:

- a script or the engine raising (step, bucket, answer, the error);
- text shown to a learner with a template left unrendered (`{{`, `{%`) or
  a raw metadata name in it (`metadata.score` where `{{metadata.score}}`
  shows the value);
- steps no walk reached (a hint, not proof: the dice may have missed);
- how many walks reached the activity's end.

Exit 1 when any file raised. Seeded (`--seed`), so a finding repeats.
"""

import argparse
import contextlib
import io
import random
import re
import sys
import traceback

import yaml

import activity_engine

ANSWERS = [
    "yes",
    "no",
    "help",
    "exit",
    "start",
    "1",
    "2",
    "3",
    "7",
    "42",
    "0",
    "-1",
    "A1",
    "B2",
    "C3",
    "J10",
    "rock",
    "north",
    "random",
    "Spanish",
    "I don't know",
    "",
    "   ",
    "🙂",
    "a b c",
    "3.14",
    "1,2",
    "x" * 300,
]

# A metadata name shown as it is written, not as its value: "metadata.score"
# outside a template (a script's own `metadata.get(...)` is never shown).
RAW_NAME = re.compile(r"(?<![\w.{\[])metadata\.[a-z_]\w*(?!\s*\()")
TEMPLATE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)


class WalkIO(activity_engine.ActivityIO):
    def __init__(self, rng, findings):
        self.rng = rng
        self.findings = findings
        self.bucket = None
        self.where = ""

    def say(self, content, username="System", shown_as=None):
        text = str(content)
        if "{{" in text or "{%" in text:
            self.findings["unrendered"].add((self.where, text[:160]))
        for name in RAW_NAME.findall(TEMPLATE.sub("", text)):
            self.findings["raw_names"].add((self.where, name))

    def notice(self, content, pause=None):
        pass

    def categorize(self, question, response, buckets, tokens_for_ai, model):
        self.bucket = self.rng.choice(list(buckets))
        return str(self.bucket)

    def translate(self, text, language, model):
        return text

    def feedback(self, *args, **kwargs):
        return "(feedback)"

    def feedback_prompts(self, *args, **kwargs):
        return [{"name": "Feedback", "content": "(feedback)"}]

    def run_script(self, metadata, script):
        env = {"__builtins__": __builtins__, "metadata": metadata}
        env["script_result"] = None
        exec(script, env, env)  # our own activity files, as the server runs them
        return env["script_result"]

    def complete(self, state, username, feedback_model):
        state.completed = True


def an_answer(rng, question):
    """Something a learner might type: a stock answer, or a word or number
    lifted from the question itself."""
    words = re.findall(r"[A-Za-z0-9']+", str(question))
    if words and rng.random() < 0.4:
        return rng.choice(words)
    return rng.choice(ANSWERS)


def steps_of(content):
    return {
        (section["section_id"], step["step_id"])
        for section in content["sections"]
        for step in section["steps"]
    }


def walk(content, rng, findings, turns):
    """One walk; True when it reached the activity's end."""
    first = content["sections"][0]
    state = activity_engine.MemoryState(
        first["section_id"],
        first["steps"][0]["step_id"],
        max_attempts=content.get("default_max_attempts_per_step", 3),
        metadata={"language": "English"},
    )
    walker = WalkIO(rng, findings)
    _, feedback_model = activity_engine.models_for(content, "MODEL_1")
    walker.where = f"{state.section_id}:{state.step_id}"
    findings["reached"].add((state.section_id, state.step_id))
    try:
        activity_engine.show_until_question(
            content, state, walker, "Walker", feedback_model
        )
    except Exception as error:
        findings["raised"].add((walker.where, "-", "(start)", describe(error)))
        return False
    for _ in range(turns):
        if state.completed:
            return True
        step = activity_engine.current_step(content, state)
        if not step:
            return True
        here = (state.section_id, state.step_id)
        findings["reached"].add(here)
        said = an_answer(rng, step.get("question", ""))
        walker.where = f"{here[0]}:{here[1]}"
        walker.bucket = None
        try:
            activity_engine.answer(content, state, said, "Walker", walker, "MODEL_1")
        except Exception as error:  # the finding itself
            findings["raised"].add(
                (walker.where, str(walker.bucket), said[:40], describe(error))
            )
            return False
        findings["reached"].add((state.section_id, state.step_id))
    return state.completed


def describe(error):
    last = traceback.extract_tb(error.__traceback__)[-1]
    return f"{type(error).__name__}: {str(error)[:140]} (line {last.lineno})"


def walk_file(path, walks, turns, seed):
    with open(path, encoding="utf-8") as handle:
        content = yaml.safe_load(handle)
    findings = {"raised": set(), "unrendered": set(), "raw_names": set()}
    findings["reached"] = set()
    ended = 0
    for number in range(walks):
        rng = random.Random(f"{seed}:{path}:{number}")
        random.seed(f"{seed}:{path}:{number}")  # the engine's own dice
        with contextlib.redirect_stdout(io.StringIO()):
            ended += bool(walk(content, rng, findings, turns))
    findings["unreached"] = steps_of(content) - findings["reached"]
    findings["ended"] = ended
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="+")
    parser.add_argument("--walks", type=int, default=25)
    parser.add_argument("--turns", type=int, default=150)
    parser.add_argument("--seed", default="walk")
    args = parser.parse_args(argv)

    failed = 0
    for path in args.files:
        try:
            found = walk_file(path, args.walks, args.turns, args.seed)
        except Exception as error:
            print(f"✗ {path}: could not be walked: {describe(error)}")
            failed += 1
            continue
        mark = "✗" if found["raised"] else "✓"
        print(
            f"{mark} {path}: {found['ended']}/{args.walks} walks reached its end, "
            f"{len(found['unreached'])} steps never reached"
        )
        for where, bucket, said, error in sorted(found["raised"]):
            print(f"    raised at {where} [{bucket}] answer {said!r}: {error}")
        for where, text in sorted(found["unrendered"])[:8]:
            print(f"    unrendered template at {where}: {text!r}")
        for where, name in sorted(found["raw_names"])[:8]:
            print(f"    raw name shown at {where}: {name} (say {{{{{name}}}}})")
        for section, step in sorted(found["unreached"])[:12]:
            print(f"    never reached: {section}:{step}")
        failed += bool(found["raised"])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
