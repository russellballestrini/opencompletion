"""/version & /health: what a deploy check reads."""

import re


def test_version_names_the_running_commit(test_app, monkeypatch):
    from routes import status

    monkeypatch.setattr(status, "_commit", {})
    monkeypatch.delenv("OPENCOMPLETION_GIT_COMMIT", raising=False)
    response = test_app.test_client().get("/version")
    assert response.status_code == 200
    assert "no-store" in response.headers["Cache-Control"]
    body = response.json
    assert body["service"] == "opencompletion"
    assert re.fullmatch(r"[0-9a-f]{7,12}|unknown", body["commit"])
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", body["started_at"])


def test_a_pinned_commit_wins_for_a_deploy_without_git(test_app, monkeypatch):
    from routes import status

    monkeypatch.setattr(status, "_commit", {})
    monkeypatch.setenv("OPENCOMPLETION_GIT_COMMIT", "abc1234def5678")
    body = test_app.test_client().get("/version").json
    assert body["commit"] == "abc1234def56" and body["committed_at"] is None


def test_version_without_git_says_unknown(test_app, monkeypatch, tmp_path):
    from routes import status

    monkeypatch.setattr(status, "_commit", {})
    monkeypatch.delenv("OPENCOMPLETION_GIT_COMMIT", raising=False)
    monkeypatch.setattr(test_app, "root_path", str(tmp_path))
    body = test_app.test_client().get("/version").json
    assert body["commit"] == "unknown"


def test_health_is_ok_with_our_database(test_app):
    response = test_app.test_client().get("/health")
    assert response.status_code == 200
    assert response.json == {"status": "ok", "database": "ok"}
    assert "no-store" in response.headers["Cache-Control"]


def test_health_is_503_without_saying_why(test_app, monkeypatch):
    from models import db

    def broken(*args, **kwargs):
        raise RuntimeError("secret connection string in here")

    monkeypatch.setattr(db.session, "execute", broken)
    response = test_app.test_client().get("/health")
    assert response.status_code == 503
    assert response.json == {"status": "error", "database": "error"}
    assert "secret" not in response.get_data(as_text=True)


def test_version_says_what_our_start_did_to_our_database(test_app, monkeypatch):
    import db_migrate

    # Started some way that skips db_migrate (as these tests are): say so.
    monkeypatch.setattr(db_migrate, "LAST", {})
    body = test_app.test_client().get("/version").json
    assert (body["migrations"], body["migration"]) == ("not run", None)

    monkeypatch.setattr(
        db_migrate, "LAST", {"state": "migrated", "revision": "2026111001"}
    )
    body = test_app.test_client().get("/version").json
    assert (body["migrations"], body["migration"]) == ("migrated", "2026111001")
    # A state & a revision only: never a path or an address.
    assert "/" not in str(body["migrations"]) + str(body["migration"])
