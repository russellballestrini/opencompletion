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


def run_round(context, calls, content, notify=lambda event: None):
    """Run each call (tools.run) & return the messages for the next ask.
    notify gets {"tool", "label", "where": browser|server|None, "status":
    running|done|error, ...} as each call starts & ends, for the room."""
    from tools import label_for, offered, run

    messages = [assistant_turn(content, calls)]
    for call in calls:
        arguments = parse_arguments(call["arguments"])
        label = label_for(call["name"], arguments or {})
        where = offered(context).get(call["name"])
        notify(
            {"tool": call["name"], "label": label, "where": where, "status": "running"}
        )
        if arguments is None:
            result = {"error": "arguments were not a JSON object"}
        else:
            result = run(context, call["name"], arguments)
        done = {"tool": call["name"], "label": label, "where": where}
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


def sources_of(result):
    """The pages a result came from, as [{url, title}] for the room."""
    if result.get("artifacts"):
        return [
            {"url": item["url"], "title": item["name"]}
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
