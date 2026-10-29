"""Tools a chat model may call. Read docs/TOOLS.md first.

Two today, both opt-out from vars.sh (tools/policy.py):

    web_search   keyless search over five vendors' free tiers
                 (tools/keyless_search.py). Runs in the asker's browser
                 (tools/browser.py + static/js/chat/8-tools.js), or on
                 our server for someone on OPENCOMPLETION_SERVER_EGRESS_USERS.
    web_fetch    read one page (tools/web_fetch.py, the shared crawler in
                 tools/crawler/). Server only, so egress users only.

app.chat_gpt is the only caller: `schemas(context)` names what a reply
may call, `ToolCallCollector` & `run_round` (tools/loop.py) do the rest.
Adding a tool: a schema, a branch in `run`, & a line in `offered`.
"""

from dataclasses import dataclass
from typing import Optional

from tools import browser, policy
from tools.loop import ToolCallCollector, run_round  # noqa: F401
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

SCHEMAS = {"web_search": WEB_SEARCH, "web_fetch": WEB_FETCH}


@dataclass(frozen=True)
class ToolContext:
    """Who asked: their socket (where a browser tool runs) & their sign in
    (whether our network may run one)."""

    sid: Optional[str] = None
    email: Optional[str] = None

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
    return tools


def schemas(context):
    return [SCHEMAS[name] for name in offered(context)]


def label_for(name, arguments):
    return str(arguments.get("query") or arguments.get("url") or name)[:200]


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
    except Exception as e:  # noqa: BLE001 — the model reads why
        return {"error": f"{name} failed: {e}"}
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
