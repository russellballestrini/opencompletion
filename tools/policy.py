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
    OPENCOMPLETION_RUN_CODE=on|off       one-off code runs on unsandbox
                                         (default on; guests too unless
                                         OPENCOMPLETION_GUEST_CODE_EXEC=off)
    OPENCOMPLETION_RUN_CODE_TIMEOUT_S=60 a run's time limit
    OPENCOMPLETION_SANDBOX=on|off        a sandbox per chat (egress users)
    OPENCOMPLETION_SANDBOX_IMAGE=        a platform golden image (golden-*);
                                         unset: unsandbox's own default,
                                         unsandbox-ubuntu (with uncloseai-cli)
    OPENCOMPLETION_SANDBOX_IDLE_S=600    idle this long, it is frozen
    OPENCOMPLETION_SANDBOX_TIMEOUT_S=120 one shell command's time limit
    OPENCOMPLETION_DELEGATE=on|off       uncloseai-cli in that sandbox
                                         (needs OPENCOMPLETION_PUBLIC_URL)
    OPENCOMPLETION_DELEGATE_TIMEOUT_S=900
    OPENCOMPLETION_PUBLIC_URL=           our address as a sandbox reaches it,
                                         e.g. https://demo.opencompletion.com

Our network is never a guest's: a search for anyone not on the egress
list runs in the asker's own browser (tools/browser.py), from their own
address, so what they search is on them. Fetching a page has no browser
path (a page fetched from another origin is blocked by CORS), so it is
offered only to people on the list.

unsandbox (tools/sandbox.py) spends this server's unsandbox account &
leaves from unsandbox's own egress, never ours: one-off runs (no network)
for anyone allowed to run code, a sandbox per chat (with network) & the
uncloseai-cli inside it for people on the list.
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


def unsandbox_configured():
    return bool(
        os.environ.get("UNSANDBOX_PUBLIC_KEY")
        and os.environ.get("UNSANDBOX_SECRET_KEY")
    )


def run_code_enabled(signed_in):
    """One-off runs: on with unsandbox keys, for guests only while
    OPENCOMPLETION_GUEST_CODE_EXEC allows them (routes/code.py's rule)."""
    if not (unsandbox_configured() and _flag("OPENCOMPLETION_RUN_CODE")):
        return False
    return (
        signed_in
        or os.environ.get("OPENCOMPLETION_GUEST_CODE_EXEC", "").strip().lower()
        not in OFF
    )


def sandbox_enabled():
    return unsandbox_configured() and _flag("OPENCOMPLETION_SANDBOX")


def public_url():
    return os.environ.get("OPENCOMPLETION_PUBLIC_URL", "").strip().rstrip("/")


def delegate_enabled():
    return (
        sandbox_enabled()
        and _flag("OPENCOMPLETION_DELEGATE")
        and public_url().startswith(("http://", "https://"))
    )


def sandbox_image():
    """A golden image to name, or None: unsandbox then uses its own default
    (CONTAINER_IMAGE, unsandbox-ubuntu, which carries uncloseai-cli). The
    API takes only platform golden images (golden-*) by name."""
    return os.environ.get("OPENCOMPLETION_SANDBOX_IMAGE", "").strip() or None


def run_code_timeout():
    return _positive_int("OPENCOMPLETION_RUN_CODE_TIMEOUT_S", 60)


def sandbox_idle():
    return _positive_int("OPENCOMPLETION_SANDBOX_IDLE_S", 600)


def sandbox_timeout():
    return _positive_int("OPENCOMPLETION_SANDBOX_TIMEOUT_S", 120)


def delegate_timeout():
    return _positive_int("OPENCOMPLETION_DELEGATE_TIMEOUT_S", 900)
