"""A small per-client rate limit, kept in memory, for actions a bot could
loop on (forking every message of every chat).

Clients are told apart by IP. Behind our Caddy proxy every request comes
from Caddy, so the client is the address Caddy appended to
X-Forwarded-For: OPENCOMPLETION_PROXY_HOPS counts the proxies we trust in
front of us (default 1; 0 = use the socket address, for a server facing
the internet directly). Entries left of those are the client's own claim
and never trusted, so a bot can't dodge the limit by sending its own
X-Forwarded-For.

One process, one memory: a restart forgets every count, and each worker
of a multi-process server would count on its own. Our server runs one
gevent process, so a limit here is the limit.
"""

import os
import threading
import time
import ipaddress
from collections import OrderedDict, deque

from flask import jsonify, request

# At most this many clients are tracked; the stalest is forgotten first,
# so a flood of addresses can't grow our memory without bound.
MAX_CLIENTS = 10000


def client_ip():
    """This request's client address, per OPENCOMPLETION_PROXY_HOPS."""
    try:
        hops = max(0, int(os.environ.get("OPENCOMPLETION_PROXY_HOPS", "1")))
    except ValueError:
        hops = 1
    chain = [
        part.strip()
        for part in request.headers.get("X-Forwarded-For", "").split(",")
        if part.strip()
    ]
    if hops and len(chain) >= hops:
        return one_client(chain[-hops])
    return one_client(request.remote_addr or "unknown")


def one_client(address):
    """An address as we count it: an IPv6 client is its /64, since one
    connection is handed a whole /64 & could send from a new address in it
    each time. Anything else, or unreadable, as given."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if ip.version == 6 and not ip.ipv4_mapped:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return address


def parse_limit(text, default):
    """'10/600' -> (10, 600): at most 10 actions per 600 seconds. A count
    of 0 (or a bare '0') turns the limit off; anything unreadable falls
    back to default."""
    if str(text).strip() == "0":
        return 0, default[1]
    try:
        count, seconds = (int(part) for part in str(text).split("/"))
        if count >= 0 and seconds > 0:
            return count, seconds
    except (TypeError, ValueError):
        pass
    return default


class Throttle:
    """At most `count` actions per `seconds` for each client key."""

    def __init__(self, env_name, default=(10, 600)):
        self.count, self.seconds = parse_limit(os.environ.get(env_name), default)
        self._hits = OrderedDict()
        self._lock = threading.Lock()

    def hit(self, key, now=None):
        """Record one action for `key`. Returns 0 when it is allowed, else
        how many whole seconds until the oldest counted action expires."""
        if not self.count:
            return 0
        now = time.time() if now is None else now
        with self._lock:
            hits = self._hits.pop(key, None) or deque()
            while hits and hits[0] <= now - self.seconds:
                hits.popleft()
            if len(hits) >= self.count:
                self._hits[key] = hits
                return max(1, int(hits[0] + self.seconds - now + 0.999))
            hits.append(now)
            self._hits[key] = hits
            while len(self._hits) > MAX_CLIENTS:
                self._hits.popitem(last=False)
            return 0

    def reset(self):
        with self._lock:
            self._hits.clear()


def over_limit(throttle, what, key=None, whose="from your address"):
    """Count one of `what` ("new rooms", "code runs") for this request's
    client. None when it is allowed, else (what to tell them, seconds to
    wait). Works in a Socket.IO handler too: flask-socketio hands it the
    request its socket connected with, proxy headers included. `key`
    counts by something else than the address (an email a code goes to),
    with `whose` saying so."""
    retry_after = throttle.hit(client_ip() if key is None else key)
    if not retry_after:
        return None
    minutes = -(-retry_after // 60)
    return (
        f"Too many {what} {whose}: {throttle.count} per "
        f"{throttle.seconds // 60} minutes. Try again in {minutes} minute"
        f"{'' if minutes == 1 else 's'}.",
        retry_after,
    )


def too_many(throttle, what, key=None, whose="from your address"):
    """over_limit() as an HTTP answer: None when allowed, else a 429 with
    Retry-After."""
    over = over_limit(throttle, what, key, whose)
    if not over:
        return None
    response = jsonify({"error": over[0]})
    response.headers["Retry-After"] = str(over[1])
    return response, 429
