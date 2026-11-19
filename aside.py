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


def messages(question, turns, now=None):
    """The one ask an aside makes: our rule, what the work shows, then
    the question."""
    shown = transcript(turns) or "(nothing yet)"
    body = f"The chat so far:\n\n{shown}"
    if now:
        body += f"\n\n---\n\n{now}"
    else:
        body += "\n\n---\n\nNo reply is running in this chat now."
    body += f"\n\n---\n\nAside question: {question}"
    return [
        {"role": "system", "content": ASIDE_SYSTEM},
        {"role": "user", "content": body},
    ]
