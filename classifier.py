"""A classifier model: typed decisions from a decision endpoint.

`categorize_response` grades a learner by asking a chat model to name a
bucket, then reads a word out of its reply & folds the first token's
logprobs onto the bucket set (`activity.py`). A CLASSIFIER model answers
the question AS a decision instead: a choice with a probability per label,
a yes/no as the probability of yes, a score over an ordered rubric. Nothing
else comes back, so nothing needs parsing & the bucket-substring trap
cannot bite.

Same shape as `uncloseai-cli/classifier.py` (born there 2026-09-18) &
`unhomeschool/classifier.py`, same operator vocabulary, so one environment
configures every tool on our mesh:

    MODEL_CLASSIFIER_ENDPOINT_N   the whole entry (required)
    MODEL_CLASSIFIER_API_KEY_N    optional: a proxy may add its own
    MODEL_CLASSIFIER_ID_N         model id (default: the vendor's `latest`)
    MODEL_CLASSIFIER_LABEL_N      display label (default: from the host)
    OPENCOMPLETION_CLASSIFIER     auto (default) | on | off

Vendor-agnostic on purpose: we expect more than one vendor to sell a
decision endpoint, exactly as more than one sells a chat endpoint. Wire
shape today: TypeSafe System One (`POST /v1/systemone`, `GET /v1/models`;
https://api.typesafe.ai/openapi.json, models `jev-latest` / `jev-preview`)
& any LiteLLM proxy fronting it. A second wire shape slots in behind
`_ask_instance` without touching a caller; callers speak `choose` /
`yes_no` / `score` / `classify`, never a vendor.

Gates. `OPENCOMPLETION_CLASSIFIER=auto` uses a classifier whenever an
endpoint is discovered, `off` never calls one (no network), `on` insists &
says so once on stderr when nothing is configured. Read at call time.

Fail-open toward the chat path: an error, a timeout, an unanswered question
is reported as unavailable & the caller's chat/logprobs path runs exactly
as before. An accelerator, never a floor. A failed instance cools
`COOLDOWN_S` & the next instance is tried.

Operation Voyeur: the key is read inside the request builder only. Never a
function argument, never in a result, never in an error, never logged.
"""

import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

log = logging.getLogger(__name__)

ENDPOINT_ENV = "MODEL_CLASSIFIER_ENDPOINT"
KEY_ENV = "MODEL_CLASSIFIER_API_KEY"
ID_ENV = "MODEL_CLASSIFIER_ID"
LABEL_ENV = "MODEL_CLASSIFIER_LABEL"
KNOB = "OPENCOMPLETION_CLASSIFIER"
TIMEOUT_ENV = "OPENCOMPLETION_CLASSIFIER_TIMEOUT_S"
COOLDOWN_ENV = "OPENCOMPLETION_CLASSIFIER_COOLDOWN_S"
MIN_P_ENV = "OPENCOMPLETION_CLASSIFIER_MIN_P"
MAX_INSTANCES = 100
DEFAULT_MODEL = "jev-latest"
# A learner waits on this call before the chat fallback's own budget starts,
# so it stays short.
DEFAULT_TIMEOUT_S = 8.0
COOLDOWN_S = 60.0
USER_AGENT = "opencompletion"
SOURCE = "classifier"

_cooling = {}
_cooling_lock = threading.Lock()
_warned = set()

# Cost & health counters, shared across threads, so a smoke test or a report
# can say what a treatment cost, not only what it decided.
_stats = {
    "calls": 0,
    "ok": 0,
    "errors": 0,
    "ms": 0.0,
    "input_tokens": 0,
    "output_tokens": 0,
}
_stats_lock = threading.Lock()


class ClassifierError(Exception):
    """The classifier could not answer. The message names the shape of the
    failure (HTTP code, timeout, missing answer), never the request."""


# ── Discovery ──────────────────────────────────────────────────────


def _label_for(base, wanted, taken):
    label = (wanted or "").strip()
    if not label:
        host = (urlparse(base).hostname or "").lower()
        parts = host.split(".")
        first = parts[0] if host else ""
        if first in ("api", "www") and len(parts) > 2:
            first = parts[1]
        label = first or "classifier"
    out, n = label, 2
    while out in taken:
        out = f"{label}{n}"
        n += 1
    return out


def instances(env=None):
    """Ordered {label: {base, key_env, model}} of every classifier endpoint
    in the environment. Key VALUES are never read here, only whether the
    variable is set, so the catalog can be printed."""
    env = os.environ if env is None else env
    out = {}
    for i in range(MAX_INSTANCES):
        base = (env.get(f"{ENDPOINT_ENV}_{i}") or "").strip()
        if not base:
            continue
        key_env = f"{KEY_ENV}_{i}"
        label = _label_for(base, env.get(f"{LABEL_ENV}_{i}"), out)
        out[label] = {
            "base": base.rstrip("/"),
            "key_env": key_env if (env.get(key_env) or "").strip() else None,
            "model": (env.get(f"{ID_ENV}_{i}") or "").strip() or DEFAULT_MODEL,
        }
    return out


def mode(env=None):
    """The knob as one of auto | on | off. Unset & unknown words read as auto."""
    env = os.environ if env is None else env
    v = (env.get(KNOB) or "auto").strip().lower()
    if v in ("0", "false", "no", "off"):
        return "off"
    if v in ("1", "true", "yes", "on"):
        return "on"
    return "auto"


def _env_float(env, name, default):
    try:
        return float(env.get(name) or default)
    except ValueError:
        return default


def default_timeout(env=None):
    env = os.environ if env is None else env
    return _env_float(env, TIMEOUT_ENV, DEFAULT_TIMEOUT_S)


def default_cooldown(env=None):
    """How long a failed instance sits out. With ONE instance a cooldown is
    a window of pure chat for every learner after one transient error, so
    the operator sizes it (0 disables)."""
    env = os.environ if env is None else env
    return _env_float(env, COOLDOWN_ENV, COOLDOWN_S)


def min_p(env=None):
    """Confidence floor: a classifier verdict below it hands the decision to
    the chat path. 0 (default) trusts every verdict."""
    env = os.environ if env is None else env
    return _env_float(env, MIN_P_ENV, 0.0)


def available(env=None):
    """Whether a decision may route to a classifier right now: the knob is
    not off & at least one instance is configured. `on` with nothing
    configured stays unavailable (it insists, it cannot conjure) & says so
    once on stderr."""
    m = mode(env)
    if m == "off":
        return False
    if instances(env):
        return True
    if m == "on":
        _warn_once(
            "on-without-endpoint",
            f"{KNOB}=on but no {ENDPOINT_ENV}_N is configured; "
            "decisions take the chat path",
        )
    return False


def _warn_once(key, text):
    if key in _warned:
        return
    _warned.add(key)
    try:
        print(f"classifier: {text}", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def describe(env=None):
    """One line for a catalog: which instances exist, their model, the knob.
    Empty when nothing is configured, unless the knob insists."""
    inst = instances(env)
    m = mode(env)
    if not inst:
        return f"classifier: none configured ({KNOB}={m})" if m == "on" else ""
    rows = ", ".join(f"{k} {v['model']}" for k, v in inst.items())
    state = {"auto": "on", "on": "on", "off": "OFF"}[m]
    return f"classifier: {rows}: {state} ({KNOB}={m})"


def stats():
    with _stats_lock:
        return dict(_stats)


def reset_stats():
    with _stats_lock:
        for k in _stats:
            _stats[k] = 0.0 if k == "ms" else 0


# ── Transport ──────────────────────────────────────────────────────


def _post_json(url, payload, key_env, timeout):
    """One POST (GET when payload is None). The bearer is read from the
    environment HERE & travels only in the header; an error carries the
    status & a body head, never the request."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    key = (os.environ.get(key_env) or "").strip() if key_env else ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        head = ""
        try:
            head = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        raise ClassifierError(f"HTTP {exc.code}: {head}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ClassifierError(f"{type(exc).__name__}: {exc}"[:200]) from exc


def _cool(label):
    with _cooling_lock:
        _cooling[label] = time.monotonic() + default_cooldown()


def _is_cooling(label):
    with _cooling_lock:
        return _cooling.get(label, 0.0) > time.monotonic()


def _reset_cooling():
    with _cooling_lock:
        _cooling.clear()


def _record(ms, ok, usage=None):
    usage = usage or {}
    with _stats_lock:
        _stats["calls"] += 1
        _stats["ok" if ok else "errors"] += 1
        _stats["ms"] += ms
        for k in ("input_tokens", "output_tokens"):
            try:
                _stats[k] += int(usage.get(k) or 0)
            except (TypeError, ValueError):
                pass


# A vendor answering "overloaded" (HTTP 529 / 429 / 503) does so for a
# moment, not a minute (measured 2026-09-18 in unhomeschool: one 529 in ~500
# calls). One short retry absorbs the blip; a second failure cools the
# instance.
RETRY_STATUSES = ("HTTP 529", "HTTP 429", "HTTP 503")
RETRY_DELAY_S = 0.5


def _post_with_retry(url, payload, key_env, timeout):
    try:
        return _post_json(url, payload, key_env, timeout)
    except ClassifierError as exc:
        if not str(exc).startswith(RETRY_STATUSES):
            raise
        time.sleep(RETRY_DELAY_S)
        return _post_json(url, payload, key_env, timeout)


def _ask_instance(label, cfg, model, state, questions, timeout):
    """One instance, one POST: the answers dict plus `_model` & `_instance`.
    Raises ClassifierError on any failure, after recording & cooling."""
    payload = {"state": state, "model": model, "questions": questions}
    t0 = time.monotonic()
    try:
        body = _post_with_retry(
            cfg["base"] + "/v1/systemone", payload, cfg["key_env"], timeout
        )
        data = json.loads(body)
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise ClassifierError("no answers in response")
        missing = [q for q in questions if q not in answers]
        if missing:
            raise ClassifierError(f"unanswered: {', '.join(missing)}")
    except Exception as exc:  # noqa: BLE001
        _record((time.monotonic() - t0) * 1000.0, False)
        _cool(label)
        log.warning("classifier %s failed: %s", label, str(exc)[:200])
        raise ClassifierError(f"{label}: {exc}"[:200]) from exc
    _record((time.monotonic() - t0) * 1000.0, True, usage=data.get("usage"))
    out = dict(answers)
    out["_model"] = data.get("model") or model
    out["_instance"] = label
    return out


def classify(state, questions, *, model=None, timeout=None, env=None):
    """Ask `questions` about `state`. Returns the vendor's answers keyed as
    the questions were, plus `_model` & `_instance`. Tries every instance
    not cooling, in order; raises ClassifierError naming the last failure.
    Does NOT consult the knob: `available()` is the caller's gate."""
    inst = instances(env)
    if not inst:
        raise ClassifierError("no classifier endpoint configured")
    last = None
    for label, cfg in inst.items():
        if _is_cooling(label):
            continue
        try:
            return _ask_instance(
                label,
                cfg,
                model or cfg["model"],
                state,
                questions,
                timeout or default_timeout(env),
            )
        except ClassifierError as exc:
            last = str(exc)
    raise ClassifierError(last or "every classifier instance is cooling")


def list_models(env=None, timeout=None):
    """`GET /v1/models` on every instance: {label: [model names] | error}."""
    out = {}
    for label, cfg in instances(env).items():
        try:
            body = _post_json(
                cfg["base"] + "/v1/models",
                None,
                cfg["key_env"],
                timeout or default_timeout(env),
            )
            rows = json.loads(body).get("models") or []
            out[label] = [r.get("name") for r in rows if isinstance(r, dict)]
        except ClassifierError as exc:
            out[label] = str(exc)
    return out


# ── Typed questions ────────────────────────────────────────────────


def _round(v):
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def choose(state, labels, *, instructions=None, criteria=None, extra=None, **kw):
    """One `choice` question over `labels`. Returns {value, p, dist, mass,
    source, confidence, model, instance}: `mass` 1.0 because a classifier's
    whole answer sits on the label set by construction; `confidence` the
    vendor's own. `criteria` maps label to when it applies; a label without
    one is read by its name alone. `extra` names further questions
    ({name: question dict}) asked in the SAME call; their raw answers come
    back under `extra`, so a decomposition costs no second round trip.
    Raises ClassifierError."""
    labels = [str(label) for label in labels]
    crit = {label: (criteria or {}).get(label) for label in labels}
    q = {"type": "choice", "criteria": crit}
    if instructions:
        q["instructions"] = instructions
    questions = {"answer": q}
    for name, eq in (extra or {}).items():
        if name != "answer":
            questions[name] = eq
    ans = classify(state, questions, **kw)
    a = ans["answer"]
    probs = a.get("probabilities") or {}
    dist = {label: float(probs.get(label, 0.0)) for label in labels}
    total = sum(dist.values())
    if total > 0:
        dist = {label: v / total for label, v in dist.items()}
    value = a.get("choice")
    if value not in labels:
        value = max(dist, key=dist.get) if total > 0 else None
    if value is None:
        raise ClassifierError("choice named no label")
    return dict(
        value=value,
        p=round(dist[value], 4),
        dist={label: round(v, 4) for label, v in dist.items()},
        mass=1.0,
        source=SOURCE,
        confidence=_round(a.get("confidence")),
        model=ans.get("_model"),
        instance=ans.get("_instance"),
        extra={k: ans[k] for k in (extra or {}) if k in ans},
    )


def yes_no(state, instructions, *, true=None, false=None, **kw):
    """One `noul` question: the probability (0..1) that the statement holds.
    `true` / `false` describe what counts as each side. Raises ClassifierError."""
    q = {"type": "noul", "instructions": instructions}
    if true or false:
        q["criteria"] = {"true": true, "false": false}
    a = classify(state, {"answer": q}, **kw)["answer"]
    p = a.get("noul")
    if p is None:
        raise ClassifierError("noul answer carried no probability")
    return float(p)


def score(state, levels, *, instructions=None, **kw):
    """One `score` question over an ordered rubric (`levels[0]` scores 0).
    Returns {score, confidence, dist} with `dist` {level index: probability}.
    Raises ClassifierError."""
    q = {"type": "score", "criteria": list(levels)}
    if instructions:
        q["instructions"] = instructions
    ans = classify(state, {"answer": q}, **kw)
    a = ans["answer"]
    if a.get("score") is None:
        raise ClassifierError("score answer carried no score")
    probs = a.get("probabilities") or {}
    return dict(
        score=float(a["score"]),
        confidence=_round(a.get("confidence")),
        dist={
            int(k): round(float(v), 4)
            for k, v in probs.items()
            if str(k).lstrip("-").isdigit()
        },
        model=ans.get("_model"),
        instance=ans.get("_instance"),
    )


# ── Bridge for categorize_response ─────────────────────────────────


def _bucket_name(bucket):
    if isinstance(bucket, dict):
        return str(bucket.get("bucket_name") or bucket.get("name") or "")
    return str(bucket)


def categorize(question, response, buckets, tokens_for_ai, *, model=None, timeout=None):
    """Route one `categorize_response` to a classifier model: QUESTION +
    RESPONSE as the state, the step's `tokens_for_ai` as the instructions,
    the bucket names as the choice set. Returns the `choose` readout with
    `value` a bucket NAME from `buckets`. Raises ClassifierError, so the
    caller runs its chat path exactly as before (fail-open).

    A verdict below `OPENCOMPLETION_CLASSIFIER_MIN_P` is raised as an error
    too: a low-confidence classifier hands the decision to the chat path
    rather than deciding it."""
    names = [n for n in (_bucket_name(b) for b in buckets) if n]
    if not names:
        raise ClassifierError("step has no buckets")
    state = f"Question: {question}\nResponse: {response}"
    d = choose(
        state,
        names,
        instructions=(tokens_for_ai or "").strip() or None,
        model=model,
        timeout=timeout,
    )
    floor = min_p()
    if floor and d["p"] < floor:
        raise ClassifierError(f"low p {d['p']} < {floor}: chat decides")
    return d


def _check():
    """`python3 -m classifier` (or `make classifier-check`): list each
    instance's models & ask one sample choice. Prints outcomes; the key
    never leaves the request builder."""
    print(describe() or "classifier: none configured")
    if not instances():
        return 2
    for label, models in list_models().items():
        print(f"  {label}: models = {models}")
    try:
        d = categorize(
            "What is 5 + 7?",
            "12",
            ["correct", "close", "incorrect"],
            "Categorize as 'correct' if they answer 12 or twelve. "
            "Categorize as 'close' if within 2. Otherwise 'incorrect'.",
        )
    except ClassifierError as exc:
        print(f"  sample choice FAILED: {exc}")
        return 1
    print(
        f"  sample choice: {d['value']} p={d['p']} confidence={d['confidence']} "
        f"dist={d['dist']} via {d['instance']} ({d['model']})"
    )
    s = stats()
    print(f"  cost: {s['input_tokens']} input tokens, {s['ms']:.0f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(_check())
