"""Catch a model stuck repeating itself mid-stream, so we can hang up.

A reasoning model decoding greedily can loop forever in its thinking
("- Mr. Snuffleupagus?" on line after line) and burn GPU until the
context fills. Our chat stream feeds each delta to a RepetitionGuard (one
per channel: thinking & answer); when it reports a loop, chat_gpt closes
the stream & tells the room why. Where a disconnect reaches vLLM, it
aborts the generation; where it doesn't (some GPU hosts & proxies), the
request's max_tokens (app.chat_sampling) is what stops it.

Two shapes are caught:
- a line that keeps coming back: one line seen `limit` times among the
  last 2 * `limit` lines (lines with no letter or digit, such as `}` or
  `|---|`, never count, so code & tables are left alone);
- a phrase repeating with no newline: the text's tail is one unit of 4 to
  100 characters, repeated until it spans at least 200 characters and
  `limit` copies.

OPENCOMPLETION_REPEAT_LIMIT sets `limit` (default 8; 0 turns it off).
"""

import os
import re
from collections import Counter, deque

DEFAULT_LIMIT = 8
_HAS_WORD = re.compile(r"[^\W_]")
# How much tail text the no-newline check keeps & how often it looks.
_TAIL_CHARS = 4000
_TAIL_EVERY = 256
_UNIT_MIN, _UNIT_MAX, _SPAN_MIN = 4, 100, 200


def repeat_limit():
    """OPENCOMPLETION_REPEAT_LIMIT as an int; 0 means off."""
    try:
        return max(0, int(os.environ.get("OPENCOMPLETION_REPEAT_LIMIT", "")))
    except ValueError:
        return DEFAULT_LIMIT


class RepetitionGuard:
    """Feed streamed text with add(); `looping` names the loop once seen."""

    def __init__(self, limit=None):
        self.limit = repeat_limit() if limit is None else limit
        self.looping = None
        self._partial = ""
        self._lines = deque(maxlen=max(2 * self.limit, 1))
        self._tail = ""
        self._since_tail_check = 0

    def add(self, text):
        """Take one streamed delta; return True once a loop is seen."""
        if not self.limit or self.looping or not text:
            return bool(self.looping)
        self._partial += text
        *done, self._partial = self._partial.split("\n")
        for line in done:
            self._line(line.strip())
        self._tail = (self._tail + text)[-_TAIL_CHARS:]
        self._since_tail_check += len(text)
        if not self.looping and self._since_tail_check >= _TAIL_EVERY:
            self._since_tail_check = 0
            self._check_tail()
        return bool(self.looping)

    def _line(self, line):
        if not _HAS_WORD.search(line):
            return
        self._lines.append(line)
        if Counter(self._lines)[line] >= self.limit:
            self.looping = line

    def _check_tail(self):
        tail = self._tail.rstrip()
        for size in range(_UNIT_MIN, _UNIT_MAX + 1):
            unit = tail[-size:]
            copies = max(self.limit, -(-_SPAN_MIN // size))
            if len(tail) >= size * copies and tail.endswith(unit * copies):
                if _HAS_WORD.search(unit):
                    self.looping = unit.strip()
                    return
