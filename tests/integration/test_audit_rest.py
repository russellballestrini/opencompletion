"""What our audit's last round closed: /vision/describe takes an image's
bytes (never an address) & is limited per address; a request body has a
ceiling; a model's run_code counts as a person's Run; an activity's
failure never shows a room a traceback, & a room holds one activity; a
row an Edit & resend hid cannot be edited; our session cookie & the
console fallback for sign-in codes follow OPENCOMPLETION_PUBLIC_URL.

Flask's & Flask-SocketIO's test clients, an in-memory database, no network.
"""

import pytest

pytestmark = pytest.mark.integration

PIXEL = "data:image/png;base64,iVBORw0KGgo="


@pytest.fixture
def app_module(test_app, monkeypatch):
    import app as app_module

    return app_module


def limit(monkeypatch, throttle, count, seconds=600):
    throttle.reset()
    monkeypatch.setattr(throttle, "count", count)
    monkeypatch.setattr(throttle, "seconds", seconds)


@pytest.fixture
def vision(app_module, monkeypatch):
    """One vision model whose every ask is recorded, never sent."""
    asked = []

    def answer(client, **kwargs):
        from types import SimpleNamespace

        asked.append(kwargs)
        message = SimpleNamespace(content="A red square.")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(app_module, "MODEL_CLIENT_MAP", {"see-1": (object(), "u")})
    monkeypatch.setattr(app_module, "is_vision_model", lambda name: name == "see-1")
    monkeypatch.setattr(app_module, "get_client_for_model", lambda name: object())
    monkeypatch.setattr(app_module, "create_completion_skip_thinking", answer)
    return asked


def test_describe_reads_an_images_bytes(test_app, vision):
    reply = test_app.test_client().post("/vision/describe", json={"image": PIXEL})
    assert reply.status_code == 200
    assert reply.get_json() == {"description": "A red square.", "model": "see-1"}


@pytest.mark.parametrize(
    "image",
    [
        "https://internal.example/secret.png",
        "http://169.254.169.254/latest/meta-data/",
        "data:text/html;base64,PGI+",
        "",
        None,
        ["x"],
    ],
)
def test_describe_never_takes_an_address(test_app, vision, image):
    reply = test_app.test_client().post("/vision/describe", json={"image": image})
    assert reply.status_code == 400
    assert vision == []


def test_describe_keeps_our_prompt_and_a_listed_model(test_app, vision):
    http = test_app.test_client()
    http.post("/vision/describe", json={"image": PIXEL, "prompt": "Write an essay"})
    (ask,) = vision
    assert ask["messages"][0]["content"][0]["text"].startswith("Describe this image")
    reply = http.post("/vision/describe", json={"image": PIXEL, "model": "gpt-other"})
    assert reply.status_code == 400 and len(vision) == 1


def test_describe_is_limited_per_address(test_app, app_module, vision, monkeypatch):
    limit(monkeypatch, app_module.VISION_THROTTLE, 2)
    http = test_app.test_client()
    for _ in range(2):
        assert http.post("/vision/describe", json={"image": PIXEL}).status_code == 200
    refused = http.post("/vision/describe", json={"image": PIXEL})
    assert refused.status_code == 429 and refused.headers["Retry-After"]
    assert len(vision) == 2


def test_describe_never_repeats_a_vendors_error(
    test_app, app_module, vision, monkeypatch
):
    def broken(client, **kwargs):
        raise RuntimeError("upstream key sk-live-123 refused at 10.0.0.7")

    monkeypatch.setattr(app_module, "create_completion_skip_thinking", broken)
    reply = test_app.test_client().post("/vision/describe", json={"image": PIXEL})
    assert reply.status_code == 502
    assert "sk-live" not in reply.get_data(as_text=True)


def test_a_body_past_our_ceiling_is_refused(test_app, app_module, monkeypatch):
    assert app_module.app.config["MAX_CONTENT_LENGTH"] >= 32 * 1024 * 1024
    monkeypatch.setitem(app_module.app.config, "MAX_CONTENT_LENGTH", 1000)
    reply = test_app.test_client().post(
        "/vision/describe", json={"image": PIXEL + "A" * 2000}
    )
    assert reply.status_code == 413 and reply.get_json()["error"]


def test_fetched_images_stop_at_our_upload_limit(app_module, monkeypatch):
    import httpx

    class Stream:
        status_code = 200
        headers = {"content-type": "image/png"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            while True:
                yield b"x" * 4096

    class Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, method, url, **kw):
            return Stream()

    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(app_module, "max_upload_bytes", lambda: 10000)
    address = "https://example.org/endless.png"
    assert app_module.fetch_external_image_as_base64(address) is None
    assert address not in app_module._external_image_cache


def test_the_image_cache_keeps_only_its_newest(app_module):
    cache = app_module.Recent(3)
    for n in range(10):
        cache[n] = n
    assert list(cache) == [7, 8, 9]
    cache[7] = "again"
    cache[10] = 10
    assert list(cache) == [9, 7, 10]


def test_a_models_run_code_counts_as_a_run(test_app, monkeypatch):
    import throttle
    import tools
    from tools import sandbox

    ran = []
    monkeypatch.setattr(
        sandbox, "run_code", lambda context, *a: ran.append(a) or {"stdout": "ok"}
    )
    monkeypatch.setenv("UNSANDBOX_PUBLIC_KEY", "unsb-pk-test")
    monkeypatch.setenv("UNSANDBOX_SECRET_KEY", "unsb-sk-test")
    monkeypatch.setenv("OPENCOMPLETION_RUN_CODE", "on")
    limit(monkeypatch, throttle.CODE_THROTTLE, 2)
    asker = tools.ToolContext(sid="s", address="203.0.113.9")
    other = tools.ToolContext(sid="t", address="203.0.113.10")
    resumed = tools.ToolContext(
        sid="s", address="203.0.113.9", resume_run={"job_id": "j1"}
    )

    def run(context):
        return tools.run(
            context, "run_code", {"language": "python", "code": "print(1)"}
        )

    assert "error" not in run(asker) and "error" not in run(asker)
    refused = run(asker)
    assert "Too many code runs" in refused["error"] and len(ran) == 2
    # Someone else's reply still runs, & a run begun before a restart is
    # collected, not counted again.
    assert "error" not in run(other)
    assert "error" not in run(resumed)


def test_a_superseded_row_cannot_be_edited(test_app, app_module):
    from models import Message, Room, db

    room = Room(name="lobby")
    db.session.add(room)
    db.session.commit()
    old = Message(username="ada", content="first", room_id=room.id)
    db.session.add(old)
    db.session.commit()
    new = Message(username="ada", content="second", room_id=room.id)
    new.revision_of_id = old.id
    db.session.add(new)
    db.session.commit()
    old.superseded_by_id = new.id
    db.session.commit()

    client = app_module.socketio.test_client(
        app_module.app, flask_test_client=test_app.test_client()
    )
    client.emit("join", {"room_name": "lobby", "username": "ada"})
    client.get_received()
    client.emit(
        "update_message",
        {"message_id": old.id, "content": "rewritten", "room_name": "lobby"},
    )
    client.emit(
        "update_message",
        {"message_id": new.id, "content": "second, mended", "room_name": "lobby"},
    )
    updated = [e for e in client.get_received() if e["name"] == "message_updated"]
    assert [e["args"][0]["message_id"] for e in updated] == [new.id]
    db.session.expire_all()
    assert db.session.get(Message, old.id).content == "first"
    assert db.session.get(Message, new.id).content == "second, mended"


@pytest.mark.parametrize(
    "public_url, switch, expected",
    [
        (None, None, False),
        ("http://localhost:5001", None, False),
        ("https://chat.example.org", None, True),
        ("https://chat.example.org", "off", False),
        (None, "on", True),
    ],
)
def test_cookie_secure_follows_how_we_are_served(
    monkeypatch, public_url, switch, expected
):
    import auth

    for name, value in (
        ("OPENCOMPLETION_PUBLIC_URL", public_url),
        ("OPENCOMPLETION_COOKIE_SECURE", switch),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert auth.cookie_secure() is expected


def no_mail(monkeypatch):
    import smtplib

    def refuse(*a, **k):
        raise ConnectionRefusedError()

    monkeypatch.setattr(smtplib, "SMTP", refuse)
    for name in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


def test_a_dev_server_prints_a_code_it_cannot_mail(monkeypatch, capsys):
    import auth

    no_mail(monkeypatch)
    monkeypatch.delenv("OPENCOMPLETION_PUBLIC_URL", raising=False)
    monkeypatch.delenv("OPENCOMPLETION_OTP_CONSOLE", raising=False)
    assert auth.send_otp_email("ada@example.org", "123456") == "console"
    assert "123456" in capsys.readouterr().out


def test_a_deployment_never_logs_a_code(monkeypatch, capsys):
    import auth

    no_mail(monkeypatch)
    monkeypatch.setenv("OPENCOMPLETION_PUBLIC_URL", "https://chat.example.org")
    monkeypatch.delenv("OPENCOMPLETION_OTP_CONSOLE", raising=False)
    assert auth.send_otp_email("ada@example.org", "123456") is None
    assert "123456" not in capsys.readouterr().out
    monkeypatch.setenv("OPENCOMPLETION_OTP_CONSOLE", "on")
    assert auth.send_otp_email("ada@example.org", "123456") == "console"
