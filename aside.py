"""`/btw <question>`: an aside about a room's work in progress, as
uncloseai-cli's /btw is (repl.py `_bg_send`, docs/HARNESS-EDITS.md
2026-08-29): a copy of what a running reply knows, its conversation, its
tool rounds so far & the tool running now with its latest progress, asked
one question by the same model, with no tools. Its question & answer are
shown to the whole room & never stored, chained or read by any reply
(a plain message steers a running reply; an aside never does).

Pure: app.py hands in what a reply knows & sends what this builds."""

ASIDE_SYSTEM = (
    "You are answering an aside: someone in this chat asked a quick "
    "question about the work below while it carries on. You cannot run "
    "tools or change anything; answer from what is shown, briefly, & say "
    "plainly when it does not show the answer."
)
# What one aside may read of a room, its newest part kept.
TRANSCRIPT_CHARS = 40000
RESULT_CHARS = 2000
ARGUMENT_CHARS = 300
MAX_TOKENS = 1024
# Messages waiting on a reply, as an aside reads them: the newest few, cut.
WAITING_MAX = 20
WAITING_CHARS = 500


def text_of(content):
    """A message's words: a vision turn's text parts, never its images."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def transcript(turns):
    """Chat turns, tool calls & results among them, as plain text: an
    aside asks with no tools, & some servers refuse tool turns then."""
    lines = []
    for turn in turns:
        role = turn.get("role")
        words = text_of(turn.get("content")).strip()
        if role == "system":
            continue
        if role == "tool":
            if len(words) > RESULT_CHARS:
                words = words[:RESULT_CHARS] + " …"
            lines.append(f"[tool result] {words}")
            continue
        if words:
            lines.append(f"[{'assistant' if role == 'assistant' else 'user'}] {words}")
        for call in turn.get("tool_calls") or []:
            function = call.get("function") or {}
            arguments = str(function.get("arguments") or "")[:ARGUMENT_CHARS]
            lines.append(f"[assistant called {function.get('name')}] {arguments}")
    text = "\n\n".join(lines)
    if len(text) > TRANSCRIPT_CHARS:
        text = "…\n" + text[-TRANSCRIPT_CHARS:]
    return text


def running_now(model, seconds, writing, trail):
    """What a running reply is doing this moment, in words."""
    lines = [f"{model} is still working on this reply ({int(seconds)}s so far)."]
    if writing.strip():
        lines.append(f"It has written this round so far:\n{writing.strip()[-4000:]}")
    running = [entry for entry in trail or [] if entry.get("status") == "running"]
    for entry in running[-1:]:
        about = " ".join(
            str(entry[key]) for key in ("label", "detail") if entry.get(key)
        )
        lines.append(f"Running now: {entry.get('tool')} {about}".rstrip())
        if entry.get("summary"):
            lines.append(f"Its progress summary: {entry['summary']}")
        if entry.get("progress"):
            lines.append(f"Its latest log:\n{str(entry['progress'])[-3000:]}")
    if not running:
        lines.append("No tool is running at this moment.")
    return "\n\n".join(lines)


def waiting(messages):
    """Messages sent to a running reply it has not read yet (steering)."""
    if not messages:
        return ""
    lines = "\n".join(
        f"- {username}: {str(words)[:WAITING_CHARS]}"
        for username, words in messages[-WAITING_MAX:]
    )
    return f"Sent to it while it works, not yet read:\n{lines}"


def room_facts(title, solo, people, message_count):
    """Which chat this is & who is in it now."""
    kind = "a solo chat" if solo else "a public chatroom"
    named = f' "{title}"' if title else ""
    lines = [f"This is{named}, {kind}, holding {message_count} messages."]
    if people:
        lines.append("Here now: " + ", ".join(sorted(people)) + ".")
    return " ".join(lines)


def activity_facts(title, section, step, question, attempts, max_attempts):
    """The activity running in this chat, as its learners see it. Never its
    metadata, grading notes, buckets or scripts: a game's hidden state
    (a vault's code, an enemy fleet) is no aside's to tell."""
    lines = [
        f'An activity is running here: "{title}".',
        f"It is at section {section}, step {step}; "
        f"attempts on this step: {attempts} of {max_attempts}.",
    ]
    if question:
        lines.append(f"Its current question:\n{str(question).strip()[:2000]}")
    lines.append("Its answers & hidden state are not shown to you; never guess them.")
    return "\n".join(lines)


def sandbox_facts(state, created, last_used, has_session, model):
    """This chat's sandbox: an unsandbox service its replies run in."""
    lines = [f"This chat has a sandbox (state: {state}, made {created})."]
    if last_used:
        lines.append(f"It was last used {last_used}.")
    if model:
        lines.append(f"Its delegates answer with {model}.")
    if has_session:
        lines.append("Its delegates share one uncloseai-cli session.")
    return " ".join(lines)


def messages(question, turns, now=None, facts=()):
    """The one ask an aside makes: our rule, what the chat holds (facts:
    the room, its activity & sandbox), what the work shows, then the
    question."""
    shown = transcript(turns) or "(nothing yet)"
    body = "\n\n".join(fact for fact in facts if fact)
    body = (body + "\n\n---\n\n" if body else "") + f"The chat so far:\n\n{shown}"
    if now:
        body += f"\n\n---\n\n{now}"
    else:
        body += "\n\n---\n\nNo reply is running in this chat now."
    body += f"\n\n---\n\nAside question: {question}"
    return [
        {"role": "system", "content": ASIDE_SYSTEM},
        {"role": "user", "content": body},
    ]
