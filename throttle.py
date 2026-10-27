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
from collections import OrderedDict, deque

from flask import request

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
        return chain[-hops]
    return request.remote_addr or "unknown"


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
