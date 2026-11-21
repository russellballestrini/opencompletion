"""bedrock.py: AWS Bedrock's Claude models, with boto3 replaced. No test
here reaches AWS; what Bedrock itself answers is not proven by them."""

import json
import sys
from types import SimpleNamespace

import pytest

import bedrock


def delta(text):
    return {
        "chunk": {
            "bytes": json.dumps(
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": text},
                }
            ).encode()
        }
    }


class FakeBody(list):
    closed = False

    def close(self):
        self.closed = True


class FakeBoto3:
    """boto3 as far as bedrock.py uses it: a Session, its region & a
    client whose stream is `events` (or raises `error`)."""

    def __init__(self, events=(), region=None, error=None):
        self.body, self.region, self.error = FakeBody(events), region, error
        self.sessions, self.clients, self.requests = [], [], []

    def Session(self, **kwargs):
        self.sessions.append(kwargs)
        return SimpleNamespace(region_name=self.region, client=self._client)

    def _client(self, service, region_name=None):
        self.clients.append((service, region_name))
        return SimpleNamespace(invoke_model_with_response_stream=self._invoke)

    def _invoke(self, **params):
        if self.error:
            raise self.error
        self.requests.append(params)
        return {"body": self.body}


@pytest.fixture
def fake(monkeypatch):
    def install(**kwargs):
        boto3 = FakeBoto3(**kwargs)
        monkeypatch.setitem(sys.modules, "boto3", boto3)
        return boto3

    monkeypatch.delenv("AWS_REGION", raising=False)
    return install


def test_messages_keep_words_only_and_never_repeat_a_role():
    history = [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Hello"},
        {"role": "user", "content": [{"type": "text", "text": "How are you?"}]},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "42"},
        {"role": "assistant", "content": "Fine"},
        {"role": "user", "content": "Great!"},
    ]
    assert bedrock.messages(history) == [
        {"role": "user", "content": "Hello How are you?"},
        {"role": "assistant", "content": "Fine"},
        {"role": "user", "content": "Great!"},
    ]
    assert bedrock.messages([]) == []


def test_stream_yields_text_deltas_and_asks_as_before(fake):
    boto3 = fake(
        events=[
            {"chunk": {"bytes": json.dumps({"type": "message_start"}).encode()}},
            delta("Hel"),
            {"metadata": {}},
            delta("lo"),
        ]
    )
    words = list(
        bedrock.stream("anthropic.claude-x", [{"role": "user", "content": "Hi"}])
    )
    assert words == ["Hel", "lo"]
    (params,) = boto3.requests
    assert params["modelId"] == "anthropic.claude-x"
    body = json.loads(params["body"])
    assert body["messages"] == [{"role": "user", "content": "Hi"}]
    assert body["max_tokens"] == 4096 and body["temperature"] == 0
    assert body["anthropic_version"] == "bedrock-2023-05-31"


def test_stop_hangs_up(fake):
    boto3 = fake(events=[delta("one"), delta("two"), delta("three")])
    heard = []
    for words in bedrock.stream("m", [], stopped=lambda: len(heard) >= 1):
        heard.append(words)
    assert heard == ["one"] and boto3.body.closed


def test_region_is_aws_region_then_boto3s_own_then_our_default(fake, monkeypatch):
    boto3 = fake()
    bedrock.client()
    assert boto3.sessions == [{}]
    assert boto3.clients == [("bedrock-runtime", "us-west-2")]

    boto3 = fake(region="eu-central-1")
    bedrock.client("work")
    assert boto3.sessions == [{"profile_name": "work"}]
    assert boto3.clients == [("bedrock-runtime", "eu-central-1")]

    monkeypatch.setenv("AWS_REGION", "us-east-1")
    bedrock.client()
    assert boto3.clients[-1] == ("bedrock-runtime", "us-east-1")


def test_bedrock_never_loads_boto3_until_asked():
    source = open(bedrock.__file__).read()
    assert "\nimport boto3" not in source and "\nfrom boto3" not in source
    app_source = open(bedrock.__file__.replace("bedrock.py", "app.py")).read()
    assert "import boto3" not in app_source


@pytest.fixture
def room(test_app, monkeypatch):
    """A room holding one question, our socket's emits kept, & nothing
    titled (that asks a model)."""
    import app as app_module
    from models import Message, Room, db

    room = Room(name="bedrock-room")
    db.session.add(room)
    db.session.commit()
    db.session.add(Message(username="ada", content="Say hello", room_id=room.id))
    db.session.commit()
    emitted = []
    monkeypatch.setattr(
        app_module.socketio,
        "emit",
        lambda event, data=None, **k: emitted.append((event, data)),
    )
    monkeypatch.setattr(app_module.socketio, "sleep", lambda *a: None)
    monkeypatch.setattr(app_module, "title_untitled_room", lambda name: None)
    monkeypatch.setattr(app_module.gevent, "spawn", lambda *a, **k: None)
    return SimpleNamespace(app=app_module, emitted=emitted, id=room.id)


def test_a_reply_is_stored_and_the_room_hears_chunks_and_a_completion(fake, room):
    from models import Message

    boto3 = fake(events=[delta("Hello"), delta(" there")])
    model = "anthropic.claude-test"
    room.app.chat_claude("ada", "bedrock-room", model_name=model)

    # Asked with our room's history.
    assert json.loads(boto3.requests[0]["body"])["messages"] == [
        {"role": "user", "content": "Say hello"}
    ]
    reply = Message.query.filter_by(room_id=room.id, username=model).one()
    assert reply.content == "Hello there"

    chunks = [data for event, data in room.emitted if event == "message_chunk"]
    assert [c["content"] for c in chunks] == ["Hello", " there", ""]
    assert chunks[0]["is_first_chunk"] and chunks[0]["username"] == "ada"
    assert chunks[0]["model_name"] == model
    assert all(c["id"] == reply.id for c in chunks)
    assert chunks[-1]["is_complete"] and chunks[-1]["final"] == "Hello there"
    assert ("delete_processing_message", reply.id) in room.emitted


def test_a_failure_becomes_the_reply(fake, room):
    from models import Message

    fake(error=RuntimeError("no credentials"))
    model = "anthropic.claude-test"
    room.app.chat_claude("ada", "bedrock-room", model_name=model)

    reply = Message.query.filter_by(room_id=room.id, username=model).one()
    assert "AWS Bedrock: no credentials" in reply.content
    said = [data for event, data in room.emitted if event == "chat_message"]
    assert said and said[-1]["content"] == reply.content
    assert not [d for e, d in room.emitted if e == "message_chunk"]


def test_dispatch_sends_a_bedrock_claude_model_to_chat_claude():
    source = open(bedrock.__file__.replace("bedrock.py", "app.py")).read()
    assert 'if "anthropic.claude" in model:' in source
