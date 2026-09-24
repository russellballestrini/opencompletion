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


def test_job_status_and_cancel_pass_through(client, keys):
    job = {"job_id": "job-1", "status": "completed", "stdout": "1\n", "exit_code": 0}
    with patch("un.get_job", return_value=job) as get_job:
        assert client.get("/api/code/jobs/job-1").json == job
    get_job.assert_called_once_with("job-1")
    with patch("un.cancel_job", return_value={"cancelled": True}) as cancel:
        assert client.delete("/api/code/jobs/job-1").json == {"cancelled": True}
    cancel.assert_called_once_with("job-1")
