"""Our speech proxy (routes/speech.py): SPEECH_ENDPOINT_n / SPEECH_API_KEY_n
configure endpoints like models, keys never reach the browser, voices merge
across endpoints & audio falls through a dead endpoint. requests is
patched; nothing leaves the machine."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from routes import speech

pytestmark = pytest.mark.integration

VOICES_A = {"data": [{"id": "tts-1-f5", "voices": ["aria", "leo"]}]}
VOICES_B = {
    "data": [
        {"id": "tts-1-f5", "voices": ["other"]},
        {"id": "tts-1-kokoro", "voices": ["af_bella"]},
    ]
}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in list(speech.os.environ):
        if name.startswith(("SPEECH_ENDPOINT_", "SPEECH_API_KEY_")):
            monkeypatch.delenv(name)
    speech._voices_cache.clear()
    yield
    speech._voices_cache.clear()


@pytest.fixture
def client(test_app):
    return test_app.test_client()


@pytest.fixture
def two_endpoints(monkeypatch):
    monkeypatch.setenv("SPEECH_ENDPOINT_1", "https://a.test/v1/")
    monkeypatch.setenv("SPEECH_API_KEY_1", "key-a")
    monkeypatch.setenv("SPEECH_ENDPOINT_2", "https://b.test/v1")


def reply(status=200, json_body=None, chunks=(b"ID3audio",), content_type="audio/mpeg"):
    response = MagicMock()
    response.status_code = status
    response.headers = {"Content-Type": content_type}
    response.json.return_value = json_body
    response.iter_content.return_value = iter(chunks)
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(str(status))
    return response


def voices_by_url(url, **kwargs):
    return reply(json_body=VOICES_A if url.startswith("https://a.test") else VOICES_B)


def test_endpoints_default_to_public_speech_keyless():
    assert speech.speech_endpoints({}) == [(speech.DEFAULT_SPEECH_ENDPOINT, "")]


def test_endpoints_ordered_by_number_with_optional_keys():
    env = {
        "SPEECH_ENDPOINT_10": "https://ten.test/v1",
        "SPEECH_ENDPOINT_2": "https://two.test/v1/",
        "SPEECH_API_KEY_2": "k2",
        "SPEECH_ENDPOINT_3": "  ",
        "SPEECH_ENDPOINT_X": "https://ignored.test/v1",
    }
    assert speech.speech_endpoints(env) == [
        ("https://two.test/v1", "k2"),
        ("https://ten.test/v1", ""),
    ]


def test_voices_merge_with_first_endpoint_owning_a_model(client, two_endpoints):
    with patch.object(speech.requests, "get", side_effect=voices_by_url) as get:
        body = client.get("/speech/voices").get_json()
    assert [m["id"] for m in body["data"]] == ["tts-1-f5", "tts-1-kokoro"]
    assert body["data"][0]["voices"] == ["aria", "leo"]
    # Only the endpoint with a key sends one.
    assert get.call_args_list[0].kwargs["headers"] == {"Authorization": "Bearer key-a"}
    assert get.call_args_list[1].kwargs["headers"] == {}


def test_voices_are_cached(client, two_endpoints):
    with patch.object(speech.requests, "get", side_effect=voices_by_url) as get:
        client.get("/speech/voices")
        client.get("/speech/voices")
    assert get.call_count == 2  # one per endpoint, once


def test_voices_skip_a_dead_endpoint(client, two_endpoints):
    def get(url, **kwargs):
        if url.startswith("https://a.test"):
            raise requests.ConnectionError("down")
        return reply(json_body=VOICES_B)

    with patch.object(speech.requests, "get", side_effect=get):
        body = client.get("/speech/voices").get_json()
    assert body["data"][0]["voices"] == ["other"]


def test_voices_502_when_nothing_answers(client):
    with patch.object(speech.requests, "get", return_value=reply(status=503)):
        assert client.get("/speech/voices").status_code == 502


@pytest.mark.parametrize("body", [None, {}, {"model": "tts-1-f5"}, {"input": "hi"}])
def test_audio_rejects_incomplete_requests(client, body):
    response = (
        client.post("/speech/audio", data="not json")
        if body is None
        else client.post("/speech/audio", json=body)
    )
    assert response.status_code == 400


def test_audio_streams_from_the_endpoint_listing_the_model(client, two_endpoints):
    request_body = {"model": "tts-1-kokoro", "voice": "af_bella", "input": "hello"}
    with patch.object(speech.requests, "get", side_effect=voices_by_url), patch.object(
        speech.requests, "post", return_value=reply()
    ) as post:
        response = client.post("/speech/audio", json=request_body)
        assert response.status_code == 200
        assert response.data == b"ID3audio"
        assert response.headers["Content-Type"] == "audio/mpeg"
    assert post.call_args.args[0] == "https://b.test/v1/audio/speech"
    assert post.call_args.kwargs["json"] == request_body
    assert post.call_args.kwargs["stream"] is True


def test_audio_sends_the_key_server_side(client, two_endpoints):
    with patch.object(speech.requests, "get", side_effect=voices_by_url), patch.object(
        speech.requests, "post", return_value=reply()
    ) as post:
        client.post("/speech/audio", json={"model": "tts-1-f5", "input": "hi"})
    assert post.call_args.args[0] == "https://a.test/v1/audio/speech"
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer key-a"}


def test_audio_falls_through_a_failing_endpoint(client, two_endpoints):
    # tts-1-f5 is listed by both; a.test answers 502, b.test serves it.
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        if url.startswith("https://a.test"):
            return reply(
                status=502, chunks=(b"bad gateway",), content_type="text/plain"
            )
        return reply(
            chunks=(b"event: sentence\n", b"data: {}\n\n"),
            content_type="text/event-stream",
        )

    with patch.object(speech.requests, "get", side_effect=voices_by_url), patch.object(
        speech.requests, "post", side_effect=post
    ):
        response = client.post(
            "/speech/audio", json={"model": "tts-1-f5", "input": "hi", "sse": True}
        )
        assert response.status_code == 200
        assert response.data == b"event: sentence\ndata: {}\n\n"
        assert response.headers["Content-Type"] == "text/event-stream"
    assert calls == ["https://a.test/v1/audio/speech", "https://b.test/v1/audio/speech"]


def test_audio_keeps_a_client_error_status(client, two_endpoints):
    with patch.object(speech.requests, "get", side_effect=voices_by_url), patch.object(
        speech.requests,
        "post",
        return_value=reply(
            status=401,
            chunks=(b'{"error":"bad key"}',),
            content_type="application/json",
        ),
    ):
        response = client.post(
            "/speech/audio", json={"model": "tts-1-f5", "input": "hi"}
        )
    assert response.status_code == 401
    assert response.get_json() == {"error": "bad key"}


def test_audio_502_when_every_endpoint_is_down(client, two_endpoints):
    with patch.object(speech.requests, "get", side_effect=voices_by_url), patch.object(
        speech.requests, "post", side_effect=requests.ConnectionError("down")
    ):
        response = client.post(
            "/speech/audio", json={"model": "tts-1-f5", "input": "hi"}
        )
    assert response.status_code == 502


# An endpoint with no /voices route (OpenAI's API) beside our own service.
OPENAI_MODELS = {
    "data": [
        {"id": "gpt-5"},
        {"id": "tts-1"},
        {"id": "tts-1-1106"},
        {"id": "gpt-4o-mini-tts"},
        {"id": "gpt-4o-mini-tts-2025-12-15"},
        {"id": "gpt-4o-transcribe-diarize"},
        {"id": "gpt-4o-transcribe"},
        {"id": "whisper-1"},
        {"id": "gpt-image-2"},
    ]
}
OURS_MODELS = {"data": [{"id": "tts-1-f5"}, {"id": "stt-1-parakeet"}]}


def ours_and_openai(url, **kwargs):
    """https://a.test is our service (it lists voices); https://b.test has
    no /voices, as OpenAI's API."""
    ours = url.startswith("https://a.test")
    if url.endswith("/voices"):
        return reply(json_body=VOICES_A) if ours else reply(status=404)
    return reply(json_body=OURS_MODELS if ours else OPENAI_MODELS)


@pytest.fixture
def no_models_cache():
    speech._models_cache.clear()
    yield
    speech._models_cache.clear()


def test_an_endpoint_without_voices_offers_the_speech_models_it_lists(
    client, two_endpoints, no_models_cache
):
    with patch.object(speech.requests, "get", side_effect=ours_and_openai):
        body = client.get("/speech/voices").get_json()
    # Ours first & exactly as it listed them; then each speech model the
    # other lists today, once (no dated copies), never a chat, image or
    # transcription model.
    assert body["data"] == [
        {"id": "tts-1-f5", "voices": ["aria", "leo"]},
        {"id": "tts-1", "voices": list(speech.OPENAI_VOICES)},
        {"id": "gpt-4o-mini-tts", "voices": list(speech.OPENAI_VOICES)},
    ]


def test_such_an_endpoints_voices_can_be_named(
    client, two_endpoints, no_models_cache, monkeypatch
):
    monkeypatch.setenv("SPEECH_VOICES_2", "marin, cedar ,")
    monkeypatch.setenv("SPEECH_VOICES_1", "ignored")  # ours lists its own
    with patch.object(speech.requests, "get", side_effect=ours_and_openai):
        body = client.get("/speech/voices").get_json()
    assert [(m["id"], m["voices"]) for m in body["data"]] == [
        ("tts-1-f5", ["aria", "leo"]),
        ("tts-1", ["marin", "cedar"]),
        ("gpt-4o-mini-tts", ["marin", "cedar"]),
    ]


def test_our_own_service_being_down_never_borrows_voice_names(
    client, two_endpoints, no_models_cache
):
    """Only a 404 (no such route) is read from /models: a /voices that
    errors is a failure, as it always was."""

    def get(url, **kwargs):
        if url.endswith("/voices"):
            return reply(status=503)
        return reply(json_body=OURS_MODELS)

    with patch.object(speech.requests, "get", side_effect=get):
        assert client.get("/speech/voices").status_code == 502


def test_audio_for_a_listed_speech_model_goes_to_the_endpoint_listing_it(
    client, two_endpoints, no_models_cache
):
    with patch.object(
        speech.requests, "get", side_effect=ours_and_openai
    ), patch.object(speech.requests, "post", return_value=reply()) as post:
        response = client.post(
            "/speech/audio",
            json={"model": "gpt-4o-mini-tts", "voice": "alloy", "input": "hi"},
        )
    assert response.status_code == 200
    assert post.call_args.args[0] == "https://b.test/v1/audio/speech"
    assert post.call_args.kwargs["json"]["model"] == "gpt-4o-mini-tts"


def test_each_endpoint_is_asked_for_a_transcription_model_it_lists(
    two_endpoints, no_models_cache, monkeypatch
):
    monkeypatch.delenv("SPEECH_STT_MODEL", raising=False)
    with patch.object(speech.requests, "get", side_effect=ours_and_openai):
        # Ours keeps the model it lists; the other gets its own plain one,
        # never ours (a name it does not know) nor a diarizing one.
        assert speech.stt_model_for("https://a.test/v1") == "stt-1-parakeet"
        assert speech.stt_model_for("https://b.test/v1") == "gpt-4o-transcribe"
        # An endpoint listing none is asked as before: for the first listed.
        assert speech.stt_model_for("https://c.test/v1") == "stt-1-parakeet"
        # SPEECH_STT_MODEL still names it everywhere.
        monkeypatch.setenv("SPEECH_STT_MODEL", "my-whisper")
        assert speech.stt_model_for("https://b.test/v1") == "my-whisper"
