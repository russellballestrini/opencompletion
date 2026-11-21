"""Notes a reply's history carries, pure text work: dropping tool notes a
model once copied from its history, & handing a model its unfinished
thinking to answer from. No Flask, database or socket."""

import re

ANSWER_FROM_NOTES = (
    "\n\n(Your thinking ran past its budget, so it was stopped. Your notes "
    "so far:\n\n{notes}\n\nNow, without thinking any further, write your "
    "final answer to the message above, based on these notes.)"
)


def answer_from_notes(chat_history, notes):
    """chat_history with the model's unfinished thinking handed back under
    our last message, asking for the answer it would have written. The
    notes ride on that message, so roles keep alternating for templates
    that need it."""
    history = [dict(message) for message in chat_history]
    ask = ANSWER_FROM_NOTES.format(notes=notes.strip())
    if history and history[-1]["role"] == "user":
        content = history[-1]["content"]
        if isinstance(content, list):  # a vision turn: add a text part
            history[-1]["content"] = content + [{"type": "text", "text": ask}]
        else:
            history[-1]["content"] = f"{content}{ask}"
    else:
        history.append({"role": "user", "content": ask.strip()})
    return history


ECHOED_TOOL_NOTE = re.compile(
    r"\n*^\(tools in (?:this|your last) reply:[^\n]*\)[ \t]*$", re.M
)


def without_echoed_notes(content):
    """A past reply minus "(tools in this reply: ...)" lines. We once put
    that note in a reply's history text & models copied it into their own
    replies; left in, those copies teach the next reply the same habit."""
    if not isinstance(content, str) or "(tools in " not in content:
        return content
    return ECHOED_TOOL_NOTE.sub("", content).rstrip()
