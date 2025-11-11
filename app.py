# import eventlet
# eventlet.monkey_patch()

import gevent
from gevent import monkey

monkey.patch_all()


import json
import yaml
import os

import random

import boto3
import together
from flask import (
    Flask,
    render_template,
    request,
    send_from_directory,
    jsonify,
    Response,
    redirect,
    url_for,
)

from flask_socketio import SocketIO, emit, join_room, leave_room

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import InvalidRequestError

from models import db, Room, UserSession, Message, ActivityState

app = Flask(__name__, instance_relative_config=True)

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-key-change-in-production")
app.config["SQLALCHEMY_DATABASE_URI"] = (
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


# Build a list of endpoints dynamically.
ENDPOINTS = []
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

if not ENDPOINTS:
    raise Exception("No MODEL_ENDPOINT_x environment variables found!")

# Build a dynamic model map by querying each endpoint.
MODEL_CLIENT_MAP = {}
SYSTEM_USERS = []


def get_client_for_endpoint(endpoint, api_key):
    # All providers use the OpenAI client; no endpoint URLs are hardcoded here.
    return OpenAI(api_key=api_key, base_url=endpoint)


def initialize_model_map():
    global SYSTEM_USERS
    MODEL_CLIENT_MAP.clear()
    for ep_config in ENDPOINTS:
        base_url = ep_config["base_url"]
        api_key = ep_config["api_key"]
        client = get_client_for_endpoint(base_url, api_key)
        try:
            response = client.models.list()
            model_list = response.data  # Assume each model object has an 'id' attribute
            print(f"[DEBUG] {base_url} returned models: {[m.id for m in model_list]}")
        except Exception as e:
            print(f"[WARN] Could not list models for endpoint '{base_url}': {e}")
            continue

        for m in model_list:
            model_id = m.id
            if model_id and model_id not in MODEL_CLIENT_MAP:
                MODEL_CLIENT_MAP[model_id] = (client, base_url)

    # Populate SYSTEM_USERS with dynamically loaded models.
    SYSTEM_USERS = list(MODEL_CLIENT_MAP.keys())
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


def get_openai_client_and_model(
    model_name="adamo1139/Hermes-3-Llama-3.1-8B-FP8-Dynamic",
):
    """Get OpenAI client and model name.

    Supports MODEL_X references (e.g., MODEL_1, MODEL_2, MODEL_3) that map to
    environment variables MODEL_ENDPOINT_X and MODEL_API_KEY_X.
    """
    # Handle MODEL_X references
    if model_name and model_name.startswith("MODEL_"):
        try:
            model_num = model_name.split("_")[1]
            endpoint_key = f"MODEL_ENDPOINT_{model_num}"
            api_key_key = f"MODEL_API_KEY_{model_num}"

            endpoint = os.environ.get(endpoint_key)
            api_key = os.environ.get(api_key_key)

            if endpoint and api_key:
                client = get_client_for_endpoint(endpoint, api_key)

                # Look up actual model name from MODEL_CLIENT_MAP for this endpoint
                actual_model = None
                for model_id, (registered_client, base_url) in MODEL_CLIENT_MAP.items():
                    if base_url == endpoint:
                        actual_model = model_id
                        break

                if actual_model:
                    return client, actual_model
                else:
                    # Fallback: query endpoint for models if not in map yet
                    try:
                        response = client.models.list()
                        if response.data:
                            actual_model = response.data[0].id
                            print(
                                f"[DEBUG] Using first model from {endpoint}: {actual_model}"
                            )
                            return client, actual_model
                    except Exception as e:
                        print(f"Warning: Could not query models from {endpoint}: {e}")

                    # Final fallback
                    print(
                        f"Warning: No models found for {endpoint}, using 'model' as fallback"
                    )
                    return client, "model"
            else:
                print(
                    f"Warning: MODEL_{model_num} not configured ({endpoint_key} or {api_key_key} missing)"
                )
                # Fall back to default model
                model_name = "adamo1139/Hermes-3-Llama-3.1-8B-FP8-Dynamic"
        except Exception as e:
            print(f"Warning: Failed to load {model_name}: {e}, falling back to default")
            model_name = "adamo1139/Hermes-3-Llama-3.1-8B-FP8-Dynamic"

    return get_client_for_model(model_name), model_name


HELP_MESSAGE = """
**Available Commands:**
- `/activity [s3_file_path]`: Start an activity from the specified S3 file path.
- `/activity cancel`: Cancel the current activity.
- `/activity info`: Display information about the current activity.
- `/activity metadata`: Display metadata for the current activity.
- `/s3 ls [s3_file_path_pattern]`: List files in S3 matching the pattern.
- `/s3 load [s3_file_path]`: Load a file from S3.
- `/s3 save [s3_key_path]`: Save the most recent code block from the chatroom to S3.
- `/title new`: Generates a new title which reflects conversation content for the current chatroom.
- `/cancel`: Cancel the most recent chat completion from streaming into the chatroom.
- `/help`: Display this help message.

**Interacting with AI Models:**
- Select a model from the dropdown menu above the chat input. Available models are dynamically loaded from configured endpoints and include options like `gpt-4o-mini`, `llama3-70b-8192`, `anthropic.claude-3-sonnet-20240229-v1:0`, and `dall-e-3` for image generation.
- Type your message and send it. The selected model will respond if it's not "None".
- For image generation, select `dall-e-3` and provide a prompt (e.g., "A futuristic cityscape").

**Getting Started:**
Welcome to the chatroom! Here, you can explore various AI models and engage in interactive activities. Here's how you can get started:

1. **Explore the Chatroom:**
   - Join a chatroom by navigating to its unique URL. You can see the list of available chatrooms on the main page.
   - Once inside, you can start a conversation by typing your message in the chatbox.

2. **Start an Activity:**
   - To begin an educational activity, use the `/activity` command followed by the path to the activity YAML file. For example:
**Getting Started:**

Welcome to the chatroom! Here, you can explore various AI models and engage in interactive activities. Here's how you can get started:

1. **Explore the Chatroom:**
   - Join a chatroom by navigating to its unique URL. You can see the list of available chatrooms on the main page.
   - Once inside, you can start a conversation by typing your message in the chatbox.

2. **Start an Activity:**
   - To begin an educational activity, use the `/activity` command followed by the path to the activity YAML file. For example:
     ```
     /activity research/activity0.yaml
     ```
   - The AI will guide you through the activity, providing feedback and information as you progress.

3. **Interact with AI Models:**
   - To interact with a specific AI model, simply type the model's command followed by your prompt. For example:
     ```
     gpt-4 What is the capital of France?
     ```
   - The system will process your message and provide a response from the selected model.

4. **Manage Files with S3:**
   - Use the `/s3` commands to load, save, or list files in your S3 bucket. For example, to list all files, use:
     ```
     /s3 ls *
     ```

5. **Get Help:**
   - If you need assistance or want to see a list of available commands, type `/help` to display this message.

Feel free to explore and experiment with different commands and models. Enjoy your time in the chatroom!
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


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(os.path.join(app.root_path, "static"), "favicon.ico")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/models", methods=["GET"])
def get_models():
    # Optionally refresh or reinitialize the model map here.
    # For now we simply return the keys.
    return jsonify({"models": list(MODEL_CLIENT_MAP.keys())})


@app.route("/api/activities", methods=["GET"])
def get_activities():
    """Return the list of available activities."""
    activities = []

    if app.config.get("LOCAL_ACTIVITIES"):
        # List local activity files from research directory
        import os

        research_dir = "research"
        if os.path.exists(research_dir):
            for filename in sorted(os.listdir(research_dir)):
                if filename.endswith((".yaml", ".yml")):
                    activities.append(f"research/{filename}")
    else:
        # For S3 activities, you would list from S3
        # This is a placeholder - you'd need to implement S3 listing
        pass

    return jsonify({"activities": activities})


@app.route("/api/generate-artifact-name", methods=["POST"])
def generate_artifact_name():
    """Generate a meaningful filename for an artifact using AI.

    Returns a 1-3 word filename with dashes based on what the code does.
    Respects ENABLE_AI_ARTIFACT_NAMING environment variable (enabled by default).
    """
    # Check if feature is enabled (default: true)
    enabled = os.environ.get("ENABLE_AI_ARTIFACT_NAMING", "true").lower() == "true"
    if not enabled:
        return jsonify({"filename": "compiled_binary"})

    try:
        data = request.get_json()
        code = data.get("code", "")
        language = data.get("language", "")

        if not code:
            return jsonify({"filename": "compiled_binary"})

        # Use MODEL_1 (Hermes) to generate filename
        client, model = get_openai_client_and_model("MODEL_1")

        system_prompt = """You are a filename generator. Given code, generate a SHORT, descriptive filename that represents what the code does.

Rules:
- Output ONLY the filename, nothing else
- Use 1-3 words maximum
- Use lowercase with dashes between words (e.g., "fizzbuzz" or "hello-world" or "prime-checker")
- NO file extension
- NO explanations or commentary
- Be specific about what the code does

Examples:
- Code that prints "Hello World" → "hello-world"
- Code that checks for prime numbers → "prime-checker"
- Code that plays FizzBuzz → "fizzbuzz"
- Code that sorts an array → "array-sort"
- Code that calculates factorial → "factorial"
"""

        user_prompt = f"Language: {language}\n\nCode:\n{code}\n\nGenerate filename:"

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            max_tokens=20
        )

        filename = response.choices[0].message.content.strip()

        # Clean up the filename (remove quotes, extensions, whitespace)
        filename = filename.strip('"\'')
        filename = filename.split('.')[0]  # Remove any extension
        filename = filename.replace(' ', '-')
        filename = filename.lower()

        # Validate filename (alphanumeric and dashes only)
        import re
        if not re.match(r'^[a-z0-9-]+$', filename):
            filename = "compiled_binary"

        # Ensure it's not too long (max 50 chars)
        if len(filename) > 50:
            filename = filename[:50]

        return jsonify({"filename": filename})

    except Exception as e:
        print(f"Error generating artifact name: {e}")
        return jsonify({"filename": "compiled_binary"})


@app.route("/chat/<room_name>")
def chat(room_name):
    # Query all rooms so that newest is first.
    rooms = Room.query.order_by(Room.id.desc()).all()

    # Get username from query parameters
    username = request.args.get("username", "guest")

    # Pass username and rooms into the template
    return render_template(
        "chat.html", room_name=room_name, rooms=rooms, username=username
    )


@app.route("/download_chat_history", methods=["GET"])
def download_chat_history():
    room_name = request.args.get("room_name")
    room = get_room(room_name)

    if not room:
        return jsonify({"error": "Room not found"}), 404

    messages = Message.query.filter_by(room_id=room.id).all()

    if not messages:
        return jsonify({"error": "No messages found"}), 404

    chat_history = [
        {
            "role": "system" if message.username in SYSTEM_USERS else "user",
            "content": message.content,
        }
        for message in messages
        if not message.is_base64_image()
    ]

    if not chat_history:
        return jsonify({"error": "No valid messages found"}), 404

    response = Response(
        response=json.dumps(chat_history, indent=2),
        status=200,
        mimetype="application/json",
    )
    response.headers["Content-Disposition"] = f"attachment; filename={room.name}.json"
    return response


@app.route("/download_chat_history_md", methods=["GET"])
def download_chat_history_md():
    room_name = request.args.get("room_name")
    room = get_room(room_name)

    if not room:
        return jsonify({"error": "Room not found"}), 404

    messages = Message.query.filter_by(room_id=room.id).all()

    if not messages:
        return jsonify({"error": "No messages found"}), 404

    # Access system users from the existing context
    chat_history_md = []
    toc = []
    for index, message in enumerate(messages):
        if not message.is_base64_image():  # Correctly call the method
            role = "System" if message.username in SYSTEM_USERS else "User"
            header = f"### {role}: {message.username} (Turn {index + 1})"
            toc.append(
                f"- [{role}: {message.username} (Turn {index + 1})](#{role.lower()}-{message.username.lower().replace(' ', '-')}-turn-{index + 1})"
            )
            chat_history_md.append(f"{header}\n\n{message.content}\n\n---\n")

    if not chat_history_md:
        return jsonify({"error": "No valid messages found"}), 404

    markdown_content = (
        f"# Chat History for {room.name}\n\n## Table of Contents\n"
        + "\n".join(toc)
        + "\n\n"
        + "\n".join(chat_history_md)
    )

    response = Response(response=markdown_content, status=200, mimetype="text/markdown")
    response.headers["Content-Disposition"] = f'attachment; filename="{room.name}.md"'
    return response


@app.route("/search")
def search_page():
    # Query all rooms so that newest is first.
    rooms = Room.query.order_by(Room.id.desc()).all()

    keywords = request.args.get("keywords", "")
    username = request.args.get("username", "guest")
    if not keywords:
        return render_template(
            "search.html",
            rooms=rooms,
            keywords=keywords,
            results=[],
            username=username,
            error="Keywords are required",
        )

    # Call the function to search messages
    search_results = search_messages(keywords)

    # If there's exactly one search result, redirect directly to that room
    if len(search_results) == 1:
        room_result = search_results[0]
        room_name = room_result["room_name"]

        # Build the redirect URL with current parameters
        redirect_params = {}
        if username and username != "guest":
            redirect_params["username"] = username

        # Preserve other URL parameters like model, voice, etc.
        for param in ["model", "voice"]:
            value = request.args.get(param)
            if value:
                redirect_params[param] = value

        redirect_url = url_for("chat", room_name=room_name, **redirect_params)
        return redirect(redirect_url)

    return render_template(
        "search.html",
        rooms=rooms,
        keywords=keywords,
        results=search_results,
        username=username,
        error=None,
    )


def search_messages(keywords):
    search_results = {}

    # Split the keywords by spaces and sanitize
    keyword_list = keywords.lower().split()

    # Sanitize keywords to prevent SQL injection
    sanitized_keywords = []
    for keyword in keyword_list:
        # Remove potentially dangerous characters and limit length
        sanitized_keyword = "".join(
            c for c in keyword if c.isalnum() or c.isspace() or c in "-_"
        )[:50]
        if sanitized_keyword.strip():  # Only add non-empty keywords
            sanitized_keywords.append(sanitized_keyword.strip())

    if not sanitized_keywords:
        return {}

    # Search for messages containing any of the sanitized keywords using parameterized query
    messages = Message.query.filter(
        db.or_(
            *[Message.content.ilike(f"%{keyword}%") for keyword in sanitized_keywords]
        )
    ).all()

    for message in messages:
        room = Room.query.get(message.room_id)
        if room:
            # Calculate the score based on the number of occurrences of all keywords
            score = sum(
                message.content.lower().count(keyword) for keyword in keyword_list
            )

            if room.id not in search_results:
                search_results[room.id] = {
                    "room_id": room.id,
                    "room_name": room.name,
                    "room_title": room.title,
                    "score": 0,
                }

            search_results[room.id]["score"] += score

    # Convert the dictionary to a list and sort results by score in descending order
    search_results_list = list(search_results.values())
    search_results_list.sort(key=lambda x: x["score"], reverse=True)

    return search_results_list


# Handle user joining a room
@socketio.on("join")
def on_join(data):
    room_name = data["room_name"]
    username = data["username"]
    room = get_room(room_name)

    # Add the user to the active users list
    room.add_user(username)

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
        room.title = gpt_generate_room_title(previous_messages)
        db.session.add(room)
        socketio.emit("update_room_title", {"title": room.title}, room=room.name)
        # Emit an event to update this room's title in the sidebar for all users.
        updated_room_data = {"id": room.id, "name": room.name, "title": room.title}
        socketio.emit("update_room_list", updated_room_data, room=None)

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
    room = get_room(room_name)
    username = data["username"]
    message = data["message"].strip()
    model = data.get("model", "None")

    new_message = Message(
        username=username,
        content=message,
        room_id=room.id,
    )
    db.session.add(new_message)
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
        if command.startswith("/cancel"):
            gevent.spawn(cancel_generation, room_name)

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
            gevent.spawn(chat_gpt, username, room_name, model_name=model)


@socketio.on("delete_message")
def handle_delete_message(data):
    msg_id = data["message_id"]
    # Delete the message from the database
    message = db.session.query(Message).filter(Message.id == msg_id).one_or_none()
    if message:
        db.session.delete(message)
        db.session.commit()

    # Notify all clients in the room to remove the message from their DOM
    emit("message_deleted", {"message_id": msg_id}, room=data["room_name"])


@socketio.on("update_message")
def handle_update_message(data):
    message_id = data["message_id"]
    new_content = data["content"]
    room_name = data["room_name"]

    # Find the message by ID
    message = Message.query.get(message_id)
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
                        room=room.name,
                    )
                    first_chunk = False
                else:
                    socketio.emit(
                        "message_chunk",
                        {"id": msg_id, "content": content},
                        room=room.name,
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
        socketio.emit("delete_processing_message", msg_id, room=room.name)
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
        room=room.name,
    )

    socketio.emit("delete_processing_message", msg_id, room=room.name)


def chat_gpt(username, room_name, model_name="gpt-4o-mini"):
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
                "role": "assistant" if msg.username in SYSTEM_USERS else "user",
                # "content": f"{msg.username}: {msg.content}",
                "content": msg.content,
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
        if "o3" in model_name:
            # o3 does not support temperature at all!
            chunks = openai_client.chat.completions.create(
                model=model_name,
                messages=chat_history,
                n=1,
                stream=True,
            )
        else:
            chunks = openai_client.chat.completions.create(
                model=model_name,
                messages=chat_history,
                n=1,
                temperature=temperature,
                stream=True,
            )
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
        socketio.emit("delete_processing_message", msg_id, room=room.name)
        # exit early to avoid clobbering the error message.
        return None

    for chunk in chunks:
        # Check if there has been a cancellation request, break if there is.
        if cancellation_requests.get(msg_id):
            del cancellation_requests[msg_id]
            break

        content = chunk.choices[0].delta.content

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
                    room=room.name,
                )
                first_chunk = False
            else:
                socketio.emit(
                    "message_chunk",
                    {"id": msg_id, "content": content},
                    room=room.name,
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
        room=room.name,
    )

    socketio.emit("delete_processing_message", msg_id, room=room.name)


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
        socketio.emit("delete_processing_message", msg_id, room=room.name)
        # exit early to avoid clobbering the error message.
        return None

    for chunk in chunks:
        # Check if there has been a cancellation request, break if there is.
        if cancellation_requests.get(msg_id):
            del cancellation_requests[msg_id]
            break

        content = chunk["choices"][0]["delta"].get("content")

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
                    room=room.name,
                )
                first_chunk = False
            else:
                socketio.emit(
                    "message_chunk",
                    {"id": msg_id, "content": content},
                    room=room.name,
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
        room=room.name,
    )

    socketio.emit("delete_processing_message", msg_id, room=room.name)


def gpt_generate_room_title(messages):
    """
    Generate a title for the room based on a list of messages.
    """
    openai_client, model_name = get_openai_client_and_model()

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
    response = openai_client.chat.completions.create(
        messages=chat_history,
        model=model_name,  # or any appropriate model
        max_tokens=20,
        n=1,
    )

    title = response.choices[0].message.content
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

        # Update the room title in the database
        room.title = new_title
        db.session.add(room)
        db.session.commit()

        # Emit the new title to the room.
        socketio.emit("update_room_title", {"title": new_title}, room=room_name)

        # Emit an event to update this rooms title in the sidebar for all users.
        updated_room_data = {"id": room.id, "name": room.name, "title": room.title}
        socketio.emit("update_room_list", updated_room_data, room=None)

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
    socketio.run(app, host="0.0.0.0", port=args.port, use_reloader=True)
