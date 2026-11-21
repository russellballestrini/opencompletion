"""/version & /health: what a deploy check reads."""

import re
import time


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


# /health/models: whether we have a chat model to answer with, read from
# the map in memory. Recognisable fakes, so a leak would show.
FAKE_URL_A = "https://endpoint-a.leak-canary.example/v1"
FAKE_URL_B = "https://endpoint-b.leak-canary.example/v1"
FAKE_KEY = "sk-leak-canary-0123456789"


def model_map(monkeypatch, listed, built_seconds_ago=5):
    """Our map as a build would have left it: listed = {url: [model ids]},
    with two endpoints configured. built_seconds_ago None = never built."""
    import app as app_module

    def no_network(*args, **kwargs):
        raise AssertionError("/health/models asked an endpoint")

    monkeypatch.setattr(app_module, "initialize_model_map", no_network)
    monkeypatch.setattr(app_module, "endpoint_is_healthy", no_network)
    monkeypatch.setattr(app_module, "_model_map_refresh", None)
    monkeypatch.setattr(
        app_module,
        "ENDPOINTS",
        [{"base_url": url, "api_key": FAKE_KEY} for url in (FAKE_URL_A, FAKE_URL_B)],
    )
    client_map, endpoints = {}, {}
    for url, ids in listed.items():
        for model_id in ids:
            endpoints.setdefault(model_id, []).append((object(), url))
            client_map.setdefault(model_id, (object(), url))
    monkeypatch.setattr(app_module, "MODEL_CLIENT_MAP", client_map)
    monkeypatch.setattr(app_module, "MODEL_ENDPOINTS", endpoints)
    monkeypatch.setattr(
        app_module,
        "_model_map_refreshed_at",
        0.0 if built_seconds_ago is None else time.time() - built_seconds_ago,
    )
    return app_module


def test_health_models_is_ok_with_a_listed_model(test_app, monkeypatch):
    model_map(monkeypatch, {FAKE_URL_A: ["canary-chat-model"]})
    response = test_app.test_client().get("/health/models")
    assert response.status_code == 200
    assert "no-store" in response.headers["Cache-Control"]
    body = response.json
    assert 5 <= body.pop("map_age_seconds") < 60
    assert body == {
        "status": "ok",
        "models": "ok",
        "endpoints_configured": 2,
        "endpoints_listing": 1,
        "model_count": 1,
        "chat_model_count": 1,
    }


def test_health_models_counts_endpoints_and_models_once(test_app, monkeypatch):
    # One model on both endpoints is one model; an image model never chats.
    model_map(
        monkeypatch,
        {
            FAKE_URL_A: ["canary-chat-model", "gpt-image-1"],
            FAKE_URL_B: ["canary-chat-model"],
        },
    )
    body = test_app.test_client().get("/health/models").json
    assert (body["endpoints_listing"], body["model_count"]) == (2, 2)
    assert body["chat_model_count"] == 1


def test_health_models_is_503_with_no_model_listed(test_app, monkeypatch):
    model_map(monkeypatch, {})
    response = test_app.test_client().get("/health/models")
    assert response.status_code == 503
    body = response.json
    assert (body["status"], body["models"]) == ("error", "no models")
    assert (body["endpoints_configured"], body["endpoints_listing"]) == (2, 0)
    assert (body["model_count"], body["chat_model_count"]) == (0, 0)


def test_health_models_is_503_with_only_image_models(test_app, monkeypatch):
    model_map(monkeypatch, {FAKE_URL_A: ["gpt-image-1"]})
    response = test_app.test_client().get("/health/models")
    assert response.status_code == 503
    assert response.json["models"] == "no models"
    assert response.json["model_count"] == 1


def test_health_models_is_503_for_a_map_never_built(test_app, monkeypatch):
    # OPENCOMPLETION_SKIP_MODEL_MAP=1: say so, & never build it here.
    app_module = model_map(monkeypatch, {}, built_seconds_ago=None)
    spawned = []
    monkeypatch.setattr(app_module.gevent, "spawn", lambda *a: spawned.append(a))
    response = test_app.test_client().get("/health/models")
    assert response.status_code == 503
    body = response.json
    assert (body["status"], body["models"]) == ("error", "never built")
    assert body["map_age_seconds"] is None
    assert spawned == []


def test_health_models_names_no_endpoint_key_or_model(test_app, monkeypatch):
    client = test_app.test_client()
    for listed in ({FAKE_URL_A: ["canary-chat-model"]}, {}):
        model_map(monkeypatch, listed)
        text = client.get("/health/models").get_data(as_text=True)
        for secret in ("leak-canary", FAKE_KEY, "canary-chat-model", "http"):
            assert secret not in text
    model_map(monkeypatch, {}, built_seconds_ago=None)
    assert "canary" not in client.get("/health/models").get_data(as_text=True)


def test_health_models_nudges_a_stale_map_as_models_does(test_app, monkeypatch):
    import app as app_module

    model_map(
        monkeypatch,
        {FAKE_URL_A: ["canary-chat-model"]},
        built_seconds_ago=app_module.ENDPOINT_HEALTH_TTL + 5,
    )
    spawned = []
    monkeypatch.setattr(app_module.gevent, "spawn", lambda *a: spawned.append(a))
    response = test_app.test_client().get("/health/models")
    # Answered from the map we have; its rebuild runs behind, never here.
    assert response.status_code == 200
    assert response.json["map_age_seconds"] >= app_module.ENDPOINT_HEALTH_TTL
    assert spawned == [(app_module.refresh_model_map,)]


def test_health_stays_ok_with_no_model_listed(test_app, monkeypatch):
    # A load balancer's check never changes with our models.
    model_map(monkeypatch, {})
    response = test_app.test_client().get("/health")
    assert response.status_code == 200
    assert response.json == {"status": "ok", "database": "ok"}
