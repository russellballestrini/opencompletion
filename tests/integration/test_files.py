"""File uploads (blob_store.py, routes/files.py): stored once per content
in SQLite, served under the room's access rule, limited in size & rate,
read by models; plus our Stop button & voice input's transcription."""

import io
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.integration

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture(autouse=True)
def fresh_limits():
    from routes import files, speech

    files.UPLOAD_THROTTLE.reset()
    speech.TRANSCRIBE_THROTTLE.reset()
    yield
    files.UPLOAD_THROTTLE.reset()
    speech.TRANSCRIBE_THROTTLE.reset()


def make_user(email, name):
    from models import User, db

    user = User(email=email, display_name=name)
    db.session.add(user)
    db.session.commit()
    return user


def make_room(name, owner=None, private=False):
    from models import Room, db

    room = Room(name=name, is_private=private, owner_id=owner.id if owner else None)
    db.session.add(room)
    db.session.commit()
    return room


def client_for(test_app, user=None, ip="203.0.113.7"):
    client = test_app.test_client()
    client.environ_base["HTTP_X_FORWARDED_FOR"] = ip
    if user:
        with client.session_transaction() as session:
            session["user_id"] = user.id
    return client


def upload(client, room, data, name, mime="application/octet-stream"):
    return client.post(
        f"/api/rooms/{room}/attachments",
        data={"file": (io.BytesIO(data), name, mime)},
        content_type="multipart/form-data",
    )


# --- uploading & serving --------------------------------------------------


def test_upload_then_fetch_a_file(test_app):
    make_room("lobby")
    client = client_for(test_app)
    response = upload(client, "lobby", b"hello world\n", "notes.txt", "text/plain")
    assert response.status_code == 201, response.json
    body = response.json
    assert body["filename"] == "notes.txt" and body["size"] == 12
    assert not body["is_image"]
    served = client.get(body["url"])
    assert served.status_code == 200 and served.data == b"hello world\n"
    assert served.headers["X-Content-Type-Options"] == "nosniff"
    assert served.headers["Content-Disposition"].startswith("inline")


def test_images_show_inline_but_svg_and_html_download(test_app):
    make_room("lobby")
    client = client_for(test_app)
    image = upload(client, "lobby", PNG, "dot.png", "image/png").json
    assert image["is_image"]
    assert client.get(image["url"]).mimetype == "image/png"
    for name, mime in (("x.svg", "image/svg+xml"), ("x.html", "text/html")):
        got = client.get(
            upload(client, "lobby", b"<script>1</script>", name, mime).json["url"]
        )
        assert got.headers["Content-Disposition"].startswith("attachment")
        assert got.mimetype == "application/octet-stream"
        assert "sandbox" in got.headers["Content-Security-Policy"]


def test_unchanged_files_answer_304(test_app):
    make_room("lobby")
    client = client_for(test_app)
    url = upload(client, "lobby", b"same", "a.txt").json["url"]
    etag = client.get(url).headers["ETag"]
    assert client.get(url, headers={"If-None-Match": etag}).status_code == 304


def test_files_over_our_limit_are_refused(test_app, monkeypatch):
    monkeypatch.setenv("OPENCOMPLETION_UPLOAD_MAX_MB", "0.001")  # ~1 KB
    make_room("lobby")
    response = upload(client_for(test_app), "lobby", b"x" * 2000, "big.bin")
    assert response.status_code == 413
    assert "limited to" in response.json["error"]


def test_empty_or_missing_files_are_refused(test_app):
    make_room("lobby")
    client = client_for(test_app)
    assert upload(client, "lobby", b"", "empty.txt").status_code == 400
    assert client.post("/api/rooms/lobby/attachments").status_code == 400


def test_a_solo_chats_files_are_its_owners_alone(test_app):
    owner = make_user("owner@example.test", "owner")
    make_room("secret", owner=owner, private=True)
    mine = upload(client_for(test_app, owner), "secret", b"diary", "diary.txt")
    assert mine.status_code == 201
    url = mine.json["url"]
    stranger = make_user("x@example.test", "x")
    for client in (client_for(test_app), client_for(test_app, stranger)):
        assert client.get(url).status_code == 404
        assert upload(client, "secret", b"spam", "spam.txt").status_code == 404
    assert client_for(test_app, owner).get(url).data == b"diary"


def test_uploads_are_limited_per_address(test_app):
    from routes import files

    make_room("lobby")
    bot = client_for(test_app, ip="198.51.100.9")
    for n in range(files.UPLOAD_THROTTLE.count):
        assert upload(bot, "lobby", f"{n}".encode(), f"{n}.txt").status_code == 201
    blocked = upload(bot, "lobby", b"one more", "more.txt")
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) > 0


# --- storage: once per content, cleaned up with its room --------------------


def test_same_bytes_are_stored_once(test_app):
    from models import Attachment, Blob

    make_room("one")
    make_room("two")
    client = client_for(test_app)
    upload(client, "one", b"shared bytes", "a.txt")
    upload(client, "two", b"shared bytes", "b.txt")
    assert Attachment.query.count() == 2
    assert Blob.query.count() == 1


def test_deleting_a_room_drops_its_files_and_unshared_bytes(test_app):
    from models import Attachment, Blob

    owner = make_user("owner@example.test", "owner")
    make_room("doomed", owner=owner)
    make_room("keeper")
    client = client_for(test_app, owner)
    upload(client, "doomed", b"only here", "only.txt")
    upload(client, "doomed", b"in both", "both.txt")
    upload(client, "keeper", b"in both", "both.txt")
    from models import Room

    doomed = Room.query.filter_by(name="doomed").one()
    assert client.delete(f"/api/rooms/{doomed.id}/delete").status_code == 200
    assert Attachment.query.count() == 1
    assert [b.size for b in Blob.query.all()] == [len(b"in both")]


def test_a_fork_keeps_its_own_copies_of_files(test_app):
    from models import Attachment, Message, Room, db

    room = make_room("source")
    client = client_for(test_app)
    url = upload(client, "source", b"data", "data.csv").json["url"]
    db.session.add(
        Message(username="ada", content=f"see [data]({url})", room_id=room.id)
    )
    db.session.add(Message(username="hermes", content="ok", room_id=room.id))
    db.session.commit()
    reply = Message.query.filter_by(username="hermes").one()
    fork_name = client.post(f"/api/messages/{reply.id}/fork", json={}).json["room"][
        "name"
    ]
    fork = Room.query.filter_by(name=fork_name).one()
    copied = Message.query.filter_by(room_id=fork.id, username="ada").one().content
    copy = Attachment.query.filter_by(room_id=fork.id).one()
    assert copy.url in copied and url not in copied
    assert client.get(copy.url).data == b"data"


# --- what a model reads ---------------------------------------------------


def test_text_files_are_inlined_and_images_ride_along(test_app):
    from blob_store import attachment_context

    room = make_room("lobby")
    client = client_for(test_app)
    text = upload(client, "lobby", b"print('hi')\n", "hello.py").json["url"]
    image = upload(client, "lobby", PNG, "dot.png", "image/png").json["url"]
    message = f"look [hello.py]({text}) ![dot]({image})"

    plain = attachment_context(message, room.id)
    assert "[File: hello.py]\nprint('hi')" in plain

    parts = attachment_context(message, room.id, include_images=True)
    assert parts[0]["type"] == "text" and "print('hi')" in parts[0]["text"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_a_message_never_pulls_in_another_rooms_file(test_app):
    from blob_store import attachment_context

    owner = make_user("owner@example.test", "owner")
    make_room("secret", owner=owner, private=True)
    leak = upload(client_for(test_app, owner), "secret", b"password=1", "s.txt").json
    public = make_room("public")
    content = f"what is in {leak['url']}"
    assert attachment_context(content, public.id) == content


def test_fetched_web_images_are_kept_as_blobs(test_app):
    import app as app_module
    from models import Attachment, Message

    room = make_room("vision")
    data_url = "data:image/png;base64," + __import__("base64").b64encode(PNG).decode()
    app_module._external_image_cache.clear()
    app_module.save_fetched_image_as_message(
        "https://example.test/a.png", data_url, room.id
    )
    saved = Attachment.query.filter_by(source_url="https://example.test/a.png").one()
    assert saved.room_id == room.id and saved.mime_type == "image/png"
    assert Message.query.count() == 0  # no base64 message any more
    app_module._external_image_cache.clear()
    assert (
        app_module.find_saved_base64_for_url("https://example.test/a.png", room.id)
        == data_url
    )


# --- Stop -----------------------------------------------------------------


def test_stop_ends_a_streaming_reply(test_app, monkeypatch):
    import app as app_module
    from models import Message

    make_room("stoppable")

    def endless():
        while True:
            delta = SimpleNamespace(
                reasoning_content=None, reasoning=None, content="la "
            )
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=delta, finish_reason=None)]
            )

    sent = []

    def emit(event, data=None, **kwargs):
        sent.append(event)
        # Our Stop button, pressed once the reply is under way.
        if event == "message_chunk" and len(sent) > 5:
            app_module.stop_requests.add("stoppable")

    stream = endless()
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: stream))
    )
    monkeypatch.setattr(
        app_module, "get_openai_client_and_model", lambda n: (client, n)
    )
    monkeypatch.setattr(app_module.socketio, "emit", emit)
    monkeypatch.setattr(app_module.socketio, "sleep", lambda s: None)
    monkeypatch.setattr(app_module, "title_untitled_room", lambda name: None)

    app_module.stoppable_reply(app_module.chat_gpt, "ada", "stoppable", model_name="m")

    reply = Message.query.filter_by(username="m").one().content
    assert reply.startswith("la ") and len(reply) < 200
    assert stream.gi_frame is None  # hung up: the model server stops too
    assert "stoppable" not in app_module.stop_requests
    assert "stoppable" not in app_module.streaming_replies
    assert sent.count("replies_streaming") == 2  # one up, one down


def test_stop_needs_access_to_the_room(test_app):
    import app as app_module

    owner = make_user("owner@example.test", "owner")
    make_room("secret", owner=owner, private=True)
    app_module.streaming_replies["secret"] = 1
    try:
        guest = app_module.socketio.test_client(
            app_module.app, flask_test_client=test_app.test_client()
        )
        guest.emit("stop_replies", {"room_name": "secret"})
        assert "secret" not in app_module.stop_requests
    finally:
        app_module.streaming_replies.pop("secret", None)
        app_module.stop_requests.discard("secret")


# --- voice input ----------------------------------------------------------


def transcribe(client, data=b"RIFFaudio", name="speech.webm"):
    return client.post(
        "/speech/transcribe",
        data={"file": (io.BytesIO(data), name, "audio/webm")},
        content_type="multipart/form-data",
    )


def test_voice_is_transcribed_by_our_speech_service(test_app, monkeypatch):
    from routes import speech

    calls = []

    def post(url, files, data, headers, timeout):
        calls.append((url, data["model"]))
        return SimpleNamespace(ok=True, json=lambda: {"text": "  hello muppets "})

    monkeypatch.setenv("SPEECH_ENDPOINT_1", "http://stt.example.test/v1")
    monkeypatch.setattr(speech.requests, "post", post)
    response = transcribe(client_for(test_app))
    assert response.json == {"text": "hello muppets"}
    assert calls == [("http://stt.example.test/v1/audio/transcriptions", "whisper-1")]


def test_voice_input_says_when_no_service_transcribes(test_app, monkeypatch):
    from routes import speech

    monkeypatch.setenv("SPEECH_ENDPOINT_1", "http://stt.example.test/v1")
    monkeypatch.setattr(
        speech.requests,
        "post",
        lambda *a, **k: SimpleNamespace(ok=False, status_code=404),
    )
    response = transcribe(client_for(test_app))
    assert response.status_code == 502 and "unavailable" in response.json["error"]
    assert transcribe(client_for(test_app), data=b"").status_code == 400


def test_a_model_reads_the_files_in_its_chat(test_app, monkeypatch):
    import app as app_module
    from models import Message, db

    room = make_room("reading")
    url = upload(client_for(test_app), "reading", b"Elmo,red\n", "muppets.csv").json[
        "url"
    ]
    db.session.add(
        Message(username="ada", content=f"colours? [csv]({url})", room_id=room.id)
    )
    db.session.commit()
    sent = {}

    def create(**kwargs):
        sent.update(kwargs)
        return iter(())

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(
        app_module, "get_openai_client_and_model", lambda n: (client, n)
    )
    monkeypatch.setattr(app_module.socketio, "emit", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "title_untitled_room", lambda name: None)
    app_module.chat_gpt("ada", "reading", model_name="m")
    question = sent["messages"][-1]["content"]
    assert "[File: muppets.csv]\nElmo,red" in question
