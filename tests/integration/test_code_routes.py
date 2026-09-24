"""Our Unsandbox proxy (routes/code.py): credentials stay on the server,
bad requests get clear 4xx answers, and upstream failures keep their real
status & body. The SDK is patched; nothing leaves the machine."""

from unittest.mock import MagicMock, patch

import pytest
import requests

pytestmark = pytest.mark.integration

KEYS = {"UNSANDBOX_PUBLIC_KEY": "unsb-pk-test", "UNSANDBOX_SECRET_KEY": "unsb-sk-test"}


@pytest.fixture
def client(test_app):
    """A signed-in browser: running code spends our Unsandbox account."""
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


def test_guests_need_to_sign_in_to_run_code(guest, keys, monkeypatch):
    monkeypatch.delenv("OPENCOMPLETION_GUEST_CODE_EXEC", raising=False)
    with patch("un._make_request") as call:
        response = guest.post(
            "/api/code/execute", json={"language": "python", "code": "print(1)"}
        )
    assert response.status_code == 401
    assert response.json["error"] == "Sign in to run code"
    call.assert_not_called()


def test_a_server_can_open_code_runs_to_guests(guest, keys, monkeypatch):
    monkeypatch.setenv("OPENCOMPLETION_GUEST_CODE_EXEC", "on")
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
