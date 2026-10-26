"""Text to speech through our server, so speech API keys stay server-side.

Speech endpoints are configured the way models are: SPEECH_ENDPOINT_n is an
OpenAI-style base URL (".../v1") and SPEECH_API_KEY_n its optional key, for
any n. With none set we use speech.ai.unturf.com keyless, as before.

/speech/voices merges every endpoint's /voices list (the first endpoint
listing a model owns its entry); /speech/audio streams /audio/speech back
from an endpoint that lists the requested model, falling through to the
next one when an endpoint is down or answers 5xx.
"""

import os
import time

import requests
from flask import Blueprint, Response, jsonify, request, stream_with_context

bp = Blueprint("speech", __name__)

DEFAULT_SPEECH_ENDPOINT = "https://speech.ai.unturf.com/v1"
VOICES_TTL_S = 60
VOICES_TIMEOUT_S = 10
# Connect fast, but give a long reply time to synthesize & stream.
AUDIO_TIMEOUT_S = (5, 300)

# {"at": monotonic time, "data": merged model list, "routes": {model: [endpoint]}}
_voices_cache = {}


def speech_endpoints(env=None):
    """Every configured (base_url, api_key), ordered by n."""
    env = os.environ if env is None else env
    prefix = "SPEECH_ENDPOINT_"
    nums = sorted(
        int(name[len(prefix) :])
        for name, value in env.items()
        if name.startswith(prefix)
        and name[len(prefix) :].isdigit()
        and (value or "").strip()
    )
    endpoints = [
        (
            env[f"{prefix}{n}"].strip().rstrip("/"),
            (env.get(f"SPEECH_API_KEY_{n}") or "").strip(),
        )
        for n in nums
    ]
    return endpoints or [(DEFAULT_SPEECH_ENDPOINT, "")]


def _headers(api_key):
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _load_voices(verbose=False):
    """Merged voices across endpoints, cached VOICES_TTL_S. None when no
    endpoint answered. verbose (startup) prints what each endpoint lists,
    like app.py's model loading."""
    cached = _voices_cache.get("at")
    if not verbose and cached is not None and time.monotonic() - cached < VOICES_TTL_S:
        return _voices_cache
    data, routes = [], {}
    for base_url, api_key in speech_endpoints():
        try:
            response = requests.get(
                f"{base_url}/voices",
                headers=_headers(api_key),
                timeout=VOICES_TIMEOUT_S,
            )
            response.raise_for_status()
            models = response.json().get("data") or []
        except (requests.RequestException, ValueError, AttributeError) as exc:
            print(f"[WARN] speech voices from {base_url} failed: {exc}")
            continue
        if verbose:
            listed = {
                m.get("id"): len(m.get("voices") or [])
                for m in models
                if isinstance(m, dict) and m.get("id")
            }
            print(
                f"[DEBUG] {base_url} returned speech models: "
                + ", ".join(f"{k} ({n} voices)" for k, n in listed.items())
            )
        for model in models:
            model_id = model.get("id") if isinstance(model, dict) else None
            if not model_id:
                continue
            if model_id not in routes:
                data.append(model)
            routes.setdefault(model_id, []).append((base_url, api_key))
    if not routes:
        return None
    _voices_cache.update(at=time.monotonic(), data=data, routes=routes)
    return _voices_cache


def log_startup():
    """Load & print our speech models once at startup."""
    voices = _load_voices(verbose=True)
    if voices is None:
        print("[WARN] Loaded speech models: none (no speech endpoint answered)")
    else:
        print("Loaded speech models:", list(voices["routes"]))


@bp.route("/speech/voices", methods=["GET"])
def speech_voices():
    voices = _load_voices()
    if voices is None:
        return jsonify({"error": "no speech endpoint answered"}), 502
    return jsonify({"object": "list", "data": voices["data"]})


@bp.route("/speech/audio", methods=["POST"])
def speech_audio():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not body.get("input") or not body.get("model"):
        return jsonify({"error": "model & input are required"}), 400

    voices = _load_voices()
    candidates = (voices or {}).get("routes", {}).get(body["model"])
    if not candidates:
        candidates = speech_endpoints()

    upstream, last_error = None, "no speech endpoint configured"
    for i, (base_url, api_key) in enumerate(candidates):
        try:
            upstream = requests.post(
                f"{base_url}/audio/speech",
                json=body,
                headers=_headers(api_key),
                stream=True,
                timeout=AUDIO_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            last_error = f"{base_url}: {exc}"
            upstream = None
            continue
        if upstream.status_code >= 500 and i < len(candidates) - 1:
            last_error = f"{base_url}: HTTP {upstream.status_code}"
            upstream.close()
            upstream = None
            continue
        break

    if upstream is None:
        print(f"[WARN] speech audio failed: {last_error}")
        return jsonify({"error": "speech service unavailable"}), 502

    def relay():
        try:
            for chunk in upstream.iter_content(chunk_size=None):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(relay()),
        status=upstream.status_code,
        content_type=upstream.headers.get("Content-Type", "application/octet-stream"),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
