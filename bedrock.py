"""AWS Bedrock's Claude models, streamed: the one kind of chat model we
reach with no OpenAI-compatible endpoint. A model id holding
`anthropic.claude` is asked here (dispatch_message, app.py's chat_claude).

No Flask, database or socket: app.py hands in the room's history & what
tells us to stop, & sends the words this yields. boto3 is imported only
when a reply is asked for, so a server that never uses Bedrock never
loads it. Credentials & region come from our AWS environment or profile
(`app.py --profile <name>`); no key of ours is read here."""

import json
import os

# Only when nothing names a region: where this path always asked before.
DEFAULT_REGION = "us-west-2"
MAX_TOKENS = 4096


def allowed(model):
    """True for a model id we may ask Bedrock for: one named in
    OPENCOMPLETION_BEDROCK_MODELS (comma separated; unset = none). A model
    id comes from whoever posts a message, & what we send Bedrock is
    asked under our AWS credentials, so never any id that merely looks
    like one of its Claude models."""
    named = os.environ.get("OPENCOMPLETION_BEDROCK_MODELS", "")
    return str(model) in {name.strip() for name in named.split(",") if name.strip()}


def text_of(content):
    """A turn's words: a vision turn's text parts, never its images."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def messages(chat_history):
    """Our room's history as Bedrock's Claude takes it: people's & models'
    words only (no system or tool turns, no images), & never two turns
    running from one role, which it refuses: their words are joined."""
    grouped = []
    for turn in chat_history:
        role, words = turn.get("role"), text_of(turn.get("content"))
        if role not in ("user", "assistant") or not words:
            continue
        if grouped and grouped[-1]["role"] == role:
            grouped[-1]["content"] += " " + words
        else:
            grouped.append({"role": role, "content": words})
    return grouped


def client(profile=None):
    """A bedrock-runtime client for `profile` (else our default
    credentials), in AWS_REGION, else the region boto3 finds itself
    (AWS_DEFAULT_REGION, the profile's), else DEFAULT_REGION."""
    import boto3

    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    region = os.environ.get("AWS_REGION") or session.region_name or DEFAULT_REGION
    return session.client("bedrock-runtime", region_name=region)


def request(model_name, chat_history):
    return {
        "modelId": model_name,
        "contentType": "application/json",
        "accept": "*/*",
        "body": json.dumps(
            {
                "messages": messages(chat_history),
                "max_tokens": MAX_TOKENS,
                "temperature": 0,
                "top_k": 250,
                "top_p": 0.999,
                "stop_sequences": ["\n\nHuman:"],
                "anthropic_version": "bedrock-2023-05-31",
            }
        ).encode(),
    }


def stream(model_name, chat_history, profile=None, stopped=lambda: False):
    """Ask model_name & yield its answer's words as they arrive. `stopped`
    is asked before each event: once true we hang up, so Bedrock stops
    generating too. Whatever boto3 raises is the caller's to report."""
    body = client(profile).invoke_model_with_response_stream(
        **request(model_name, chat_history)
    )["body"]
    for event in body:
        if stopped():
            getattr(body, "close", lambda: None)()
            return
        if "chunk" not in event:
            continue
        data = json.loads(event["chunk"]["bytes"].decode())
        if data.get("type") != "content_block_delta":
            continue
        delta = data.get("delta") or {}
        if delta.get("type") == "text_delta" and delta.get("text"):
            yield delta["text"]
