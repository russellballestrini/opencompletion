"""What a deploy check asks: which commit is running & what our start did
to our database (/version), & whether we answer with our database behind
us (/health), & whether we have a chat model to answer with
(/health/models).

None calls a model endpoint: a dead MODEL_ENDPOINT_n must not make a
health check hang or fail (the app already fails over between them), and
none says why something failed, since all are public. /health/models
reads our model map from memory, so a load balancer keeps /health & a
monitor asks this one.
"""

import os
import subprocess
import time

from flask import Blueprint, current_app, jsonify
from sqlalchemy import text

import db_migrate
from models import db
from routes import DEPS

bp = Blueprint("status", __name__)

STARTED_AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _git(root, *args):
    try:
        out = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


_commit = {}


def running_commit(root):
    """{commit, committed_at} of the checkout we run from, read once per
    process (a deploy restarts us). OPENCOMPLETION_GIT_COMMIT wins, for a
    deploy that ships without .git."""
    if root not in _commit:
        pinned = os.environ.get("OPENCOMPLETION_GIT_COMMIT", "").strip()
        _commit[root] = {
            "commit": pinned[:12] or _git(root, "rev-parse", "--short=7", "HEAD"),
            "committed_at": None if pinned else _git(root, "log", "-1", "--format=%cI"),
        }
    return _commit[root]


def _no_store(response):
    response.cache_control.no_store = True
    return response


@bp.route("/version")
def version():
    info = running_commit(current_app.root_path)
    return _no_store(
        jsonify(
            service="opencompletion",
            commit=info["commit"] or "unknown",
            committed_at=info["committed_at"],
            started_at=STARTED_AT,
            # What our start did to our database (db_migrate.py): "not run"
            # means we were started some way that skips it, so a deploy
            # carrying a migration would need it run by hand.
            migrations=db_migrate.LAST.get("state", "not run"),
            migration=db_migrate.LAST.get("revision"),
        )
    )


@bp.route("/health")
def health():
    try:
        db.session.execute(text("SELECT 1"))
        database = "ok"
    except Exception:  # noqa: BLE001 — public: say down, never why
        db.session.rollback()
        database = "error"
    healthy = database == "ok"
    response = jsonify(status="ok" if healthy else "error", database=database)
    response.status_code = 200 if healthy else 503
    return _no_store(response)


@bp.route("/health/models")
def health_models():
    """200 while our model map lists a chat model, else 503: counts & the
    map's age only, never an endpoint, a key or a model id. Asking nudges
    a stale map to rebuild in the background, as /models does; a map never
    built stays so (we never wait on an endpoint here)."""
    DEPS["refresh_stale_model_map"]()
    known = DEPS["model_map_status"]()
    if not known["built"]:
        state = "never built"
    elif known["chat_models"]:
        state = "ok"
    else:
        state = "no models"
    response = jsonify(
        status="ok" if state == "ok" else "error",
        models=state,
        endpoints_configured=known["endpoints"],
        endpoints_listing=known["endpoints_listing"],
        model_count=known["models"],
        chat_model_count=known["chat_models"],
        map_age_seconds=known["age_seconds"],
    )
    response.status_code = 200 if state == "ok" else 503
    return _no_store(response)
