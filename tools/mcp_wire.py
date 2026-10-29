# This is free software for the public good of a permacomputer hosted at
# permacomputer.com, an always-on computer by the people, for the people.
# One which is durable, easy to repair, & distributed like tap water
# for machine learning intelligence.
"""JSON-RPC 2.0 over HTTPS POST, with the SSE reply shape — nothing more.

"MCP over Streamable HTTP" is an HTTPS POST carrying a JSON-RPC body,
answered as plain JSON or as an SSE event stream whose ``data:`` lines
carry the same JSON. This module speaks that wire shape with urllib
and stops there: no protocol layer, no SDK, no session state, no
discovery, no OAuth. Two callers share it — ``keyless_search.py``
(Exa and Parallel answer ``tools/call`` this way, no auth) and
``ham_client.py`` (one bearer header on top). Lifted out of
keyless_search on 2026-09-12 so the SSE parser exists once.

Stdlib-only and transferable: drop it next to any script that needs
to POST a ``tools/call``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 30


class JsonRpcError(RuntimeError):
    """Transport failure, non-2xx status, JSON-RPC ``error`` member, or
    an unrecognizable body. ``http_status`` is set for the HTTP case so
    a caller can tell a 429 from a 401 without parsing the message."""

    def __init__(self, message, http_status=None):
        super().__init__(message)
        self.http_status = http_status


def http_post_json(url, payload, headers=None, timeout=DEFAULT_TIMEOUT,
                   user_agent='unclose'):
    """POST *payload* as JSON (GET when None). Returns
    ``(status, response_headers, body_text)``.

    Raises JsonRpcError on transport failure and non-2xx statuses,
    carrying the status code and the body head. Request headers are
    never echoed into the error — a bearer token must not travel into
    a tool result or a log line.
    """
    merged = {'User-Agent': user_agent,
              'Accept': 'application/json, text/event-stream'}
    merged.update(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        merged['Content-Type'] = 'application/json'
    request = urllib.request.Request(url, data=data, headers=merged)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode('utf-8', errors='replace')
            # Tolerant of minimal fakes: the fields a test double omits
            # read as a plain 200 with no headers.
            status = getattr(response, 'status', None) or 200
            hdrs = getattr(response, 'headers', None)
            return status, dict(hdrs.items()) if hdrs else {}, body
    except urllib.error.HTTPError as exc:
        body = ''
        try:
            body = exc.read().decode('utf-8', errors='replace')[:300]
        except Exception:
            pass
        raise JsonRpcError(f'HTTP {exc.code}: {body}',
                           http_status=exc.code) from exc
    except Exception as exc:
        raise JsonRpcError(f'request failed: {exc}') from exc


def _sse_payloads(body):
    """Yield candidate ``data`` payloads of the SSE events in *body*.

    Each ``data:`` line is yielded on its own first — the shape every
    stream observed so far uses, one JSON object per line, and the one
    a proxy that drops blank lines between events still preserves.
    Consecutive ``data:`` lines of one blank-line-delimited event are
    then yielded joined with a newline, which is what the SSE spec
    means by a multi-line payload. A caller that parses JSON skips
    whichever reading is not one.
    """
    chunk = []
    for line in body.splitlines() + ['']:
        if line.startswith('data:'):
            payload = line[5:].lstrip()
            chunk.append(payload)
            yield payload
        elif not line.strip() and chunk:
            if len(chunk) > 1:
                yield '\n'.join(chunk)
            chunk = []


def jsonrpc_messages(body):
    """Every JSON-RPC object in a reply body — a plain JSON document, a
    JSON batch, or the ``data:`` events of an SSE stream. Non-JSON
    payloads are skipped (a stream may carry keepalive comments)."""
    stripped = body.strip()
    if not stripped:
        return []
    if stripped[0] in '{[':
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return [m for m in parsed if isinstance(m, dict)]
    out = []
    for payload in _sse_payloads(body):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def jsonrpc_result(body, request_id=None):
    """The ``result`` of the response to *request_id* (or the first
    response carrying one when *request_id* is None). A stream may
    interleave notifications before the response; those are skipped.

    Raises JsonRpcError for a JSON-RPC ``error`` member or a body with
    no response in it.
    """
    for msg in jsonrpc_messages(body):
        if 'result' not in msg and 'error' not in msg:
            continue  # notification / request from the server
        if request_id is not None and msg.get('id') != request_id:
            continue
        err = msg.get('error')
        if err:
            if isinstance(err, dict):
                raise JsonRpcError(str(err.get('message') or err))
            raise JsonRpcError(str(err))
        return msg.get('result') or {}
    raise JsonRpcError('unrecognized response shape')


def tool_text(result):
    """``(text, is_error)`` from a ``tools/call`` result: every text
    content item joined, and the server's ``isError`` flag. An error
    result's text is the server explaining what was wrong with the
    call — a caller feeding a model wants that verbatim."""
    content = result.get('content') or [] if isinstance(result, dict) else []
    texts = [str(c['text']) for c in content
             if isinstance(c, dict) and c.get('text')]
    text = '\n'.join(texts)
    if not text and isinstance(result, dict) and 'structuredContent' in result:
        text = json.dumps(result['structuredContent'], ensure_ascii=False)
    return text, bool(isinstance(result, dict) and result.get('isError'))


def tools_call(url, tool, arguments, headers=None, timeout=DEFAULT_TIMEOUT,
               request_id=1, user_agent='unclose'):
    """One ``tools/call`` round-trip: ``(text, is_error)``."""
    payload = {'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call',
               'params': {'name': tool, 'arguments': arguments}}
    _status, _headers, body = http_post_json(
        url, payload, headers=headers, timeout=timeout, user_agent=user_agent)
    return tool_text(jsonrpc_result(body, request_id))
