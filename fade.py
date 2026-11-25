"""The fade: a room's history fitted to its model's context before each ask,
the way uncloseai-cli fades a long chain (its _fade_middle).

Kept whole: the system messages, the room's first prompt & the current turn
(the last message from a person & all after it). The middle fades in steps
until the prompt leaves room for an answer:

1. its tool results become one-line stubs naming the call, so the model knows
   what it had & can call it again; then, still short, its replies' tool
   rounds go whole (calls & stubs), each reply's words carrying one line
   counting what it called, so a reply's own words are the middle's last
   thing to shrink (fox, 2026-11-25: a reply's 280 searches outlived its
   8k stat sheet, which shrank to 400 characters & the next reply had
   forgotten it);
2. its replies & messages shrink to their opening;
3. its oldest turns go, a line on the next kept message saying how many;
4. the current turn's older tool rounds become stubs too (its newest stays).

No model call: it is cheap, deterministic & runs on every ask. fox,
2026-11-04: a reply after ten searches & fetches stopped ~500 tokens in, its
64k context full of page text."""

import json

FADED_RESULT = "[faded: {what} result, {n:,} chars; call it again to see it]"
FADED_TEXT = " …(faded)"
# A middle message's opening kept at step 2.
OPENING_CHARS = 400


def tokens(messages, tools, chars_per_token):
    """Our estimate of a prompt's tokens: its messages & schemas as sent."""
    size = len(json.dumps(messages, ensure_ascii=False))
    size += len(json.dumps(tools or [], ensure_ascii=False))
    return int(size / max(chars_per_token, 1.0))


def _calls_by_id(messages):
    """tool_call id -> "name argument" for every call in `messages`."""
    named = {}
    for m in messages:
        for call in m.get("tool_calls") or []:
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except ValueError:
                arguments = {}
            about = next(
                (
                    str(arguments[k]).splitlines()[0][:120]
                    for k in ("url", "query", "path", "command", "language", "task")
                    if isinstance(arguments, dict) and arguments.get(k)
                ),
                "",
            )
            named[call.get("id")] = f"{function.get('name', 'tool')} {about}".strip()
    return named


def _stub_result(messages, i, named):
    text = str(messages[i].get("content") or "")
    if text.startswith("[faded:"):
        return 0
    what = named.get(messages[i].get("tool_call_id"), "tool")
    marker = FADED_RESULT.format(what=what, n=len(text))
    if len(marker) >= len(text):
        return 0
    messages[i] = dict(messages[i], content=marker)
    return len(text) - len(marker)


FADED_ROUNDS = "[faded: this reply's {n:,} tool calls ({names})]\n\n"


def _drop_round(messages, i):
    """Remove the tool round at i (an assistant's calls & the results right
    after it); the next words of that reply (before the next person) carry
    what was dropped, as `_faded` counts. Returns the messages removed."""
    ids = {c.get("id") for c in messages[i].get("tool_calls") or []}
    names = [
        (c.get("function") or {}).get("name", "tool")
        for c in messages[i].get("tool_calls") or []
    ]
    j = i + 1
    while (
        j < len(messages)
        and messages[j].get("role") == "tool"
        and messages[j].get("tool_call_id") in ids
    ):
        j += 1
    words = str(messages[i].get("content") or "")
    del messages[i:j]
    k = i
    while k < len(messages) and messages[k].get("role") != "user":
        target = messages[k]
        if target.get("role") == "assistant" and not target.get("tool_calls"):
            counts = target.setdefault("_faded", {})
            for name in names:
                counts[name] = counts.get(name, 0) + 1
            if words.strip():  # words said beside the calls stay, before
                target["content"] = words + "\n\n" + str(target.get("content") or "")
            break
        k += 1
    return j - i


def _note_dropped_rounds(messages):
    for m in messages:
        counts = m.pop("_faded", None)
        if counts and isinstance(m.get("content"), str):
            names = ", ".join(
                f"{name} ×{n}" if n > 1 else name
                for name, n in sorted(counts.items(), key=lambda kv: -kv[1])
            )
            m["content"] = (
                FADED_ROUNDS.format(n=sum(counts.values()), names=names) + m["content"]
            )


def _shrink_text(messages, i):
    content = messages[i].get("content")
    if not isinstance(content, str) or len(content) <= OPENING_CHARS + len(FADED_TEXT):
        return 0
    if content.endswith(FADED_TEXT):
        return 0
    messages[i] = dict(messages[i], content=content[:OPENING_CHARS] + FADED_TEXT)
    return len(content) - OPENING_CHARS - len(FADED_TEXT)


def fit(messages, tools, budget, chars_per_token):
    """`messages` faded until our estimate of the prompt is within `budget`
    tokens, or as far as it can go. Returns (messages, steps taken); the
    list given is never changed."""
    messages = [dict(m) for m in messages]

    def over():
        return tokens(messages, tools, chars_per_token) > budget

    if not over():
        return messages, 0
    people = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if not people:
        return messages, 0
    current = people[-1]
    # Head: system messages & the room's first prompt (when older than now)
    head = next((i for i, m in enumerate(messages) if m.get("role") != "system"), 0)
    first_prompt = people[0] if people[0] < current else None
    middle = [i for i in range(head, current) if i != first_prompt]
    named = _calls_by_id(messages)
    steps = 0
    # 1. middle tool results, oldest first
    for i in middle:
        if not over():
            return messages, steps
        if messages[i].get("role") == "tool" and _stub_result(messages, i, named):
            steps = max(steps, 1)
    # 1b. middle tool rounds go whole, oldest first, before any words shrink
    i = head
    while over() and i < current:
        if i != first_prompt and messages[i].get("tool_calls"):
            removed = _drop_round(messages, i)
            current -= removed
            if first_prompt is not None and first_prompt > i:
                first_prompt -= removed
            steps = max(steps, 1)
            continue
        i += 1
    _note_dropped_rounds(messages)
    middle = [i for i in range(head, current) if i != first_prompt]
    # 2. middle replies & messages to their opening, oldest first
    for i in middle:
        if not over():
            return messages, steps
        if messages[i].get("role") in ("assistant", "user") and _shrink_text(
            messages, i
        ):
            steps = max(steps, 2)
    # 3. the oldest middle turns go: from just after the head up to the next
    # message from a person, so no call is parted from its result
    dropped = 0
    keep_upto = first_prompt + 1 if first_prompt is not None else head
    while over():
        people = [i for i, m in enumerate(messages) if m.get("role") == "user"]
        current = people[-1]
        if keep_upto >= current:
            break
        nxt = next((i for i in people if i > keep_upto), current)
        del messages[keep_upto:nxt]
        dropped += nxt - keep_upto
        steps = max(steps, 3)
    if dropped:
        people = [i for i, m in enumerate(messages) if m.get("role") == "user"]
        after = next(
            (i for i in people if first_prompt is None or i > first_prompt), None
        )
        if after is not None and isinstance(messages[after].get("content"), str):
            messages[after]["content"] = (
                f"({dropped} earlier messages of this conversation were faded to "
                f"fit the model's context.)\n\n{messages[after]['content']}"
            )
    # 4. the current turn's older tool rounds (its newest round stays whole)
    if over():
        people = [i for i, m in enumerate(messages) if m.get("role") == "user"]
        current = people[-1]
        rounds = [
            i for i in range(current, len(messages)) if messages[i].get("tool_calls")
        ]
        newest = rounds[-1] if rounds else len(messages)
        for i in range(current, newest):
            if not over():
                break
            if messages[i].get("role") == "tool" and _stub_result(messages, i, named):
                steps = max(steps, 4)
    return messages, steps
