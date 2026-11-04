"""The tool round of a streamed chat reply.

A model that wants a tool streams `delta.tool_calls` instead of (or
after) its answer: an id & name once, its JSON arguments in pieces.
ToolCallCollector puts those pieces back together; run_round runs every
call & returns the two kinds of message the model reads next: its own
turn asking for the tools, then one `tool` message per call.
app.chat_gpt streams again with those added, until the model answers
without a tool (or OPENCOMPLETION_TOOL_ROUNDS runs out).
"""

import hashlib
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
# A tick waits this long for its summary, then shows the rest without it.
SUMMARY_TIMEOUT_S = 8
GAP = "⋯"
# What `unclose --peek` prints that is not the session: its read-only
# banner, quiet heartbeats, join notices & its own stop line.
_PEEK_NOISE = re.compile(r"^\s*(🔒|… quiet\b|├ \[|\(peek stopped)")


def append_log(log, text):
    """`log` with `text`'s lines appended as they are: a cursor peek hands
    over exactly what is new, so nothing overlaps & nothing is missed."""
    return log + [
        line.rstrip()
        for line in str(text).splitlines()
        if line.strip() and not _PEEK_NOISE.match(line)
    ]


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


# A summary is shown whole: a cut "...due to fou" read as broken (fox,
# 2026-10-31). Longer than this it ends at its last full sentence.
SUMMARY_CHARS = 900
SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def whole_sentences(text, limit=SUMMARY_CHARS):
    """`text` on one line, ending at a full sentence: a reply the model's
    token cap cut mid-sentence loses that half sentence, one too long is
    trimmed to its last sentence within `limit`."""
    text = " ".join(str(text).split())
    head = text[:limit]
    ends = [m.end() for m in SENTENCE_END.finditer(head)]
    if ends and (len(text) > limit or not SENTENCE_END.search(text[-1:])):
        return head[: ends[-1]]
    return head if len(text) <= limit else clip_words(text, limit)


def closing_notes(result, log):
    """What a closing summary reads: how a long call ended, the end of its
    log & its final answer."""
    if result.get("error"):
        ended = f"It ended with an error: {result['error']}"
    else:
        ended = f"It finished (exit code {result.get('exit_code', 0)})."
    notes = [ended, "End of its log:", "\n".join(log)[-3000:]]
    if result.get("answer"):
        notes += ["Its final answer:", str(result["answer"])[-2000:]]
    return "\n".join(notes)


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


def run_round(context, calls, content, notify=lambda event: None, resume=None):
    """Run each call (tools.run) & return the messages for the next ask.
    notify gets {"tool", "label", "where": browser|server|None, "status":
    running|done|error, ...} as each call starts & ends, for the room.

    resume (a checkpoint saved before a restart: call_index, done, run)
    picks the round up at the call that was running, with the results the
    calls before it had, reattaching to its run instead of starting it."""
    from tools import label_for, offered, run

    messages = [assistant_turn(content, calls)]
    first = 0
    if resume:
        messages += list(resume.get("done") or [])
        first = int(resume.get("call_index") or 0)
    for index, call in enumerate(calls):
        if index < first:
            continue
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
                event.update(last.get("card") or {})  # session, oversight
                return event

            def summarize(fresh, final=False, last=last):
                """A sentence or two on the newest log, within
                SUMMARY_TIMEOUT_S, or the last one kept. final: the run
                has ended; say what it achieved & how it ended."""
                try:
                    with gevent.Timeout(SUMMARY_TIMEOUT_S):
                        previous = last.get("summary", "")
                        said = (
                            context.summarize(previous, fresh, final=True)
                            if final
                            else context.summarize(previous, fresh)
                        )
                except BaseException as e:  # noqa: BLE001 — optional; Timeout too
                    if isinstance(e, (KeyboardInterrupt, SystemExit)):
                        raise
                    print(f"[WARN] delegate summary skipped: {e or 'timed out'}")
                    said = None
                if said:
                    last["summary"] = whole_sentences(str(said))

            def progress(
                text, vitals=None, card=None, new_only=False, log=log, last=last
            ):
                """One tick: the log, the vitals & (when the log grew) a
                fresh summary are all gathered first, then shown as ONE
                update, so the card changes once a tick, all together
                (fox, 2026-10-31)."""
                log[:] = (append_log if new_only else merge_log)(log, text or "")
                shaped = shape_vitals(vitals)
                if shaped:
                    last["vitals"] = shaped
                if card:
                    last["card"] = dict(card)
                grew = len(log) > last.get("summarized_upto", 0)
                due = time.monotonic() - last.get("summarized_at", 0) >= SUMMARY_EVERY_S
                if context.summarize and grew and due:
                    fresh = "\n".join(log[last.get("summarized_upto", 0) :])[-6000:]
                    last.update(
                        summarized_upto=len(log), summarized_at=time.monotonic()
                    )
                    summarize(fresh)
                if log or shaped:
                    notify(running_event())

            call_context = replace(context, progress=progress)
            if resume and index == first:
                call_context = replace(call_context, resume_run=resume.get("run"))
            if context.checkpoint:
                # The run, plus where it sits in this round: the results of
                # the calls before it, so a resumed round needn't redo them.
                call_context = replace(
                    call_context,
                    checkpoint=lambda run_state, index=index: context.checkpoint(
                        {"call_index": index, "done": messages[1:], "run": run_state}
                    ),
                )
            result = run(call_context, call["name"], arguments)
            if context.summarize and log:
                # A closing summary replaces the last one, written mid-run
                # ("...it is now collecting...") & never kept: the finished
                # card says what the run achieved & how it ended (fox,
                # 2026-11-01).
                summarize(closing_notes(result, log), final=True)
        done = {"tool": call["name"], "label": label, "where": where}
        shown = detail_of(call["name"], arguments or {}, result)
        if shown and arguments is not None and log:
            shown["log"] = log_text(log)  # kept with the reply, in its fold
        if last.get("vitals"):
            done["vitals"] = last["vitals"]  # the run's final numbers
        done.update(last.get("card") or {})  # its session & last review
        done.pop("pulse", None)
        if last.get("summary"):
            # The finished card keeps its running layout, summary included.
            done["summary"] = last["summary"]
        if shown:
            done["detail"] = shown
        if result.get("error"):
            done.update(status="error", error=str(result["error"])[:200])
        else:
            done.update(status="done", sources=sources_of(result))
        # Exactly what the model is handed, & its digest for our chain
        seen = json.dumps(result, ensure_ascii=False)
        done["result_sha256"] = hashlib.sha256(seen.encode("utf-8")).hexdigest()
        notify(done)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": seen,
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


def _arguments_of(entry):
    """A past call's arguments, rebuilt from its trail entry under the
    names its tool takes: its label is its main argument & a code tool's
    detail keeps its input (6000 chars)."""
    detail = entry.get("detail") or {}
    label = str(entry.get("label") or "")
    code = detail.get("input") or ""
    return {
        "web_search": {"query": label},
        "web_fetch": {"url": label},
        "sandbox_shell": {"command": code or label},
        "sandbox_write_file": {"path": label, "content": code},
        "sandbox_share_file": {"path": label},
        "run_code": {"language": detail.get("language") or label, "code": code},
        "delegate": {"task": code or label},
    }.get(entry["tool"], {})


def _result_of(entry):
    """What a past call came back with, as far as its trail kept it."""
    if entry.get("status") == "error":
        return {"error": str(entry.get("error") or "failed")}
    detail = entry.get("detail") or {}
    result = {
        k: detail[k]
        for k in ("stdout", "stderr", "exit_code")
        if detail.get(k) not in (None, "")
    }
    urls = [s.get("url") for s in entry.get("sources") or [] if isinstance(s, dict)]
    if entry["tool"] == "sandbox_share_file" and urls:
        name = str(entry.get("label") or "").rsplit("/", 1)[-1]
        result["markdown"] = f"![{name}]({urls[0]})"
    elif urls:
        result["sources"] = urls[:10]
    return result or {"ok": True}


def reply_turns(content, tool_trail, key):
    """A past reply as it happened, for the history a model reads: its
    words up to each round of calls, those calls (arguments rebuilt from
    the trail), their results, then the words after. Its words alone read
    as a reply narrating work it never called ("Let me pull... The chart
    rendered"), & models copied that, writing results with no call at all
    (fox, 2026-11-04). `key` (the reply's id) keeps call ids unique."""
    if isinstance(tool_trail, str) or tool_trail is None:
        try:
            tool_trail = json.loads(tool_trail or "[]")
        except ValueError:
            tool_trail = []
    rounds = {}
    for entry in tool_trail:
        if isinstance(entry, dict) and entry.get("tool") not in (None, "", "offered"):
            at = entry.get("at")
            # no offset kept (older trails): called before its words
            at = at if isinstance(at, int) and not isinstance(at, bool) else 0
            rounds.setdefault(min(max(at, 0), len(content)), []).append(entry)
    turns, start, index = [], 0, 0
    for at in sorted(rounds):
        calls, results = [], []
        for entry in rounds[at]:
            call_id = f"past_{key}_{index}"
            index += 1
            calls.append(
                {
                    "id": call_id,
                    "name": entry["tool"],
                    "arguments": json.dumps(_arguments_of(entry), ensure_ascii=False),
                }
            )
            results.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(_result_of(entry), ensure_ascii=False)[:4000],
                }
            )
        turns.append(assistant_turn(content[start:at], calls))
        turns.extend(results)
        start = at
    turns.append({"role": "assistant", "content": content[start:]})
    return turns


def trail_lines(tool_trail):
    """What a reply's tools did & how each ended, one line each: "web_fetch
    https://a.org → failed: HTTP 404". `tool_trail` is Message.tool_trail
    (JSON) or its list. For the history a model reads & the transcripts
    people download, so neither drops the evidence behind a reply."""
    if isinstance(tool_trail, str) or tool_trail is None:
        try:
            tool_trail = json.loads(tool_trail or "[]")
        except ValueError:
            return []
    lines = []
    for entry in tool_trail[:12]:
        if not isinstance(entry, dict) or entry.get("tool") in (None, "", "offered"):
            continue
        what = f"{entry['tool']} {entry.get('label') or ''}".strip()[:120]
        if entry.get("status") == "error":
            lines.append(f"{what} → failed: {str(entry.get('error') or '')[:120]}")
        else:
            found = len(entry.get("sources") or [])
            lines.append(f"{what} → ok" + (f", {found} sources" if found else ""))
    return lines
