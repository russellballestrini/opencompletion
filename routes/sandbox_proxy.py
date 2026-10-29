"""Our model, for the uncloseai-cli in a chat's sandbox (tools/sandbox.py).

An OpenAI-compatible /api/sandbox/v1 that answers one kind of key: the
token tools/sandbox.arm_proxy put in a sandbox's env vault. We keep only
its hash (Sandbox.token_sha256). A running sandbox's token is served with
the model its chat delegated with, through the same client & keys chat
uses (`get_openai_client_and_model`), so our keys never enter a sandbox.
A frozen sandbox's token is refused; a new one is armed when the sandbox
next delegates with a different model.
"""

import json

from flask import Blueprint, Response, jsonify, request

from models import Sandbox
from throttle import Throttle
from tools.sandbox import token_hash

import routes

bp = Blueprint("sandbox_proxy", __name__)

# A delegated task makes many calls (every ReAct turn is one), so the
# default is generous: 600 per 10 minutes per sandbox.
proxy_limit = Throttle("OPENCOMPLETION_SANDBOX_PROXY_LIMIT", (600, 600))

# OpenAI chat fields the client takes as arguments; every other field a
# sandbox sends (chat_template_kwargs, top_k, min_p, continue_final_message,
# ...) rides in extra_body, as our own chat sends them. uncloseai-cli turns
# Qwen's thinking off with chat_template_kwargs: dropped, the model thought
# through its whole budget & answered empty.
STANDARD = (
    "messages",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "stream",
    "stream_options",
    "response_format",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "logprobs",
    "top_logprobs",
)
# Never taken from a sandbox: the model is its chat's, one answer per call,
# & nothing that could carry or name a credential or another endpoint.
REFUSED = ("model", "n", "user", "api_key", "api_base", "base_url", "extra_headers")


def _error(status, message):
    return jsonify({"error": {"message": message, "type": "sandbox_proxy"}}), status


def _sandbox():
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token.startswith("ocsb-"):
        return None
    sandbox = Sandbox.query.filter_by(token_sha256=token_hash(token)).first()
    if sandbox is None or sandbox.state != "running" or not sandbox.model:
        return None
    return sandbox


@bp.route("/api/sandbox/v1/models", methods=["GET"])
def models():
    sandbox = _sandbox()
    if sandbox is None:
        return _error(401, "unknown or expired sandbox token")
    return jsonify(
        {"object": "list", "data": [{"id": sandbox.model, "object": "model"}]}
    )


@bp.route("/api/sandbox/v1/chat/completions", methods=["POST"])
def chat_completions():
    sandbox = _sandbox()
    if sandbox is None:
        return _error(401, "unknown or expired sandbox token")
    wait = proxy_limit.hit(sandbox.id)
    if wait:
        response, status = _error(429, "too many requests from this sandbox")
        response.headers["Retry-After"] = str(wait)
        return response, status
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return _error(400, "messages required")
    ask = {key: body[key] for key in STANDARD if key in body}
    extra = {
        key: value
        for key, value in body.items()
        if key not in STANDARD and key not in REFUSED
    }
    if extra:
        ask["extra_body"] = extra
    client, model = routes.DEPS["get_openai_client_and_model"](sandbox.model)
    ask["model"] = model
    try:
        answer = client.chat.completions.create(**ask)
    except Exception as e:  # noqa: BLE001 — the harness reads why
        status = getattr(e, "status_code", None) or 502
        return _error(status if 400 <= status < 600 else 502, str(e)[:500])
    if not ask.get("stream"):
        return jsonify(answer.model_dump())

    def events():
        try:
            for chunk in answer:
                yield f"data: {json.dumps(chunk.model_dump())}\n\n"
        finally:
            getattr(answer, "close", lambda: None)()
        yield "data: [DONE]\n\n"

    return Response(events(), mimetype="text/event-stream")
