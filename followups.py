"""Follow-up suggestions' pure half: the prompt a model is asked with,
whether they are on, & reading its answer into clean questions. No Flask,
database or socket; app.py's `suggest_followups` asks the model & emits."""

import os
import re

# Follow-up suggestions: after a reply, 2-3 short next questions as chips
# below it. OPENCOMPLETION_FOLLOWUPS=off turns them off (one small extra
# completion per reply, on the model that answered).
FOLLOWUP_PROMPT = (
    "Suggest three short follow-up questions the person might ask next in "
    "this conversation. Write them as they would, in their language. One "
    "per line, each under 12 words, no numbering, nothing else."
)
FOLLOWUP_MAX = 3
FOLLOWUP_CHARS = 120


def followups_enabled():
    return os.environ.get("OPENCOMPLETION_FOLLOWUPS", "on").lower() not in (
        "0",
        "off",
        "false",
        "no",
    )


def parse_followups(text):
    """A model's suggestions, one per line, as up to FOLLOWUP_MAX clean
    questions: list markers & quotes stripped, duplicates & blanks gone."""
    suggestions = []
    for line in (text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip(" \"'“”")
        if line and len(line) <= FOLLOWUP_CHARS and line not in suggestions:
            suggestions.append(line)
        if len(suggestions) == FOLLOWUP_MAX:
            break
    return suggestions
