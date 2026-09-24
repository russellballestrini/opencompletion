# import eventlet
# eventlet.monkey_patch()

import gevent
from gevent import monkey

monkey.patch_all()


import json
import time
import os

import boto3
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room

from models import db, Room, UserSession, Message, ActivityState, User

app = Flask(__name__, instance_relative_config=True)

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-key-change-in-production")
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

from flask_migrate import Migrate

migrate = Migrate(app, db)

# socketio = SocketIO(app, async_mode="eventlet")
socketio = SocketIO(app, async_mode="gevent")

# Global dictionary to keep track of cancellation requests
cancellation_requests = {}

from openai import OpenAI
import activity
import auth
from activity_utils import create_completion_skip_thinking, strip_reasoning

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

# Build a dynamic model map by querying each endpoint.
MODEL_CLIENT_MAP = {}
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
        client.with_options(timeout=5.0).models.list()
        healthy = True
    except Exception as e:
        print(f"[WARN] Endpoint {base_url} failed health check: {e}")
        healthy = False
    _endpoint_health[base_url] = (healthy, time.time())
    return healthy


def is_vision_model(model_name: str) -> bool:
    """Check if a model supports vision/image input."""
    if not model_name:
        return False
    model_lower = model_name.lower()
    vision_indicators = ["-vl", "vl:", "vision", "gpt-4o", "gpt-4-turbo"]
    return any(indicator in model_lower for indicator in vision_indicators)


def initialize_model_map():
    MODEL_CLIENT_MAP.clear()
    for ep_config in ENDPOINTS:
        base_url = ep_config["base_url"]
        api_key = ep_config["api_key"]
        client = get_client_for_endpoint(base_url, api_key)
        try:
            response = client.with_options(timeout=10.0).models.list()
            model_list = response.data  # Assume each model object has an 'id' attribute
            print(f"[DEBUG] {base_url} returned models: {[m.id for m in model_list]}")
        except Exception as e:
            print(f"[WARN] Could not list models for endpoint '{base_url}': {e}")
            continue

        for m in model_list:
            model_id = m.id
            if model_id and model_id not in MODEL_CLIENT_MAP:
                MODEL_CLIENT_MAP[model_id] = (client, base_url)

    # Grow SYSTEM_USERS in place (activity.py holds a reference to this
    # list) and never remove names: messages from a model that left the
    # rotation must keep their assistant role in rebuilt chat history.
    for name in list(MODEL_CLIENT_MAP.keys()) + ["system"]:
        if name not in SYSTEM_USERS:
            SYSTEM_USERS.append(name)
    print("Loaded models:", list(MODEL_CLIENT_MAP.keys()))


if MODEL_CLIENT_MAP:
    pass
else:
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
    """Save a fetched external image as a new message in the database.

    This persists the base64 version so we don't need to fetch again.
    Only saves if no saved version exists for this URL in this room.
    """
    print(
        f"[Vision Save] Attempting to save base64 for room {room_id}: {external_url[:60]}..."
    )
    try:
        # Check if already saved for this URL in this room
        # Escape special LIKE characters in URL (%, _, \)
        escaped_url = escape_like_pattern(external_url)
        existing = Message.query.filter(
            Message.room_id == room_id,
            Message.content.like(f'%alt="Fetched from {escaped_url}"%', escape="\\"),
        ).first()

        if existing:
            print(f"[Vision Save] Already exists as message {existing.id}")
            return

        # Create img tag with base64 data
        img_content = f'<img src="{data_url}" alt="Fetched from {external_url}">'
        new_message = Message(
            username="system",  # Mark as system message
            content=img_content,
            room_id=room_id,
        )
        db.session.add(new_message)
        db.session.commit()
        print(f"[Vision Save] SUCCESS - Saved as message {new_message.id}")

        # Also cache it in memory
        _external_image_cache[external_url] = data_url
    except Exception as e:
        print(f"[Vision Save] FAILED: {e}")
        import traceback

        traceback.print_exc()
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

    messages = (
        Message.query.filter_by(room_id=room_id)
        .order_by(Message.id.asc())
        .limit(50)
        .all()
    )

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
        messages = (
            Message.query.filter_by(room_id=room.id)
            .order_by(Message.id.asc())
            .limit(10)
            .all()
        )

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
        response = client.with_options(timeout=10.0).models.list()
        if response.data:
            actual_model = response.data[0].id
            print(f"[DEBUG] Using first model from {endpoint}: {actual_model}")
            return client, actual_model
    except Exception as e:
        print(f"Warning: Could not query models from {endpoint}: {e}")
        return None

    print(f"Warning: No models found for {endpoint}, using 'model' as fallback")
    return client, "model"


def get_openai_client_and_model(model_name=None):
    """Get OpenAI client and model name.

    Supports MODEL_X references (e.g., MODEL_1, MODEL_2, MODEL_3) that map to
    environment variables MODEL_ENDPOINT_X and MODEL_API_KEY_X. A MODEL_X
    whose endpoint is down or unconfigured falls through to the next healthy
    configured endpoint, so activities keep working when the primary leaves
    rotation. Set DEFAULT_MODEL to change which reference is main.
    """
    if not model_name or model_name == "None":
        model_name = DEFAULT_MODEL

    if model_name.startswith("MODEL_"):
        requested = model_name.split("_")[1]
        candidates = [requested] + [n for n in CONFIGURED_MODEL_NUMS if n != requested]
        for num in candidates:
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

    # Direct model id (chat dropdown). If its endpoint went down, hand the
    # request to the default rotation; the reply is attributed to whichever
    # model actually served it.
    client = get_client_for_model(model_name)
    if client is not None:
        base_url = MODEL_CLIENT_MAP[model_name][1]
        if endpoint_is_healthy(client, base_url):
            return client, model_name
        print(f"[WARN] {model_name} endpoint unhealthy; using default rotation")
    else:
        print(f"[WARN] Unknown model '{model_name}'; using default rotation")
    if DEFAULT_MODEL.startswith("MODEL_"):
        return get_openai_client_and_model(None)
    return client, model_name


HELP_MESSAGE = """
**Talking to a model**
- Pick a model under **Model** in our room controls (the right sidebar; the ⚙ button on a phone). With **None**, messages go only to people.
- Models are listed live from this server's configured endpoints, so the list changes as endpoints come & go.
- An image model (a name containing `dall-e`) turns your message into an image.

**Commands**
- `/activity research/activity0.yaml`: start an activity (or choose one under **Activity**).
- `/activity cancel`, `/activity info`, `/activity metadata`: manage the running activity.
- `/s3 ls [pattern]`, `/s3 load [path]`, `/s3 save [key]`: list, load or save files in this server's S3 bucket, when one is configured.
- `/title new`: write a new room title from the conversation so far.
- `/cancel`: stop the reply that is streaming in.
- `/help`: show this message.

**Rooms**
- Everyone in a public room sees every message; anyone with its link can join.
- Private rooms are visible to their owner only. **Rooms** in our sidebar lists them all.
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
from routes.pages import safe_next_url  # noqa: E402,F401
from routes.rooms import (  # noqa: E402,F401
    emit_room_list_update,
    room_access_denied,
    search_messages,
)

routes.register(
    app,
    get_openai_client_and_model=get_openai_client_and_model,
    system_users=SYSTEM_USERS,
)


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

    # If room doesn't exist yet, it will be created in get_room() when user joins
    # But check if they're trying to access a private room they don't own
    if room_access_denied(room, user):
        return "Access denied: This is a private room", 403

    # Query public rooms and user's private rooms for sidebar
    public_rooms = (
        Room.query.filter_by(is_private=False, is_archived=False)
        .order_by(Room.id.desc())
        .all()
    )
    private_rooms = []
    if user:
        private_rooms = (
            Room.query.filter_by(is_private=True, is_archived=False, owner_id=user.id)
            .order_by(Room.id.desc())
            .all()
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
        public_rooms=public_rooms,
        private_rooms=private_rooms,
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
@socketio.on("join")
def on_join(data):
    room_name = data["room_name"]
    user = auth.get_current_user()
    existing = Room.query.filter_by(name=room_name).first()
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

    # update the title bar with the proper room title, if it exists for just this new client.
    if room.title:
        socketio.emit("update_room_title", {"title": room.title}, room=request.sid)

    # Fetch previous messages from the database
    previous_messages = Message.query.filter_by(room_id=room.id).all()

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
            },
            room=request.sid,
        )

    message_count = len(previous_messages)
    if room.title is None and message_count >= 6:
        room.title = gpt_generate_room_title(previous_messages) or room.title
        db.session.add(room)
        socketio.emit("update_room_title", {"title": room.title}, room=room.name)
        # Emit an event to update this room's title in the sidebar for all users.
        updated_room_data = {"id": room.id, "name": room.name, "title": room.title}
        emit_room_list_update(room, updated_room_data)

    # commit session & active user list and title to database.
    db.session.commit()

    # Broadcast to all clients in the room that a new user has joined.
    emit(
        "chat_message",
        {"id": None, "content": f"{username} has joined the room."},
        room=room.name,
    )
    emit(
        "chat_message",
        {
            "id": None,
            "content": f"Estimated {total_token_count} total tokens in conversation.",
        },
        room=request.sid,
    )


# Handle user leaving a room
@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
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
            {"id": None, "content": f"{username} has left the room."},
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

    # Update room's updated_at timestamp (Unix epoch)
    from datetime import datetime

    room.updated_at = int(datetime.utcnow().timestamp())
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

    commands = message.splitlines()
    for command in commands:
        if command.startswith("/help"):
            socketio.emit(
                "chat_message",
                {"id": "tmp-1", "username": "System", "content": HELP_MESSAGE},
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
            activity.handle_activity_response, room_name, message, username, model
        )
        return

    if model != "None":
        emit(
            "chat_message",
            {"id": None, "content": "<span id='processing'>Processing...</span>"},
            room=room.name,
        )
        if "anthropic.claude" in model:
            gevent.spawn(chat_claude, username, room_name, model_name=model)
        if "dall-e" in model:
            gevent.spawn(generate_dalle_image, room_name, message, username)
        else:
            # All other models (Groq, Together, Mistral, etc.) use OpenAI client
            enable_thinking = data.get("enable_thinking", True)
            gevent.spawn(
                chat_gpt,
                username,
                room_name,
                model_name=model,
                enable_thinking=enable_thinking,
            )


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


@socketio.on("get_activity_status")
def handle_get_activity_status(data):
    """Get the current activity status for a room."""
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
        all_messages = (
            Message.query.filter_by(room_id=room.id).order_by(Message.id.desc()).all()
        )

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
            if cancellation_requests.get(msg_id):
                del cancellation_requests[msg_id]
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


def chat_gpt(username, room_name, model_name="gpt-4o-mini", enable_thinking=True):
    openai_client, model_name = get_openai_client_and_model(model_name)

    temperature = 0
    limit = 20
    if "gpt-4" in model_name:
        limit = 1000
    if "o1-" in model_name:
        temperature = 1
    if "o3-" in model_name:
        temperature = 1
    if "o4-" in model_name:
        temperature = 1

    # Check if this is a vision-capable model
    vision_enabled = is_vision_model(model_name)
    if vision_enabled:
        print(f"Vision model detected: {model_name}")

    with app.app_context():
        room = get_room(room_name)
        room_id = room.id  # Save room_id to avoid DetachedInstanceError later
        last_messages = (
            Message.query.filter_by(room_id=room_id)
            .order_by(Message.id.desc())
            .limit(limit)
            .all()
        )

        chat_history = []

        # Find the most recent image message ID (only this one gets base64)
        most_recent_image_id = None
        if vision_enabled:
            for msg in last_messages:  # newest first
                is_image_msg = msg.is_base64_image() or extract_external_image_url(
                    msg.content
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

            # Regular text message - always include
            chat_history.append({"role": role, "content": msg.content})

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
    }
    if "o3" not in model_name:
        # o3 does not support temperature at all!
        create_kwargs["temperature"] = temperature
    try:
        if enable_thinking:
            chunks = openai_client.chat.completions.create(**create_kwargs)
        else:
            # Suppress chain-of-thought at the template level. Endpoints that
            # reject the chat_template_kwargs param (OpenAI, Groq, Mistral,
            # Gemini) get retried without it inside the helper; templates that
            # ignore the kwarg (e.g. Hermes) simply drop it.
            chunks = create_completion_skip_thinking(openai_client, **create_kwargs)
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

    for chunk in chunks:
        # Check if there has been a cancellation request, break if there is.
        if cancellation_requests.get(msg_id):
            del cancellation_requests[msg_id]
            break

        delta = chunk.choices[0].delta
        # Qwen3.x / Deepseek-R1 / o1: thinking streams via delta.reasoning_content,
        # final answer via delta.content. Forward reasoning deltas to the client
        # so it can lazy-create a collapsible thinking block; do NOT persist them
        # (transient, model-private intermediate state).
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            socketio.emit(
                "message_chunk",
                {"id": msg_id, "reasoning_content": reasoning},
                room=room_name,
            )
            socketio.sleep(0)

        content = delta.content

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


def chat_llama(username, room_name, model_name="mistral-7b-instruct-v0.2.Q3_K_L.gguf"):
    import llama_cpp

    # https://llama-cpp-python.readthedocs.io/en/latest/api-reference/
    model = llama_cpp.Llama(model_name, n_gpu_layers=-1, n_ctx=32000)

    limit = 15
    with app.app_context():
        room = get_room(room_name)
        last_messages = (
            Message.query.filter_by(room_id=room.id)
            .order_by(Message.id.desc())
            .limit(limit)
            .all()
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
        if cancellation_requests.get(msg_id):
            del cancellation_requests[msg_id]
            break

        delta = chunk["choices"][0]["delta"]
        # See OpenAI-client path above for rationale on reasoning_content.
        reasoning = delta.get("reasoning_content")
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


def gpt_generate_room_title(messages):
    """
    Generate a title for the room based on a list of messages.

    Returns None when no model is reachable — callers keep the old title.
    """
    openai_client, model_name = get_openai_client_and_model()
    if openai_client is None:
        print("[WARN] No model available for room title generation")
        return None

    chat_history = [
        {
            "role": "system" if msg.username in SYSTEM_USERS else "user",
            "content": f"{msg.username}: {msg.content}",
        }
        for msg in reversed(messages)
        if not msg.is_base64_image()
    ]

    chat_history.append(
        {
            "role": "system",
            "content": "return a short title for the title bar of this conversation.",
        }
    )

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


def generate_new_title(room_name, username):
    with app.app_context():
        room = get_room(room_name)
        # Get the last few messages to generate a title
        last_messages = (
            Message.query.filter_by(room_id=room.id)
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
    socketio.emit(
        "chat_message",
        {"id": None, "content": "Processing..."},
        room=room_name,
    )

    openai_client = OpenAI()
    # Initialize the content variable to hold either the image tag or an error message
    content = ""

    try:
        # Call the DALL-E 3 API to generate an image in base64 format
        response = openai_client.images.generate(
            model="dall-e-3",
            prompt=message,
            n=1,
            size="1024x1024",
            response_format="b64_json",
        )

        # Access the base64-encoded image data
        image_data = response.data[0].b64_json
        revised_prompt = response.data[0].revised_prompt

        # Create an HTML img tag with the base64 data (escape user input for XSS protection)
        import html

        escaped_message = html.escape(message)
        escaped_prompt = html.escape(revised_prompt)
        content = f'<img src="data:image/jpeg;base64,{image_data}" alt="{escaped_message}"><p>{escaped_prompt}</p>'

    except Exception as e:
        # Set the content to an error message
        content = f"Error generating image: {e}"

    # Store the content in the database and emit to the frontend
    with app.app_context():
        room = get_room(room_name)
        new_message = Message(
            username=username,
            content=content,  # Store the img tag or error message as the content
            room_id=room.id,  # Make sure you have the room ID available
        )
        db.session.add(new_message)
        db.session.commit()

        # Emit the message with the content to the frontend
        socketio.emit(
            "chat_message",
            {"id": new_message.id, "username": username, "content": content},
            room=room_name,
        )


def find_most_recent_code_block(room_name):
    with app.app_context():
        # Get the room object from the database
        room = get_room(room_name)
        if not room:
            return None  # Room not found

        # Get the most recent message for the room
        latest_message = (
            Message.query.filter_by(room_id=room.id)
            .order_by(Message.id.desc())
            .offset(1)
            .first()
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
            Message.query.filter_by(room_id=room.id)
            .order_by(Message.id.desc())
            .offset(1)
            .first()
        )

    if latest_message:
        # Set the cancellation request for the given message ID
        cancellation_requests[latest_message.id] = True
        # Optionally, inform the user that the generation has been canceled
        socketio.emit(
            "chat_message",
            {
                "id": None,
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
    args = parser.parse_args()
    # Set profile_name and other configurations as global attributes of the app object
    app.config["PROFILE_NAME"] = args.profile
    app.config["LOCAL_ACTIVITIES"] = args.local_activities

    # Run the SocketIO server with the specified port
    # Disable reloader to avoid gevent fork compatibility issues
    socketio.run(app, host="0.0.0.0", port=args.port, use_reloader=False)
