"""Text to speech through our server, so speech API keys stay server-side.

Speech endpoints are configured the way models are: SPEECH_ENDPOINT_n is an
OpenAI-style base URL (".../v1") and SPEECH_API_KEY_n its optional key, for
any n. With none set we use speech.ai.unturf.com keyless, as before.

/speech/voices merges every endpoint's /voices list (the first endpoint
listing a model owns its entry); /speech/audio streams /audio/speech back
from an endpoint that lists the requested model, falling through to the
next one when an endpoint is down or answers 5xx. /speech/transcribe sends
our mic's recordings to /audio/transcriptions the same way.

Our own speech service lists its voices at /voices. An endpoint with no
such route (OpenAI's API: a 404) is read from its /models instead, as chat
& image models are: every speech model it lists today is offered, so none
is named here & a retired one drops out by itself. Its voices can't be
listed, so SPEECH_VOICES_n names them (else OPENAI_VOICES). An endpoint
that does answer /voices is never touched by any of this.
"""

import os
import re
import time

import requests
from flask import Blueprint, Response, jsonify, request, stream_with_context
from werkzeug.exceptions import RequestEntityTooLarge

from blob_store import max_bytes
from throttle import Throttle, client_ip

bp = Blueprint("speech", __name__)

DEFAULT_SPEECH_ENDPOINT = "https://speech.ai.unturf.com/v1"
VOICES_TTL_S = 60
VOICES_TIMEOUT_S = 10
# Connect fast, but give a long reply time to synthesize & stream.
AUDIO_TIMEOUT_S = (5, 300)

# {"at": monotonic time, "data": merged model list, "routes": {model: [endpoint]}}
_voices_cache = {}
# {"at": monotonic time, "ids": [every model id our endpoints list]}
_models_cache = {}
# A transcription model by its id: stt-1-parakeet, whisper-1, ...transcribe.
STT_MODEL = re.compile(r"^(stt|whisper)|transcri|parakeet|asr", re.I)
DEFAULT_STT_MODEL = "whisper-1"
# A speech model by its id (tts-1, gpt-4o-mini-tts, ...), for an endpoint
# with no /voices; SPEECH_TTS_MODELS (a regex) names another server's.
TTS_MODEL = re.compile(os.environ.get("SPEECH_TTS_MODELS") or r"tts", re.I)
# A dated or numbered copy of a model listed beside it (tts-1-1106,
# gpt-4o-mini-tts-2025-12-15): left out, so a picker offers each model once.
SNAPSHOT = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{4})$")
# OpenAI's API lists no voices; these are the ones every speech model of
# theirs has answered to. SPEECH_VOICES_n (comma separated) replaces them
# for endpoint n.
OPENAI_VOICES = (
    "alloy",
    "ash",
    "coral",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
)


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


def endpoint_voices(base_url, env=None):
    """The voices SPEECH_VOICES_n names for the endpoint at `base_url`
    (comma separated), else OPENAI_VOICES: for an endpoint that lists
    speech models & no voices."""
    env = os.environ if env is None else env
    prefix = "SPEECH_ENDPOINT_"
    for name, value in env.items():
        if name.startswith(prefix) and (value or "").strip().rstrip("/") == base_url:
            named = env.get(f"SPEECH_VOICES_{name[len(prefix):]}") or ""
            voices = [v.strip() for v in named.split(",") if v.strip()]
            if voices:
                return voices
    return list(OPENAI_VOICES)


def _models_at(base_url, api_key):
    """The model ids one endpoint lists at /models; raises as requests does."""
    response = requests.get(
        f"{base_url}/models", headers=_headers(api_key), timeout=VOICES_TIMEOUT_S
    )
    response.raise_for_status()
    return [
        m.get("id")
        for m in response.json().get("data") or []
        if isinstance(m, dict) and m.get("id")
    ]


def _without_snapshots(ids):
    """`ids` less each dated copy of a model that is itself in `ids`."""
    return [
        i for i in ids if SNAPSHOT.sub("", i) == i or SNAPSHOT.sub("", i) not in ids
    ]


def _voices_from_models(base_url, api_key):
    """Voice entries for an endpoint with no /voices route, from the speech
    models its /models lists; [] when it lists none or does not answer."""
    try:
        listed = _models_at(base_url, api_key)
    except (requests.RequestException, ValueError, AttributeError) as exc:
        print(f"[WARN] speech models from {base_url} failed: {exc}")
        return []
    speech = [i for i in listed if TTS_MODEL.search(i) and not STT_MODEL.search(i)]
    voices = endpoint_voices(base_url)
    return [{"id": i, "voices": voices} for i in _without_snapshots(speech)]


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
            if response.status_code == 404:
                # No such route: an endpoint like OpenAI's, read from its
                # /models. A /voices that is down or errors stays a failure,
                # so our own service never gets another's voice names.
                models = _voices_from_models(base_url, api_key)
            else:
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
                f"[DEBUG] {base_url} returned voices: "
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


def _load_models(verbose=False):
    """Every model id our speech endpoints list at /models (speech &
    transcription alike), cached VOICES_TTL_S; [] when none answered."""
    cached = _models_cache.get("at")
    if not verbose and cached is not None and time.monotonic() - cached < VOICES_TTL_S:
        return _models_cache["ids"]
    ids, by_endpoint = [], {}
    for base_url, api_key in speech_endpoints():
        try:
            listed = _models_at(base_url, api_key)
        except (requests.RequestException, ValueError, AttributeError) as exc:
            if verbose:
                print(f"[WARN] speech models from {base_url} failed: {exc}")
            continue
        if verbose:
            print(f"[DEBUG] {base_url} returned speech models: {listed}")
        by_endpoint[base_url] = listed
        ids += [i for i in listed if i not in ids]
    _models_cache.update(at=time.monotonic(), ids=ids, by_endpoint=by_endpoint)
    return ids


def stt_model():
    """(model, why): SPEECH_STT_MODEL when set, else the transcription model
    our speech endpoint lists (stt-1-parakeet today), else whisper-1, the
    OpenAI-style name most whisper servers answer to."""
    named = (os.environ.get("SPEECH_STT_MODEL") or "").strip()
    if named:
        return named, "SPEECH_STT_MODEL"
    for model_id in _load_models():
        if STT_MODEL.search(model_id):
            return model_id, "listed by our speech endpoint"
    return DEFAULT_STT_MODEL, "default; no endpoint lists one"


def stt_model_for(base_url):
    """The transcription model to ask one endpoint for: SPEECH_STT_MODEL
    when set (everywhere, as before), else one that endpoint itself lists
    (never a diarizing or dated one while a plain one is listed), else
    stt_model()'s. So each endpoint is asked for a model it knows: our own
    service for stt-1-parakeet, OpenAI's for whichever it lists today."""
    model, why = stt_model()
    if why == "SPEECH_STT_MODEL":
        return model
    listed = _models_cache.get("by_endpoint", {}).get(base_url) or []
    own = _without_snapshots([i for i in listed if STT_MODEL.search(i)])
    plain = [i for i in own if "diariz" not in i.lower()]
    return (plain or own or [model])[0]


def log_startup():
    """Load & print what speech offers once at startup: voices for each
    speech model & the model our mic's recordings are transcribed with."""
    _load_models(verbose=True)
    voices = _load_voices(verbose=True)
    if voices is None:
        print("[WARN] Loaded speech: none (no speech endpoint answered)")
    else:
        print(
            "Loaded speech:",
            ", ".join(
                f"{m.get('id')} ({len(m.get('voices') or [])} voices)"
                for m in voices["data"]
            ),
        )
    model, why = stt_model()
    print(f"Loaded transcription: {model} ({why})")


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


# Voice input: speech to text through the same endpoints, so audio goes to
# our speech service, never a browser vendor's. The transcription model is
# SPEECH_STT_MODEL, else the one our speech endpoint lists (stt_model), else
# whisper-1.
TRANSCRIBE_TIMEOUT_S = (5, 120)
TRANSCRIBE_THROTTLE = Throttle("OPENCOMPLETION_TRANSCRIBE_LIMIT", default=(30, 600))


@bp.route("/speech/transcribe", methods=["POST"])
def speech_transcribe():
    """Audio from our mic button (multipart "file", within our upload
    limit) -> {"text": ...} from the first endpoint that transcribes it."""
    too_long = jsonify({"error": "That recording is too long."}), 413
    request.max_content_length = max_bytes() + 64 * 1024
    if (request.content_length or 0) > request.max_content_length:
        return too_long
    try:
        upload = request.files.get("file")
    except RequestEntityTooLarge:
        return too_long
    data = upload.stream.read(max_bytes() + 1) if upload else b""
    if not data:
        return jsonify({"error": "No audio was recorded."}), 400
    if len(data) > max_bytes():
        return too_long
    retry_after = TRANSCRIBE_THROTTLE.hit(client_ip())
    if retry_after:
        response = jsonify(
            {"error": "Too many recordings from your address; try again soon."}
        )
        response.headers["Retry-After"] = str(retry_after)
        return response, 429

    last_error = "no speech endpoint configured"
    for base_url, api_key in speech_endpoints():
        model = stt_model_for(base_url)
        try:
            answer = requests.post(
                f"{base_url}/audio/transcriptions",
                files={
                    "file": (
                        upload.filename or "speech.webm",
                        data,
                        upload.mimetype or "audio/webm",
                    )
                },
                data={"model": model},
                headers=_headers(api_key),
                timeout=TRANSCRIBE_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            last_error = f"{base_url}: {exc}"
            continue
        if answer.ok:
            try:
                return jsonify({"text": (answer.json().get("text") or "").strip()})
            except ValueError:
                last_error = f"{base_url}: not JSON"
                continue
        last_error = f"{base_url}: HTTP {answer.status_code}"
    print(f"[WARN] speech transcription failed: {last_error}")
    return (
        jsonify(
            {"error": "Voice input is unavailable: no speech service transcribed it."}
        ),
        502,
    )
