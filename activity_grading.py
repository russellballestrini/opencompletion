"""How a question step is graded, beside the categorizing itself:
unyaml's grading primitives (unhomeschool's lessons/SCHEMA.md), shared
by our chat rooms & our terminal runner through activity_engine.py.

- bucket_criteria & required_elements: written into the instructions every
  grader reads (a chat model's prompt, a classifier model's instructions),
  so a missing element is partial, never correct.
- A clarifying bucket on every question step: a learner may always ask, &
  asking never costs an attempt. `allow_clarifying_questions: false` (the
  activity or a step) turns it off, for a game whose answers are moves.
- Multiple choice: an answer's chosen ids are compared with `answer`, no
  model asked; the answer key never leaves this module (`choices_for_page`).

No Flask, database or socket here.
"""

import re

CLARIFYING = "asking_clarifying_questions"
CLARIFYING_BUCKETS = (CLARIFYING, "clarifying", "asking_clarifying_question")
MULTIPLE_CHOICE_TYPES = ("multiple_choice", "multiple-choice", "mc")

# What a step without its own clarifying transition does with one: answer
# the question asked, hint without giving the answer away, ask again.
CLARIFYING_TRANSITION = {
    "counts_as_attempt": False,
    "ai_feedback": {
        "tokens_for_ai": (
            "The learner asked a question instead of answering. Answer it plainly"
            " & briefly, give a hint toward the question if it helps, never the"
            " answer itself, then invite them to answer the question."
        )
    },
}


def is_multiple_choice(step):
    return str(step.get("question_type", "")).lower() in MULTIPLE_CHOICE_TYPES


def clarifying_allowed(content, step):
    if is_multiple_choice(step):
        return False
    if "allow_clarifying_questions" in step:
        return bool(step["allow_clarifying_questions"])
    return bool(content.get("allow_clarifying_questions", True))


def own_clarifying_bucket(step):
    for bucket in step.get("buckets") or []:
        if str(bucket) in CLARIFYING_BUCKETS:
            return bucket
    return None


def buckets_for(content, step):
    """The buckets a step's answer may land in: its own; a multiple-choice
    step's default (correct, incorrect); plus the clarifying bucket unless
    it has one or the activity or step turned it off."""
    if "buckets" in step:
        buckets = list(step["buckets"])
    elif is_multiple_choice(step):
        buckets = ["correct", "incorrect"]
    else:
        buckets = []
    if (
        buckets
        and clarifying_allowed(content, step)
        and own_clarifying_bucket(step) is None
    ):
        buckets.append(CLARIFYING)
    return buckets


def transition_for(step, bucket):
    """A step's transition for a bucket; the built-in one for a clarifying
    bucket the step never wrote. None when there is neither."""
    transitions = step.get("transitions") or {}
    if bucket in transitions:
        return transitions[bucket]
    if bucket == CLARIFYING and own_clarifying_bucket(step) is None:
        return CLARIFYING_TRANSITION
    return None


def grading_instructions(content, step):
    """The step's tokens_for_ai with its criteria, its required elements &
    the clarifying bucket written in, for whichever grader reads them."""
    parts = [step.get("tokens_for_ai") or ""]
    criteria = step.get("bucket_criteria") or {}
    if criteria:
        lines = "\n".join(f"- {bucket}: {text}" for bucket, text in criteria.items())
        parts.append("Category boundaries:\n" + lines)
    elements = step.get("required_elements") or []
    if elements:
        partial = next(
            (b for b in step.get("buckets") or [] if str(b).startswith("partial")),
            None,
        )
        rule = (
            f"Missing any one element = {partial}, never correct."
            if partial
            else "Missing any one element is never correct."
        )
        lines = "\n".join(f"- {element}" for element in elements)
        parts.append(f"A correct answer has every one of these:\n{lines}\n{rule}")
    if CLARIFYING in buckets_for(content, step) and own_clarifying_bucket(step) is None:
        parts.append(
            f"- {CLARIFYING}: the learner asks a question about the question,"
            " or for help, instead of answering it."
        )
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


# ── multiple choice ──────────────────────────────────────────────────


def choices_for_page(step):
    """What a page may show of a multiple-choice step: ids & texts, whether
    several may be picked. Never the answer."""
    return {
        "choices": [
            {"id": str(c["id"]), "text": str(c["text"])}
            for c in step.get("choices") or []
        ],
        "multiple": answer_mode(step) == "all",
    }


def choices_markdown(step):
    """The choices under a question, so typing an id works anywhere (a
    terminal, a page without buttons)."""
    lines = [f"- **{c['id']}**: {c['text']}" for c in choices_for_page(step)["choices"]]
    if answer_mode(step) == "all":
        lines.append("\n_Pick every one that fits (e.g. `a, c`)._")
    return "\n".join(lines)


def answer_mode(step):
    mode = step.get("answer_mode")
    if mode in ("one", "any", "all"):
        return mode
    return "all" if isinstance(step.get("answer"), list) else "one"


def chosen_ids(step, response):
    """The choice ids an answer names: ids ("a", "a, c", "b)"), or a
    choice's whole text ("Carbon dioxide"). Unknown words are dropped."""
    choices = choices_for_page(step)["choices"]
    ids = {c["id"].lower(): c["id"] for c in choices}
    texts = {c["text"].strip().lower(): c["id"] for c in choices}
    said = str(response or "").strip()
    if said.lower() in texts:
        return {texts[said.lower()]}
    picked = set()
    for token in re.split(r"[\s,;/&]+|\band\b", said.lower()):
        token = token.strip(" .)(:-*_`'\"")
        if token in ids:
            picked.add(ids[token])
    return picked


def grade_choice(step, response):
    """The bucket a multiple-choice answer lands in: mc_correct (default
    the first bucket) or mc_incorrect (default `incorrect`). An answer that
    names no choice, or a wrong set, is incorrect, never an error."""
    buckets = step.get("buckets") or ["correct", "incorrect"]
    right = step.get("mc_correct", buckets[0])
    wrong = step.get(
        "mc_incorrect", "incorrect" if "incorrect" in buckets else buckets[-1]
    )
    answer = step.get("answer")
    key = {str(a) for a in (answer if isinstance(answer, list) else [answer])}
    picked = chosen_ids(step, response)
    mode = answer_mode(step)
    if mode == "all":
        ok = picked == key
    else:
        ok = len(picked) == 1 and picked <= key
    return right if ok else wrong
