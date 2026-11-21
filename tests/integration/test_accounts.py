"""Email-code sign in end to end (routes/accounts.py + auth.py): sign up,
sign in, names, sign out, and a code that cannot be brute forced."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def client(test_app):
    import auth

    auth._otp_failures.clear()
    return test_app.test_client()


def request_code(client, email):
    """Ask for a code; return the one our server generated."""
    sent = {}

    def capture(address, code):
        sent["code"] = code
        return "email"

    with patch("auth.send_otp_email", side_effect=capture):
        response = client.post("/auth/send-otp", json={"email": email})
    assert response.status_code == 200 and response.json["delivery"] == "email"
    return sent["code"]


def wrong(code):
    return f"{(int(code) + 1) % 1000000:06d}"


def test_sign_up_sign_in_and_sign_out(client):
    code = request_code(client, "Ada@Example.test")
    verified = client.post(
        "/auth/verify-otp", json={"email": "ada@example.test", "otp_code": code}
    ).json
    assert verified["needs_display_name"] is True

    claimed = client.post("/auth/claim-name", json={"display_name": "ada"})
    assert claimed.status_code == 200
    assert client.get("/auth/status").json["user"]["display_name"] == "ada"

    assert client.post("/auth/logout").status_code == 200
    assert client.get("/auth/status").json == {"authenticated": False}

    # Coming back: an existing account signs straight in.
    code = request_code(client, "ada@example.test")
    again = client.post(
        "/auth/verify-otp", json={"email": "ada@example.test", "otp_code": code}
    ).json
    assert again["needs_display_name"] is False
    assert again["user"]["display_name"] == "ada"


def test_a_code_works_once(client):
    code = request_code(client, "once@example.test")
    body = {"email": "once@example.test", "otp_code": code}
    assert client.post("/auth/verify-otp", json=body).status_code == 200
    assert client.post("/auth/verify-otp", json=body).status_code == 400


def test_five_wrong_codes_burn_the_real_one(client):
    code = request_code(client, "target@example.test")
    for _ in range(5):
        response = client.post(
            "/auth/verify-otp",
            json={"email": "target@example.test", "otp_code": wrong(code)},
        )
        assert response.status_code == 400
    # The right code no longer works; a guesser must trigger a new email.
    response = client.post(
        "/auth/verify-otp", json={"email": "target@example.test", "otp_code": code}
    )
    assert response.status_code == 400

    fresh = request_code(client, "target@example.test")
    response = client.post(
        "/auth/verify-otp", json={"email": "target@example.test", "otp_code": fresh}
    )
    assert response.status_code == 200


def test_misses_below_the_limit_still_allow_the_real_code(client):
    code = request_code(client, "typo@example.test")
    for _ in range(4):
        client.post(
            "/auth/verify-otp",
            json={"email": "typo@example.test", "otp_code": wrong(code)},
        )
    response = client.post(
        "/auth/verify-otp", json={"email": "typo@example.test", "otp_code": code}
    )
    assert response.status_code == 200


@pytest.mark.parametrize("name", ["ab", "has space", "semi;colon", "x" * 51])
def test_display_names_follow_the_stated_rule(client, name):
    code = request_code(client, "rules@example.test")
    client.post(
        "/auth/verify-otp", json={"email": "rules@example.test", "otp_code": code}
    )
    assert (
        client.post("/auth/claim-name", json={"display_name": name}).status_code == 400
    )


def test_claiming_a_name_needs_a_verified_email_first(client):
    response = client.post("/auth/claim-name", json={"display_name": "sneaky"})
    assert response.status_code == 400


def test_username_checks_and_changes(client):
    code = request_code(client, "bo@example.test")
    client.post("/auth/verify-otp", json={"email": "bo@example.test", "otp_code": code})
    client.post("/auth/claim-name", json={"display_name": "bob"})

    assert client.get("/api/check-username?username=bob").json["available"] is False
    assert client.get("/api/check-username?username=cy").status_code == 400  # too short
    assert client.get("/api/check-username?username=cyd").json["available"] is True

    response = client.post("/api/update-username", json={"new_username": "cyd"})
    assert response.status_code == 200
    assert client.get("/auth/status").json["user"]["display_name"] == "cyd"


def test_changing_names_requires_sign_in(client):
    response = client.post("/api/update-username", json={"new_username": "anyone"})
    assert response.status_code == 401


@pytest.mark.parametrize(
    "path", ["/auth/send-otp", "/auth/verify-otp", "/auth/claim-name"]
)
def test_bad_bodies_get_400_not_500(client, path):
    assert client.post(path, data="not json").status_code == 400


def test_guesses_without_a_live_code_are_not_remembered(client):
    import auth

    for i in range(20):
        client.post(
            "/auth/verify-otp",
            json={"email": f"nobody{i}@example.test", "otp_code": "123456"},
        )
    assert auth._otp_failures == {}


@pytest.mark.parametrize(
    "path", ["/auth/send-otp", "/auth/verify-otp", "/auth/claim-name"]
)
@pytest.mark.parametrize(
    "body", [["a@b.org"], {"email": 7, "otp_code": 1, "display_name": ["x"]}]
)
def test_wrong_shapes_get_400_not_500(client, path, body):
    assert client.post(path, json=body).status_code == 400


@pytest.mark.parametrize(
    "email",
    [
        "a@x.com,b@y.org",
        "a@x.com b@y.org",
        "Ada <a@x.com>",
        "a@x",
        "a@@x.com",
        "a@x..com",
        "a" * 250 + "@x.com",
    ],
)
def test_a_code_goes_to_one_plain_address_only(client, email):
    with patch("auth.send_otp_email", return_value="email") as sent:
        response = client.post("/auth/send-otp", json={"email": email})
    assert response.status_code == 400
    assert not sent.called


def limit(monkeypatch, throttle, count, seconds=600):
    throttle.reset()
    monkeypatch.setattr(throttle, "count", count)
    monkeypatch.setattr(throttle, "seconds", seconds)


def test_codes_are_limited_per_target_email(test_app, monkeypatch):
    from routes import accounts

    limit(monkeypatch, accounts.OTP_EMAIL_THROTTLE, 2, 900)

    def ask(email, address):
        return test_app.test_client().post(
            "/auth/send-otp",
            json={"email": email},
            headers={"X-Forwarded-For": address},
        )

    with patch("auth.send_otp_email", return_value="email") as sent:
        # Each ask from another address: only the inbox is counted.
        assert ask("ada@example.org", "203.0.113.1").status_code == 200
        assert ask("Ada@Example.org", "203.0.113.2").status_code == 200
        refused = ask("ada@example.org", "203.0.113.3")
        assert ask("bob@example.org", "203.0.113.3").status_code == 200
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) > 0
    assert "for that email" in refused.get_json()["error"]
    assert sent.call_count == 3


def test_guesses_are_limited_per_address(client, monkeypatch):
    from routes import accounts

    limit(monkeypatch, accounts.OTP_VERIFY_THROTTLE, 2)
    for n in range(2):
        guess = {"email": f"nobody{n}@example.org", "otp_code": "000000"}
        assert client.post("/auth/verify-otp", json=guess).status_code == 400
    code = request_code(client, "ada@example.org")
    refused = client.post(
        "/auth/verify-otp", json={"email": "ada@example.org", "otp_code": code}
    )
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) > 0
