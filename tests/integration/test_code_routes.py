"""Our unsandbox proxy (routes/code.py): credentials stay on the server,
bad requests get clear 4xx answers, and upstream failures keep their real
status & body. The SDK is patched; nothing leaves the machine."""

from unittest.mock import MagicMock, patch

import pytest
import requests

pytestmark = pytest.mark.integration

KEYS = {"UNSANDBOX_PUBLIC_KEY": "unsb-pk-test", "UNSANDBOX_SECRET_KEY": "unsb-sk-test"}


@pytest.fixture
def client(test_app):
    """A signed-in browser: running code spends our unsandbox account."""
    from models import User, db

    user = User(email="coder@example.test", display_name="coder")
    db.session.add(user)
    db.session.commit()
    client = test_app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = user.id
    return client


@pytest.fixture
def guest(test_app):
    return test_app.test_client()


@pytest.fixture
def keys(monkeypatch):
    for name, value in KEYS.items():
        monkeypatch.setenv(name, value)


def test_without_credentials_every_route_says_not_configured(client, monkeypatch):
    for name in KEYS:
        monkeypatch.delenv(name, raising=False)
    body = {"language": "python", "code": "print(1)"}
    assert client.post("/api/code/execute", json=body).status_code == 503
    assert client.get("/api/code/jobs/job-1").status_code == 503
    assert client.delete("/api/code/jobs/job-1").status_code == 503


@pytest.mark.parametrize(
    "body", [None, {}, {"language": "python"}, {"code": "print(1)"}]
)
def test_execute_rejects_incomplete_requests(client, keys, body):
    response = (
        client.post("/api/code/execute", data="not json")
        if body is None
        else client.post("/api/code/execute", json=body)
    )
    assert response.status_code == 400


def test_execute_returns_only_the_job_id(client, keys):
    with patch("un._make_request", return_value={"job_id": "job-42"}) as call:
        response = client.post(
            "/api/code/execute", json={"language": "python", "code": "print(1)"}
        )
    assert response.status_code == 200
    assert response.json == {"job_id": "job-42"}
    method, path, public, secret, payload = call.call_args.args
    assert (method, path) == ("POST", "/execute")
    assert payload == {
        "language": "python",
        "code": "print(1)",
        "return_artifact": True,
    }
    # Keys sign the upstream request & never come back to the browser.
    assert "unsb-sk-test" not in response.get_data(as_text=True)
    assert "unsb-pk-test" not in response.get_data(as_text=True)


def test_upstream_http_errors_keep_their_status_and_body(client, keys):
    upstream = MagicMock(status_code=429, text="slow down")
    error = requests.exceptions.HTTPError(response=upstream)
    with patch("un._make_request", side_effect=error):
        response = client.post(
            "/api/code/execute", json={"language": "python", "code": "print(1)"}
        )
    assert response.status_code == 429
    assert response.json["upstream_status"] == 429
    assert response.json["upstream_body"] == "slow down"


def start_job(client, job_id="job-1"):
    with patch("un._make_request", return_value={"job_id": job_id}):
        client.post("/api/code/execute", json={"language": "python", "code": "1"})


def test_job_status_and_cancel_pass_through(client, keys):
    start_job(client)
    job = {"job_id": "job-1", "status": "completed", "stdout": "1\n", "exit_code": 0}
    with patch("un.get_job", return_value=job) as get_job:
        assert client.get("/api/code/jobs/job-1").json == job
    get_job.assert_called_once_with("job-1")
    with patch("un.cancel_job", return_value={"cancelled": True}) as cancel:
        assert client.delete("/api/code/jobs/job-1").json == {"cancelled": True}
    cancel.assert_called_once_with("job-1")


def test_a_server_can_keep_code_runs_to_signed_in_people(guest, keys, monkeypatch):
    monkeypatch.setenv("OPENCOMPLETION_GUEST_CODE_EXEC", "off")
    with patch("un._make_request") as call:
        response = guest.post(
            "/api/code/execute", json={"language": "python", "code": "print(1)"}
        )
    assert response.status_code == 401
    assert response.json["error"] == "Sign in to run code"
    call.assert_not_called()


def test_guests_run_code_by_default(guest, keys, monkeypatch):
    monkeypatch.delenv("OPENCOMPLETION_GUEST_CODE_EXEC", raising=False)
    start_job(guest, "job-guest")
    with patch("un.get_job", return_value={"status": "completed"}):
        assert guest.get("/api/code/jobs/job-guest").status_code == 200


def test_jobs_answer_only_to_the_session_that_started_them(
    client, test_app, keys, monkeypatch
):
    start_job(client, "job-mine")
    stranger = test_app.test_client()
    monkeypatch.setenv("OPENCOMPLETION_GUEST_CODE_EXEC", "on")
    with patch("un.get_job") as get_job, patch("un.cancel_job") as cancel:
        assert stranger.get("/api/code/jobs/job-mine").status_code == 404
        assert stranger.delete("/api/code/jobs/job-mine").status_code == 404
        assert client.get("/api/code/jobs/job-other").status_code == 404
    get_job.assert_not_called()
    cancel.assert_not_called()


# ── Model helpers: fix-code & artifact names ──────────────────────────────


def model_says(text):
    answer = MagicMock()
    answer.choices[0].message.content = text
    return patch("routes.code.create_completion_skip_thinking", return_value=answer)


@pytest.fixture
def a_model(monkeypatch):
    import routes

    monkeypatch.setitem(
        routes.DEPS, "get_openai_client_and_model", lambda name: (MagicMock(), "m")
    )


FIX = {"code": "print(1", "language": "python", "stderr": "SyntaxError"}


def test_fix_code_and_artifact_names_ask_a_model(client, a_model):
    with model_says("print(1)") as asked:
        fixed = client.post("/api/fix-code", json=FIX)
    assert fixed.get_json()["fixed_code"] == "print(1)"
    assert asked.call_count == 1
    with model_says("hello_world"):
        named = client.post("/api/generate-artifact-name", json={"code": "x"})
    assert named.get_json() == {"filename": "hello_world"}


@pytest.mark.parametrize("path", ["/api/fix-code", "/api/generate-artifact-name"])
@pytest.mark.parametrize(
    "body",
    [
        ["code"],
        {"code": 7, "language": "python"},
        {"code": "x" * 60001, "language": "python"},
    ],
)
def test_model_helpers_refuse_wrong_or_huge_input(client, a_model, path, body):
    with model_says("x") as asked:
        assert client.post(path, json=body).status_code == 400
    assert not asked.called


def test_fix_code_caps_stderr_too(client, a_model):
    with model_says("x") as asked:
        response = client.post("/api/fix-code", json={**FIX, "stderr": "e" * 60001})
    assert response.status_code == 400
    assert not asked.called


def test_fix_code_never_shows_why_a_model_failed(client, a_model):
    with patch(
        "routes.code.create_completion_skip_thinking",
        side_effect=RuntimeError("http://10.0.0.5:8000 key sk-secret"),
    ):
        response = client.post("/api/fix-code", json=FIX)
    assert response.status_code == 500
    assert response.get_json() == {"error": "Failed to fix code"}


@pytest.mark.parametrize(
    "path, body",
    [("/api/fix-code", FIX), ("/api/generate-artifact-name", {"code": "x"})],
)
def test_model_helpers_count_as_code_runs(client, a_model, monkeypatch, path, body):
    from routes import code

    code.CODE_THROTTLE.reset()
    monkeypatch.setattr(code.CODE_THROTTLE, "count", 1)
    monkeypatch.setattr(code.CODE_THROTTLE, "seconds", 600)
    with model_says("fixed") as asked:
        assert client.post(path, json=body).status_code == 200
        refused = client.post(path, json=body)
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) > 0
    assert asked.call_count == 1
    code.CODE_THROTTLE.reset()
