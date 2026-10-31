"""Watching a long delegate (tools/sandbox.py): is it getting anywhere?

A delegate has no clock limit (fox, 2026-10-31: a smart overseer, not a
timer). Each 15s tick, Watch.observe says whether the run moved: a plan
step done, files changed, or activity it had not shown before (numbers
masked, so the same command re-run with new timings is no news). A run
that has not moved for OPENCOMPLETION_DELEGATE_STALL_S is stopped as
stalled. Every OPENCOMPLETION_DELEGATE_REVIEW_S its overseer (the chat's
model, ToolContext.oversee) reads a digest & decides: continue, steer
(a note the run reads at its next turn) or stop.
"""

import re

from tools import policy

# A number with its unit (433, 1.4k, 2.5MB, 8s): measurements, not news.
DIGITS = re.compile(r"\d+(?:[.,]\d+)*[a-zA-Z]{0,2}\b")
DECISION = re.compile(r"^\W*(continue|steer|stop)\b\W*(.*)", re.I | re.S)


def activity_key(line):
    """A log line as news: whitespace & numbers (timings, byte counts)
    masked, so only a different action counts as new."""
    return DIGITS.sub("#", " ".join(str(line).split()))


class Watch:
    """One run's progress, stall & review clocks (monotonic seconds)."""

    def __init__(self, now):
        self.started = now
        self.last_progress = now
        self.next_review = now + policy.delegate_review_s()
        self.seen = set()
        self.plan_done = None
        self.files = None
        self.session_id = None

    def observe(self, vitals, text, now):
        """Take one tick's vitals & log; True when the run moved."""
        vitals = vitals if isinstance(vitals, dict) else {}
        moved = False
        done = (vitals.get("plan") or {}).get("done")
        if isinstance(done, int) and not isinstance(done, bool):
            if self.plan_done is not None and done > self.plan_done:
                moved = True
            self.plan_done = max(done, self.plan_done or 0)
        files = vitals.get("files") or {}
        if isinstance(files, dict) and files:
            shape = (
                files.get("count"),
                tuple(sorted(map(str, files.get("changed") or []))),
            )
            if shape != self.files and (self.files is not None or shape[0]):
                moved = True
            self.files = shape
        for line in str(text or "").splitlines():
            key = activity_key(line)
            if key and key not in self.seen:
                self.seen.add(key)
                moved = True
        if vitals.get("session_id"):
            self.session_id = str(vitals["session_id"])[:128]
        if moved:
            self.last_progress = now
        return moved

    def stalled(self, now):
        stall = policy.delegate_stall_s()
        return bool(stall) and now - self.last_progress > stall

    def review_due(self, now):
        return bool(policy.delegate_review_s()) and now >= self.next_review

    def reviewed(self, now):
        self.next_review = now + policy.delegate_review_s()


def parse_decision(text):
    """("continue" | "steer" | "stop", note) from an overseer's one-line
    answer; anything unreadable is "continue" (a review never stops a run
    by accident)."""
    match = DECISION.match(str(text or "").strip())
    if not match:
        return "continue", ""
    decision, note = match.group(1).lower(), match.group(2).strip()
    if decision in ("steer", "stop") and not note:
        return "continue", ""
    return decision, note[:600]


def minutes(seconds):
    return f"{max(0, round(seconds / 60))}m"
