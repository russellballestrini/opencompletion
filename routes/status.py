"""What a deploy check asks: which commit is running (/version) & whether
we answer with our database behind us (/health).

Neither calls a model endpoint: a dead MODEL_ENDPOINT_n must not make a
health check hang or fail (the app already fails over between them), and
neither says why something failed, since both are public.
"""

import os
import subprocess
import time

from flask import Blueprint, current_app, jsonify
from sqlalchemy import text

from models import db

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
