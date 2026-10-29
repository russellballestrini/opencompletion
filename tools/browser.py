"""Run a tool in the asker's own browser, so it runs on their network.

The server sends `tool_request` {request_id, tool, arguments} to the one
socket that sent the message (never the room), & waits for that same
socket's `tool_result` {request_id, result}. static/js/chat/8-tools.js
answers it. A result from any other socket is ignored, so nobody else in
a room can answer, or poison, someone's search; a browser that leaves
or stays quiet past OPENCOMPLETION_TOOL_TIMEOUT_S answers with an error
the model reads, & the reply goes on without it.
"""

import uuid

import gevent
from flask import request

_socketio = None
_pending = {}  # request_id -> (socket id, AsyncResult)


def register(socketio):
    """Hook our `tool_result` event onto this Socket.IO server."""
    global _socketio
    _socketio = socketio

    @socketio.on("tool_result")
    def on_tool_result(data):
        resolve(request.sid, data)


def ask(sid, tool, arguments, timeout):
    """What the browser behind `sid` answered for `tool`, or {"error"}."""
    if _socketio is None or not sid:
        return {"error": "no browser to run this in"}
    # Imported here: some suites import app with gevent replaced by a mock.
    from gevent.event import AsyncResult

    request_id = uuid.uuid4().hex
    answer = AsyncResult()
    _pending[request_id] = (sid, answer)
    try:
        _socketio.emit(
            "tool_request",
            {"request_id": request_id, "tool": tool, "arguments": arguments},
            room=sid,
        )
        return answer.get(timeout=timeout)
    except gevent.Timeout:
        return {"error": f"the asker's browser did not answer within {timeout}s"}
    finally:
        _pending.pop(request_id, None)


def resolve(sid, data):
    """Hand a browser's answer to the reply waiting on it; only the socket
    it was asked of may answer. True when an answer was taken."""
    if not isinstance(data, dict):
        return False
    entry = _pending.get(data.get("request_id"))
    if entry is None or entry[0] != sid or entry[1].ready():
        return False
    result = data.get("result")
    entry[1].set(result if isinstance(result, dict) else {"error": "no result"})
    return True


def abandon(sid):
    """A socket left: everything waiting on it hears so now, not at its
    timeout."""
    for owner, answer in list(_pending.values()):
        if owner == sid and not answer.ready():
            answer.set({"error": "the asker's browser left"})
