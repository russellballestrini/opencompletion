"""Keyless web search across five vendors' public free tiers.

Standalone, stdlib-only, transferable. Drop this file into any project
(our multiplatform agent, a crawler, a bot) and call ``search()`` — no
API keys, no accounts, no third-party libraries, no MCP client layer.

How it works
------------
Five search vendors operate public anonymous free tiers (the same
endpoints hermes-agent's keyless tier and the opencode CLI default to):

- Exa       https://mcp.exa.ai/mcp            JSON-RPC POST, SSE reply
- Parallel  https://search.parallel.ai/mcp    JSON-RPC POST, JSON reply
- Tavily    https://api.tavily.com/search     keyless header mode
- Firecrawl https://api.firecrawl.dev/v2/search   public cloud, no auth
- Keenable  https://api.keenable.ai/v1/search/public   app-title header

Exa and Parallel expose theirs as "MCP" endpoints, but a tools/call is
just an HTTPS POST with a JSON-RPC 2.0 body — this module speaks that
shape directly with urllib. No MCP protocol layer, no SDK, no client
state, no session handshake.

Requests round-robin across the vendor ring (cursor seeded per process
so a fleet spreads evenly; advances per request so one process rotates
too). A rate-limit-shaped failure fails over to the next ring vendor
until one serves or the ring is exhausted; any other failure stops the
walk (a malformed query fails everywhere). ``served_by`` in the result
names the vendor that actually answered.

Privacy: no user identifiers are ever sent. Parallel's free tier wants
a ``session_id`` for rate limiting; we send a random per-process UUID
(rotates every restart, never persisted). The only identity header is
an app name (module constant ``APP_NAME``) on vendors that require one.

Robots: these are search APIs, not page crawls — the vendors do the
crawling server-side under their own policies. None of the five hosts
publishes a robots.txt (verified 2026-08-20).
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

from tools import mcp_wire as _wire

logger = logging.getLogger(__name__)

# Identifies this app to vendors that want an app name (Keenable requires
# one; others get it as User-Agent). Change when transplanting this module
# into another project.
APP_NAME = 'opencompletion'

EXA_MCP_URL = 'https://mcp.exa.ai/mcp'
PARALLEL_MCP_URL = 'https://search.parallel.ai/mcp'
TAVILY_API_URL = 'https://api.tavily.com'
FIRECRAWL_API_URL = 'https://api.firecrawl.dev'
KEENABLE_API_URL = 'https://api.keenable.ai'

DEFAULT_TIMEOUT = 30

# Rate-limit correlation id for Parallel's free tier — random per process,
# never persisted, derived from nothing.
_SESSION_ID = uuid.uuid4().hex


class KeylessSearchError(RuntimeError):
    """A keyless search call failed (transport, throttle, or vendor error)."""


_RATE_LIMIT_MARKERS = ('rate limit', 'rate-limit', 'ratelimit',
                       'too many requests', '429', 'quota exceeded',
                       'slow down')


def _is_rate_limitish(message):
    """Heuristic: does an error message look like free-tier throttling?"""
    lowered = (message or '').lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


# ── Transport ─────────────────────────────────────────────────────


def _http_json(url, payload=None, headers=None, timeout=DEFAULT_TIMEOUT):
    """POST *payload* as JSON (or GET when None) and return the body text.

    Raises KeylessSearchError on transport failures and non-2xx statuses,
    carrying the status code + body head so throttle detection can see
    a 429. The transport itself is mcp_wire.http_post_json — one urllib
    primitive shared with the HAM client.
    """
    try:
        _status, _headers, body = _wire.http_post_json(
            url, payload, headers=headers, timeout=timeout,
            user_agent=APP_NAME)
    except _wire.JsonRpcError as exc:
        raise KeylessSearchError(str(exc)) from exc
    return body


def _parse_jsonrpc_body(body):
    """Extract the text of a tools/call response.

    Handles plain-JSON bodies and SSE (``data: {...}`` lines) — Exa
    answers as an event stream, Parallel as direct JSON. Raises
    KeylessSearchError for JSON-RPC errors and isError tool results
    (e.g. a free-tier rate-limit message). The parsing lives in
    mcp_wire; this wrapper only owns the exception type the ring
    routes on.
    """
    try:
        text, is_error = _wire.tool_text(_wire.jsonrpc_result(body))
    except _wire.JsonRpcError as exc:
        raise KeylessSearchError(str(exc)) from exc
    if is_error:
        raise KeylessSearchError(text or 'tool call failed')
    if not text:
        raise KeylessSearchError('unrecognized response shape')
    return text


def _jsonrpc_call(url, tool, arguments, timeout=DEFAULT_TIMEOUT):
    """POST a JSON-RPC 2.0 tools/call and return its text payload."""
    payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
               'params': {'name': tool, 'arguments': arguments}}
    return _parse_jsonrpc_body(_http_json(url, payload, timeout=timeout))


# ── Vendors — each returns [{url, title, description, position}] ──


def exa_search(query, limit=5, timeout=DEFAULT_TIMEOUT):
    """Exa keyless search. Reply is formatted text blocks split by ---."""
    text = _jsonrpc_call(EXA_MCP_URL, 'web_search_exa',
                         {'query': query, 'numResults': max(1, int(limit))},
                         timeout=timeout)
    results = []
    for block in text.split('\n---\n'):
        title = ''
        url = ''
        highlight_lines = []
        in_highlights = False
        for line in block.splitlines():
            stripped = line.strip()
            if stripped.startswith('Title:'):
                title = stripped[len('Title:'):].strip()
                in_highlights = False
            elif stripped.startswith('URL:'):
                url = stripped[len('URL:'):].strip()
                in_highlights = False
            elif stripped.startswith('Highlights:'):
                in_highlights = True
            elif stripped.startswith(('Published:', 'Author:')):
                in_highlights = False
            elif in_highlights and stripped:
                highlight_lines.append(stripped)
        if url:
            results.append({'url': url, 'title': title,
                            'description': ' '.join(highlight_lines),
                            'position': len(results) + 1})
        if limit and len(results) >= limit:
            break
    return results


def parallel_search(query, limit=5, timeout=DEFAULT_TIMEOUT):
    """Parallel keyless search. Reply text payload is itself JSON."""
    text = _jsonrpc_call(PARALLEL_MCP_URL, 'web_search',
                         {'objective': query, 'search_queries': [query],
                          'session_id': _SESSION_ID},
                         timeout=timeout)
    data = json.loads(text)
    results = []
    for i, result in enumerate(data.get('results') or []):
        if limit and i >= limit:
            break
        excerpts = result.get('excerpts') or []
        results.append({'url': result.get('url') or '',
                        'title': result.get('title') or '',
                        'description': ' '.join(excerpts),
                        'position': i + 1})
    return results


def tavily_search(query, limit=5, timeout=DEFAULT_TIMEOUT):
    """Tavily keyless search — normal API + X-Tavily-Access-Mode header."""
    body = _http_json(f'{TAVILY_API_URL}/search',
                      {'query': query, 'max_results': max(1, int(limit))},
                      headers={'X-Client-Name': APP_NAME,
                               'X-Tavily-Access-Mode': 'keyless'},
                      timeout=timeout)
    data = json.loads(body)
    results = []
    for i, result in enumerate(data.get('results') or []):
        results.append({'url': result.get('url') or '',
                        'title': result.get('title') or '',
                        'description': result.get('content') or '',
                        'position': i + 1})
    return results


def firecrawl_search(query, limit=5, timeout=DEFAULT_TIMEOUT):
    """Firecrawl public cloud search — no auth header at all."""
    body = _http_json(f'{FIRECRAWL_API_URL}/v2/search',
                      {'query': query, 'limit': max(1, int(limit))},
                      timeout=timeout)
    data = json.loads(body)
    web = (data.get('data') or {}).get('web') or []
    results = []
    for i, result in enumerate(web):
        if limit and i >= limit:
            break
        results.append({'url': result.get('url') or '',
                        'title': result.get('title') or '',
                        'description': result.get('description') or '',
                        'position': i + 1})
    return results


def keenable_search(query, limit=5, timeout=DEFAULT_TIMEOUT):
    """Keenable public search — requires the app-title header."""
    body = _http_json(f'{KEENABLE_API_URL}/v1/search/public',
                      {'query': query, 'max_results': max(1, int(limit))},
                      headers={'X-Keenable-Title': APP_NAME},
                      timeout=timeout)
    data = json.loads(body)
    results = []
    for i, result in enumerate(data.get('results') or []):
        if limit and i >= limit:
            break
        results.append({'url': result.get('url') or '',
                        'title': result.get('title') or '',
                        'description': result.get('snippet')
                        or result.get('description') or '',
                        'position': i + 1})
    return results


# ── Round-robin ring + rate-limit failover ────────────────────────

RING = ('exa', 'parallel', 'tavily', 'firecrawl', 'keenable')

_SEARCHERS = {'exa': exa_search, 'parallel': parallel_search,
              'tavily': tavily_search, 'firecrawl': firecrawl_search,
              'keenable': keenable_search}

# Per-process cursor, seeded by the random session id so a fleet of
# processes spreads evenly across all five free tiers; advances once per
# unpinned request so a single process also rotates.
_ring_lock = threading.Lock()
_ring_cursor = int(_SESSION_ID, 16) % len(RING)


def _ring_order(vendor=None):
    """Vendor walk order. Pinned vendor starts the walk (rotation off);
    unpinned requests start at the cursor and advance it."""
    global _ring_cursor
    if vendor in RING:
        start = RING.index(vendor)
    else:
        with _ring_lock:
            start = _ring_cursor
            _ring_cursor = (_ring_cursor + 1) % len(RING)
    return [RING[(start + i) % len(RING)] for i in range(len(RING))]


def search(query, limit=5, vendor=None, timeout=DEFAULT_TIMEOUT):
    """Keyless web search with ring failover.

    Returns ``{'success': True, 'served_by': <vendor>, 'results': [...]}``
    where each result is ``{url, title, description, position}``, or
    ``{'success': False, 'error': <str>}``. Never raises.

    ``vendor`` pins the walk's entry point (one of ``RING``); otherwise
    the round-robin cursor picks it. Rate-limit-shaped failures advance
    to the next ring vendor; any other failure stops the walk.
    """
    if not query or not str(query).strip():
        return {'success': False, 'error': 'query required'}
    order = _ring_order(vendor)
    last_error = ''
    for i, name in enumerate(order):
        try:
            results = _SEARCHERS[name](str(query), limit=limit, timeout=timeout)
            return {'success': True, 'served_by': name, 'results': results}
        except KeylessSearchError as exc:
            last_error = str(exc)
            if not _is_rate_limitish(last_error):
                return {'success': False, 'served_by': name,
                        'error': f'{name}: {last_error}'}
            nxt = order[i + 1] if i + 1 < len(order) else None
            if nxt:
                logger.info('keyless %s throttled; failing over to %s',
                            name, nxt)
        except Exception as exc:  # noqa: BLE001 — vendor payload drift
            return {'success': False, 'served_by': name,
                    'error': f'{name}: unexpected payload: {exc}'}
    return {'success': False,
            'error': (f'{last_error} (all keyless vendors throttled: '
                      f'{", ".join(order)})')}


if __name__ == '__main__':
    import sys
    q = ' '.join(sys.argv[1:]) or 'permacomputer manifesto'
    print(json.dumps(search(q), indent=2))
