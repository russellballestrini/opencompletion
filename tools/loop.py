"""The tool round of a streamed chat reply.

A model that wants a tool streams `delta.tool_calls` instead of (or
after) its answer: an id & name once, its JSON arguments in pieces.
ToolCallCollector puts those pieces back together; run_round runs every
call & returns the two kinds of message the model reads next: its own
turn asking for the tools, then one `tool` message per call.
app.chat_gpt streams again with those added, until the model answers
without a tool (or OPENCOMPLETION_TOOL_ROUNDS runs out).
"""

import json
import re
import time
from dataclasses import replace

import gevent

# A long call's progress is a log: each report's new lines pushed under the
# old ones (fox, 2026-10-31: a delegate's box was overwritten every poll).
LOG_CHARS = 12000
# A long call's log is summarized in a sentence or two at most this often
# (fox, 2026-10-31: learn how the work goes without reading the log).
SUMMARY_EVERY_S = 15
GAP = "⋯"
# What `unclose --peek` prints that is not the session: its read-only
# banner, quiet heartbeats, join notices & its own stop line.
_PEEK_NOISE = re.compile(r"^\s*(🔒|… quiet\b|├ \[|\(peek stopped)")


def merge_log(log, text):
    """`log` (a list of lines) with the new lines of `text` appended. Each
    report repeats the tail of the last (peek replays its last rows), so
    the longest overlap between log's end & text's start is dropped; no
    overlap with a log already started means rows were missed between
    reports, marked GAP."""
    lines = [
        line.rstrip()
        for line in str(text).splitlines()
        if line.strip() and not _PEEK_NOISE.match(line)
    ]
    if not lines:
        return log
    for k in range(min(len(log), len(lines)), 0, -1):
        if log[-k:] == lines[:k]:
            return log + lines[k:]
    return log + ([GAP] if log else []) + lines


def log_text(log):
    """The log as one string, its newest LOG_CHARS."""
    return "\n".join(log)[-LOG_CHARS:]


def _count(value):
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _seconds(value):
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    return round(float(value), 1) if ok else None


def clip_words(text, limit):
    """`text` on one line, cut at a word boundary to `limit` characters
    with an ellipsis ("...spanning seco" read as a defect, 2026-10-31)."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if text[limit - 1] != " ":  # the cut lands inside a word: drop it
        cut = cut.rsplit(" ", 1)[0] or cut
    return cut.rstrip(" ,;:") + "…"


def shape_vitals(raw):
    """A running agent's vitals (uncloseai-cli's unclose-vitals/1 file),
    cut to what the room is shown: known fields only, numbers as numbers,
    text short. It comes from inside a sandbox, so nothing else passes."""
    if not isinstance(raw, dict) or raw.get("schema") != "unclose-vitals/1":
        return None
    context = raw.get("context") if isinstance(raw.get("context"), dict) else {}
    tools = raw.get("tools") if isinstance(raw.get("tools"), dict) else {}
    activity = raw.get("activity") if isinstance(raw.get("activity"), dict) else {}
    by_name = tools.get("by_name") if isinstance(tools.get("by_name"), dict) else {}
    top = sorted(
        ((str(k)[:40], v) for k, v in by_name.items() if _count(v)),
        key=lambda kv: -kv[1],
    )[:8]
    elapsed = activity.get("elapsed_s")
    shaped = {
        "status": (
            raw.get("status")
            if raw.get("status") in ("working", "done", "error")
            else None
        ),
        "turn": _count(raw.get("turn")),
        "max_turns": _count(raw.get("max_turns")),
        "context": {
            "used": _count(context.get("used")),
            "peak": _count(context.get("peak")),
            "window": _count(context.get("window")),
        },
        "tools": {"total": _count(tools.get("total")), "by_name": dict(top)},
        "activity": {
            "label": (
                clip_words(activity["label"], 80) if activity.get("label") else None
            ),
            "elapsed_s": (
                round(float(elapsed), 1)
                if isinstance(elapsed, (int, float))
                and not isinstance(elapsed, bool)
                and elapsed >= 0
                else None
            ),
        },
        "exit_code": (
            raw.get("exit_code") if isinstance(raw.get("exit_code"), int) else None
        ),
    }
    plan = raw.get("plan") if isinstance(raw.get("plan"), dict) else None
    if plan:
        shaped["plan"] = {
            "done": _count(plan.get("done")),
            "total": _count(plan.get("total")),
            "current": (
                clip_words(plan["current"], 120) if plan.get("current") else None
            ),
        }
    files = raw.get("files") if isinstance(raw.get("files"), dict) else None
    if files:
        changed = files.get("changed") if isinstance(files.get("changed"), list) else []
        shaped["files"] = {
            "changed": [str(f)[:200] for f in changed[:40]],
            "count": _count(files.get("count")),
        }
    llm = raw.get("llm") if isinstance(raw.get("llm"), dict) else None
    if llm:
        shaped["llm"] = {
            "calls": _count(llm.get("calls")),
            "retries": _count(llm.get("retries")),
            "empty": _count(llm.get("empty")),
            "last_error": (
                str(llm["last_error"])[:200] if llm.get("last_error") else None
            ),
        }
    tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else None
    if tokens:
        shaped["tokens"] = {
            "input": _count(tokens.get("input")),
            "output": _count(tokens.get("output")),
        }
    when = raw.get("time") if isinstance(raw.get("time"), dict) else None
    if when:
        shaped["time"] = {
            "elapsed_s": _seconds(when.get("elapsed_s")),
            "budget_s": _seconds(when.get("budget_s")),
        }
    return shaped


class ToolCallCollector:
    def __init__(self):
        self._calls = {}

    def add(self, tool_call_deltas):
        for delta in tool_call_deltas or []:
            index = getattr(delta, "index", None)
            if not isinstance(index, int):
                index = len(self._calls)
            call = self._calls.setdefault(
                index, {"id": "", "name": "", "arguments": ""}
            )
            if isinstance(getattr(delta, "id", None), str) and delta.id:
                call["id"] = delta.id
            function = getattr(delta, "function", None)
            name = getattr(function, "name", None)
            if isinstance(name, str) and name and not call["name"]:
                call["name"] = name
            pieces = getattr(function, "arguments", None)
            if isinstance(pieces, str):
                call["arguments"] += pieces

    @property
    def calls(self):
        """Every named call, in the order the model asked."""
        return [
            dict(call, id=call["id"] or f"call_{index}")
            for index, call in sorted(self._calls.items())
            if call["name"]
        ]


def assistant_turn(content, calls):
    return {
        "role": "assistant",
        "content": content or "",
        "tool_calls": [
            {
                "id": call["id"],
                "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"]},
            }
            for call in calls
        ],
    }


def parse_arguments(raw):
    try:
        arguments = json.loads(raw or "{}")
    except ValueError:
        return None
    return arguments if isinstance(arguments, dict) else None


OUT_OF_ROUNDS = (
    "(You have used all {rounds} tool rounds this reply may take, so no "
    "more tools now. Answer with what you have: say what you did, what "
    'you found & what is left to do. The person can say "continue" & '
    "you will have a fresh set of rounds.)"
)


def out_of_rounds(rounds):
    """The note our last ask carries once a reply's tool rounds are spent,
    so the model ends by reporting instead of stopping mid-task."""
    return {"role": "user", "content": OUT_OF_ROUNDS.format(rounds=rounds)}


def run_round(context, calls, content, notify=lambda event: None):
    """Run each call (tools.run) & return the messages for the next ask.
    notify gets {"tool", "label", "where": browser|server|None, "status":
    running|done|error, ...} as each call starts & ends, for the room."""
    from tools import label_for, offered, run

    messages = [assistant_turn(content, calls)]
    for call in calls:
        log = []
        last = {}  # the newest vitals a long call reported
        arguments = parse_arguments(call["arguments"])
        label = label_for(call["name"], arguments or {})
        where = offered(context).get(call["name"])
        running = {
            "tool": call["name"],
            "label": label,
            "where": where,
            "status": "running",
        }
        # A code tool shows what it is running while it runs.
        shown = detail_of(call["name"], arguments or {}, {})
        if shown:
            running["detail"] = shown
        notify(running)
        if arguments is None:
            result = {"error": "arguments were not a JSON object"}
        else:
            # A long call reports how it is getting on as its running line:
            # a log, new lines pushed under the old, the whole log each time
            # (anyone joining the room mid-run sees all of it).
            def running_event(running=running, log=log, last=last):
                event = dict(running, progress=log_text(log))
                for key in ("vitals", "summary"):
                    if last.get(key):
                        event[key] = last[key]
                return event

            def summarize(fresh, running_event=running_event, last=last):
                try:
                    said = context.summarize(last.get("summary", ""), fresh)
                except Exception as e:  # noqa: BLE001 — a summary is optional
                    print(f"[WARN] delegate summary failed: {e}")
                    said = None
                last["summarizing"] = False
                if said and not last.get("finished"):
                    last["summary"] = str(said).strip()[:400]
                    notify(running_event())

            def progress(text, vitals=None, log=log, last=last):
                log[:] = merge_log(log, text or "")
                shaped = shape_vitals(vitals)
                if shaped:
                    last["vitals"] = shaped
                if log or shaped:
                    notify(running_event())
                # A sentence or two on how it goes, in the background, when
                # the log grew & the last one is SUMMARY_EVERY_S old.
                grew = len(log) > last.get("summarized_upto", 0)
                due = time.monotonic() - last.get("summarized_at", 0) >= SUMMARY_EVERY_S
                if context.summarize and grew and due and not last.get("summarizing"):
                    fresh = "\n".join(log[last.get("summarized_upto", 0) :])[-6000:]
                    last.update(
                        summarizing=True,
                        summarized_upto=len(log),
                        summarized_at=time.monotonic(),
                    )
                    gevent.spawn(summarize, fresh)

            result = run(replace(context, progress=progress), call["name"], arguments)
            last["finished"] = True  # a summary landing late never reopens it
        done = {"tool": call["name"], "label": label, "where": where}
        shown = detail_of(call["name"], arguments or {}, result)
        if shown and arguments is not None and log:
            shown["log"] = log_text(log)  # kept with the reply, in its fold
        if last.get("vitals"):
            done["vitals"] = last["vitals"]  # the run's final numbers
        if shown:
            done["detail"] = shown
        if result.get("error"):
            done.update(status="error", error=str(result["error"])[:200])
        else:
            done.update(status="done", sources=sources_of(result))
        notify(done)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(result, ensure_ascii=False),
            }
        )
    return messages


# What a code tool's line can open to show: its code (or command) & what
# came back, cut so a saved trail stays small.
DETAIL_CODE_CHARS = 6000
DETAIL_OUTPUT_CHARS = 4000
DETAIL_INPUT = {
    "run_code": "code",
    "sandbox_shell": "command",
    "sandbox_write_file": "content",
    "delegate": "task",
}


def detail_of(name, arguments, result):
    """{language, input, stdout, stderr, exit_code} for a code tool, else
    None."""
    key = DETAIL_INPUT.get(name)
    if key is None:
        return None
    language = arguments.get("language") or ("bash" if name == "sandbox_shell" else "")
    if name == "sandbox_write_file":
        language = str(arguments.get("path") or "").rsplit(".", 1)[-1]
    detail = {
        "language": str(language)[:40],
        "input": str(arguments.get(key) or "")[:DETAIL_CODE_CHARS],
    }
    for field in ("stdout", "stderr", "answer"):
        if result.get(field):
            detail[field] = str(result[field])[-DETAIL_OUTPUT_CHARS:]
    if result.get("exit_code") is not None:
        detail["exit_code"] = result["exit_code"]
    return detail


def sources_of(result):
    """The pages a result came from, as [{url, title}] for the room."""
    if result.get("artifacts"):
        return [
            {"url": item["url"], "title": item["filename"]}
            for item in result["artifacts"]
            if item.get("url")
        ]
    if result.get("results"):
        return [
            {"url": item["url"], "title": item.get("title") or item["url"]}
            for item in result["results"]
        ]
    if result.get("url"):
        return [{"url": result["url"], "title": result.get("title") or result["url"]}]
    return []
