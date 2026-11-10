# import eventlet
# eventlet.monkey_patch()

import gevent
from gevent import monkey

monkey.patch_all()

# Under that patch asyncio's executor jobs must wake their loop themselves
# on CPython 3.13.12+ (gevent_asyncio.py), or a second one never returns.
import gevent_asyncio  # noqa: E402

gevent_asyncio.wake_executor_jobs()


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
    dead_attachment_links,
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
from types import SimpleNamespace

import tools
import chain
import fade
from tools import sandbox as tool_sandbox

tools.register(socketio)
tool_sandbox.init_app(app)

# Global dictionary to keep track of cancellation requests
cancellation_requests = {}
# Our Stop button: rooms whose streaming replies should end now, & how
# many replies each room has streaming (stoppable_reply keeps both).
stop_requests = set()
streaming_replies = {}
# Each reply streaming now, by its message id (ReplyStream): a client that
# joins mid-reply is handed its words so far from here, not from our last save.
live_replies = {}

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
    one serving this same model. Only opening is retried here: a stream
    that breaks while it is read carries on (ReplyStream.carry_on)."""
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


# Each model's context length & how many prompt characters a token held,
# from what its server lists (vLLM's max_model_len) or its first such
# refusal, so asks fit (fade.py, max_tokens) & aren't refused every round.
CONTEXT_WINDOWS = {}


# When our model map was last built (0: never) & whether a background
# refresh is running: /models answers from the map it has (get_models).
_model_map_refreshed_at = 0.0
_model_map_refresh = None


def initialize_model_map():
    """Ask every endpoint for its models & build our map. Listing waits on
    the network (other greenlets run meanwhile), so the new map is built
    aside & swapped in at once: nobody reads a half-built one."""
    global _model_map_refreshed_at
    client_map, endpoints = {}, {}
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
        for m in model_list:
            # vLLM lists each model's context length: asks fit from the first
            window = getattr(m, "max_model_len", None) or (
                getattr(m, "model_extra", None) or {}
            ).get("max_model_len")
            if m.id and isinstance(window, int) and window > 0:
                CONTEXT_WINDOWS.setdefault(m.id, (window, 3.0))
        for model_id in _endpoint_models[base_url]:
            endpoints.setdefault(model_id, []).append((client, base_url))
            if model_id not in client_map:
                client_map[model_id] = (client, base_url)

    # In place (others hold these dicts), with no network wait in between.
    MODEL_CLIENT_MAP.clear()
    MODEL_CLIENT_MAP.update(client_map)
    MODEL_ENDPOINTS.clear()
    MODEL_ENDPOINTS.update(endpoints)
    _model_map_refreshed_at = time.time()

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
            Message.deleted_at.is_(None),
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
        # The address is one parameter's value, as 2-vision.js sends it
        # (encodeURIComponent): unencoded, its own `&` & `#` cut it short.
        with httpx.Client(timeout=15.0) as client:
            response = client.get(CORS_PROXY_URL, params={"uri_target": image_url})

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


def refresh_model_map():
    """Build our model map again; a failure keeps the map we have."""
    global _model_map_refreshed_at
    try:
        initialize_model_map()
    except Exception as e:
        print(f"[WARN] Model map refresh failed: {e}")
        # Not again until the next TTL: a broken refresh must not loop.
        _model_map_refreshed_at = time.time()


@app.route("/models", methods=["GET"])
def get_models():
    """Our model list, at once, from the map we have. A map older than
    ENDPOINT_HEALTH_TTL is refreshed in the background (one refresh at a
    time), dropping endpoints that went down & picking up ones that
    recovered without a restart: a page never waits on a dead endpoint's
    timeout for its picker (1.5 s with two down, 2026-11-10). Only a map
    never built (OPENCOMPLETION_SKIP_MODEL_MAP) is built while we wait."""
    global _model_map_refresh
    if not _model_map_refreshed_at:
        refresh_model_map()
    elif time.time() - _model_map_refreshed_at > ENDPOINT_HEALTH_TTL and (
        _model_map_refresh is None or _model_map_refresh.ready()
    ):
        _model_map_refresh = gevent.spawn(refresh_model_map)
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


def missing_page(heading, detail, status):
    """A page that says what is missing & offers a way on, in our shell
    (themes, a phone's viewport), never a bare line of text."""
    return render_template("not_found.html", heading=heading, detail=detail), status


@app.errorhandler(404)
def page_not_found(error):
    # Our API & files answer scripts: they keep their own short replies.
    if request.path.startswith(("/api/", "/attachments/", "/static/")):
        return error
    return missing_page(
        "Nothing here", "No page lives at this address. It may have moved.", 404
    )


@app.route("/chat/<room_name>")
def chat(room_name):
    user = auth.get_current_user()

    # Get or create the room
    room = Room.query.filter_by(name=room_name).first()

    # A room that doesn't exist yet is created in get_room() when someone
    # joins, so only a name that may become a room gets a chat page.
    if room is None and not valid_room_name(room_name):
        return missing_page(
            "No such room",
            "A room's name is up to 64 lowercase letters, numbers, - or _, "
            "starting with a letter or number.",
            404,
        )

    # Check if they're trying to access a private room they don't own
    if room_access_denied(room, user):
        return missing_page(
            "This is someone's solo chat",
            "Only its owner can open it. If it is yours, sign in.",
            403,
        )

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
def resume_interrupted_replies():
    """After a restart, pick up every reply that was waiting on a delegate
    (Message.resume_state): uncloseai-cli ran on in its sandbox, so the
    reply reattaches to the run, shows it live again (Stop included) &
    carries on when it ends (fox, 2026-10-31). Returns how many."""
    with app.app_context():
        # A deleted reply is gone: nobody watches it, so it is not resumed.
        waiting = Message.query.filter(
            Message.resume_state.isnot(None), Message.deleted_at.is_(None)
        ).all()
        jobs = []
        for message in waiting:
            room = db.session.get(Room, message.room_id)
            try:
                state = json.loads(message.resume_state)
            except ValueError:
                state = None
            if room is None or not isinstance(state, dict) or not state.get("calls"):
                message.resume_state = None
                continue
            jobs.append((room.name, dict(state, message_id=message.id)))
        db.session.commit()
    for room_name, state in jobs:
        print(f"[INFO] resuming reply {state['message_id']} in {room_name}")
        gevent.spawn(
            stoppable_reply,
            chat_gpt,
            state.get("username") or "guest",
            room_name,
            model_name=state.get("model_name"),
            enable_thinking=bool(state.get("enable_thinking")),
            tool_context=tools.ToolContext(email=state.get("email")),
            resume=state,
        )
    return len(jobs)


def trail_as_of_now(saved, room_name, resuming=False):
    """A message's saved tool lines for history. A line saved while running
    is only still running if a reply streams in its room now: one a restart
    (a deploy) cut off said "running" forever, with no Stop & no updates
    (fox, 2026-10-31), so it reads as interrupted instead."""
    if not saved:
        return None
    trail = json.loads(saved)
    # A reply holding a checkpoint is being (or about to be) resumed.
    if streaming_replies.get(room_name) or resuming:
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
    # Joining mid-reply shows Stop; a client back after its reply ended
    # hears zero & drops a Stop it would otherwise keep forever.
    emit_streaming(room_name, to=request.sid)

    # update the title bar with the proper room title, if it exists for just this new client.
    if room.title:
        socketio.emit("update_room_title", {"title": room.title}, room=request.sid)

    # Fetch previous messages from the database
    previous_messages = Message.visible(room.id).all()

    # Send the history of messages only to the newly connected client.
    for message in previous_messages:
        payload = {**history_entry(message, room_name), "streaming": False}
        # A reply streaming right now: its words, thinking & tool lines as
        # of this instant, from memory. This client is already in the room
        # (join_room above) & nothing yields between this read & the emit,
        # so every chunk before it is in here & every chunk after follows:
        # the page ends with exactly our text.
        live = live_replies.get(message.id)
        if live is not None:
            payload.update(live.snapshot())
        emit("previous_messages", payload, room=request.sid)
    # Messages deleted lately, each a line offering Restore in its place
    # (our page puts it there by id): who wrote it & who deleted it, never
    # its words.
    for marker in deleted_markers(room):
        emit("message_deleted", marker, room=request.sid)

    # A room whose replies came before titling (or whose title call failed)
    # still shows its slug in every sidebar: title it in the background,
    # never holding up this join.
    if room.title is None and len(previous_messages) >= TITLE_MIN_MESSAGES:
        gevent.spawn(title_untitled_room, room.name)

    # commit session & active user list to database.
    db.session.commit()

    # Who came & went, as presence events, not chat lines: each join &
    # reconnect left "x has joined the room." & a token estimate in the
    # chat, piling up (fox, 2026-11-01). The others' pages fold them into
    # one line; the joiner hears its room's size as a stat, not a message.
    emit(
        "presence",
        {"username": username, "action": "joined"},
        room=room.name,
        include_self=False,
    )
    emit_room_stats(room, to=request.sid)


def history_entry(message, room_name):
    """A stored message as our page draws it from history (on join, & when
    a deleted one is restored)."""
    return {
        "id": message.id,
        "username": message.username,
        "content": message.content,
        # A model's reply can be forked from (static/js/chat/5-messages.js).
        "from_model": message.username in SYSTEM_USERS and message.username != "system",
        # Edited & resent: its pager shows what it replaced.
        "versions": message.version_count(),
        "thinking": message.thinking,
        "tools": trail_as_of_now(
            message.tool_trail, room_name, resuming=bool(message.resume_state)
        ),
    }


DEFAULT_DELETED_RETENTION_HOURS = 24


def deleted_retention_s():
    """OPENCOMPLETION_DELETED_RETENTION_HOURS (24; 0 = never) in seconds:
    how long a deleted message shows as a line offering Restore. Past it
    the row is still kept, hidden for good."""
    hours = _env_float(
        "OPENCOMPLETION_DELETED_RETENTION_HOURS", DEFAULT_DELETED_RETENTION_HOURS
    )
    return max(0, int(hours * 3600))


def restorable(message):
    """Whether a deleted message is still inside its Restore window."""
    window = deleted_retention_s()
    return bool(
        window
        and message.deleted_at is not None
        and message.deleted_at >= int(time.time()) - window
    )


def deleted_marker(message_id, author, deleted_by):
    """What a room hears of a deleted message: never its words."""
    return {
        "message_id": message_id,
        "username": author,
        "deleted_by": deleted_by,
        "restorable": True,
    }


def deleted_markers(room):
    """Markers for a room's messages deleted inside the Restore window,
    oldest first; ones an Edit & resend replaced since are left out."""
    window = deleted_retention_s()
    if not window:
        return []
    rows = (
        db.session.query(Message.id, Message.username, Message.deleted_by)
        .filter(
            Message.room_id == room.id,
            Message.superseded_by_id.is_(None),
            Message.deleted_at >= int(time.time()) - window,
        )
        .order_by(Message.id)
        .all()
    )
    return [deleted_marker(*row) for row in rows]


def emit_room_stats(room, to=None):
    """The conversation's size (its visible messages' tokens) in the room
    controls: to one joiner, else to everyone in the room after a message,
    a reply, an edit or a delete (it was sent on join only & went stale)."""
    try:
        tokens = sum(
            m.count_tokens() or 0
            for m in Message.visible(room.id).all()
            if not m.is_base64_image()
        )
        db.session.commit()  # counts made now are kept
        stats = {"tokens": tokens}
        stats.update(next_reply_size(room))
    except Exception as e:  # noqa: BLE001 — a stat never breaks the chat
        db.session.rollback()
        print(f"[WARN] room stats for {room.name}: {e}")
        return
    socketio.emit("room_stats", stats, room=to or room.name)


def next_reply_size(room):
    """What the next reply sends its model (the one that answered last, else
    the default's): its history (the room's latest messages, their tools
    rebuilt) in tokens, against the model's window, when that is known. The room's own total is no measure of fullness: a
    reply reads only its latest messages, faded to fit (fox, 2026-11-07: a
    102-message room read as full at ~55k of 64k while a reply sent 17.8k)."""
    last = (
        Message.visible(room.id)
        .filter(Message.username.in_(SYSTEM_USERS))
        .order_by(Message.id.desc())
        .first()
    )
    model = last.username if last else None
    if not model and DEFAULT_MODEL.startswith("MODEL_"):
        model = _model_for_num(DEFAULT_MODEL.split("_")[1])
    learned = CONTEXT_WINDOWS.get(model)
    if not learned:
        return {}
    window, per_token = learned
    _, history = reply_history(room.name, model)
    budget = window - answer_room() - CONTEXT_MARGIN if answer_room() else window
    history, _ = fade.fit(history, None, budget, per_token)
    return {"next": fade.tokens(history, None, per_token), "window": window}


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
            "presence",
            {"username": username, "action": "left"},
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
    chain.append(
        room.id,
        room.name,
        "user",
        messageId=new_message.id,
        username=username,
        contentSha256=chain.sha256_text(new_message.content),
    )

    emit(
        "chat_message",
        {
            "id": new_message.id,
            "username": username,
            "content": message,
        },
        room=room.name,
    )
    emit_room_stats(room)

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
        elif is_image_model(model):
            gevent.spawn(generate_image, room_name, message, username, model)
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


def message_in_room(message_id, room_name, deleted=False):
    """(message, room) when `message_id` lives in `room_name` & the caller
    may use that room, else (None, None). Edits & deletes act on one room's
    messages only; an ID from another room, or a private room the caller
    does not own, is ignored. A deleted message is as unknown as a missing
    one, except to Restore (`deleted=True`), which finds only those."""
    room = Room.query.filter_by(name=room_name).first()
    if not room or room_access_denied(room, auth.get_current_user()):
        return None, None
    message = db.session.get(Message, message_id)
    if not message or message.room_id != room.id:
        return None, None
    if (message.deleted_at is not None) != deleted:
        return None, None
    return message, room


def acting(room):
    """Who is doing this, for a chained event: `by`, the name this socket
    joined the room under (never a name the event claims; None when it
    never joined), & `byUserId`, their account when signed in. In a public
    chatroom anyone may edit or delete any message, so a chain that named
    only a message's author could never say who changed it."""
    joined = UserSession.query.filter_by(
        session_id=request.sid, room_id=room.id
    ).first()
    user = auth.get_current_user()
    return {
        "by": joined.username if joined else None,
        "byUserId": user.id if user else None,
    }


@socketio.on("delete_message")
def handle_delete_message(data):
    msg_id = data["message_id"]
    message, room = message_in_room(msg_id, data.get("room_name"))
    if not message:
        return
    if message.superseded_by_id:
        return  # an earlier version is read only; our page never offers it
    # The row stays, marked: every reader skips it (Message.visible) & a
    # Restore brings it back. Anyone may delete here, so a delete that
    # could not be undone was vandalism nobody could repair.
    who = acting(room)
    author = message.username
    message.deleted_at = int(time.time())
    message.deleted_by = who["by"]
    db.session.commit()
    # username stays the message's author, as every such line has read.
    chain.append(
        room.id,
        room.name,
        "delete",
        messageId=msg_id,
        username=author,
        **who,
    )

    # Everyone in the room swaps the message for a line offering Restore
    # (with no Restore window, it just goes).
    marker = deleted_marker(msg_id, author, who["by"])
    marker["restorable"] = restorable(message)
    emit("message_deleted", marker, room=room.name)
    emit_room_stats(room)


@socketio.on("restore_message")
def handle_restore_message(data):
    """Undo a delete, for everyone in the room: the message returns to its
    place with the words it had. Anyone who may open the room may restore,
    as anyone may delete; the chain names who (`by`).

    Not refused while a reply streams: a streaming reply read its history
    when it began, so a message returning changes nothing it holds, &
    Delete is not refused then either (an undo must be no harder to reach
    than what it undoes)."""
    data = data or {}
    msg_id = data.get("message_id")
    if not isinstance(msg_id, int):
        return
    message, room = message_in_room(msg_id, data.get("room_name"), deleted=True)
    if not message:
        return
    refusal = None
    if message.superseded_by_id:
        # Edit & resend replaced this part of the conversation since: back
        # in place it would sit inside a branch nobody is reading.
        refusal = (
            "An Edit & resend replaced that part of our chat since: "
            "it can't be restored."
        )
    elif not restorable(message):
        refusal = "That message was deleted too long ago to restore here."
    if refusal:
        emit(
            "restore_refused",
            {"message_id": msg_id, "error": refusal},
            room=request.sid,
        )
        return
    message.deleted_at = None
    message.deleted_by = None
    db.session.commit()
    chain.append(
        room.id,
        room.name,
        "restore",
        messageId=message.id,
        username=message.username,
        contentSha256=chain.sha256_text(message.content),
        **acting(room),
    )
    emit("message_restored", history_entry(message, room.name), room=room.name)
    emit_room_stats(room)


@socketio.on("update_message")
def handle_update_message(data):
    message_id = data["message_id"]
    new_content = data["content"]
    message, room = message_in_room(message_id, data.get("room_name"))
    room_name = room.name if room else None
    if message:
        # Update the message content
        message.content = new_content
        message.count_tokens(fresh=True)
        db.session.add(message)
        db.session.commit()
        chain.append(
            room.id,
            room_name,
            "edit",
            messageId=message.id,
            username=message.username,
            contentSha256=chain.sha256_text(new_content),
            **acting(room),
        )

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
        emit_room_stats(room)


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
    # Deleted messages in the branch are superseded with it, so none can
    # be restored into a conversation that moved on.
    branch = (
        Message.query.filter_by(room_id=room.id, superseded_by_id=None)
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
    chain.append(
        room.id,
        room.name,
        "user",
        messageId=revision.id,
        username=revision.username,
        contentSha256=chain.sha256_text(revision.content),
    )
    chain.append(
        room.id,
        room.name,
        "supersede",
        revisionId=revision.id,
        revisionOf=message.id,
        supersededIds=[old.id for old in branch],
    )

    emit(
        "messages_hidden",
        {"ids": [old.id for old in branch], "revision_id": revision.id},
        room=room.name,
    )
    emit(
        "chat_message",
        {
            "id": revision.id,
            "username": revision.username,
            "content": content,
            "versions": revision.version_count(),
        },
        room=room.name,
    )
    emit_room_stats(room)
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
                new_message.count_tokens(fresh=True)
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
            new_message.count_tokens(fresh=True)
            db.session.add(new_message)
            db.session.commit()

    socketio.emit(
        "message_chunk",
        {"id": msg_id, "content": "", "is_complete": True, "final": buffer},
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


DEFAULT_STREAM_RETRIES = 2


def stream_retries():
    """OPENCOMPLETION_STREAM_RETRIES (2; 0 = off): how often a reply whose
    stream breaks while it is read asks again, carrying on where it
    stopped (ReplyStream.carry_on)."""
    return max(0, _env_int("OPENCOMPLETION_STREAM_RETRIES", DEFAULT_STREAM_RETRIES))


# What a reply whose stream broke is told when it is asked to carry on.
STREAM_BROKE_NOTE = (
    "(Your reply was cut off by a network error. Continue exactly where it "
    "stopped, without repeating anything.)"
)


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
    the prompt's tokens (both remembered for the model), else None.

    vLLM's "your prompt contains at least N input tokens" is no count: N is
    the window less what we asked for, plus one, a floor on the prompt. Taken
    as the count, each retry gave back ~257 tokens & was refused again until
    the reply broke (fox, 2026-11-04). After a floor the retry asks for at
    most half as much, & for what our own estimate of the prompt leaves,
    whichever is less."""
    model = kwargs.get("model")
    length = CONTEXT_LENGTH.search(text)
    if not length:
        return None
    window = int(length.group(1))
    used = PROMPT_TOKENS.search(text)
    used = int(used.group(1) or used.group(2)) if used else 0
    floor = "at least" in text.lower()
    learned = CONTEXT_WINDOWS.get(model)
    per_token = learned[1] if learned else CHARS_PER_TOKEN
    if used:
        # 10% fewer characters a token than measured: a later prompt of
        # other text may tokenize a little denser. A floor on the prompt's
        # tokens is a ceiling on its characters a token: still safe to keep.
        per_token = min(per_token, 0.9 * prompt_chars(kwargs) / used)
    if model:
        if model not in CONTEXT_WINDOWS:
            print(f"[INFO] {model}: {window}-token context; fitting max_tokens to it")
        CONTEXT_WINDOWS[model] = (window, max(per_token, 1.0))
    if not used:
        return None
    if floor:
        estimate = int(prompt_chars(kwargs) / max(per_token, 1.0))
        room = min(
            window - max(used, estimate) - CONTEXT_MARGIN,
            kwargs.get("max_tokens", window) // 2,
        )
    else:
        room = window - used - CONTEXT_MARGIN
    return room if room >= MIN_ANSWER_TOKENS else None


def shrink_history(kwargs):
    """Make an ask's prompt smaller when it alone is past the model's
    context: drop the oldest whole turn of history (from the first message
    after the system ones up to the next message from a person), keeping
    the last person's message & all after it; with no older turn left,
    halve the longest tool result. False when nothing more can go."""
    messages = list(kwargs.get("messages") or [])
    people = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    first = next((i for i, m in enumerate(messages) if m.get("role") != "system"), None)
    if first is not None and people and first < people[-1]:
        cut = next(i for i in people if i > first)
        kwargs["messages"] = messages[:first] + messages[cut:]
        return True
    results = [
        i
        for i, m in enumerate(messages)
        if m.get("role") == "tool" and len(str(m.get("content") or "")) > 2000
    ]
    if not results:
        return False
    longest = max(results, key=lambda i: len(str(messages[i]["content"])))
    text = str(messages[longest]["content"])
    messages[longest] = dict(
        messages[longest],
        content=text[: len(text) // 2] + " …(cut to fit the model's context)",
    )
    kwargs["messages"] = messages
    return True


DEFAULT_ANSWER_ROOM = 8192


def answer_room():
    """OPENCOMPLETION_ANSWER_ROOM (8192; 0 = off): the tokens an ask keeps
    free for its answer; past that the conversation fades (fade.py)."""
    return max(0, _env_int("OPENCOMPLETION_ANSWER_ROOM", DEFAULT_ANSWER_ROOM))


def fade_to_fit(kwargs):
    """Fade kwargs' messages so the prompt leaves answer_room() (or its
    max_tokens, if less) for the answer, when the model's context is known.
    Returns the fade's step (0: nothing faded)."""
    learned = CONTEXT_WINDOWS.get(kwargs.get("model"))
    room = answer_room()
    if not learned or not room or not kwargs.get("messages"):
        return 0
    window, per_token = learned
    if kwargs.get("max_tokens"):
        room = min(room, kwargs["max_tokens"])
    budget = window - room - CONTEXT_MARGIN
    faded, step = fade.fit(kwargs["messages"], kwargs.get("tools"), budget, per_token)
    if step:
        kwargs["messages"] = faded
        print(f"[INFO] {kwargs.get('model')}: conversation faded (step {step}) to fit")
    return step


def open_completion(client, kwargs, skip_thinking=False, refusals=None, sent=None):
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
    faded = fade_to_fit(kwargs)
    fit_max_tokens(kwargs)
    for _ in range(16):
        try:
            if sent is not None:
                sent.update(max_tokens=kwargs.get("max_tokens"), faded=faded)
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
            if CONTEXT_LENGTH.search(str(e)) and shrink_history(kwargs):
                # The prompt alone is past its context: older turns go
                print(
                    f"[WARN] {kwargs.get('model')}: prompt past its context, shortened"
                )
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


def newest_image_id(last_messages, room_id):
    """The id of the newest message carrying an image (only it gets
    base64), or None."""
    for msg in last_messages:  # newest first
        is_image_msg = (
            msg.is_base64_image()
            or extract_external_image_url(msg.content)
            or has_image_attachment(msg.content, room_id)
        )
        if is_image_msg:
            print(f"Vision: will include base64 for most recent image (msg {msg.id})")
            return msg.id
    return None


DEFAULT_HISTORY_LIMIT = 200
UNFITTED_HISTORY_LIMIT = 20


def history_limit(model_name):
    """How many of a room's latest messages a reply reads. With its model's
    context known, OPENCOMPLETION_HISTORY_LIMIT (200): the fade (fade.py)
    then fits them to the window, so a long room keeps what fits instead of
    a fixed 20 (fox, 2026-11-07: a reply sent 17.8k of 64k). Unknown, 20."""
    if model_name and "gpt-4" in model_name:
        return 1000
    if CONTEXT_WINDOWS.get(model_name):
        return max(1, _env_int("OPENCOMPLETION_HISTORY_LIMIT", DEFAULT_HISTORY_LIMIT))
    return UNFITTED_HISTORY_LIMIT


def reply_history(room_name, model_name, resume=None):
    """(room_id, chat_history): the room's latest messages, oldest first, as
    model_name reads them. A resumed reply is left out of its own history."""
    limit = history_limit(model_name)

    # Check if this is a vision-capable model
    vision_enabled = is_vision_model(model_name)
    if vision_enabled:
        print(f"Vision model detected: {model_name}")

    with app.app_context():
        room = get_room(room_name)
        room_id = room.id  # Save room_id to avoid DetachedInstanceError later
        visible = Message.visible(room_id)
        if resume:
            # The reply being resumed is not history: it is being written.
            visible = visible.filter(Message.id != resume["message_id"])
        last_messages = visible.order_by(Message.id.desc()).limit(limit).all()

        chat_history = []

        # Find the most recent image message ID (only this one gets base64)
        most_recent_image_id = (
            newest_image_id(last_messages, room_id) if vision_enabled else None
        )

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
            if role == "assistant":
                # As it happened: words, the calls between them, their
                # results (split on its saved words, where the trail's
                # offsets point, then each piece's files inlined)
                for turn in tools.loop.reply_turns(
                    msg.content or "", msg.tool_trail, msg.id
                ):
                    if turn["role"] == "assistant":
                        piece = without_echoed_notes(turn["content"])
                        turn["content"] = (
                            attachment_context(piece, room_id) if piece else piece
                        )
                    chat_history.append(turn)
                continue
            chat_history.append({"role": role, "content": content})
    return room_id, chat_history


ECHOED_TOOL_NOTE = re.compile(
    r"\n*^\(tools in (?:this|your last) reply:[^\n]*\)[ \t]*$", re.M
)


def without_echoed_notes(content):
    """A past reply minus "(tools in this reply: ...)" lines. We once put
    that note in a reply's history text & models copied it into their own
    replies; left in, those copies teach the next reply the same habit."""
    if not isinstance(content, str) or "(tools in " not in content:
        return content
    return ECHOED_TOOL_NOTE.sub("", content).rstrip()


def start_reply_message(model_name, room_id, resume=None):
    """The reply's Message: (msg_id, its words so far, its finished tool
    calls), or None when the message a resume names is gone."""
    if resume:
        with app.app_context():
            resumed_message = db.session.get(Message, resume["message_id"])
            if resumed_message is None:
                return None
            resumed_trail = [
                entry
                for entry in json.loads(resumed_message.tool_trail or "[]")
                if entry.get("status") != "running"
            ]
            return resumed_message.id, resumed_message.content or "", resumed_trail
    # save empty message, we need the ID when we chunk the response.
    with app.app_context():
        new_message = Message(username=model_name, content="", room_id=room_id)
        db.session.add(new_message)
        db.session.commit()
        return new_message.id, "", []


class ReplyStream:
    """One model reply streaming into a room (chat_gpt): its words so far
    (buffer), its thinking, its tool calls & rounds, & what emits each piece
    to the room & keeps it in its Message as it goes."""

    def __init__(
        self,
        username,
        room_name,
        model_name,
        room_id,
        started,
        resume,
        enable_thinking,
        openai_client,
        chat_history,
    ):
        self.username = username
        self.room_name = room_name
        self.model_name = model_name
        self.room_id = room_id
        self.tool_events = []  # chain hashes of this reply's tool results
        # buffer: Content buffer for accumulating the chunks
        self.msg_id, self.buffer, resumed_trail = started
        self.enable_thinking = enable_thinking
        self.openai_client = openai_client
        self.chat_history = chat_history
        # A resumed reply already has its words on everyone's screen: new ones
        # join them rather than start it over.
        self.first_chunk = not (resume and self.buffer)
        # Every piece of thinking, kept with the reply (Message.thinking) & handed
        # back to the model if its thinking runs past its budget.
        self.thinking_parts = []
        self.tool_trail = list(
            resumed_trail
        )  # each finished tool call, Message.tool_trail
        # This round's place, for save_checkpoint: its calls & text, & the
        # reply's own messages before it (prior), past chat_history.
        self.round_info = {}
        self.tool_context = None
        self.tool_schemas = None
        self.offered = []
        self.tool_calls = None
        # Why a server refused this reply's tools, if it did (open_completion).
        self.tool_refusals = []
        self.sent = {}  # what the last ask sent: max_tokens, fade step
        self.with_tools, self.rounds, self.round_start = chat_history, 0, 0
        self.chunks = None
        self.looped, self.cut_at_limit = None, False
        # The ask being streamed (its kwargs, skip_thinking), so a stream
        # that breaks is asked again (carry_on); breaks so far this reply.
        self.last_ask, self.stream_breaks = None, 0
        self.cap, self.cap_name = None, "OPENCOMPLETION_MAX_TOKENS"
        self.footnote = ""
        self.notice = ""
        # The tool lines the room sees now: tool_trail plus one still running.
        self.shown_trail = list(self.tool_trail)

    def snapshot(self):
        """This reply as it stands, for a client joining while it streams
        (on_join). Memory only: reading it never yields to the stream."""
        now = {
            "content": self.buffer,
            "tools": list(self.shown_trail) or None,
            "streaming": True,
        }
        # A resumed reply's earlier thinking is in its Message, not here.
        if self.thinking_parts:
            now["thinking"] = "".join(self.thinking_parts)
        return now

    def prepare(self, tool_context, resume):
        """The first ask's arguments, with the tools this reply may call."""
        model_name, openai_client = self.model_name, self.openai_client
        room_name = self.room_name
        self.create_kwargs = {
            "model": model_name,
            "messages": self.chat_history,
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
                room_id=self.room_id,
                model=model_name,
                stopped=lambda: room_name in stop_requests,
                summarize=(
                    (
                        lambda previous, fresh, final=False: summarize_delegate(
                            openai_client, model_name, previous, fresh, final=final
                        )
                    )
                    if tools.policy.delegate_summaries_enabled()
                    else None
                ),
                checkpoint=lambda state: self.save_checkpoint(state),
                oversee=lambda digest: oversee_delegate(
                    openai_client, model_name, digest
                ),
            )
        self.tool_context = tool_context
        self.tool_schemas = tools.schemas(tool_context)
        if self.tool_schemas:
            self._offer_tools(resume)
        # The calls the model streams in its current ask, when it may call any.
        self.tool_calls = tools.ToolCallCollector() if self.tool_schemas else None

    def _offer_tools(self, resume):
        chat_history = self.chat_history
        self.create_kwargs["tools"] = self.tool_schemas
        # Which tools it has & that it never claims a run it did not make.
        chat_history.insert(0, tools.guidance(self.tool_context))
        self.offered = list(tools.offered(self.tool_context))
        if not resume:
            print(
                f"[INFO] {self.model_name} may call {', '.join(self.offered)} "
                f"for {self.username}"
            )
        # Asked for a tool by name: the first ask must call it.
        named = tools.named_tool(
            self.tool_context, chat_history[-1]["content"] if chat_history else None
        )
        if named:
            self.create_kwargs["tool_choice"] = {
                "type": "function",
                "function": {"name": named},
            }

    def open_stream(self, client):
        # Without thinking, chain-of-thought is suppressed at the template
        # level. Endpoints that reject the chat_template_kwargs param
        # (OpenAI, Groq, Mistral, Gemini) get retried without it inside the
        # helper; templates that ignore the kwarg (e.g. Hermes) simply drop it.
        self.last_ask = (self.create_kwargs, not self.enable_thinking)
        return open_completion(
            client,
            self.create_kwargs,
            skip_thinking=not self.enable_thinking,
            refusals=self.tool_refusals,
        )

    def open_ask(self, kwargs, skip_thinking, remember=True):
        """Open a stream for `kwargs`, failing over to another endpoint
        serving this model; remembered as the ask being read (carry_on)."""
        if remember:
            self.last_ask = (kwargs, skip_thinking)
        return open_with_failover(
            self.openai_client,
            self.model_name,
            lambda client: open_completion(
                client,
                kwargs,
                skip_thinking=skip_thinking,
                refusals=self.tool_refusals,
                sent=self.sent,
            ),
        )

    def carry_on(self, error, ask_start):
        """The stream broke while it was read: ask again, handing the model
        what it wrote of this ask (from ask_start) to continue from, up to
        stream_retries() times a reply. The new stream, or None when the
        reply ends here (saying why)."""
        if self.room_name in stop_requests or self.last_ask is None:
            return None
        self.stream_breaks += 1
        print(
            f"[WARN] {self.model_name} stream broke in {self.room_name} "
            f"({self.stream_breaks}/{stream_retries()}): {error}"
        )
        if self.stream_breaks <= stream_retries():
            kwargs, skip_thinking = self.last_ask
            partial = self.buffer[ask_start:]
            if partial:
                kwargs = dict(
                    kwargs,
                    messages=list(kwargs["messages"])
                    + [
                        {"role": "assistant", "content": partial},
                        {"role": "user", "content": STREAM_BROKE_NOTE},
                    ],
                )
            if self.tool_calls is not None:  # half-streamed calls are dropped
                self.tool_calls = tools.ToolCallCollector()
            try:
                return self.open_ask(kwargs, skip_thinking, remember=False)
            except Exception as e:  # noqa: BLE001 — said below, like the break
                error = e
        self.notice = f"\n\n_The stream from {self.model_name} broke ({error})._"
        return None

    def fail(self, e):
        """The first ask failed: the reply becomes its error."""
        msg_id, model_name = self.msg_id, self.model_name
        live_replies.pop(msg_id, None)
        with app.app_context():
            message_content = f"{model_name} Error: {e}"
            new_message = (
                db.session.query(Message).filter(Message.id == msg_id).one_or_none()
            )
            if new_message:
                new_message.content = message_content
                new_message.count_tokens(fresh=True)
                db.session.add(new_message)
                db.session.commit()
        socketio.emit(
            "chat_message",
            {
                "id": msg_id,
                "username": model_name,
                "content": message_content,
            },
            room=self.room_name,
        )
        socketio.emit("delete_processing_message", msg_id, room=self.room_name)

    def emit_content(self, content):
        self.buffer += content
        if self.first_chunk:
            socketio.emit(
                "message_chunk",
                {
                    "id": self.msg_id,
                    "content": content,
                    "username": self.username,
                    "model_name": self.model_name,
                    "is_first_chunk": True,
                },
                room=self.room_name,
            )
            self.first_chunk = False
        else:
            socketio.emit(
                "message_chunk",
                {"id": self.msg_id, "content": content},
                room=self.room_name,
            )
        socketio.sleep(0)  # Force immediate handling

    def consume(self, chunks, budget_chars):
        """Stream one completion into our reply. Returns (looped, cut at the
        token cap, thinking ran past budget_chars before any answer)."""
        msg_id, room_name = self.msg_id, self.room_name
        # One guard per channel: a model looping in its thinking or its
        # answer is cut off, and closing our stream makes the server stop.
        thinking_guard, answer_guard = RepetitionGuard(), RepetitionGuard()
        thought, cut = 0, False
        ask_start = len(self.buffer)
        while True:
            try:
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
                        self.thinking_parts.append(reasoning)
                        thought += len(reasoning)
                        if thinking_guard.add(reasoning):
                            return ("thinking", thinking_guard.looping), False, False
                        if budget_chars and self.first_chunk and thought > budget_chars:
                            return None, False, True
                    if self.tool_calls is not None and getattr(
                        delta, "tool_calls", None
                    ):
                        self.tool_calls.add(delta.tool_calls)
                    content = delta.content
                    if content:
                        self.emit_content(content)
                        if answer_guard.add(content):
                            return ("reply", answer_guard.looping), False, False
                return None, cut, False
            except Exception as e:  # noqa: BLE001 — a broken stream carries on
                chunks = self.carry_on(e, ask_start)
                if chunks is None:
                    return None, False, False
                self.chunks = chunks

    def save_checkpoint(self, state):
        """A long call (delegate) started: keep where to pick this reply up
        if our server restarts before it ends (Message.resume_state)."""
        checkpoint = dict(
            self.round_info,
            **state,
            username=self.username,
            model_name=self.model_name,
            enable_thinking=self.enable_thinking,
            email=getattr(self.tool_context, "email", None),
        )
        with app.app_context():
            reply = db.session.get(Message, self.msg_id)
            if reply is not None:
                reply.resume_state = json.dumps(checkpoint)
                db.session.commit()

    def clear_checkpoint(self):
        with app.app_context():
            reply = db.session.get(Message, self.msg_id)
            if reply is not None and reply.resume_state:
                reply.resume_state = None
                db.session.commit()

    def save_progress(self, trail):
        self.shown_trail = list(trail)
        with app.app_context():
            reply = db.session.get(Message, self.msg_id)
            if reply is not None:
                reply.content = self.buffer
                reply.tool_trail = json.dumps(trail) if trail else None
                db.session.commit()

    def notify_tool(self, event):
        """Show the room a tool starting & ending, inside this reply, at the
        point of its text where it happened (`at`, an offset into it), so
        text & tools read in the order they came."""
        event = dict(event, asker=self.username, at=len(self.buffer))
        if event["status"] != "running":
            self.tool_trail.append(event)
            # Evidence chained before the reply that may cite it (the
            # list of tools offered goes on the reply's own event)
            digest = event.get("tool") != "offered" and chain.append(
                self.room_id,
                self.room_name,
                "tool",
                messageId=self.msg_id,
                tool=event.get("tool"),
                label=event.get("label"),
                where=event.get("where"),
                asker=self.username,
                status=event.get("status"),
                error=event.get("error"),
                sources=[
                    s.get("url")
                    for s in event.get("sources") or []
                    if isinstance(s, dict)
                ],
                resultSha256=event.get("result_sha256"),
            )
            if digest:
                self.tool_events.append(digest)
        # Saved as it goes, so a page opened (or refreshed) mid-reply shows
        # the tool still running & the words so far, not an empty reply.
        self.save_progress(
            self.tool_trail + ([event] if event["status"] == "running" else [])
        )
        socketio.emit(
            "tool_activity",
            dict(event, id=self.msg_id, model_name=self.model_name),
            room=self.room_name,
        )
        socketio.sleep(0)

    def first_answer(self, resume):
        """Stream the first ask (a resumed reply has none: it picks up at the
        tool call it was waiting on), answering from notes when its thinking
        runs past its budget."""
        budget = thinking_budget() if self.enable_thinking else 0
        over_budget = False
        if resume:
            self.tool_calls = SimpleNamespace(calls=resume.get("calls") or [])
        else:
            self.looped, self.cut_at_limit, over_budget = self.consume(
                self.chunks, budget * CHARS_PER_TOKEN
            )
        self.cap = self.create_kwargs.get("max_tokens")
        if over_budget:
            self.answer_past_budget(budget)

    def answer_past_budget(self, budget):
        # Thinking ran past its budget with no answer begun: hang up & ask
        # again without thinking, handing the model its notes, so a long
        # think still ends in an answer instead of a cap. Works on any
        # OpenAI-compatible server (no assistant-prefix continuation).
        model_name = self.model_name
        getattr(self.chunks, "close", lambda: None)()
        print(f"[INFO] {model_name} thought past {budget} tokens in {self.room_name}")
        answer_kwargs = dict(
            self.create_kwargs,
            messages=answer_from_notes(self.chat_history, "".join(self.thinking_parts)),
        )
        answer_kwargs.pop("tools", None)  # answering from notes: no tools
        answer_kwargs.pop("tool_choice", None)
        self.tool_calls = None
        self.cap, self.cap_name = answer_tokens(), "OPENCOMPLETION_ANSWER_TOKENS"
        if self.cap:
            answer_kwargs["max_tokens"] = self.cap
        else:
            answer_kwargs.pop("max_tokens", None)
        try:
            self.chunks = self.open_ask(answer_kwargs, True)
            self.looped, self.cut_at_limit, _ = self.consume(self.chunks, 0)
            self.footnote = (
                f"\n\n_Thought past its {budget}-token budget "
                "(OPENCOMPLETION_THINKING_BUDGET), so it answered from its notes._"
            )
        except Exception as e:
            self.looped = None
            self.emit_content(
                f"\n\n_{model_name} could not answer from its notes: {e}_"
            )

    def run_tool_rounds(self, resume):
        # The model asked for tools: run them (tools.run_round: in the asker's
        # browser or on our server, tools/policy.py says which), hand it what
        # they found & stream its next ask into this same reply. Its last
        # round goes without tools, so a reply always ends in an answer.
        # The conversation so far (with_tools), its tool rounds & where this
        # round's text began are kept on the reply, so a correction round
        # (correct_dead_links) carries on from them.
        chat_history, room_name = self.chat_history, self.room_name
        if resume:
            self.rounds = max(0, int(resume.get("rounds") or 1) - 1)
            self.round_start = int(resume.get("round_start") or 0)
            self.with_tools = chat_history + list(resume.get("prior") or [])
        resume_round = resume
        while self.tool_calls is not None and self.tool_calls.calls and not self.looped:
            if room_name in stop_requests:
                break
            self.rounds += 1
            self.round_info.update(
                calls=self.tool_calls.calls,
                round_start=self.round_start,
                rounds=self.rounds,
                prior=self.with_tools[len(chat_history) :],
            )
            with app.app_context():  # sandboxes & run artifacts are rows
                self.with_tools = self.with_tools + tools.run_round(
                    self.tool_context,
                    self.tool_calls.calls,
                    self.buffer[self.round_start :],
                    self.notify_tool,
                    resume=resume_round,
                )
            resume_round = None
            self.clear_checkpoint()  # the round ended: nothing left to pick up
            if room_name in stop_requests:  # Stop pressed while a tool ran
                break
            self.round_start = len(self.buffer)
            if not self._ask_after_tools(self.with_tools, self.rounds):
                break

    def correct_dead_links(self):
        """A reply linking files that do not exist (a sandbox path with no
        file, an /attachments/ link no file of this chat has) gets one more
        ask, with its tools, naming them: it makes & shares them or says
        they are not there, in this same reply. Once per reply."""
        if not self.tool_schemas or self.looped or self.room_name in stop_requests:
            return
        if self.rounds >= tools.max_rounds():
            return
        dead = self.dead_links()
        if not dead:
            return
        print(f"[INFO] {self.model_name} linked missing files in {self.room_name}")
        self.with_tools = self.with_tools + [
            {"role": "assistant", "content": self.buffer[self.round_start :]},
            {"role": "user", "content": DEAD_LINKS_NOTE.format(links=", ".join(dead))},
        ]
        self.round_start = len(self.buffer)
        self.emit_content("\n\n")
        if self._ask_after_tools(self.with_tools, self.rounds, keep_tools=True):
            self.run_tool_rounds(None)

    def dead_links(self):
        """Every link of this reply to a file that is not there."""
        with app.app_context():
            dead = dead_attachment_links(self.buffer, self.room_id)
            if "sandbox_share_file" in self.offered:
                try:
                    dead += tool_sandbox.missing_linked_files(
                        self.tool_context, self.buffer
                    )
                except Exception as e:  # noqa: BLE001 — unknown is not missing
                    print(f"[WARN] could not check sandbox links: {e}")
        return dead

    def _ask_after_tools(self, with_tools, rounds, keep_tools=False):
        """Stream the ask after a round of tools: False when it would not
        open. keep_tools: a correction ask, never the out-of-rounds one."""
        ask = dict(self.create_kwargs, messages=with_tools)
        ask.pop("tool_choice", None)  # forced on the first ask only
        if rounds >= tools.max_rounds() and not keep_tools:
            ask.pop("tools", None)
            ask["messages"] = with_tools + [tools.out_of_rounds(rounds)]
        self.tool_calls = tools.ToolCallCollector() if "tools" in ask else None
        try:
            self.chunks = self.open_ask(ask, not self.enable_thinking)
        except Exception as e:
            self.emit_content(
                f"\n\n_{self.model_name} could not answer after its tools: {e}_"
            )
            return False
        self.looped, self.cut_at_limit, _ = self.consume(self.chunks, 0)
        return True

    def add_closing_notices(self):
        """Say why the reply ended short (refused tools, the token cap, a
        loop) at its end."""
        model_name = self.model_name
        if self.tool_refusals:
            # Say so: without tools, nothing in this reply was run or checked.
            self.footnote += (
                f"\n\n_No tools this reply: {model_name} refused them "
                f"({self.tool_refusals[0]}), so nothing here was run or checked._"
            )
        if self.cut_at_limit and not self.looped:
            room = self.sent.get("max_tokens")
            if self.cap and room and room < self.cap:
                # Fitted below our cap: it was the model's context that ran out
                self.notice = (
                    f"\n\n_Stopped: the conversation left only {room:,} tokens of "
                    "the model's context for this answer._"
                )
            elif self.cap:
                self.notice = (
                    f"\n\n_Stopped at our {self.cap}-token limit ({self.cap_name})._"
                )
            else:
                self.notice = "\n\n_Stopped: the model reached its length limit._"

        if self.looped:
            # Hang up. Where a disconnect reaches the server it aborts the
            # generation; elsewhere it runs on to max_tokens, never forever.
            close = getattr(self.chunks, "close", None)
            if close:
                close()
            where, phrase = self.looped
            self.notice = (
                f"\n\n_Stopped: {model_name} kept repeating "
                f"“{phrase[:80]}” in its {where}. Ask again, or rephrase._"
            )
            print(
                f"[WARN] {model_name} looped in its {where} in {self.room_name}: "
                f"{phrase!r}"
            )
        if self.notice or self.footnote:
            tail = self.footnote + self.notice
            self.buffer += tail
            socketio.emit(
                "message_chunk",
                {
                    "id": self.msg_id,
                    "content": tail,
                    "username": self.username,
                    "model_name": model_name,
                    "is_first_chunk": self.first_chunk,
                    # A stopped reply keeps its thinking open: it shows why.
                    "keep_thinking_open": bool(self.notice),
                },
                room=self.room_name,
            )

    def share_linked_files(self):
        # A link to a file in this chat's sandbox ("[deck](/tmp/deck.pptx)")
        # is a dead link for the person: share each one & point the link at it,
        # or say there is no such file (fox, 2026-11-01). An /attachments/
        # link no file of this chat has reads as words the same way.
        relinked, shared, missing = self.buffer, [], []
        with app.app_context():
            if self.tool_schemas and "sandbox_share_file" in self.offered:
                relinked, shared, missing = tool_sandbox.export_linked_files(
                    self.tool_context, self.buffer
                )
            dead = dead_attachment_links(relinked, self.room_id)
        if dead:
            relinked = re.sub(
                r"!?\[([^\]\n]*)\]\((/attachments/[^)\s]+)\)",
                lambda m: (
                    f"{m.group(1) or m.group(2)} (no such file in this chat)"
                    if m.group(2) in dead
                    else m.group(0)
                ),
                relinked,
            )
            missing = missing + dead
        if not (shared or missing):
            return
        self.buffer = relinked
        for item in shared:
            self.notify_tool(
                {
                    "tool": "sandbox_share_file",
                    "label": item["path"],
                    "where": "sandbox",
                    "status": "done",
                    # The app shared it, not the model: shown as such,
                    # so a model skipping sandbox_share_file is seen.
                    "auto": True,
                    "sources": [{"url": item["url"], "title": item["filename"]}],
                }
            )
        socketio.emit(
            "message_links",
            {
                "id": self.msg_id,
                "links": {item["path"]: item["url"] for item in shared},
                "missing": missing,
            },
            room=self.room_name,
        )

    def finish(self):
        """Save the whole reply, tell the room it is complete & offer
        follow-ups."""
        msg_id, room_name, buffer = self.msg_id, self.room_name, self.buffer
        # Save the entire completion to the database
        with app.app_context():
            new_message = (
                db.session.query(Message).filter(Message.id == msg_id).one_or_none()
            )
            if new_message:
                new_message.content = buffer
                new_message.thinking = "".join(self.thinking_parts) or None
                new_message.tool_trail = (
                    json.dumps(self.tool_trail) if self.tool_trail else None
                )
                new_message.resume_state = None
                new_message.count_tokens(fresh=True)
                db.session.add(new_message)
                db.session.commit()
                chain.append(
                    self.room_id,
                    room_name,
                    "assistant",
                    messageId=msg_id,
                    model=self.model_name,
                    contentSha256=chain.sha256_text(buffer),
                    thinkingSha256=chain.sha256_text(new_message.thinking),
                    toolsOffered=list(getattr(self, "offered", None) or []),
                    toolEvents=list(self.tool_events),
                )
                chain.checkpoint(self.room_id)

        # The end of a reply is authoritative: its whole text & thinking go
        # with the signal, so a page that missed a chunk (a dropped socket,
        # a tab asleep) never keeps a short reply marked complete.
        live_replies.pop(msg_id, None)
        socketio.emit(
            "message_chunk",
            {
                "id": msg_id,
                "content": "",
                "is_complete": True,
                "final": buffer,
                "thinking": "".join(self.thinking_parts) or None,
                "model_name": self.model_name,
            },
            room=room_name,
        )

        socketio.emit("delete_processing_message", msg_id, room=room_name)
        if isinstance(self.room_id, int):
            with app.app_context():
                room = db.session.get(Room, self.room_id)
                if room is not None:
                    emit_room_stats(room)
        if followups_enabled() and buffer.strip() and not self.notice:
            if room_name not in stop_requests:
                gevent.spawn(
                    suggest_followups,
                    self.openai_client,
                    self.model_name,
                    room_name,
                    msg_id,
                    self.chat_history[-1]["content"] if self.chat_history else "",
                    buffer,
                )


def chat_gpt(
    username,
    room_name,
    model_name="gpt-4o-mini",
    enable_thinking=True,
    tool_context=None,
    resume=None,
):
    """Stream a model's reply into the room, running the tools it calls.
    resume (Message.resume_state & its message_id, from
    resume_interrupted_replies) carries on a reply our server restarted in
    the middle of: same message, its words so far, its conversation, at
    the tool call it was waiting on."""
    openai_client, model_name = get_openai_client_and_model(model_name)
    room_id, chat_history = reply_history(room_name, model_name, resume)
    started = start_reply_message(model_name, room_id, resume)
    if started is None:
        return None
    reply = ReplyStream(
        username,
        room_name,
        model_name,
        room_id,
        started,
        resume,
        enable_thinking,
        openai_client,
        chat_history,
    )
    reply.prepare(tool_context, resume)
    live_replies[reply.msg_id] = reply
    finished = False
    try:
        finished = stream_reply(reply, resume)
    finally:
        # However it ended (finish & fail drop it themselves, an exception
        # must too), it streams no more.
        if live_replies.get(reply.msg_id) is reply:
            del live_replies[reply.msg_id]
    if finished:
        title_untitled_room(room_name)


def stream_reply(reply, resume):
    """chat_gpt's reply, from its first ask to its end. True once it
    finished; False when its first ask failed (the reply is its error)."""
    openai_client, model_name = reply.openai_client, reply.model_name
    try:
        # An endpoint that fails before streaming hands over to another
        # endpoint serving this same model. A resumed reply has nothing to
        # ask yet: it picks up at the tool call it was waiting on.
        reply.chunks = (
            None
            if resume
            else open_with_failover(openai_client, model_name, reply.open_stream)
        )
    except Exception as e:
        reply.fail(e)
        # exit early to avoid clobbering the error message.
        return False

    if reply.tool_schemas and not resume:
        # The reply's first line names the tools it may call, so a reply
        # that calls none shows whether it had them to call.
        reply.notify_tool({"tool": "offered", "names": reply.offered, "status": "done"})

    reply.first_answer(resume)
    reply.run_tool_rounds(resume)
    reply.correct_dead_links()
    reply.add_closing_notices()
    reply.share_linked_files()
    reply.finish()
    return True


# What a reply linking files that are not there is told (correct_dead_links).
DEAD_LINKS_NOTE = (
    "(Your reply links files that do not exist: {links}. Make & share each "
    "one, or say it is not available. Do not repeat the rest of your reply.)"
)


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


OVERSEER_PROMPT = (
    "You handed a coding task to an agent & are checking on it. Answer in "
    "one line: CONTINUE, or STEER: <a sentence or two it should act on>, "
    "or STOP: <why it cannot finish>. Steer only when it is off track; "
    "stop only when it is stuck for good."
)


def oversee_delegate(openai_client, model_name, digest):
    """The overseer's one-line review of a delegate (tools/oversight.py):
    the task, how long it has run & since it last moved, its vitals & its
    newest activity."""
    vitals = digest.get("vitals") or {}
    plan = vitals.get("plan") or {}
    files = vitals.get("files") or {}
    facts = [
        f"Running {digest.get('elapsed')}; last progress {digest.get('since_progress')} ago.",
        f"Plan: {plan.get('done', '?')}/{plan.get('total', '?')} done"
        + (f", now: {plan.get('current')}" if plan.get("current") else ""),
        f"Turn {vitals.get('turn', '?')}; files changed: {files.get('count', 0)}.",
    ]
    response = create_completion_skip_thinking(
        openai_client,
        model=model_name,
        messages=[
            {"role": "system", "content": OVERSEER_PROMPT},
            {
                "role": "user",
                "content": f"Task:\n{str(digest.get('task'))[:4000]}\n\n"
                + "\n".join(facts)
                + f"\n\nIts newest activity:\n{str(digest.get('log_tail'))[-4000:]}",
            },
        ],
        temperature=0.2,
        max_tokens=160,
        timeout=25,
    )
    return strip_reasoning(response.choices[0].message.content or "").strip()


DELEGATE_CLOSING_PROMPT = (
    "A coding agent you watched in a sandbox has ended. From your last "
    "summary, the end of its log & its final answer, say in one or two "
    "plain sentences what it achieved & how it ended. Name concrete "
    "results (tests passed, files written, errors). No preamble, no "
    "markdown, past tense."
)


def summarize_delegate(openai_client, model_name, previous, fresh, final=False):
    """A sentence or two on how a delegate is going (tools/loop.py asks
    about every 15s while its log grows). Best effort: None on failure."""
    try:
        response = create_completion_skip_thinking(
            openai_client,
            model=model_name,
            messages=[
                {
                    "role": "system",
                    "content": (
                        DELEGATE_CLOSING_PROMPT if final else DELEGATE_SUMMARY_PROMPT
                    ),
                },
                {
                    "role": "user",
                    "content": f"Earlier summary:\n{str(previous)[:1000] or '(none)'}"
                    f"\n\nNew log lines:\n{str(fresh)[-6000:]}",
                },
            ],
            temperature=0.3,
            max_tokens=220,
            timeout=20,
        )
        said = strip_reasoning(response.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001 — the log still shows
        print(f"[WARN] delegate summary failed: {e}")
        return None
    return said or None


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
                new_message.count_tokens(fresh=True)
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
            new_message.count_tokens(fresh=True)
            db.session.add(new_message)
            db.session.commit()

    socketio.emit(
        "message_chunk",
        {"id": msg_id, "content": "", "is_complete": True, "final": buffer},
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


# Models that draw instead of chat, by id. OpenAI's /v1/models lists them
# beside its chat models (gpt-image-2, ...; dall-e-2 & dall-e-3 left its API
# on 2026-05-12), so our picker offers whichever exist today & no id is
# written here. OPENCOMPLETION_IMAGE_MODELS (a regex) names another server's.
IMAGE_MODELS = re.compile(
    os.environ.get("OPENCOMPLETION_IMAGE_MODELS")
    or r"^(gpt-image|chatgpt-image|dall-e)",
    re.I,
)


def is_image_model(model):
    return bool(model and IMAGE_MODELS.search(model))


def image_request(model, prompt):
    """What we send /images/generations for `model`: one image at
    OPENCOMPLETION_IMAGE_SIZE (1024x1024) & OPENCOMPLETION_IMAGE_QUALITY
    (unset: the model's own default; low, medium or high price an image
    very differently). GPT image models always answer base64 & refuse
    response_format; a DALL-E still served elsewhere must be asked for it."""
    request = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": os.environ.get("OPENCOMPLETION_IMAGE_SIZE") or "1024x1024",
    }
    quality = os.environ.get("OPENCOMPLETION_IMAGE_QUALITY")
    if quality:
        request["quality"] = quality
    if model.lower().startswith("dall-e"):
        request["response_format"] = "b64_json"
    return request


def generate_image(room_name, message, username, model):
    """An image for `message` from the image model picked, asked of the
    endpoint that lists it, kept in our blob store as an attachment of the
    room & posted as the model's reply: its markdown with the prompt as the
    model revised it (when it says), like any file in a chat (served under
    the room's rule)."""
    import html

    image, error = None, "it sent no image"
    try:
        client = get_client_for_model(model) or OpenAI()
        response = client.images.generate(**image_request(model, message))
        first = response.data[0]
        if getattr(first, "b64_json", None):
            image = base64.b64decode(first.b64_json)
        revised_prompt = getattr(first, "revised_prompt", None) or ""
    except Exception as e:
        error = e

    with app.app_context():
        room = get_room(room_name)
        if image is None:
            content = f"Error generating image with {model}: {html.escape(str(error))}"
        else:
            mime = "image/" + (getattr(response, "output_format", None) or "png")
            attachment = save_attachment(
                room.id, image, "image." + mime.split("/")[1], mime, username
            )
            content = attachment_markdown(attachment, message[:200])
            if revised_prompt:
                content += "\n\n" + html.escape(revised_prompt)
        new_message = Message(username=model, content=content, room_id=room.id)
        db.session.add(new_message)
        db.session.commit()
        # Its "Processing..." placeholder goes (the oldest one in the room).
        socketio.emit("delete_processing_message", new_message.id, room=room_name)
        socketio.emit(
            "chat_message",
            {
                "id": new_message.id,
                "username": model,
                "content": content,
                "from_model": True,
            },
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
    parser.add_argument(
        "--chain-check",
        action="store_true",
        help="Verify every room's hash chain (deleted rooms' too), then exit",
    )
    args = parser.parse_args()

    # A deploy is a restart: our database follows our code before anything
    # reads it (db_migrate.py). A migration that fails stops us here.
    import db_migrate

    print(db_migrate.bring_up_to_date(app))

    if args.chain_check:
        with app.app_context():
            reports = [chain.verify(key) for key in chain.chains()]
        for r in reports:
            print(
                f"{'ok ' if r['ok'] else 'BAD'} {r['chain']} ({r['room']}): "
                f"{r['entries']} events, {r['state']}"
                + ("" if r["room_alive"] else ", room deleted")
                + (
                    f", breaks {r['breaks']} first at {r['first_break']} ({r['first_break_reason']})"
                    if r["breaks"]
                    else ""
                )
                + (f", seq gaps {r['seq_gaps'][:5]}" if r["seq_gaps"] else "")
                + (
                    f", bad checkpoints {r['bad_checkpoints'][:5]}"
                    if r["bad_checkpoints"]
                    else ""
                )
                + (
                    f", changed messages {r['content_mismatches'][:10]}"
                    if r["content_mismatches"]
                    else ""
                )
            )
        bad = sum(not r["ok"] for r in reports)
        print(f"{len(reports)} chain(s), {bad} with a problem.")
        raise SystemExit(1 if bad else 0)

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
    # Replies a restart cut off while a delegate ran pick up where they were.
    gevent.spawn_later(2, resume_interrupted_replies)
    # Set profile_name and other configurations as global attributes of the app object
    app.config["PROFILE_NAME"] = args.profile
    app.config["LOCAL_ACTIVITIES"] = args.local_activities

    # Run the SocketIO server with the specified port
    # Disable reloader to avoid gevent fork compatibility issues
    socketio.run(app, host="0.0.0.0", port=args.port, use_reloader=False)
