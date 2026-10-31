"""Tools a chat model may call. Read docs/TOOLS.md first.

Each is opt-out from vars.sh (tools/policy.py):

    web_search   keyless search over five vendors' free tiers
                 (tools/keyless_search.py). Runs in the asker's browser
                 (tools/browser.py + static/js/chat/8-tools.js), or on
                 our server for someone on OPENCOMPLETION_SERVER_EGRESS_USERS.
    web_fetch    read one page (tools/web_fetch.py, the shared crawler in
                 tools/crawler/). Server only, so egress users only.
    run_code     one-off code on unsandbox, no network (tools/sandbox.py):
                 anyone allowed to run code.
    sandbox_shell, sandbox_write_file, delegate
                 this chat's own unsandbox sandbox & the uncloseai-cli in
                 it (tools/sandbox.py, routes/sandbox_proxy.py): egress
                 users only.

app.chat_gpt is the only caller: `schemas(context)` names what a reply
may call, `ToolCallCollector` & `run_round` (tools/loop.py) do the rest.
Adding a tool: a schema, a branch in `run`, & a line in `offered`.
"""

from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from tools import browser, policy
from tools.loop import ToolCallCollector, out_of_rounds, run_round  # noqa: F401
from tools.policy import max_rounds  # noqa: F401

register = browser.register
abandon = browser.abandon

SEARCH_LIMIT = 5
SNIPPET_CHARS = 600

WEB_SEARCH = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web. Use it for anything recent, anything you are "
            "unsure of, or when the person asks you to look something up. "
            "Returns pages (url, title, snippet); cite the urls you use."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for"},
            },
            "required": ["query"],
        },
    },
}

WEB_FETCH = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": (
            "Read one web page as text, for when a search snippet is not "
            "enough or the person gives you a link."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "An http(s) address"},
            },
            "required": ["url"],
        },
    },
}

RUN_CODE = {
    "type": "function",
    "function": {
        "name": "run_code",
        "description": (
            "Run a program once in a fresh sandbox (no network) & read its "
            "output: for calculations, checking code, or making a file "
            "(an image, a CSV). Only files saved under /tmp/artifacts/ come "
            "back (e.g. plt.savefig('/tmp/artifacts/plot.png')); each comes "
            "back with a url, show an image with ![name](url). Never link a "
            "file that did not come back."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "language": {"type": "string", "description": "e.g. python, bash, c"},
                "code": {"type": "string", "description": "The whole program"},
                "os": {
                    "type": "string",
                    "enum": ["linux", "freebsd"],
                    "description": "linux (default) or freebsd",
                },
            },
            "required": ["language", "code"],
        },
    },
}

SANDBOX_SHELL = {
    "type": "function",
    "function": {
        "name": "sandbox_shell",
        "description": (
            "Run a shell command in this chat's own Ubuntu sandbox, which "
            "keeps its files & installs between turns & has network (git "
            "clone, pip install). Returns stdout, stderr & the exit code."
        ),
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}

SANDBOX_WRITE_FILE = {
    "type": "function",
    "function": {
        "name": "sandbox_write_file",
        "description": "Write a text file in this chat's sandbox (made or replaced).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "e.g. /root/work/main.py"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
}

DELEGATE = {
    "type": "function",
    "function": {
        "name": "delegate",
        "description": (
            "Hand a multi-step coding task to uncloseai-cli, an agent that "
            "works in this chat's sandbox (reads & writes files, runs "
            "commands) until done, & returns its final answer. For work "
            "too long for a few shell commands. Say exactly what done means."
        ),
        "parameters": {
            "type": "object",
            "properties": {"task": {"type": "string"}},
            "required": ["task"],
        },
    },
}

SCHEMAS = {
    "web_search": WEB_SEARCH,
    "web_fetch": WEB_FETCH,
    "run_code": RUN_CODE,
    "sandbox_shell": SANDBOX_SHELL,
    "sandbox_write_file": SANDBOX_WRITE_FILE,
    "delegate": DELEGATE,
}


@dataclass(frozen=True)
class ToolContext:
    """Who asked: their socket (where a browser tool runs) & their sign in
    (whether our network may run one); chat_gpt adds the chat (a sandbox
    & attachments belong to it) & the model answering."""

    sid: Optional[str] = None
    email: Optional[str] = None
    room_id: Optional[int] = None
    model: Optional[str] = None
    # Set by chat_gpt & run_round for long tools (delegate): stopped() is
    # true once the room's Stop was pressed; progress(text) shows how a
    # running call is getting on, under its line.
    stopped: Optional[Callable[[], bool]] = field(default=None, compare=False)
    progress: Optional[Callable[[str], None]] = field(default=None, compare=False)
    # Set by chat_gpt: summarize(previous, new_log) -> a sentence or two on
    # how a long call is going (tools/loop.py), or None.
    summarize: Optional[Callable[[str, str], Optional[str]]] = field(
        default=None, compare=False
    )

    @property
    def server_egress(self):
        return policy.server_egress_allowed(self.email)


def context(sid, user=None):
    return ToolContext(sid=sid, email=getattr(user, "email", None))


def offered(context):
    """{tool name: "server" | "browser"}: what a reply to `context` may
    call, & where each runs."""
    if context is None:
        return {}
    tools = {}
    if policy.web_search_enabled():
        if context.server_egress:
            tools["web_search"] = "server"
        elif context.sid:
            tools["web_search"] = "browser"
    if policy.web_crawl_enabled() and context.server_egress:
        tools["web_fetch"] = "server"
    if policy.run_code_enabled(signed_in=bool(context.email)):
        tools["run_code"] = "unsandbox"
    if policy.sandbox_enabled() and context.server_egress:
        tools["sandbox_shell"] = tools["sandbox_write_file"] = "sandbox"
        if policy.delegate_enabled() and context.model:
            tools["delegate"] = "sandbox"
    return tools


def schemas(context):
    return [SCHEMAS[name] for name in offered(context)]


def label_for(name, arguments):
    if name == "run_code":
        label = arguments.get("language") or "code"
        if arguments.get("os") == "freebsd":
            label += " on FreeBSD"
        return str(label)[:200]
    if arguments.get("task"):
        # A delegate's task is paragraphs; its line names it in a glance and
        # the whole task stays in the line's fold.
        first = str(arguments["task"]).strip().splitlines()[0]
        return first if len(first) <= 80 else first[:79].rstrip() + "…"
    for key in ("query", "url", "command", "path"):
        if arguments.get(key):
            return str(arguments[key]).splitlines()[0][:200]
    return name


def run(context, name, arguments):
    """Run one call where `offered` says it runs. A dict for the model,
    {"error"} included; never raises. A tool not offered (a model making
    one up, or a flag turned off mid-reply) is refused."""
    where = offered(context).get(name)
    if where is None:
        return {"error": f"{name} is not available here"}
    try:
        if name == "web_search":
            query = str(arguments.get("query") or "").strip()
            if not query:
                return {"error": "query required"}
            if where == "server":
                from tools import keyless_search

                found = keyless_search.search(query, limit=SEARCH_LIMIT)
            else:
                found = browser.ask(
                    context.sid,
                    "web_search",
                    {"query": query, "limit": SEARCH_LIMIT},
                    policy.browser_timeout(),
                )
            return shape_search(found)
        if name == "web_fetch":
            from tools import web_fetch

            return web_fetch.fetch_page(arguments.get("url"))
        if where in ("unsandbox", "sandbox"):
            from tools import sandbox

            if name == "run_code":
                return sandbox.run_code(
                    context,
                    arguments.get("language"),
                    arguments.get("code"),
                    arguments.get("os") or "linux",
                )
            if name == "sandbox_shell":
                return sandbox.shell(context, arguments.get("command"))
            if name == "sandbox_write_file":
                return sandbox.write_file(
                    context, arguments.get("path"), arguments.get("content")
                )
            if name == "delegate":
                return sandbox.delegate(context, arguments.get("task"))
    except Exception as e:  # noqa: BLE001 — the model reads why
        return {"error": f"{name} failed: {e}{_api_says(e)}"}
    return {"error": f"{name} is not available here"}


def shape_search(found):
    """A search answer, from our server or a browser we don't trust, cut
    to what a model needs: up to SEARCH_LIMIT results of string url,
    title & a snippet of SNIPPET_CHARS."""
    if not isinstance(found, dict):
        return {"error": "no result"}
    if found.get("error") or found.get("success") is False:
        return {"error": str(found.get("error") or "search failed")[:500]}
    results = []
    for item in found.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if not url.startswith(("http://", "https://")):
            continue
        results.append(
            {
                "url": url[:2000],
                "title": str(item.get("title") or "")[:300],
                "snippet": str(item.get("description") or "")[:SNIPPET_CHARS],
            }
        )
        if len(results) == SEARCH_LIMIT:
            break
    shaped = {"results": results}
    if isinstance(found.get("served_by"), str):
        shaped["served_by"] = found["served_by"][:40]
    return shaped


def _api_says(error):
    """What an HTTP API said with its error (unsandbox explains a 400 in
    its body), cut short; "" when there is no body."""
    response = getattr(error, "response", None)
    text = getattr(response, "text", "") or ""
    try:
        import json

        body = json.loads(text)
        text = body.get("error") or body.get("message") or text
    except (ValueError, AttributeError):
        pass
    text = str(text).strip()
    return f" ({text[:300]})" if text else ""


GUIDANCE = (
    "Today is {today}. You can call these tools: {names}. Use them to do "
    "things, never imagine their results: when the person asks you to "
    "run, test, build, install, clone, fetch, search or check something, "
    "call the tool that does it. Never say code ran, passed, built or "
    "works unless a tool result in this conversation shows it; if you "
    "only wrote code, say it is untested. If the person asks for a tool "
    "that is not in this list, say it is not available here."
)
GUIDANCE_DELEGATE = (
    " For a multi-step coding task (change a repo until its tests pass), "
    "call delegate with the whole task rather than many shell commands."
)


def guidance(context):
    """The system message a reply that may call tools starts with, or None
    when it is offered none."""
    import time

    names = list(offered(context))
    if not names:
        return None
    text = GUIDANCE.format(today=time.strftime("%Y-%m-%d"), names=", ".join(names))
    if "delegate" in names:
        text += GUIDANCE_DELEGATE
    return {"role": "system", "content": text}


def is_guidance(message):
    """True for the note `guidance` makes, so a server refusing tools gets
    the ask without it (a model must not be told of tools it lacks)."""
    return (
        isinstance(message, dict)
        and message.get("role") == "system"
        and str(message.get("content", "")).startswith("Today is ")
        and "You can call these tools:" in str(message.get("content", ""))
    )


def named_tool(context, text):
    """The tool a message asks for by name ("use the delegate tool", "call
    run_code", "delegate: ..."), when it is offered, else None. chat_gpt
    then forces that call on the reply's first ask (tool_choice): a model
    asked by name must call it, never write its result itself."""
    import re

    if not isinstance(text, str):
        return None
    lowered = text.strip().lower()
    for name in offered(context):
        spoken = re.escape(name).replace("_", "[_ ]")
        if (
            re.search(rf"\b(use|call|run)\s+(the\s+)?{spoken}\b", lowered)
            or re.search(rf"\b{spoken}\s+tool\b", lowered)
            or lowered.startswith(f"{name}:")
        ):
            return name
    return None
