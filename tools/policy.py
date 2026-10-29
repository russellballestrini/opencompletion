"""Which tools a chat offers, and whose network each one runs on.

Every knob is read at call time, so a test (or vars.sh) changes it
without a restart.

    OPENCOMPLETION_WEB_SEARCH=on|off     keyless web search (default on)
    OPENCOMPLETION_WEB_CRAWL=on|off      fetching a page (default on)
    OPENCOMPLETION_SERVER_EGRESS_USERS=  emails whose tools may run on OUR
                                         network (default: nobody)
    OPENCOMPLETION_TOOL_ROUNDS=4         asks per reply that may call
                                         tools; the next must answer
    OPENCOMPLETION_TOOL_TIMEOUT_S=30     how long a browser has to answer

Our network is never a guest's: a search for anyone not on the egress
list runs in the asker's own browser (tools/browser.py), from their own
address, so what they search is on them. Fetching a page has no browser
path (a page fetched from another origin is blocked by CORS), so it is
offered only to people on the list.
"""

import os

OFF = ("0", "off", "false", "no")


def _flag(name):
    return os.environ.get(name, "on").strip().lower() not in OFF


def web_search_enabled():
    return _flag("OPENCOMPLETION_WEB_SEARCH")


def web_crawl_enabled():
    return _flag("OPENCOMPLETION_WEB_CRAWL")


def egress_users():
    raw = os.environ.get("OPENCOMPLETION_SERVER_EGRESS_USERS", "")
    return {email.strip().lower() for email in raw.split(",") if email.strip()}


def server_egress_allowed(email):
    """True only for a signed-in email on OPENCOMPLETION_SERVER_EGRESS_USERS."""
    return bool(email) and email.strip().lower() in egress_users()


def _positive_int(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def max_rounds():
    return _positive_int("OPENCOMPLETION_TOOL_ROUNDS", 4)


def browser_timeout():
    return _positive_int("OPENCOMPLETION_TOOL_TIMEOUT_S", 30)
