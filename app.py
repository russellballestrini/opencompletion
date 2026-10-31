# import eventlet
# eventlet.monkey_patch()

import gevent
from gevent import monkey

monkey.patch_all()


import json
import time
import base64
import os
import re

import boto3
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room

from models import db, Room, UserSession, Message, ActivityState, User, Attachment
from blob_store import (
    attachment_context,
    attachment_markdown,
    data_url as attachment_data_url,
    has_image_attachment,
    inline_images_to_attachments,
    max_bytes as max_upload_bytes,
    save_attachment,
)

app = Flask(__name__, instance_relative_config=True)

# SQLALCHEMY_DATABASE_URI picks another database (pytest.ini & CI use
# sqlite:///:memory:); by default a SQLite file lives in instance/.
os.makedirs(app.instance_path, exist_ok=True)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("SQLALCHEMY_DATABASE_URI") or (
    f"sqlite:///{os.path.join(app.instance_path, 'chat.db')}"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# Enable template auto-reload to prevent stale templates during development
app.config["TEMPLATES_AUTO_RELOAD"] = True

db.init_app(app)

# Every /static/ link carries ?v=<a hash of the file>, so a deploy's new
# JavaScript & CSS reach browsers at once instead of waiting out a cache.
_static_versions = {}


@app.url_defaults
def static_version(endpoint, values):
    if endpoint != "static" or "filename" not in values or "v" in values:
        return
    path = os.path.join(app.static_folder, values["filename"])
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return
    cached = _static_versions.get(path)
    if cached is None or cached[0] != mtime:
        import hashlib

        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()[:10]
        cached = _static_versions[path] = (mtime, digest)
    values["v"] = cached[1]


from flask_migrate import Migrate

migrate = Migrate(app, db)

# socketio = SocketIO(app, async_mode="eventlet")
socketio = SocketIO(app, async_mode="gevent")

# Tools a chat model may call (web search, web fetch): all of it lives in
# tools/ & docs/TOOLS.md; chat_gpt is its one caller.
import dataclasses

import tools
from tools import sandbox as tool_sandbox

tools.register(socketio)
tool_sandbox.init_app(app)

# Global dictionary to keep track of cancellation requests
cancellation_requests = {}
# Our Stop button: rooms whose streaming replies should end now, & how
# many replies each room has streaming (stoppable_reply keeps both).
stop_requests = set()
streaming_replies = {}

import openai
from openai import OpenAI
import activity
import auth

# Session cookies are signed with SECRET_KEY, else a random key kept in
# instance/secret_key (auth.load_secret_key); never a published default.
app.config["SECRET_KEY"] = auth.load_secret_key(app.instance_path)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
from activity_utils import create_completion_skip_thinking, notice_id, strip_reasoning

# Build a list of endpoints dynamically.
ENDPOINTS = []
CONFIGURED_MODEL_NUMS = []
MAX_ENDPOINTS = 1000

for i in range(MAX_ENDPOINTS):
    endpoint = os.environ.get(f"MODEL_ENDPOINT_{i}")
    if not endpoint:
        continue
    # API key is optional; if not provided, use a default.
    api_key = os.environ.get(f"MODEL_API_KEY_{i}", "not-needed")
    ENDPOINTS.append(
        {
            "base_url": endpoint,
            "api_key": api_key,
        }
    )
    CONFIGURED_MODEL_NUMS.append(str(i))

if not ENDPOINTS:
    raise Exception("No MODEL_ENDPOINT_x environment variables found!")

# Default model reference. Switch mains by setting DEFAULT_MODEL in vars.sh
# (e.g. DEFAULT_MODEL=MODEL_2); a MODEL_n that is down falls through to the
# next healthy configured endpoint automatically.
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", f"MODEL_{CONFIGURED_MODEL_NUMS[0]}")

# Build a dynamic model map by querying each endpoint. MODEL_CLIENT_MAP
# holds each model id's pinned endpoint: the lowest MODEL_ENDPOINT_n that
# lists it. MODEL_ENDPOINTS holds every endpoint serving it, pin first, so
# a model whose pin goes down fails over to another copy of itself.
MODEL_CLIENT_MAP = {}
MODEL_ENDPOINTS = {}
# base_url -> model ids it last listed; kept while it is down, so a MODEL_n
# reference still knows which model to look for elsewhere.
_endpoint_models = {}
SYSTEM_USERS = []


def get_client_for_endpoint(endpoint, api_key):
    # All providers use the OpenAI client; no endpoint URLs are hardcoded here.
    return OpenAI(api_key=api_key, base_url=endpoint)


# Endpoint health cache: base_url -> (healthy, checked_at). The TTL keeps
# down endpoints out of rotation without hammering them on every request.
ENDPOINT_HEALTH_TTL = 60
_endpoint_health = {}


def endpoint_is_healthy(client, base_url):
    healthy, checked_at = _endpoint_health.get(base_url, (None, 0.0))
    if healthy is not None and time.time() - checked_at < ENDPOINT_HEALTH_TTL:
        return healthy
    try:
        client.with_options(timeout=5.0, max_retries=0).models.list()
        healthy = True
    except Exception as e:
        print(f"[WARN] Endpoint {base_url} failed health check: {e}")
        healthy = False
    _endpoint_health[base_url] = (healthy, time.time())
    return healthy


def mark_endpoint_down(base_url):
    """Take an endpoint out of rotation until its next health check."""
    _endpoint_health[base_url] = (False, time.time())


def _same_url(a, b):
    return str(a).rstrip("/") == str(b).rstrip("/")


def endpoints_for_model(model_name):
    """Every (client, base_url) serving model_name, its pin first."""
    if model_name in MODEL_ENDPOINTS:
        return MODEL_ENDPOINTS[model_name]
    if model_name in MODEL_CLIENT_MAP:
        return [MODEL_CLIENT_MAP[model_name]]
    return []


def healthy_endpoint_for_model(model_name):
    """The first healthy (client, base_url) serving model_name, or None."""
    endpoints = endpoints_for_model(model_name)
    for client, base_url in endpoints:
        if endpoint_is_healthy(client, base_url):
            if not _same_url(base_url, endpoints[0][1]):
                print(
                    f"[WARN] {model_name}: {endpoints[0][1]} is down; "
                    f"serving it from {base_url}"
                )
            return client, base_url
    return None


# Failures worth another endpoint: it was unreachable, timed out or broke
# (5xx). A 4xx is our request's fault and would fail anywhere.
FAILOVER_ERRORS = (openai.APIConnectionError, openai.InternalServerError)


def open_with_failover(openai_client, model_name, open_stream):
    """open_stream(client) starts a completion; if opening the stream fails
    (no connection, a timeout, a 5xx), mark that endpoint down & try the next
    one serving this same model. Only opening is retried: a stream that
    breaks while it is read, even before its first token, is not."""
    given = str(getattr(openai_client, "base_url", ""))
    endpoints = endpoints_for_model(model_name)
    # Health is keyed by the configured URL; the client's own adds a "/".
    given = next((url for _, url in endpoints if _same_url(url, given)), given)
    attempts = [(openai_client, given)] + [
        (client, url) for client, url in endpoints if not _same_url(url, given)
    ]
    for i, (client, base_url) in enumerate(attempts):
        if i and not endpoint_is_healthy(client, base_url):
            continue
        try:
            return open_stream(client)
        except FAILOVER_ERRORS as e:
            mark_endpoint_down(base_url)
            rest = attempts[i + 1 :]
            if not any(endpoint_is_healthy(c, url) for c, url in rest):
                raise
            print(f"[WARN] {model_name} failed on {base_url} ({e}); trying next")


def is_vision_model(model_name: str) -> bool:
    """Check if a model supports vision/image input."""
    if not model_name:
        return False
    model_lower = model_name.lower()
    vision_indicators = ["-vl", "vl:", "vision", "gpt-4o", "gpt-4-turbo"]
    return any(indicator in model_lower for indicator in vision_indicators)


def initialize_model_map():
    MODEL_CLIENT_MAP.clear()
    MODEL_ENDPOINTS.clear()
    for ep_config in ENDPOINTS:
        base_url = ep_config["base_url"]
        api_key = ep_config["api_key"]
        client = get_client_for_endpoint(base_url, api_key)
        try:
            # One try: a dead endpoint costs 10 s, not 10 s per retry.
            response = client.with_options(timeout=10.0, max_retries=0).models.list()
            model_list = response.data  # Assume each model object has an 'id' attribute
            print(f"[DEBUG] {base_url} returned models: {[m.id for m in model_list]}")
        except Exception as e:
            print(f"[WARN] Could not list models for endpoint '{base_url}': {e}")
            continue

        _endpoint_models[base_url] = [m.id for m in model_list if m.id]
        for model_id in _endpoint_models[base_url]:
            MODEL_ENDPOINTS.setdefault(model_id, []).append((client, base_url))
            if model_id not in MODEL_CLIENT_MAP:
                MODEL_CLIENT_MAP[model_id] = (client, base_url)

    # Grow SYSTEM_USERS in place (activity.py holds a reference to this
    # list) and never remove names: messages from a model that left the
    # rotation must keep their assistant role in rebuilt chat history.
    for name in list(MODEL_CLIENT_MAP.keys()) + ["system"]:
        if name not in SYSTEM_USERS:
            SYSTEM_USERS.append(name)
    print("Loaded models:", list(MODEL_CLIENT_MAP.keys()))


# Creating & migrating tables (make init-db, make upgrade-db) needs no models:
# OPENCOMPLETION_SKIP_MODEL_MAP=1 skips asking every endpoint for its list, so
# one endpoint that never answers can't stall a migration.
if not MODEL_CLIENT_MAP and os.environ.get("OPENCOMPLETION_SKIP_MODEL_MAP") != "1":
    initialize_model_map()


# 4) Lookup function: get an OpenAI client for a given model name
def get_client_for_model(model_name: str):
    """
    If the model name is known, return its dedicated client.
    Otherwise, return None and LOL at the user when everything breaks.
    """
    if model_name in MODEL_CLIENT_MAP:
        print(f"Completion Endpoint Processing: {MODEL_CLIENT_MAP[model_name][1]}")
        return MODEL_CLIENT_MAP[model_name][0]


def extract_base64_from_img_tag(content: str) -> tuple[str, str] | None:
    """Extract base64 data and media type from an HTML img tag.

    Returns (media_type, base64_data) or None if not found.
    """
    import re

    # Match data:image/TYPE;base64,DATA patterns in img src
    pattern = r'<img[^>]*src="data:image/(jpeg|png|gif|webp);base64,([^"]+)"'
    match = re.search(pattern, content)
    if match:
        media_type = f"image/{match.group(1)}"
        base64_data = match.group(2)
        return (media_type, base64_data)
    return None


def extract_external_image_url(content: str) -> str | None:
    """Extract external image URL from an HTML img tag.

    Returns the URL or None if not found.
    """
    import re

    # Match external URLs in img src (http/https)
    pattern = r'<img[^>]*src="(https?://[^"]+)"'
    match = re.search(pattern, content)
    if match:
        return match.group(1)
    return None


# Cache for fetched external images (URL -> base64 data URL)
_external_image_cache = {}

# CORS proxy for ethical fetching (respects robots.txt)
CORS_PROXY_URL = "https://cors-proxy.uncloseai.com/api/fetch"


def escape_like_pattern(s: str) -> str:
    """Escape special characters for SQL LIKE patterns."""
    # Escape %, _, and \ which have special meaning in LIKE
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def find_saved_base64_for_url(external_url: str, room_id: int) -> str | None:
    """Look up if we've already saved a base64 version of this URL in this room.

    Returns the data URL if found, None otherwise.
    """
    # Check in-memory cache first
    if external_url in _external_image_cache:
        return _external_image_cache[external_url]

    # Saved as a blob (indexed by source_url): one lookup, no scan
    saved = Attachment.query.filter_by(room_id=room_id, source_url=external_url).first()
    if saved:
        _external_image_cache[external_url] = attachment_data_url(saved)
        return _external_image_cache[external_url]

    # Older rooms saved fetched images as base64 inside a message

    # Look for a saved message with this URL in the alt text
    try:
        # Escape special LIKE characters in URL (%, _, \)
        escaped_url = escape_like_pattern(external_url)
        # Search for messages containing "Fetched from {external_url}"
        saved_msg = Message.query.filter(
            Message.room_id == room_id,
            Message.content.like(f'%alt="Fetched from {escaped_url}"%', escape="\\"),
        ).first()

        if saved_msg:
            # Extract base64 from the saved message
            img_data = extract_base64_from_img_tag(saved_msg.content)
            if img_data:
                media_type, base64_data = img_data
                data_url = f"data:{media_type};base64,{base64_data}"
                # Add to memory cache for faster lookups
                _external_image_cache[external_url] = data_url
                print(
                    f"Found saved base64 for {external_url} in message {saved_msg.id}"
                )
                return data_url
    except Exception as e:
        print(f"Error looking up saved base64: {e}")

    return None


def fetch_external_image_as_base64(image_url: str) -> str | None:
    """Fetch external image via CORS proxy and convert to base64.

    Returns data URL (data:image/...;base64,...) or None on failure.
    """
    import base64
    import httpx

    # Check cache first
    if image_url in _external_image_cache:
        return _external_image_cache[image_url]

    try:
        proxy_url = f"{CORS_PROXY_URL}?uri_target={image_url}"
        with httpx.Client(timeout=15.0) as client:
            response = client.get(proxy_url)

            if response.status_code == 403:
                print(f"Image blocked by robots.txt: {image_url}")
                return None

            response.raise_for_status()

            # Check content type
            content_type = response.headers.get("content-type", "")
            if not content_type.startswith("image/"):
                print(f"Not an image: {image_url} ({content_type})")
                return None

            # Convert to base64
            b64_data = base64.b64encode(response.content).decode("utf-8")
            media_type = content_type.split(";")[0]  # Remove charset if present
            data_url = f"data:{media_type};base64,{b64_data}"

            # Cache it
            _external_image_cache[image_url] = data_url
            return data_url

    except Exception as e:
        print(f"Failed to fetch external image {image_url}: {e}")
        return None


def save_fetched_image_as_message(
    external_url: str, data_url: str, room_id: int
) -> None:
    """Keep a fetched external image so the next turn needn't fetch it:
    its bytes go to our blob store (blob_store.py) as an attachment of the
    room, found again by source_url. Images over our upload limit are only
    cached in memory. (Older rooms kept these as base64 inside messages;
    find_saved_base64_for_url still reads those.)"""
    _external_image_cache[external_url] = data_url
    try:
        if Attachment.query.filter_by(room_id=room_id, source_url=external_url).first():
            return
        header, _, encoded = data_url.partition(",")
        data = base64.b64decode(encoded)
        if len(data) > max_upload_bytes():
            return
        mime_type = header[len("data:") :].split(";")[0] or "image/jpeg"
        name = external_url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "image"
        save_attachment(
            room_id, data, name, mime_type, "system", source_url=external_url
        )
    except Exception as e:
        print(f"[Vision Save] FAILED: {e}")
        db.session.rollback()


def build_message_content(msg, is_vision: bool, room_id: int = None) -> dict | str:
    """Build message content, handling images for vision models.

    For vision models with images, returns multimodal content array.
    Otherwise returns plain text content.

    If room_id is provided and an external image is fetched, saves the
    base64 version as a new message for future use.
    """
    if not is_vision:
        return msg.content

    # Check if this message contains a base64 image
    img_data = extract_base64_from_img_tag(msg.content)
    if img_data:
        media_type, base64_data = img_data
        # Return multimodal content with image
        return [
            {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{base64_data}"},
            }
        ]

    # Check for external image URL
    external_url = extract_external_image_url(msg.content)
    if external_url:
        print(
            f"[Vision] Found external URL in message {msg.id}: {external_url[:80]}..."
        )

        # First check if we already have a saved base64 version
        if room_id is not None:
            saved_data_url = find_saved_base64_for_url(external_url, room_id)
            if saved_data_url:
                print(f"[Vision] Using saved base64 for {external_url[:50]}...")
                return [{"type": "image_url", "image_url": {"url": saved_data_url}}]

        # Not saved yet - fetch and save
        print(f"[Vision] Fetching external image: {external_url[:80]}...")
        data_url = fetch_external_image_as_base64(external_url)
        if data_url:
            print(f"[Vision] Fetched successfully, saving to room {room_id}...")
            # Save the fetched image as a new message for persistence
            if room_id is not None:
                save_fetched_image_as_message(external_url, data_url, room_id)
            return [{"type": "image_url", "image_url": {"url": data_url}}]
        else:
            print(f"[Vision] Failed to fetch external image")

    # Plain text message
    return msg.content


def extract_first_image_for_og(room_id: int) -> str | None:
    """Extract the first image URL from messages for Open Graph meta tags.

    Checks messages in the room for:
    1. Base64 images (returns as data URL - may be large)
    2. External image URLs in markdown format ![alt](url)
    3. External image URLs in img tags

    Returns image URL/data URL or None if no image found.
    """
    import re

    messages = Message.visible(room_id).order_by(Message.id.asc()).limit(50).all()

    for msg in messages:
        if not msg.content:
            continue

        # Check for base64 image
        if msg.is_base64_image():
            img_data = extract_base64_from_img_tag(msg.content)
            if img_data:
                media_type, base64_data = img_data
                return f"data:{media_type};base64,{base64_data}"

        # Check for markdown image ![alt](url)
        md_img_match = re.search(r"!\[[^\]]*\]\(([^)]+)\)", msg.content)
        if md_img_match:
            url = md_img_match.group(1)
            if url.startswith(("http://", "https://")):
                return url

        # Check for img tag with src
        img_src_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', msg.content)
        if img_src_match:
            url = img_src_match.group(1)
            if url.startswith(("http://", "https://")):
                return url

    return None


def generate_og_description(room, max_chars: int = 500) -> str:
    """Generate a description for Open Graph meta tags.

    Uses room title if available, then extracts text from first few messages.
    Strips markdown/code/images and limits to max_chars.
    """
    import re

    parts = []

    # Start with room title if available
    if room and room.title:
        parts.append(room.title)

    # Get first few messages for description
    if room:
        messages = Message.visible(room.id).order_by(Message.id.asc()).limit(10).all()

        for msg in messages:
            if not msg.content:
                continue

            # Skip base64 images
            if msg.is_base64_image():
                continue

            text = msg.content

            # Remove code blocks
            text = re.sub(r"```[\s\S]*?```", "", text)
            text = re.sub(r"`[^`]+`", "", text)

            # Remove markdown images
            text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)

            # Remove HTML tags
            text = re.sub(r"<[^>]+>", "", text)

            # Remove markdown links but keep text
            text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

            # Remove markdown headers
            text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)

            # Remove bold/italic markers
            text = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", text)
            text = re.sub(r"_{1,2}([^_]+)_{1,2}", r"\1", text)

            # Clean up whitespace
            text = re.sub(r"\s+", " ", text).strip()

            if text:
                parts.append(text)

            # Stop if we have enough content
            if len(" ".join(parts)) > max_chars:
                break

    description = " ".join(parts)

    # Truncate to max_chars
    if len(description) > max_chars:
        description = description[: max_chars - 3].rsplit(" ", 1)[0] + "..."

    return description if description else "A chat room on OpenCompletion"


def _resolve_model_num(model_num, check_health=True):
    """Resolve MODEL_<num> env config to (client, model_name), or None."""
    endpoint = os.environ.get(f"MODEL_ENDPOINT_{model_num}")
    api_key = os.environ.get(f"MODEL_API_KEY_{model_num}")
    if not endpoint or not api_key:
        return None
    client = get_client_for_endpoint(endpoint, api_key)
    if check_health and not endpoint_is_healthy(client, endpoint):
        return None

    # Explicit MODEL_NAME_X wins: endpoints like Gemini list dozens of
    # models (some retired) and "first listed" picks wrong.
    explicit_model = os.environ.get(f"MODEL_NAME_{model_num}")
    if explicit_model:
        return client, explicit_model

    for model_id, (registered_client, base_url) in MODEL_CLIENT_MAP.items():
        if base_url == endpoint:
            return client, model_id

    # Fallback: query endpoint for models if not in map yet
    try:
        response = client.with_options(timeout=10.0, max_retries=0).models.list()
        if response.data:
            actual_model = response.data[0].id
            print(f"[DEBUG] Using first model from {endpoint}: {actual_model}")
            return client, actual_model
    except Exception as e:
        print(f"Warning: Could not query models from {endpoint}: {e}")
        return None

    print(f"Warning: No models found for {endpoint}, using 'model' as fallback")
    return client, "model"


def _model_for_num(model_num):
    """The model id MODEL_<num> serves (MODEL_NAME_n, else what its endpoint
    last listed), even while that endpoint is down; None if unknown."""
    explicit = os.environ.get(f"MODEL_NAME_{model_num}")
    if explicit:
        return explicit
    endpoint = os.environ.get(f"MODEL_ENDPOINT_{model_num}")
    listed = _endpoint_models.get(endpoint) or []
    return listed[0] if listed else None


def get_openai_client_and_model(model_name=None):
    """Get OpenAI client and model name.

    Supports MODEL_X references (e.g., MODEL_1, MODEL_2, MODEL_3) that map to
    environment variables MODEL_ENDPOINT_X and MODEL_API_KEY_X. A MODEL_X
    whose endpoint is down first tries other endpoints serving the same
    model, then falls through to the next healthy configured endpoint, so
    activities keep working when the primary leaves rotation. Set
    DEFAULT_MODEL to change which reference is main.

    A model id (chat's dropdown) is pinned to the lowest MODEL_ENDPOINT_n
    listing it & fails over to the other endpoints listing it, in order.
    """
    if not model_name or model_name == "None":
        model_name = DEFAULT_MODEL

    if model_name.startswith("MODEL_"):
        requested = model_name.split("_")[1]
        resolved = _resolve_model_num(requested)
        if resolved:
            return resolved
        # Its endpoint is down: the same model on another endpoint first.
        pinned = _model_for_num(requested)
        if pinned:
            found = healthy_endpoint_for_model(pinned)
            if found:
                return found[0], pinned
        candidates = [requested] + [n for n in CONFIGURED_MODEL_NUMS if n != requested]
        for num in candidates[1:]:
            resolved = _resolve_model_num(num)
            if resolved:
                if num != requested:
                    print(
                        f"[WARN] MODEL_{requested} unavailable; "
                        f"falling back to MODEL_{num} ({resolved[1]})"
                    )
                return resolved
        # Nothing healthy: build the first configured client anyway so the
        # failure surfaces as a real completion error instead of a crash.
        for num in candidates:
            resolved = _resolve_model_num(num, check_health=False)
            if resolved:
                return resolved
        print(f"Warning: no MODEL_x endpoints configured for {model_name}")
        return None, model_name

    # Direct model id (chat dropdown): its pinned endpoint first, then any
    # other endpoint serving the same model. Only when every copy is down
    # does the default rotation answer; the reply is attributed to whichever
    # model actually served it.
    endpoints = endpoints_for_model(model_name)
    found = healthy_endpoint_for_model(model_name)
    if found:
        return found[0], model_name
    if endpoints:
        print(f"[WARN] every {model_name} endpoint is down; using default rotation")
    else:
        print(f"[WARN] Unknown model '{model_name}'; using default rotation")
    if DEFAULT_MODEL.startswith("MODEL_"):
        return get_openai_client_and_model(None)
    return (endpoints[0][0] if endpoints else None), model_name


HELP_MESSAGE = """
**Talking to a model**
- Pick a model in our message box, beside Send. **People only (no model)** sends your message to people alone.
- Models are listed live from this server's endpoints, so the list changes as they come & go.

**Your chats**
- **New chat** (top of our sidebar) starts one: **Solo** is only you (sign in first), **Chatroom** is public & anyone with its link can join.
- Your chats & public chats are listed in our sidebar; **Rooms** shows them all.
- Room controls (fork, voice, activities, downloads, who is here) sit beside our chat; on a phone, tap ⚙.

**Commands**
- `/title new`: write a new title for this chat from the conversation so far.
- `/cancel`: stop the reply that is streaming in.
- `/help`: show this message.
"""


def get_room(room_name):
    """Utility function to get room from room name."""
    room = Room.query.filter_by(name=room_name).first()
    if room:
        return room
    else:
        # Create a new room since it doesn't exist
        new_room = Room()
        new_room.name = room_name
        db.session.add(new_room)
        db.session.commit()
        return new_room


def get_s3_client():
    """Utility function to get the S3 client with the appropriate profile."""
    if app.config.get("PROFILE_NAME"):
        session = boto3.Session(profile_name=app.config["PROFILE_NAME"])
        s3_client = session.client("s3")
    else:
        s3_client = boto3.client("s3")
    return s3_client


# Initialize activity module after socketio and db are configured
activity.init_activity_module(
    app,
    socketio,
    db,
    {
        "get_room": get_room,
        "get_s3_client": get_s3_client,
        "get_openai_client_and_model": get_openai_client_and_model,
        "SYSTEM_USERS": SYSTEM_USERS,
    },
)


# HTTP routes live in routes/ (pages, accounts, rooms, code); chat stays
# here. The re-exports keep `app.<name>` working for callers & tests.
import routes  # noqa: E402
from repetition_guard import RepetitionGuard  # noqa: E402
from routes.pages import safe_next_url  # noqa: E402,F401
from routes.rooms import (  # noqa: E402,F401
    emit_room_list_update,
    room_access_denied,
    search_messages,
    valid_room_name,
)

routes.register(
    app,
    get_openai_client_and_model=get_openai_client_and_model,
    system_users=SYSTEM_USERS,
    retitle_room=lambda room: retitle_room(room),
    set_room_title=lambda room, title: set_room_title(room, title),
)


_model_map_refreshed_at = 0.0


@app.route("/models", methods=["GET"])
def get_models():
    # Refresh rotation periodically: drop endpoints that went down and pick
    # up ones that recovered, without restarting the app.
    global _model_map_refreshed_at
    if time.time() - _model_map_refreshed_at > ENDPOINT_HEALTH_TTL:
        _model_map_refreshed_at = time.time()
        try:
            initialize_model_map()
        except Exception as e:
            print(f"[WARN] Model map refresh failed: {e}")
    return jsonify({"models": list(MODEL_CLIENT_MAP.keys())})


@app.route("/vision", methods=["GET"])
def get_vision_status():
    """Return vision model availability status."""
    vision_models = [m for m in MODEL_CLIENT_MAP.keys() if is_vision_model(m)]
    return jsonify(
        {
            "available": len(vision_models) > 0,
            "models": vision_models,
            "default": vision_models[0] if vision_models else None,
        }
    )


@app.route("/vision/describe", methods=["POST"])
def describe_image():
    """Generate alt text description for an image using vision model."""

    data = request.get_json()
    if not data or "image" not in data:
        return jsonify({"error": "Missing 'image' field (base64 data URL)"}), 400

    image_url = data["image"]  # Expected format: data:image/jpeg;base64,...
    prompt = data.get(
        "prompt", "Describe this image in one brief sentence for use as alt text."
    )
    vision_models = [m for m in MODEL_CLIENT_MAP.keys() if is_vision_model(m)]
    model_name = data.get("model", vision_models[0] if vision_models else None)

    if not model_name or not is_vision_model(model_name):
        return jsonify({"error": f"Model {model_name} is not a vision model"}), 400

    try:
        client = get_client_for_model(model_name)
        response = create_completion_skip_thinking(
            client,
            model=model_name,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
            max_tokens=150,
            temperature=0.3,
        )
        description = strip_reasoning(response.choices[0].message.content.strip())
        return jsonify({"description": description, "model": model_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/chat/<room_name>")
def chat(room_name):
    user = auth.get_current_user()

    # Get or create the room
    room = Room.query.filter_by(name=room_name).first()

    # A room that doesn't exist yet is created in get_room() when someone
    # joins, so only a name that may become a room gets a chat page.
    if room is None and not valid_room_name(room_name):
        return "No such room", 404

    # Check if they're trying to access a private room they don't own
    if room_access_denied(room, user):
        return "Access denied: this is someone's solo chat", 403

    # Use authenticated user's display name, or None (will prompt on client side)
    username = user.display_name if user else None

    # Generate Open Graph metadata for social sharing
    og_image = None
    og_description = "A chat room on OpenCompletion"
    og_title = f"{room_name} - OpenCompletion"

    if room:
        # Try to get first image from messages
        og_image = extract_first_image_for_og(room.id)
        og_description = generate_og_description(room)
        if room.title:
            og_title = f"{room.title} - OpenCompletion"

    # Pass username, rooms, room (current room), and user into the template
    return render_template(
        "chat.html",
        room_name=room_name,
        current_room=room,
        username=username,
        user=user,
        og_title=og_title,
        og_description=og_description,
        og_image=og_image,
    )


def resolve_username(claimed):
    """The name a socket event may post under.

    A signed-in person always posts as their own display name, whatever the
    client sent. A guest picks any name except a registered display name,
    `system` or a model's name (those mark messages as system/assistant turns
    in model prompts); a clash gets " (guest)" appended so it stays visible.
    Commas are dropped because rooms store their user lists as CSV.
    """
    user = auth.get_current_user()
    if user:
        return user.display_name
    name = str(claimed or "").replace(",", " ").strip()[:50] or "guest"
    lowered = name.lower()
    reserved = {n.lower() for n in SYSTEM_USERS} | {"system"}
    taken = User.query.filter(db.func.lower(User.display_name) == lowered).first()
    if lowered in reserved or taken:
        return f"{name} (guest)"
    return name


# Handle user joining a room
def trail_as_of_now(saved, room_name):
    """A message's saved tool lines for history. A line saved while running
    is only still running if a reply streams in its room now: one a restart
    (a deploy) cut off said "running" forever, with no Stop & no updates
    (fox, 2026-10-31), so it reads as interrupted instead."""
    if not saved:
        return None
    trail = json.loads(saved)
    if streaming_replies.get(room_name):
        return trail
    for entry in trail:
        if isinstance(entry, dict) and entry.get("status") == "running":
            entry["status"] = "error"
            entry["error"] = "interrupted: the server restarted while it ran"
    return trail


@socketio.on("join")
def on_join(data):
    room_name = data["room_name"]
    user = auth.get_current_user()
    existing = Room.query.filter_by(name=room_name).first()
    if existing is None and not valid_room_name(room_name):
        return
    if room_access_denied(existing, user):
        emit("access_denied", {"room_name": room_name}, room=request.sid)
        return
    username = resolve_username(data.get("username"))
    room = get_room(room_name)

    # Set owner for newly created rooms (if room has no owner and user is authenticated)
    if room.owner_id is None:
        if user:
            room.owner_id = user.id
            db.session.add(room)
            db.session.commit()

    # Add the user to the active users list
    room.add_user(username)

    # Tell this client the name it really posts under (see resolve_username).
    emit("your_username", {"username": username}, room=request.sid)

    # Store session data in the database
    user_session = UserSession(
        session_id=request.sid, username=username, room_name=room_name, room_id=room.id
    )
    db.session.add(user_session)
    db.session.commit()

    # Emit the active and inactive users list to the new joiner
    emit(
        "active_users",
        {
            "active_users": room.get_active_users(),
            "inactive_users": room.get_inactive_users(),
        },
        room=request.sid,
    )

    # Emit the active and inactive users list to everyone in the room
    emit(
        "active_users",
        {
            "active_users": room.get_active_users(),
            "inactive_users": room.get_inactive_users(),
        },
        room=room_name,
        include_self=False,
    )

    # This makes the client start listening for new events for this room.
    join_room(room_name)
    # From here on this client hears the room: our home page's first message
    # waits for this before it is sent (static/js/chat/3-room.js).
    emit("joined", {"room_name": room_name}, room=request.sid)
    # Joining mid-reply: show Stop too.
    if streaming_replies.get(room_name):
        emit_streaming(room_name, to=request.sid)

    # update the title bar with the proper room title, if it exists for just this new client.
    if room.title:
        socketio.emit("update_room_title", {"title": room.title}, room=request.sid)

    # Fetch previous messages from the database
    previous_messages = Message.visible(room.id).all()

    # count the number of tokens in this room.
    total_token_count = 0

    # Send the history of messages only to the newly connected client.
    for message in previous_messages:
        if not message.is_base64_image():
            total_token_count += message.token_count
        emit(
            "previous_messages",
            {
                "id": message.id,
                "username": message.username,
                "content": message.content,
                # A model's reply can be forked from (static/js/chat/5-messages.js).
                "from_model": message.username in SYSTEM_USERS
                and message.username != "system",
                "thinking": message.thinking,
                "tools": trail_as_of_now(message.tool_trail, room_name),
            },
            room=request.sid,
        )

    # A room whose replies came before titling (or whose title call failed)
    # still shows its slug in every sidebar: title it in the background,
    # never holding up this join.
    if room.title is None and len(previous_messages) >= TITLE_MIN_MESSAGES:
        gevent.spawn(title_untitled_room, room.name)

    # commit session & active user list to database.
    db.session.commit()

    # Broadcast to all clients in the room that a new user has joined.
    emit(
        "chat_message",
        {"id": notice_id(), "content": f"{username} has joined the room."},
        room=room.name,
    )
    emit(
        "chat_message",
        {
            "id": notice_id(),
            "content": f"Estimated {total_token_count} total tokens in conversation.",
        },
        room=request.sid,
    )


# Handle user leaving a room
@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    tools.abandon(sid)  # a tool waiting on this browser hears now
    user_session = UserSession.query.filter_by(session_id=sid).first()

    if user_session:
        room_name = user_session.room_name
        username = user_session.username
        room = Room.query.filter_by(name=room_name).first()
        room.remove_user(username)
        leave_room(room_name)
        # Broadcast to all clients in the room that a user has left the room.
        # Emit the active and inactive users list to everyone in the room
        emit(
            "active_users",
            {
                "active_users": room.get_active_users(),
                "inactive_users": room.get_inactive_users(),
            },
            room=room.name,
            include_self=False,
        )
        emit(
            "chat_message",
            {"id": notice_id(), "content": f"{username} has left the room."},
            room=room.name,
            include_self=False,
        )
        # Remove session data from the database
        db.session.delete(user_session)
        db.session.commit()


@socketio.on("chat_message")
def handle_message(data):
    room_name = data["room_name"]
    existing = Room.query.filter_by(name=room_name).first()
    if existing is None and not valid_room_name(room_name):
        return
    if room_access_denied(existing, auth.get_current_user()):
        return
    room = get_room(room_name)
    username = resolve_username(data.get("username"))
    message = data["message"].strip()
    model = data.get("model", "None")

    new_message = Message(
        username=username,
        content=message,
        room_id=room.id,
    )
    db.session.add(new_message)

    # Update room's updated_at timestamp (Unix epoch). time.time() is the
    # real epoch; the old utcnow().timestamp() read a UTC clock as local
    # time & skewed rooms by the server's UTC offset.
    room.updated_at = int(time.time())
    db.session.add(room)

    db.session.commit()

    emit(
        "chat_message",
        {
            "id": new_message.id,
            "username": username,
            "content": message,
        },
        room=room.name,
    )

    dispatch_message(
        room,
        username,
        message,
        [model],
        data.get("enable_thinking", True),
        tool_context=tools.context(request.sid, auth.get_current_user()),
    )


def dispatch_message(
    room, username, message, models, enable_thinking=True, tool_context=None
):
    """What a posted message sets off: a slash command, the room's running
    activity, or a reply from each of `models` ("None" = people only).
    `tool_context` (tools.context: the asker's socket & sign in) decides
    which tools a reply may call; None offers none."""
    room_name = room.name
    commands = message.splitlines()
    for command in commands:
        if command.startswith("/help"):
            socketio.emit(
                "chat_message",
                {"id": notice_id(), "username": "System", "content": HELP_MESSAGE},
                room=room_name,
            )
            return
        if command.startswith("/activity cancel"):
            gevent.spawn(activity.cancel_activity, room_name, username)
            return
        if command.startswith("/activity info"):
            gevent.spawn(activity.display_activity_info, room_name, username)
            return
        if command.startswith("/activity metadata"):
            gevent.spawn(activity.display_activity_metadata, room_name, username)
            return
        if command.startswith("/activity"):
            s3_file_path = command.split(" ", 1)[1].strip()
            gevent.spawn(activity.start_activity, room_name, s3_file_path, username)
            return
        if command.startswith("/s3 ls"):
            s3_file_path_pattern = command.split(" ", 2)[2].strip()
            gevent.spawn(list_s3_files, room.name, s3_file_path_pattern, username)
        if command.startswith("/s3 load"):
            s3_file_path = command.split(" ", 2)[2].strip()
            gevent.spawn(load_s3_file, room_name, s3_file_path, username)
        if command.startswith("/s3 save"):
            s3_key_path = command.split(" ", 2)[2].strip()
            gevent.spawn(save_code_block_to_s3, room_name, s3_key_path, username)
        if command.startswith("/title new"):
            gevent.spawn(generate_new_title, room_name, username)
            return
        if command.startswith("/cancel"):
            gevent.spawn(cancel_generation, room_name)
            return

    activity_state = ActivityState.query.filter_by(room_id=room.id).first()
    if activity_state:
        gevent.spawn(
            activity.handle_activity_response,
            room_name,
            message,
            username,
            models[0] if models else "None",
        )
        return

    for model in models:
        if model in (None, "None"):
            continue
        emit(
            "chat_message",
            {
                "id": notice_id("processing"),
                "content": "<span id='processing'>Processing...</span>",
            },
            room=room.name,
        )
        # elif: a Claude model once also fell through to chat_gpt, so two
        # replies streamed for one message.
        if "anthropic.claude" in model:
            gevent.spawn(
                stoppable_reply, chat_claude, username, room_name, model_name=model
            )
        elif "dall-e" in model:
            gevent.spawn(generate_dalle_image, room_name, message, username)
        else:
            # All other models (Groq, Together, Mistral, etc.) use OpenAI client
            gevent.spawn(
                stoppable_reply,
                chat_gpt,
                username,
                room_name,
                model_name=model,
                enable_thinking=enable_thinking,
                tool_context=tool_context,
            )


def emit_streaming(room_name, to=None):
    """Tell a room (or one client) how many replies are streaming in it, so
    our Stop button shows while there is something to stop."""
    socketio.emit(
        "replies_streaming",
        {"count": streaming_replies.get(room_name, 0)},
        room=to or room_name,
    )


def stoppable_reply(reply, username, room_name, **kwargs):
    """Run a model reply (chat_gpt, chat_claude) counted as streaming in
    its room, so Stop can end it; the count drops however the reply ends."""
    streaming_replies[room_name] = streaming_replies.get(room_name, 0) + 1
    emit_streaming(room_name)
    try:
        return reply(username, room_name, **kwargs)
    finally:
        streaming_replies[room_name] -= 1
        if not streaming_replies[room_name]:
            del streaming_replies[room_name]
            stop_requests.discard(room_name)
        emit_streaming(room_name)


@socketio.on("stop_replies")
def on_stop_replies(data):
    """Our Stop button: end every reply streaming in this room now. What
    was written so far stays."""
    room_name = (data or {}).get("room_name")
    room = Room.query.filter_by(name=room_name).first()
    if room is None or room_access_denied(room, auth.get_current_user()):
        return
    if streaming_replies.get(room_name):
        stop_requests.add(room_name)


def message_in_room(message_id, room_name):
    """(message, room) when `message_id` lives in `room_name` & the caller
    may use that room, else (None, None). Edits & deletes act on one room's
    messages only; an ID from another room, or a private room the caller
    does not own, is ignored."""
    room = Room.query.filter_by(name=room_name).first()
    if not room or room_access_denied(room, auth.get_current_user()):
        return None, None
    message = db.session.get(Message, message_id)
    if not message or message.room_id != room.id:
        return None, None
    return message, room


@socketio.on("delete_message")
def handle_delete_message(data):
    msg_id = data["message_id"]
    message, room = message_in_room(msg_id, data.get("room_name"))
    if not message:
        return
    db.session.delete(message)
    db.session.commit()

    # Notify all clients in the room to remove the message from their DOM
    emit("message_deleted", {"message_id": msg_id}, room=room.name)


@socketio.on("update_message")
def handle_update_message(data):
    message_id = data["message_id"]
    new_content = data["content"]
    message, room = message_in_room(message_id, data.get("room_name"))
    room_name = room.name if room else None
    if message:
        # Update the message content
        message.content = new_content
        message.count_tokens()
        db.session.add(message)
        db.session.commit()

        # Emit an event to update the message on all clients
        emit(
            "message_updated",
            {
                "message_id": message_id,
                "content": new_content,
                "username": message.username,
            },
            room=room_name,
        )


def models_that_answered(message):
    """The models that replied to `message`: the model messages right after
    it, up to the next one a person (or "system") wrote, in order."""
    later = (
        Message.visible(message.room_id)
        .filter(Message.id > message.id)
        .order_by(Message.id)
    )
    models = []
    for reply in later:
        if reply.username not in SYSTEM_USERS or reply.username == "system":
            break
        if reply.username not in models:
            models.append(reply.username)
    return models


@socketio.on("edit_and_resend")
def handle_edit_and_resend(data):
    """Edit a person's message & have it answered again, for everyone in
    the room: the old message & everything after it are hidden (superseded
    by the revision, never deleted), the revision is posted in its place &
    the models that answered the old one answer again (else our picker's
    model)."""
    message, room = message_in_room(data.get("message_id"), data.get("room_name"))
    content = (data.get("content") or "").strip()
    if not message or not content or message.superseded_by_id:
        return
    if message.username in SYSTEM_USERS:
        return  # a model's reply is edited in place, never resent
    # Only its author resends a message (it hides everything after it for
    # everyone): the name this socket joined the room under, never a name
    # the event claims.
    joined = UserSession.query.filter_by(
        session_id=request.sid, room_id=room.id
    ).first()
    if not joined or joined.username != message.username:
        emit(
            "edit_resend_refused",
            {"error": "Only its author can resend a message."},
            room=request.sid,
        )
        return
    if streaming_replies.get(room.name):
        emit(
            "edit_resend_refused",
            {"error": "A reply is still streaming: Stop it, then resend."},
            room=request.sid,
        )
        return

    models = models_that_answered(message) or [data.get("model") or "None"]
    branch = (
        Message.visible(room.id)
        .filter(Message.id >= message.id)
        .order_by(Message.id)
        .all()
    )
    revision = Message(username=message.username, content=content, room_id=room.id)
    revision.revision_of_id = message.id
    db.session.add(revision)
    db.session.flush()
    for old in branch:
        old.superseded_by_id = revision.id
    room.updated_at = int(time.time())
    db.session.commit()

    emit(
        "messages_hidden",
        {"ids": [old.id for old in branch], "revision_id": revision.id},
        room=room.name,
    )
    emit(
        "chat_message",
        {"id": revision.id, "username": revision.username, "content": content},
        room=room.name,
    )
    dispatch_message(
        room,
        revision.username,
        content,
        models,
        data.get("enable_thinking", True),
        tool_context=tools.context(request.sid, auth.get_current_user()),
    )


@socketio.on("get_activity_status")
def handle_get_activity_status(data):
    """Get the current activity status for a room.

    A room that does not exist, or someone else's private room, answers
    "no activity": asking must neither create a room nor reveal what
    runs in a private one.
    """
    room = Room.query.filter_by(name=data.get("room_name")).first()
    if not room or room_access_denied(room, auth.get_current_user()):
        emit("activity_status", {"active": False}, room=request.sid)
        return
    activity.handle_get_activity_status(data)


def group_consecutive_roles(messages):
    if not messages:
        return []

    grouped_messages = []
    current_role = messages[0]["role"]
    current_content = []

    for message in messages:
        if message["role"] == current_role:
            current_content.append(message["content"])
        else:
            grouped_messages.append(
                {"role": current_role, "content": " ".join(current_content)}
            )
            current_role = message["role"]
            current_content = [message["content"]]

    # Append the last grouped message
    grouped_messages.append(
        {"role": current_role, "content": " ".join(current_content)}
    )

    return grouped_messages


def chat_claude(
    # username, room_name, model_name="anthropic.claude-3-5-sonnet-20240620-v1:0"
    username,
    room_name,
    model_name="anthropic.claude-3-sonnet-20240229-v1:0",
):
    with app.app_context():
        room = get_room(room_name)
        # claude has a 200,000 token context window for prompts.
        all_messages = Message.visible(room.id).order_by(Message.id.desc()).all()

    chat_history = []
    for msg in reversed(all_messages):
        if msg.is_base64_image():
            continue
        role = "assistant" if msg.username in SYSTEM_USERS else "user"
        chat_history.append({"role": role, "content": msg.content})

    # only claude cares about this constrant.
    chat_history = group_consecutive_roles(chat_history)

    # Initialize the Bedrock client using boto3 and profile name.
    if app.config.get("PROFILE_NAME"):
        session = boto3.Session(profile_name=app.config["PROFILE_NAME"])
        client = session.client("bedrock-runtime", region_name="us-west-2")
    else:
        client = boto3.client("bedrock-runtime", region_name="us-west-2")

    # Define the request parameters
    params = {
        "modelId": model_name,
        "contentType": "application/json",
        "accept": "*/*",
        "body": json.dumps(
            {
                "messages": chat_history,
                "max_tokens": 4096,
                "temperature": 0,
                "top_k": 250,
                "top_p": 0.999,
                "stop_sequences": ["\n\nHuman:"],
                "anthropic_version": "bedrock-2023-05-31",
            }
        ).encode(),
    }

    # Process the event stream
    buffer = ""

    # save empty message, we need the ID when we chunk the response.
    with app.app_context():
        new_message = Message(username=model_name, content=buffer, room_id=room.id)
        db.session.add(new_message)
        db.session.commit()
        msg_id = new_message.id

    try:
        # Invoke the model with response stream
        response = client.invoke_model_with_response_stream(**params)["body"]

        first_chunk = True
        for event in response:
            content = ""

            # Check if there has been a cancellation request, break if there is.
            if cancellation_requests.get(msg_id) or room_name in stop_requests:
                cancellation_requests.pop(msg_id, None)
                # Hang up, so the model server stops generating too.
                getattr(response, "close", lambda: None)()
                break

            if "chunk" in event:
                chunk_data = json.loads(event["chunk"]["bytes"].decode())

                if chunk_data["type"] == "content_block_delta":
                    if chunk_data["delta"]["type"] == "text_delta":
                        content = chunk_data["delta"]["text"]

            if content:
                buffer += content  # Accumulate content

                if first_chunk:
                    socketio.emit(
                        "message_chunk",
                        {
                            "id": msg_id,
                            "content": content,
                            "username": username,
                            "model_name": model_name,
                            "is_first_chunk": True,
                        },
                        room=room_name,
                    )
                    first_chunk = False
                else:
                    socketio.emit(
                        "message_chunk",
                        {"id": msg_id, "content": content},
                        room=room_name,
                    )
                socketio.sleep(0)  # Force immediate handling

    except Exception as e:
        with app.app_context():
            message_content = f"AWS Bedrock Error: {e}"
            new_message = (
                db.session.query(Message).filter(Message.id == msg_id).one_or_none()
            )
            if new_message:
                new_message.content = message_content
                new_message.count_tokens()
                db.session.add(new_message)
                db.session.commit()
        socketio.emit(
            "chat_message",
            {
                "id": msg_id,
                "username": model_name,
                "content": message_content,
            },
            room=room_name,
        )
        socketio.emit("delete_processing_message", msg_id, room=room_name)
        # exit early to avoid clobbering the error message.
        return None

    # Save the entire completion to the database
    with app.app_context():
        new_message = (
            db.session.query(Message).filter(Message.id == msg_id).one_or_none()
        )
        if new_message:
            new_message.content = buffer
            new_message.count_tokens()
            db.session.add(new_message)
            db.session.commit()

    socketio.emit(
        "message_chunk",
        {"id": msg_id, "content": "", "is_complete": True},
        room=room_name,
    )

    socketio.emit("delete_processing_message", msg_id, room=room_name)
    title_untitled_room(room_name)


def _env_float(name, default):
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# One request's thinking & answer together; a loop that a hang-up can't
# stop ends here. Qwen suggests 32k for thinking mode; ours is a thinking
# budget plus an answer.
DEFAULT_MAX_TOKENS = 24576
# Thinking past this many tokens with no answer begun: hang up & have the
# model answer from its notes (chat_gpt), in up to DEFAULT_ANSWER_TOKENS.
DEFAULT_THINKING_BUDGET = 16384
DEFAULT_ANSWER_TOKENS = 8192
# Qwen's thinking-mode top_k; sent as an extra body field, which servers
# that don't know it reject (open_completion then drops it).
DEFAULT_TOP_K = 20
# Thinking is counted in characters as it streams; ~4 per token.
CHARS_PER_TOKEN = 4


def _env_int(name, default):
    return int(_env_float(name, default))


def chat_sampling(model_name):
    """Sampling for a chat reply. Greedy decoding (temperature 0, our old
    default) sends reasoning models such as Qwen3 into endless repetition
    in their thinking, so chat samples at Qwen's thinking-mode settings:
    OPENCOMPLETION_TEMPERATURE (0.6), OPENCOMPLETION_TOP_P (0.95) &
    OPENCOMPLETION_TOP_K (20; 0 = not sent), plus
    OPENCOMPLETION_PRESENCE_PENALTY when set (Qwen suggests up to 1.5 for
    quantized models that repeat).

    OPENCOMPLETION_MAX_TOKENS (24576; 0 = no cap) bounds every reply in the
    request itself: hanging up on a looping model (RepetitionGuard) stops
    the generation only where a disconnect reaches the server & aborts it,
    which not every GPU host or proxy does, while a server always stops at
    max_tokens. OpenAI's o-series take temperature 1 only & no top_p (o3:
    neither)."""
    if any(tag in model_name for tag in ("o1-", "o3", "o4-")):
        return {} if "o3" in model_name else {"temperature": 1}
    sampling = {
        "temperature": _env_float("OPENCOMPLETION_TEMPERATURE", 0.6),
        "top_p": _env_float("OPENCOMPLETION_TOP_P", 0.95),
    }
    top_k = _env_int("OPENCOMPLETION_TOP_K", DEFAULT_TOP_K)
    if top_k > 0:
        sampling["extra_body"] = {"top_k": top_k}
    presence = _env_float("OPENCOMPLETION_PRESENCE_PENALTY", None)
    if presence is not None:
        sampling["presence_penalty"] = presence
    max_tokens = _env_int("OPENCOMPLETION_MAX_TOKENS", DEFAULT_MAX_TOKENS)
    if max_tokens > 0:
        sampling["max_tokens"] = max_tokens
    return sampling


def thinking_budget():
    """OPENCOMPLETION_THINKING_BUDGET in tokens (16384; 0 = no budget)."""
    return max(0, _env_int("OPENCOMPLETION_THINKING_BUDGET", DEFAULT_THINKING_BUDGET))


def answer_tokens():
    """OPENCOMPLETION_ANSWER_TOKENS (8192; 0 = no cap): the answer a model
    writes from its notes once its thinking ran past its budget."""
    return max(0, _env_int("OPENCOMPLETION_ANSWER_TOKENS", DEFAULT_ANSWER_TOKENS))


# Words a server's 400 uses when it is the tools it refuses (llama.cpp:
# "tools param requires --jinja flag"; groq: "`tool calling` is not
# supported"). Any other 400 (a prompt past the context, say) keeps them.
TOOL_REFUSAL_WORDS = ("tool", "function", "jinja")


# What a server's 400 says about its context: "maximum context length is
# 65536 tokens ... your prompt contains at least 40961 input tokens".
CONTEXT_LENGTH = re.compile(r"maximum context length is (\d+)")
PROMPT_TOKENS = re.compile(r"(\d+) input tokens|prompt contains (?:at least )?(\d+)")
# Each model's context length & how many prompt characters a token held,
# learned from its first such refusal, so later asks fit max_tokens to the
# room left & are not refused every tool round.
CONTEXT_WINDOWS = {}
# Room kept between our guess of a prompt's tokens & a server's count.
CONTEXT_MARGIN = 256
# Below this much room an answer cannot fit; max_tokens is dropped instead
# & the server stops at its context, as before.
MIN_ANSWER_TOKENS = 512
# Characters a token holds before a server has told us (more tokens than
# most text holds, so the guess errs short of the window).
CHARS_PER_TOKEN = 3.0


def prompt_chars(kwargs):
    """How long an ask's prompt is: its messages & tool schemas as sent."""
    return len(json.dumps(kwargs.get("messages") or [], ensure_ascii=False)) + len(
        json.dumps(kwargs.get("tools") or [], ensure_ascii=False)
    )


def fit_max_tokens(kwargs):
    """Lower kwargs' max_tokens to what the model's learned context leaves
    after its prompt."""
    learned = CONTEXT_WINDOWS.get(kwargs.get("model"))
    if not learned or "max_tokens" not in kwargs:
        return
    window, per_token = learned
    room = int(window - prompt_chars(kwargs) / per_token) - CONTEXT_MARGIN
    if room < kwargs["max_tokens"]:
        kwargs["max_tokens"] = max(room, MIN_ANSWER_TOKENS)


def room_from_refusal(kwargs, text):
    """The max_tokens that fits, from a refusal naming the context length &
    the prompt's tokens (both remembered for the model), else None."""
    model = kwargs.get("model")
    length = CONTEXT_LENGTH.search(text)
    if not length:
        return None
    window = int(length.group(1))
    used = PROMPT_TOKENS.search(text)
    used = int(used.group(1) or used.group(2)) if used else 0
    per_token = CHARS_PER_TOKEN
    if used:
        # 10% fewer characters a token than measured: a later prompt of
        # other text may tokenize a little denser.
        per_token = min(CHARS_PER_TOKEN, 0.9 * prompt_chars(kwargs) / used)
    if model:
        if model not in CONTEXT_WINDOWS:
            print(f"[INFO] {model}: {window}-token context; fitting max_tokens to it")
        CONTEXT_WINDOWS[model] = (window, max(per_token, 1.0))
    if not used:
        return None
    room = window - used - CONTEXT_MARGIN
    return room if room >= MIN_ANSWER_TOKENS else None


def open_completion(client, kwargs, skip_thinking=False, refusals=None):
    """Start a streamed chat completion, forgiving the asks a server may
    refuse with a 400: an unknown `top_k` (dropped), a max_tokens past what
    its context has room for (cut to the room its refusal names, & the
    context remembered so the next ask fits first time; dropped when the
    refusal names no room, & the server then stops at its context, our loop
    guard & thinking budget still holding) & `tools` on a server or
    model that says it can't call them (dropped, with the note telling the
    model it has them, & the reason added to `refusals` so the reply says
    it went without). Any other 400 is raised: a reply never loses its
    tools to an error that was about something else."""
    kwargs = dict(kwargs)
    fit_max_tokens(kwargs)
    for _ in range(4):
        try:
            if skip_thinking:
                return create_completion_skip_thinking(client, **kwargs)
            return client.chat.completions.create(**kwargs)
        except openai.BadRequestError as e:
            text = str(e).lower()
            extra = dict(kwargs.get("extra_body") or {})
            if "top_k" in text and "top_k" in extra:
                extra.pop("top_k")
                if extra:
                    kwargs["extra_body"] = extra
                else:
                    kwargs.pop("extra_body", None)
                continue
            if "max_tokens" in kwargs and (
                "context" in text or "max_tokens" in text or "maximum" in text
            ):
                room = room_from_refusal(kwargs, str(e))
                if room is not None and room < kwargs["max_tokens"]:
                    # Ask again for what fits: the cap still holds.
                    kwargs["max_tokens"] = room
                    continue
                print(f"[WARN] {kwargs.get('model')} refused max_tokens: {e}")
                kwargs.pop("max_tokens")
                continue
            if "tool_choice" in kwargs and "tool" in text:
                # Can't force a named tool: still offer them all.
                print(f"[WARN] {kwargs.get('model')} refused tool_choice: {e}")
                kwargs.pop("tool_choice")
                continue
            if "tools" in kwargs and any(w in text for w in TOOL_REFUSAL_WORDS):
                print(f"[WARN] {kwargs.get('model')} refused tools: {e}")
                kwargs.pop("tools")
                kwargs.pop("tool_choice", None)
                kwargs["messages"] = [
                    m for m in kwargs.get("messages", []) if not tools.is_guidance(m)
                ]
                if refusals is not None:
                    refusals.append(str(e)[:200])
                continue
            raise
    if skip_thinking:
        return create_completion_skip_thinking(client, **kwargs)
    return client.chat.completions.create(**kwargs)


ANSWER_FROM_NOTES = (
    "\n\n(Your thinking ran past its budget, so it was stopped. Your notes "
    "so far:\n\n{notes}\n\nNow, without thinking any further, write your "
    "final answer to the message above, based on these notes.)"
)


def answer_from_notes(chat_history, notes):
    """chat_history with the model's unfinished thinking handed back under
    our last message, asking for the answer it would have written. The
    notes ride on that message, so roles keep alternating for templates
    that need it."""
    history = [dict(message) for message in chat_history]
    ask = ANSWER_FROM_NOTES.format(notes=notes.strip())
    if history and history[-1]["role"] == "user":
        content = history[-1]["content"]
        if isinstance(content, list):  # a vision turn: add a text part
            history[-1]["content"] = content + [{"type": "text", "text": ask}]
        else:
            history[-1]["content"] = f"{content}{ask}"
    else:
        history.append({"role": "user", "content": ask.strip()})
    return history


def chat_gpt(
    username,
    room_name,
    model_name="gpt-4o-mini",
    enable_thinking=True,
    tool_context=None,
):
    openai_client, model_name = get_openai_client_and_model(model_name)

    limit = 20
    if "gpt-4" in model_name:
        limit = 1000

    # Check if this is a vision-capable model
    vision_enabled = is_vision_model(model_name)
    if vision_enabled:
        print(f"Vision model detected: {model_name}")

    with app.app_context():
        room = get_room(room_name)
        room_id = room.id  # Save room_id to avoid DetachedInstanceError later
        last_messages = (
            Message.visible(room_id).order_by(Message.id.desc()).limit(limit).all()
        )

        chat_history = []

        # Find the most recent image message ID (only this one gets base64)
        most_recent_image_id = None
        if vision_enabled:
            for msg in last_messages:  # newest first
                is_image_msg = (
                    msg.is_base64_image()
                    or extract_external_image_url(msg.content)
                    or has_image_attachment(msg.content, room_id)
                )
                if is_image_msg:
                    most_recent_image_id = msg.id
                    print(
                        f"Vision: will include base64 for most recent image (msg {msg.id})"
                    )
                    break

        # Build chat history (oldest first) - include all text, only most recent image
        for msg in reversed(last_messages):
            is_image_msg = msg.is_base64_image() or extract_external_image_url(
                msg.content
            )

            # Skip ALL images for non-vision models
            if is_image_msg and not vision_enabled:
                continue

            role = "assistant" if msg.username in SYSTEM_USERS else "user"

            # For vision: only include base64 for the most recent image
            # Older images are skipped entirely (they're just base64, no useful text)
            if vision_enabled and is_image_msg:
                if msg.id == most_recent_image_id:
                    content = build_message_content(
                        msg, vision_enabled, room_id=room_id
                    )
                    chat_history.append({"role": role, "content": content})
                # Skip older image messages - they have no text context
                continue

            # Regular text message - always include, with the text & code
            # files it carries inlined (& its images, for a vision model's
            # newest image turn)
            content = attachment_context(
                msg.content,
                room_id,
                include_images=vision_enabled and msg.id == most_recent_image_id,
            )
            chat_history.append({"role": role, "content": content})

    buffer = ""  # Content buffer for accumulating the chunks

    # save empty message, we need the ID when we chunk the response.
    with app.app_context():
        new_message = Message(username=model_name, content=buffer, room_id=room_id)
        db.session.add(new_message)
        db.session.commit()
        msg_id = new_message.id

    first_chunk = True

    create_kwargs = {
        "model": model_name,
        "messages": chat_history,
        "n": 1,
        "stream": True,
        **chat_sampling(model_name),
    }
    # The tools this reply may call (tools/, docs/TOOLS.md), by who asked
    # (tool_context), in this chat, for this model; none offered leaves
    # this a plain chat reply.
    if tool_context is not None:
        tool_context = dataclasses.replace(
            tool_context,
            room_id=room_id,
            model=model_name,
            stopped=lambda: room_name in stop_requests,
            summarize=(
                (
                    lambda previous, fresh: summarize_delegate(
                        openai_client, model_name, previous, fresh
                    )
                )
                if tools.policy.delegate_summaries_enabled()
                else None
            ),
        )
    tool_schemas = tools.schemas(tool_context)
    if tool_schemas:
        create_kwargs["tools"] = tool_schemas
        # Which tools it has & that it never claims a run it did not make.
        chat_history.insert(0, tools.guidance(tool_context))
        offered = list(tools.offered(tool_context))
        print(f"[INFO] {model_name} may call {', '.join(offered)} for {username}")
        # Asked for a tool by name: the first ask must call it.
        named = tools.named_tool(
            tool_context, chat_history[-1]["content"] if chat_history else None
        )
        if named:
            create_kwargs["tool_choice"] = {
                "type": "function",
                "function": {"name": named},
            }
    # The calls the model streams in its current ask, when it may call any.
    tool_calls = tools.ToolCallCollector() if tool_schemas else None
    # Why a server refused this reply's tools, if it did (open_completion).
    tool_refusals = []

    def open_stream(client):
        # Without thinking, chain-of-thought is suppressed at the template
        # level. Endpoints that reject the chat_template_kwargs param
        # (OpenAI, Groq, Mistral, Gemini) get retried without it inside the
        # helper; templates that ignore the kwarg (e.g. Hermes) simply drop it.
        return open_completion(
            client,
            create_kwargs,
            skip_thinking=not enable_thinking,
            refusals=tool_refusals,
        )

    try:
        # An endpoint that fails before streaming hands over to another
        # endpoint serving this same model.
        chunks = open_with_failover(openai_client, model_name, open_stream)
    except Exception as e:
        with app.app_context():
            message_content = f"{model_name} Error: {e}"
            new_message = (
                db.session.query(Message).filter(Message.id == msg_id).one_or_none()
            )
            if new_message:
                new_message.content = message_content
                new_message.count_tokens()
                db.session.add(new_message)
                db.session.commit()
        socketio.emit(
            "chat_message",
            {
                "id": msg_id,
                "username": model_name,
                "content": message_content,
            },
            room=room_name,
        )
        socketio.emit("delete_processing_message", msg_id, room=room_name)
        # exit early to avoid clobbering the error message.
        return None

    # Every piece of thinking, kept with the reply (Message.thinking) & handed
    # back to the model if its thinking runs past its budget.
    thinking_parts = []

    def emit_content(content):
        nonlocal buffer, first_chunk
        buffer += content
        if first_chunk:
            socketio.emit(
                "message_chunk",
                {
                    "id": msg_id,
                    "content": content,
                    "username": username,
                    "model_name": model_name,
                    "is_first_chunk": True,
                },
                room=room_name,
            )
            first_chunk = False
        else:
            socketio.emit(
                "message_chunk", {"id": msg_id, "content": content}, room=room_name
            )
        socketio.sleep(0)  # Force immediate handling

    def consume(chunks, budget_chars):
        """Stream one completion into our reply. Returns (looped, cut at the
        token cap, thinking ran past budget_chars before any answer)."""
        # One guard per channel: a model looping in its thinking or its
        # answer is cut off, and closing our stream makes the server stop.
        thinking_guard, answer_guard = RepetitionGuard(), RepetitionGuard()
        thought, cut = 0, False
        for chunk in chunks:
            # Check if there has been a cancellation request, stop if so.
            if cancellation_requests.get(msg_id) or room_name in stop_requests:
                cancellation_requests.pop(msg_id, None)
                # Hang up, so the model server stops generating too.
                getattr(chunks, "close", lambda: None)()
                return None, False, False
            if not chunk.choices:
                continue
            if getattr(chunk.choices[0], "finish_reason", None) == "length":
                cut = True
            delta = chunk.choices[0].delta
            # Qwen3.x / Deepseek-R1 / o1: thinking streams via
            # delta.reasoning_content (vLLM >= 0.31: delta.reasoning), the
            # answer via delta.content. Thinking goes to the client as its
            # own channel (a collapsible block) & is saved apart from the
            # answer, never sent back to a model as history.
            reasoning = getattr(delta, "reasoning_content", None) or getattr(
                delta, "reasoning", None
            )
            if reasoning:
                socketio.emit(
                    "message_chunk",
                    {"id": msg_id, "reasoning_content": reasoning},
                    room=room_name,
                )
                socketio.sleep(0)
                thinking_parts.append(reasoning)
                thought += len(reasoning)
                if thinking_guard.add(reasoning):
                    return ("thinking", thinking_guard.looping), False, False
                if budget_chars and first_chunk and thought > budget_chars:
                    return None, False, True
            if tool_calls is not None and getattr(delta, "tool_calls", None):
                tool_calls.add(delta.tool_calls)
            content = delta.content
            if content:
                emit_content(content)
                if answer_guard.add(content):
                    return ("reply", answer_guard.looping), False, False
        return None, cut, False

    tool_trail = []  # each finished tool call, kept as Message.tool_trail

    def save_progress(trail):
        with app.app_context():
            reply = db.session.get(Message, msg_id)
            if reply is not None:
                reply.content = buffer
                reply.tool_trail = json.dumps(trail) if trail else None
                db.session.commit()

    def notify_tool(event):
        """Show the room a tool starting & ending, inside this reply, at the
        point of its text where it happened (`at`, an offset into it), so
        text & tools read in the order they came."""
        event = dict(event, asker=username, at=len(buffer))
        if event["status"] != "running":
            tool_trail.append(event)
        # Saved as it goes, so a page opened (or refreshed) mid-reply shows
        # the tool still running & the words so far, not an empty reply.
        save_progress(tool_trail + ([event] if event["status"] == "running" else []))
        socketio.emit(
            "tool_activity",
            dict(event, id=msg_id, model_name=model_name),
            room=room_name,
        )
        socketio.sleep(0)

    if tool_schemas:
        # The reply's first line names the tools it may call, so a reply
        # that calls none shows whether it had them to call.
        notify_tool({"tool": "offered", "names": offered, "status": "done"})

    budget = thinking_budget() if enable_thinking else 0
    looped, cut_at_limit, over_budget = consume(chunks, budget * CHARS_PER_TOKEN)
    cap, cap_name = create_kwargs.get("max_tokens"), "OPENCOMPLETION_MAX_TOKENS"
    footnote = ""
    if over_budget:
        # Thinking ran past its budget with no answer begun: hang up & ask
        # again without thinking, handing the model its notes, so a long
        # think still ends in an answer instead of a cap. Works on any
        # OpenAI-compatible server (no assistant-prefix continuation).
        getattr(chunks, "close", lambda: None)()
        print(f"[INFO] {model_name} thought past {budget} tokens in {room_name}")
        answer_kwargs = dict(
            create_kwargs,
            messages=answer_from_notes(chat_history, "".join(thinking_parts)),
        )
        answer_kwargs.pop("tools", None)  # answering from notes: no tools
        answer_kwargs.pop("tool_choice", None)
        tool_calls = None
        cap, cap_name = answer_tokens(), "OPENCOMPLETION_ANSWER_TOKENS"
        if cap:
            answer_kwargs["max_tokens"] = cap
        else:
            answer_kwargs.pop("max_tokens", None)
        try:
            chunks = open_with_failover(
                openai_client,
                model_name,
                lambda client: open_completion(
                    client, answer_kwargs, skip_thinking=True
                ),
            )
            looped, cut_at_limit, _ = consume(chunks, 0)
            footnote = (
                f"\n\n_Thought past its {budget}-token budget "
                "(OPENCOMPLETION_THINKING_BUDGET), so it answered from its notes._"
            )
        except Exception as e:
            looped = None
            emit_content(f"\n\n_{model_name} could not answer from its notes: {e}_")

    # The model asked for tools: run them (tools.run_round: in the asker's
    # browser or on our server, tools/policy.py says which), hand it what
    # they found & stream its next ask into this same reply. Its last
    # round goes without tools, so a reply always ends in an answer.
    rounds, round_start, with_tools = 0, 0, chat_history
    while tool_calls is not None and tool_calls.calls and not looped:
        if room_name in stop_requests:
            break
        rounds += 1
        with app.app_context():  # sandboxes & run artifacts are rows
            with_tools = with_tools + tools.run_round(
                tool_context, tool_calls.calls, buffer[round_start:], notify_tool
            )
        if room_name in stop_requests:  # Stop pressed while a tool ran
            break
        round_start = len(buffer)
        ask = dict(create_kwargs, messages=with_tools)
        ask.pop("tool_choice", None)  # forced on the first ask only
        if rounds >= tools.max_rounds():
            ask.pop("tools", None)
            ask["messages"] = with_tools + [tools.out_of_rounds(rounds)]
        tool_calls = tools.ToolCallCollector() if "tools" in ask else None
        try:
            chunks = open_with_failover(
                openai_client,
                model_name,
                lambda client: open_completion(
                    client,
                    ask,
                    skip_thinking=not enable_thinking,
                    refusals=tool_refusals,
                ),
            )
        except Exception as e:
            emit_content(f"\n\n_{model_name} could not answer after its tools: {e}_")
            break
        looped, cut_at_limit, _ = consume(chunks, 0)

    if tool_refusals:
        # Say so: without tools, nothing in this reply was run or checked.
        footnote += (
            f"\n\n_No tools this reply: {model_name} refused them "
            f"({tool_refusals[0]}), so nothing here was run or checked._"
        )
    notice = ""
    if cut_at_limit and not looped:
        notice = (
            f"\n\n_Stopped at our {cap}-token limit ({cap_name})._"
            if cap
            else "\n\n_Stopped: the model reached its length limit._"
        )
    if looped:
        # Hang up. Where a disconnect reaches the server it aborts the
        # generation; elsewhere it runs on to max_tokens, never forever.
        close = getattr(chunks, "close", None)
        if close:
            close()
        where, phrase = looped
        notice = (
            f"\n\n_Stopped: {model_name} kept repeating "
            f"\u201c{phrase[:80]}\u201d in its {where}. Ask again, or rephrase._"
        )
        print(f"[WARN] {model_name} looped in its {where} in {room_name}: {phrase!r}")
    if notice or footnote:
        tail = footnote + notice
        buffer += tail
        socketio.emit(
            "message_chunk",
            {
                "id": msg_id,
                "content": tail,
                "username": username,
                "model_name": model_name,
                "is_first_chunk": first_chunk,
                # A stopped reply keeps its thinking open: it shows why.
                "keep_thinking_open": bool(notice),
            },
            room=room_name,
        )

    # Save the entire completion to the database
    with app.app_context():
        new_message = (
            db.session.query(Message).filter(Message.id == msg_id).one_or_none()
        )
        if new_message:
            new_message.content = buffer
            new_message.thinking = "".join(thinking_parts) or None
            new_message.tool_trail = json.dumps(tool_trail) if tool_trail else None
            new_message.count_tokens()
            db.session.add(new_message)
            db.session.commit()

    socketio.emit(
        "message_chunk",
        {"id": msg_id, "content": "", "is_complete": True},
        room=room_name,
    )

    socketio.emit("delete_processing_message", msg_id, room=room_name)
    if followups_enabled() and buffer.strip() and not notice:
        if room_name not in stop_requests:
            gevent.spawn(
                suggest_followups,
                openai_client,
                model_name,
                room_name,
                msg_id,
                chat_history[-1]["content"] if chat_history else "",
                buffer,
            )
    title_untitled_room(room_name)


# Follow-up suggestions: after a reply, 2-3 short next questions as chips
# below it. OPENCOMPLETION_FOLLOWUPS=off turns them off (one small extra
# completion per reply, on the model that answered).
FOLLOWUP_PROMPT = (
    "Suggest three short follow-up questions the person might ask next in "
    "this conversation. Write them as they would, in their language. One "
    "per line, each under 12 words, no numbering, nothing else."
)
FOLLOWUP_MAX = 3
FOLLOWUP_CHARS = 120


def followups_enabled():
    return os.environ.get("OPENCOMPLETION_FOLLOWUPS", "on").lower() not in (
        "0",
        "off",
        "false",
        "no",
    )


def parse_followups(text):
    """A model's suggestions, one per line, as up to FOLLOWUP_MAX clean
    questions: list markers & quotes stripped, duplicates & blanks gone."""
    suggestions = []
    for line in (text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip(" \"'“”")
        if line and len(line) <= FOLLOWUP_CHARS and line not in suggestions:
            suggestions.append(line)
        if len(suggestions) == FOLLOWUP_MAX:
            break
    return suggestions


DELEGATE_SUMMARY_PROMPT = (
    "You watch a coding agent work in a sandbox. From its earlier summary & "
    "the new lines of its log, say in one or two plain sentences what it "
    "has done & what it is doing now. Name concrete results (tests passed, "
    "files written, errors). No preamble, no markdown, no speculation."
)


def summarize_delegate(openai_client, model_name, previous, fresh):
    """A sentence or two on how a delegate is going (tools/loop.py asks
    about every 15s while its log grows). Best effort: None on failure."""
    try:
        response = create_completion_skip_thinking(
            openai_client,
            model=model_name,
            messages=[
                {"role": "system", "content": DELEGATE_SUMMARY_PROMPT},
                {
                    "role": "user",
                    "content": f"Earlier summary:\n{str(previous)[:1000] or '(none)'}"
                    f"\n\nNew log lines:\n{str(fresh)[-6000:]}",
                },
            ],
            temperature=0.3,
            max_tokens=120,
            timeout=20,
        )
        said = strip_reasoning(response.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001 — the log still shows
        print(f"[WARN] delegate summary failed: {e}")
        return None
    return said[:400] or None


def suggest_followups(openai_client, model_name, room_name, msg_id, question, answer):
    """Ask the model that answered for next questions & send them to the
    room as chips under its reply. Best effort: any failure shows none."""
    if isinstance(question, list):  # a vision turn: its text parts
        question = " ".join(
            part.get("text", "") for part in question if isinstance(part, dict)
        )
    try:
        response = create_completion_skip_thinking(
            openai_client,
            model=model_name,
            messages=[
                {"role": "system", "content": FOLLOWUP_PROMPT},
                {
                    "role": "user",
                    "content": f"Question:\n{str(question)[:4000]}\n\n"
                    f"Answer:\n{strip_reasoning(answer)[:6000]}",
                },
            ],
            temperature=0.7,
            max_tokens=120,
        )
        suggestions = parse_followups(
            strip_reasoning(response.choices[0].message.content)
        )
    except Exception as e:
        print(f"[WARN] Follow-up suggestions failed: {e}")
        return
    if suggestions:
        socketio.emit(
            "followup_suggestions",
            {"message_id": msg_id, "suggestions": suggestions},
            room=room_name,
        )


def chat_llama(username, room_name, model_name="mistral-7b-instruct-v0.2.Q3_K_L.gguf"):
    import llama_cpp

    # https://llama-cpp-python.readthedocs.io/en/latest/api-reference/
    model = llama_cpp.Llama(model_name, n_gpu_layers=-1, n_ctx=32000)

    limit = 15
    with app.app_context():
        room = get_room(room_name)
        last_messages = (
            Message.visible(room.id).order_by(Message.id.desc()).limit(limit).all()
        )

        chat_history = [
            {
                "role": "system" if msg.username in SYSTEM_USERS else "user",
                "content": f"{msg.username}: {msg.content}",
            }
            for msg in reversed(last_messages)
            if not msg.is_base64_image()
        ]

    buffer = ""  # Content buffer for accumulating the chunks

    # save empty message, we need the ID when we chunk the response.
    with app.app_context():
        new_message = Message(username=model_name, content=buffer, room_id=room.id)
        db.session.add(new_message)
        db.session.commit()
        msg_id = new_message.id

    first_chunk = True

    try:
        chunks = model.create_chat_completion(
            messages=chat_history,
            stream=True,
        )
    except Exception as e:
        with app.app_context():
            message_content = f"LLama Error: {e}"
            new_message = (
                db.session.query(Message).filter(Message.id == msg_id).one_or_none()
            )
            if new_message:
                new_message.content = message_content
                new_message.count_tokens()
                db.session.add(new_message)
                db.session.commit()
        socketio.emit(
            "chat_message",
            {
                "id": msg_id,
                "username": model_name,
                "content": message_content,
            },
            room=room_name,
        )
        socketio.emit("delete_processing_message", msg_id, room=room_name)
        # exit early to avoid clobbering the error message.
        return None

    for chunk in chunks:
        # Check if there has been a cancellation request, break if there is.
        if cancellation_requests.get(msg_id) or room_name in stop_requests:
            cancellation_requests.pop(msg_id, None)
            # Hang up, so the model server stops generating too.
            getattr(chunks, "close", lambda: None)()
            break

        delta = chunk["choices"][0]["delta"]
        # See OpenAI-client path above for rationale on reasoning_content.
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            socketio.emit(
                "message_chunk",
                {"id": msg_id, "reasoning_content": reasoning},
                room=room_name,
            )
            socketio.sleep(0)

        content = delta.get("content")

        if content:
            buffer += content  # Accumulate content

            if first_chunk:
                socketio.emit(
                    "message_chunk",
                    {
                        "id": msg_id,
                        "content": content,
                        "username": username,
                        "model_name": model_name,
                        "is_first_chunk": True,
                    },
                    room=room_name,
                )
                first_chunk = False
            else:
                socketio.emit(
                    "message_chunk",
                    {"id": msg_id, "content": content},
                    room=room_name,
                )
            socketio.sleep(0)  # Force immediate handling

    # Save the entire completion to the database
    with app.app_context():
        new_message = (
            db.session.query(Message).filter(Message.id == msg_id).one_or_none()
        )
        if new_message:
            new_message.content = buffer
            new_message.count_tokens()
            db.session.add(new_message)
            db.session.commit()

    socketio.emit(
        "message_chunk",
        {"id": msg_id, "content": "", "is_complete": True},
        room=room_name,
    )

    socketio.emit("delete_processing_message", msg_id, room=room_name)


# Characters of each message a title reads: enough for the gist.
TITLE_MESSAGE_CHARS = 500


def gpt_generate_room_title(messages):
    """
    Generate a title for the room based on a list of messages.

    Returns None when no model is reachable — callers keep the old title.
    """
    openai_client, model_name = get_openai_client_and_model()
    if openai_client is None:
        print("[WARN] No model available for room title generation")
        return None

    # One system instruction first, then the conversation as one user turn:
    # chat templates such as Qwen's refuse a system message anywhere but
    # first ("System message must be at the beginning"), and a transcript
    # never asks the model to continue anyone's turn.
    transcript = "\n\n".join(
        f"{msg.username}: {msg.content[:TITLE_MESSAGE_CHARS]}"
        for msg in reversed(messages)
        if not msg.is_base64_image()
    )
    if not transcript:
        return None
    chat_history = [
        {
            "role": "system",
            "content": "Reply with only a short title (at most 8 words) for "
            "the conversation you are given. No quotes, no punctuation at "
            "the end.",
        },
        {"role": "user", "content": transcript},
    ]

    # Interaction with LLM to generate summary
    # For example, using OpenAI's GPT model
    try:
        response = create_completion_skip_thinking(
            openai_client,
            messages=chat_history,
            model=model_name,  # or any appropriate model
            max_tokens=20,
            n=1,
        )
    except Exception as e:
        print(f"[WARN] Room title generation failed: {e}")
        return None

    title = strip_reasoning(response.choices[0].message.content)
    return title.replace('"', "")


# Most recent messages a first title reads; a new chat has only a few.
TITLE_CONTEXT_MESSAGES = 20
# An untitled room is titled on join once it holds a message & a reply.
TITLE_MIN_MESSAGES = 2


def backfill_titles():
    """Title every untitled room holding a conversation, so Your chats &
    Public chats show titles, not slugs (`python app.py --backfill-titles`).
    Returns (titled, tried)."""
    with app.app_context():
        names = [
            room.name
            for room in Room.query.filter(Room.title.is_(None)).all()
            if Message.visible(room.id).count() >= TITLE_MIN_MESSAGES
        ]
    titled = 0
    for name in names:
        title_untitled_room(name)
        with app.app_context():
            room = Room.query.filter_by(name=name).first()
            if room and room.title:
                titled += 1
                print(f"[INFO] {name} -> {room.title}")
    return titled, len(names)


def title_untitled_room(room_name):
    """Name a room after its first model reply, as chat apps do, so our
    sidebars show a title instead of a room's slug from the start. A room
    that already has a title keeps it (/title new replaces one on demand).
    """
    with app.app_context():
        room = Room.query.filter_by(name=room_name).first()
        if room is None or room.title:
            return
        recent = (
            Message.visible(room.id)
            .order_by(Message.id.desc())
            .limit(TITLE_CONTEXT_MESSAGES)
            .all()
        )
        try:
            title = (gpt_generate_room_title(recent) or "").strip()[:100]
        except Exception as e:
            # Best effort: a reply with no content (a model that only
            # reasoned) or no choices never breaks the reply it follows.
            print(f"[WARN] Room title generation failed: {e}")
            return
        if not title:
            return
        # The model call yields to other greenlets: another join or reply may
        # have titled this room meanwhile, or someone ran /title new. Save
        # only while the room is still untitled, in one conditional UPDATE.
        saved = Room.query.filter(
            Room.id == room.id, db.or_(Room.title.is_(None), Room.title == "")
        ).update({"title": title}, synchronize_session=False)
        db.session.commit()
        if not saved:
            return
        db.session.refresh(room)
        socketio.emit("update_room_title", {"title": title}, room=room.name)
        emit_room_list_update(room, {"id": room.id, "name": room.name, "title": title})


def retitle_room(room):
    """A fresh title from a room's conversation, saved & sent to its page &
    every sidebar showing it; None when no model answered."""
    recent = (
        Message.visible(room.id)
        .order_by(Message.id.desc())
        .limit(TITLE_CONTEXT_MESSAGES)
        .all()
    )
    try:
        title = (gpt_generate_room_title(recent) or "").strip()[:100]
    except Exception as e:
        print(f"[WARN] Room title generation failed: {e}")
        return None
    if not title:
        return None
    set_room_title(room, title)
    return title


def set_room_title(room, title):
    """Save a room's title & tell its page & every sidebar showing it."""
    room.title = title
    db.session.add(room)
    db.session.commit()
    socketio.emit("update_room_title", {"title": title}, room=room.name)
    emit_room_list_update(room, {"id": room.id, "name": room.name, "title": title})


def generate_new_title(room_name, username):
    with app.app_context():
        room = get_room(room_name)
        # Get the last few messages to generate a title
        last_messages = (
            Message.visible(room.id)
            .order_by(Message.id.desc())
            .limit(1000)  # Adjust the limit as needed
            .all()
        )

        # Generate the title using the messages
        new_title = gpt_generate_room_title(last_messages)
        if not new_title:
            # No model reachable — keep the existing title.
            return

        # Update the room title in the database
        room.title = new_title
        db.session.add(room)
        db.session.commit()

        # Emit the new title to the room.
        socketio.emit("update_room_title", {"title": new_title}, room=room_name)

        # Emit an event to update this rooms title in the sidebar for all users.
        updated_room_data = {"id": room.id, "name": room.name, "title": room.title}
        emit_room_list_update(room, updated_room_data)

        # Optionally, send a confirmation message to the room
        confirmation_message = f"New title created: {new_title}"
        new_message = Message(
            username=username, content=confirmation_message, room_id=room.id
        )
        db.session.add(new_message)
        db.session.commit()
        socketio.emit(
            "chat_message",
            {
                "id": new_message.id,
                "username": username,
                "content": confirmation_message,
            },
            room=room_name,
        )


def generate_dalle_image(room_name, message, username):
    """A DALL-E 3 image for `message`, kept in our blob store as an
    attachment of the room & posted as its markdown with the prompt DALL-E
    revised, like any file in a chat (served under the room's rule)."""
    import html

    try:
        response = OpenAI().images.generate(
            model="dall-e-3",
            prompt=message,
            n=1,
            size="1024x1024",
            response_format="b64_json",
        )
        image = base64.b64decode(response.data[0].b64_json)
        revised_prompt = response.data[0].revised_prompt or ""
    except Exception as e:
        image, error = None, e

    with app.app_context():
        room = get_room(room_name)
        if image is None:
            content = f"Error generating image: {html.escape(str(error))}"
        else:
            attachment = save_attachment(
                room.id, image, "dall-e.png", "image/png", username
            )
            content = attachment_markdown(attachment, message[:200])
            if revised_prompt:
                content += "\n\n" + html.escape(revised_prompt)
        new_message = Message(username=username, content=content, room_id=room.id)
        db.session.add(new_message)
        db.session.commit()
        socketio.emit(
            "chat_message",
            {"id": new_message.id, "username": username, "content": content},
            room=room_name,
        )


def startup_summary():
    """What this server offers beside its models, one line a part, for the
    log on start: tools, chat settings & the site. Counts & names only:
    never a key, an email or a database password."""
    from repetition_guard import repeat_limit
    from sqlalchemy.engine import make_url

    policy = tools.policy
    on = lambda flag: "on" if flag else "off"  # noqa: E731
    egress = len(policy.egress_users())
    parts = []
    if policy.web_search_enabled():
        parts.append(
            f"web_search (our server for {egress} egress user"
            f"{'' if egress == 1 else 's'}, everyone else's own browser)"
        )
    if policy.web_crawl_enabled():
        parts.append("web_fetch (egress users)")
    if policy.unsandbox_configured():
        guests = policy.run_code_enabled(signed_in=False)
        parts.append(f"run_code ({'everyone' if guests else 'signed in only'})")
    if policy.sandbox_enabled():
        parts.append(
            f"sandbox ({policy.sandbox_image() or 'unsandbox default image'}, "
            f"frozen idle {policy.sandbox_idle()}s, kept "
            f"{policy.sandbox_retention_days() or 'forever'}d) + sandbox_share_file"
        )
        if policy.delegate_enabled():
            parts.append(
                f"delegate (summaries {on(policy.delegate_summaries_enabled())})"
            )
        else:
            parts.append("delegate off (needs OPENCOMPLETION_PUBLIC_URL)")
    elif not policy.unsandbox_configured():
        parts.append("no unsandbox tools (UNSANDBOX keys unset)")
    lines = [
        "Loaded tools: "
        + (", ".join(parts) if parts else "none")
        + f"; up to {policy.max_rounds()} rounds a reply"
    ]

    sampling = chat_sampling("model")
    lines.append(
        f"Loaded chat settings: temperature {sampling.get('temperature')}, "
        f"top_p {sampling.get('top_p')}, "
        f"top_k {(sampling.get('extra_body') or {}).get('top_k', 'off')}, "
        f"max_tokens {sampling.get('max_tokens', 'no cap')}, "
        f"thinking budget {thinking_budget() or 'off'}, "
        f"repeat limit {repeat_limit() or 'off'}, "
        f"follow-ups {on(followups_enabled())}, "
        f"uploads {max_upload_bytes() // (1024 * 1024)} MB"
    )

    database = make_url(app.config["SQLALCHEMY_DATABASE_URI"])
    try:
        with app.app_context():
            revision = db.session.execute(
                db.text("SELECT version_num FROM alembic_version")
            ).scalar()
    except Exception:  # noqa: BLE001 — a fresh database has no stamp yet
        revision = None
    lines.append(
        f"Loaded site: {os.environ.get('OPENCOMPLETION_BRAND') or 'OpenCompletion'}"
        f", public url {policy.public_url() or 'unset'}"
        f", database {database.render_as_string(hide_password=True)}"
        f" at migration {revision or 'unstamped'}"
    )
    return lines


def backfill_blobs():
    """Move every base64 image kept inside a message (DALL-E replies &
    vision images from before blob_store) into our blob store, the message
    linking it instead (`python app.py --backfill-blobs`). Returns
    (images moved, messages changed)."""
    moved = changed = 0
    with app.app_context():
        found = Message.query.filter(Message.content.contains(";base64,")).all()
        for msg in found:
            content, count = inline_images_to_attachments(
                msg.content, msg.room_id, msg.username or "system"
            )
            if count:
                msg.content = content
                db.session.commit()
                moved += count
                changed += 1
                print(f"[INFO] message {msg.id}: {count} image(s) -> blob store")
    return moved, changed


def find_most_recent_code_block(room_name):
    with app.app_context():
        # Get the room object from the database
        room = get_room(room_name)
        if not room:
            return None  # Room not found

        # Get the most recent message for the room
        latest_message = (
            Message.visible(room.id).order_by(Message.id.desc()).offset(1).first()
        )

    if latest_message:
        # Split the message content into lines
        lines = latest_message.content.split("\n")
        # Initialize variables to store the code block
        code_block_lines = []
        code_block_started = False
        for line in lines:
            # Check if the line starts with a code block fence
            if line.startswith("```"):
                # If we've already started capturing, this fence ends the block
                if code_block_started:
                    break
                else:
                    # Start capturing from the next line
                    code_block_started = True
                    continue
            elif code_block_started:
                # If we're inside a code block, capture the line
                code_block_lines.append(line)

        # Join the captured lines to form the code block content
        code_block_content = "\n".join(code_block_lines)
        return code_block_content

    # No code block found in the latest message
    return None


def save_code_block_to_s3(room_name, s3_key_path, username):
    # Initialize the S3 client
    s3_client = get_s3_client()

    # Assuming the bucket name is set in an environment variable
    bucket_name = os.environ.get("S3_BUCKET_NAME")

    # Find the most recent code block
    code_block_content = find_most_recent_code_block(room_name)

    # Initialize a variable to hold the message content
    message_content = ""

    if code_block_content:
        try:
            # Save the code block content to S3
            s3_client.put_object(
                Bucket=bucket_name, Key=s3_key_path, Body=code_block_content
            )
            # Set the success message content
            message_content = f"Code block saved to S3 at {s3_key_path}"
        except Exception as e:
            # Set the error message content if S3 save fails
            message_content = f"Error saving file to S3: {e}"
    else:
        # Set the error message content if no code block is found
        message_content = "No code block found to save to S3."

    # Save the message to the database and emit to the frontend
    with app.app_context():
        # Get the room object from the database
        room = get_room(room_name)
        if room:
            # Create a new message object
            new_message = Message(
                username=username, content=message_content, room_id=room.id
            )
            # Add the new message to the session and commit
            db.session.add(new_message)
            db.session.commit()

            # Emit the message to the frontend with the new message ID
            socketio.emit(
                "chat_message",
                {
                    "id": new_message.id,
                    "username": username,
                    "content": message_content,
                },
                room=room_name,
            )


def load_s3_file(room_name, s3_file_path, username):
    # Initialize the S3 client
    s3_client = get_s3_client()

    # Assuming the bucket name is set in an environment variable
    bucket_name = os.environ.get("S3_BUCKET_NAME")

    # Initialize message content variable
    message_content = ""

    try:
        # Retrieve the file content from S3
        response = s3_client.get_object(Bucket=bucket_name, Key=s3_file_path)
        file_content = response["Body"].read().decode("utf-8")

        # Format the file content as a code block
        message_content = f"```\n{file_content}\n```"

    except Exception as e:
        # Handle errors (e.g., file not found, access denied)
        message_content = f"Error loading file from S3: {e}"

    # Save the message to the database and emit to the chatroom
    with app.app_context():
        room = get_room(room_name)
        new_message = Message(
            username=username,
            content=message_content,
            room_id=room.id,
        )
        db.session.add(new_message)
        db.session.commit()

        # Emit the message to the chatroom with the message ID
        socketio.emit(
            "chat_message",
            {
                "id": new_message.id,
                "username": username,
                "content": message_content,
            },
            room=room_name,
        )


def list_s3_files(room_name, s3_file_path_pattern, username):
    import fnmatch
    from datetime import timezone

    # Initialize the S3 client
    s3_client = get_s3_client()

    # Assuming the bucket name is set in an environment variable
    bucket_name = os.environ.get("S3_BUCKET_NAME")

    # Initialize the list to hold all file information
    files = []

    # Initialize the pagination token
    continuation_token = None

    # Loop to handle pagination
    while True:
        # List objects in the S3 bucket with pagination support
        list_kwargs = {
            "Bucket": bucket_name,
        }
        if continuation_token:
            list_kwargs["ContinuationToken"] = continuation_token

        response = s3_client.list_objects_v2(**list_kwargs)

        # Process the current page of results
        for obj in response.get("Contents", []):
            key = obj["Key"]
            if s3_file_path_pattern == "*" or fnmatch.fnmatch(
                key, s3_file_path_pattern
            ):
                size = obj["Size"]
                last_modified = obj["LastModified"]
                # Convert last_modified to a timezone-aware datetime object
                last_modified = (
                    last_modified.replace(tzinfo=timezone.utc)
                    .astimezone(tz=None)
                    .strftime("%Y-%m-%d %H:%M:%S %Z")
                )
                files.append(
                    f"{key} (Size: {size} bytes, Last Modified: {last_modified})"
                )

        # Check if there are more pages
        if response.get("IsTruncated"):
            continuation_token = response.get("NextContinuationToken")
        else:
            break  # No more pages

    # Format the message content with the list of files and metadata
    message_content = (
        "```\n" + "\n".join(files) + "\n```" if files else "No files found."
    )

    # Save the message to the database and emit to the chatroom
    with app.app_context():
        room = Room.query.filter_by(name=room_name).first()
        if room:
            new_message = Message(
                username=username,
                content=message_content,
                room_id=room.id,
            )
            db.session.add(new_message)
            db.session.commit()

            # Emit the message to the chatroom with the message ID
            socketio.emit(
                "chat_message",
                {
                    "id": new_message.id,
                    "username": username,
                    "content": message_content,
                },
                room=room_name,
            )


def cancel_generation(room_name):
    with app.app_context():
        room = get_room(room_name)
        # Get the most recent message for the room that is being generated
        latest_message = (
            Message.visible(room.id).order_by(Message.id.desc()).offset(1).first()
        )

    if latest_message:
        # Set the cancellation request for the given message ID
        cancellation_requests[latest_message.id] = True
        # Optionally, inform the user that the generation has been canceled
        socketio.emit(
            "chat_message",
            {
                "id": notice_id(),
                "username": "System",
                "content": f"Generation for message ID {latest_message.id} has been canceled.",
            },
            room=room_name,
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Run the SocketIO application with optional configurations."
    )
    parser.add_argument("--profile", help="AWS profile name", default=None)
    parser.add_argument(
        "--local-activities",
        action="store_true",
        help="Use local activity files instead of S3",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5001,
        help="Port number to run the SocketIO server on (default: 5001)",
    )
    parser.add_argument(
        "--backfill-titles",
        action="store_true",
        help="Title every untitled room that holds a conversation, then exit",
    )
    parser.add_argument(
        "--backfill-blobs",
        action="store_true",
        help="Move base64 images inside messages into our blob store, then exit",
    )
    args = parser.parse_args()

    if args.backfill_blobs:
        moved, changed = backfill_blobs()
        print(f"Moved {moved} image(s) out of {changed} message(s).")
        raise SystemExit(0)

    if args.backfill_titles:
        titled, tried = backfill_titles()
        print(f"Titled {titled} of {tried} untitled rooms.")
        raise SystemExit(0 if titled == tried else 1)

    # Beside "Loaded models" above: what speech & the classifier offer.
    import classifier
    from routes import speech

    speech.log_startup()
    classifier_line = classifier.describe()
    if classifier_line:
        classifier_bases = classifier.instances()
        for label, listed in classifier.list_models().items():
            base = classifier_bases[label]["base"]
            print(f"[DEBUG] {base} returned classifier models: {listed}")
        print("Loaded", classifier_line)
    else:
        print("Loaded classifier: none configured")
    for line in startup_summary():
        print(line)
    # Set profile_name and other configurations as global attributes of the app object
    app.config["PROFILE_NAME"] = args.profile
    app.config["LOCAL_ACTIVITIES"] = args.local_activities

    # Run the SocketIO server with the specified port
    # Disable reloader to avoid gevent fork compatibility issues
    socketio.run(app, host="0.0.0.0", port=args.port, use_reloader=False)
