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

db.init_app(app)

from flask_migrate import Migrate

migrate = Migrate(app, db)

# socketio = SocketIO(app, async_mode="eventlet")
socketio = SocketIO(app, async_mode="gevent")

# Global dictionary to keep track of cancellation requests
cancellation_requests = {}

from openai import OpenAI


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
    global MODEL_CLIENT_MAP, SYSTEM_USERS
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
            gevent.spawn(cancel_activity, room_name, username)
            return
        if command.startswith("/activity info"):
            gevent.spawn(display_activity_info, room_name, username)
            return
        if command.startswith("/activity metadata"):
            gevent.spawn(display_activity_metadata, room_name, username)
            return
        if command.startswith("/activity"):
            s3_file_path = command.split(" ", 1)[1].strip()
            gevent.spawn(start_activity, room_name, s3_file_path, username)
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
        gevent.spawn(handle_activity_response, room_name, message, username)
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
    room_name = data["room_name"]
    room = get_room(room_name)

    if room:
        activity_state = ActivityState.query.filter_by(room_id=room.id).first()

        if activity_state:
            emit(
                "activity_status",
                {
                    "active": True,
                    "activity_name": activity_state.s3_file_path,
                    "section_id": activity_state.section_id,
                    "step_id": activity_state.step_id,
                },
                room=request.sid,
            )
        else:
            emit("activity_status", {"active": False}, room=request.sid)


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
                            "content": f"**{username} ({model_name}):**\n\n{content}",
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
                        "content": f"**{username} ({model_name}):**\n\n{content}",
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
                        "content": f"**{username} ({model_name}):**\n\n{content}",
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


def get_activity_content(file_path):
    """
    Load the activity content from either S3 or the local filesystem based on the configuration.
    """
    if app.config["LOCAL_ACTIVITIES"]:
        # Load the activity YAML from a local file with path traversal protection
        import os.path

        # Normalize the path and ensure it's within the research directory
        normalized_path = os.path.normpath(file_path)

        # Ensure path doesn't contain dangerous patterns
        if ".." in normalized_path or normalized_path.startswith("/"):
            raise ValueError(f"Invalid file path: {file_path}")

        # Ensure file is within research directory and has .yaml extension
        if not normalized_path.startswith("research/") or not normalized_path.endswith(
            ".yaml"
        ):
            raise ValueError(
                f"File must be in research/ directory and end with .yaml: {file_path}"
            )

        # Additional safety check - ensure resolved path is still in research dir
        full_path = os.path.abspath(normalized_path)
        research_dir = os.path.abspath("research/")
        if not full_path.startswith(research_dir):
            raise ValueError(f"Path traversal attempt detected: {file_path}")

        with open(normalized_path, "r") as file:
            activity_yaml = file.read()
    else:
        # Load the activity YAML from S3
        s3_client = get_s3_client()
        bucket_name = os.environ.get("S3_BUCKET_NAME")
        response = s3_client.get_object(Bucket=bucket_name, Key=file_path)
        activity_yaml = response["Body"].read().decode("utf-8")

    return yaml.safe_load(activity_yaml)


def loop_through_steps_until_question(
    activity_content, activity_state, room_name, username
):
    room = get_room(room_name)

    current_section_id = activity_state.section_id
    current_step_id = activity_state.step_id

    # Get the user's language preference from metadata
    user_language = activity_state.dict_metadata.get("language", "English")

    while True:
        section = next(
            (
                s
                for s in activity_content["sections"]
                if s["section_id"] == current_section_id
            ),
            None,
        )
        if not section:
            break

        step = next(
            (s for s in section["steps"] if s["step_id"] == current_step_id), None
        )
        if not step:
            break

        # Emit the current step content blocks
        if "content_blocks" in step:
            content = "\n\n".join(step["content_blocks"])
            translated_content = translate_text(content, user_language)
            new_message = Message(
                username="System", content=translated_content, room_id=room.id
            )
            db.session.add(new_message)
            db.session.commit()

            socketio.emit(
                "chat_message",
                {
                    "id": new_message.id,
                    "username": "System",
                    "content": translated_content,
                },
                room=room_name,
            )
            socketio.sleep(0.1)

        # Check if the current step has a question
        if "question" in step:
            question_content = step["question"]
            translated_question_content = translate_text(
                question_content, user_language
            )
            new_message = Message(
                username="System (Question)",
                content=translated_question_content,
                room_id=room.id,
            )
            db.session.add(new_message)
            db.session.commit()

            socketio.emit(
                "chat_message",
                {
                    "id": new_message.id,
                    "username": "System",
                    "content": translated_question_content,
                },
                room=room_name,
            )
            socketio.sleep(0.1)
            break

        # Move to the next step
        next_section, next_step = get_next_step(
            activity_content, current_section_id, current_step_id
        )

        if next_step:
            activity_state.attempts = 0
            activity_state.section_id = next_section["section_id"]
            activity_state.step_id = next_step["step_id"]

            db.session.add(activity_state)
            db.session.commit()

            current_section_id = next_section["section_id"]
            current_step_id = next_step["step_id"]
        else:
            # Activity completed

            # Display activity info before completing
            display_activity_info(room_name, username)

            db.session.delete(activity_state)
            db.session.commit()
            socketio.emit(
                "chat_message",
                {
                    "id": None,
                    "username": "System",
                    "content": "Activity completed!",
                },
                room=room_name,
            )
            break


def start_activity(room_name, s3_file_path, username):
    activity_content = get_activity_content(s3_file_path)

    with app.app_context():
        # Save the initial state to the database
        room = get_room(room_name)
        initial_section = activity_content["sections"][0]
        initial_step = initial_section["steps"][0]

        activity_state = ActivityState(
            room_id=room.id,
            section_id=initial_section["section_id"],
            step_id=initial_step["step_id"],
            max_attempts=activity_content.get("default_max_attempts_per_step", 3),
            s3_file_path=s3_file_path,  # Save the S3 file path
        )
        db.session.add(activity_state)
        db.session.commit()

        # Loop through steps until a question is found or the end is reached
        loop_through_steps_until_question(
            activity_content, activity_state, room_name, username
        )

        # Emit activity status update
        socketio.emit(
            "activity_status",
            {
                "active": True,
                "activity_name": s3_file_path,
                "section_id": initial_section["section_id"],
                "step_id": initial_step["step_id"],
            },
            room=room_name,
        )


def cancel_activity(room_name, username):
    with app.app_context():
        room = get_room(room_name)
        activity_state = ActivityState.query.filter_by(room_id=room.id).first()

        if not activity_state:
            socketio.emit(
                "chat_message",
                {
                    "id": None,
                    "username": "System",
                    "content": "No active activity found to cancel.",
                },
                room=room_name,
            )
            return

        # Delete the activity state
        db.session.delete(activity_state)
        db.session.commit()

        # Emit a message indicating the activity has been canceled
        socketio.emit(
            "chat_message",
            {
                "id": None,
                "username": "System",
                "content": "Activity has been canceled.",
            },
            room=room_name,
        )

        # Emit activity status update
        socketio.emit("activity_status", {"active": False}, room=room_name)


def display_activity_metadata(room_name, username):
    with app.app_context():
        room = get_room(room_name)
        activity_state = ActivityState.query.filter_by(room_id=room.id).first()

        if not activity_state:
            socketio.emit(
                "chat_message",
                {
                    "id": None,
                    "username": "System",
                    "content": "No active activity found.",
                },
                room=room_name,
            )
            return

        # Pretty print the metadata
        metadata_pretty = json.dumps(activity_state.dict_metadata, indent=2)

        # Store and emit the metadata
        metadata_message = f"```\n{metadata_pretty}\n```"
        new_message = Message(
            username="System", content=metadata_message, room_id=room.id
        )
        db.session.add(new_message)
        db.session.commit()

        socketio.emit(
            "chat_message",
            {
                "id": new_message.id,
                "username": "System",
                "content": metadata_message,
            },
            room=room_name,
        )


def execute_processing_script(metadata, script):
    # Prepare the local environment for the script
    local_env = {
        "metadata": metadata,
        "script_result": None,
    }

    # Execute the script
    exec(script, {}, local_env)

    # Return the result from the script
    return local_env["script_result"]


def handle_activity_response(room_name, user_response, username):
    with app.app_context():
        room = get_room(room_name)
        activity_state = ActivityState.query.filter_by(room_id=room.id).first()

        if not activity_state:
            return

        # Load the activity content
        activity_content = get_activity_content(activity_state.s3_file_path)

        try:
            # Find the current section and step
            section = next(
                s
                for s in activity_content["sections"]
                if s["section_id"] == activity_state.section_id
            )
            step = next(
                s for s in section["steps"] if s["step_id"] == activity_state.step_id
            )

            feedback_tokens_for_ai = step.get("feedback_tokens_for_ai", "")

            # Check if the step has a question
            if "question" in step:
                # Execute pre-script if it exists (runs before categorization, with user_response available)
                if "pre_script" in step:
                    print(f"DEBUG: Executing pre-script")
                    # Add user_response to a temporary copy of metadata for pre_script
                    temp_metadata = activity_state.dict_metadata.copy()
                    temp_metadata["user_response"] = user_response
                    pre_result = (
                        execute_processing_script(temp_metadata, step["pre_script"])
                        or {}
                    )
                    # Update metadata with pre-script results
                    for key, value in pre_result.get("metadata", {}).items():
                        activity_state.add_metadata(key, value)
                    print(f"DEBUG: Pre-script completed, updated metadata")

                # Categorize the user's response
                category = categorize_response(
                    step["question"],
                    user_response,
                    step["buckets"],
                    step.get("tokens_for_ai", ""),
                )

                # Initialize transition to None
                transition = None

                # Determine the transition based on the category
                if category in step["transitions"]:
                    transition = step["transitions"][category]
                elif category.isdigit() and int(category) in step["transitions"]:
                    transition = step["transitions"][int(category)]
                else:
                    if category.lower() in ["yes", "true"]:
                        category = True
                    elif category.lower() in ["no", "false"]:
                        category = False
                    if category in step["transitions"]:
                        transition = step["transitions"][category]

                # Emit an error message if no valid transition was found
                if transition is None:
                    socketio.emit(
                        "chat_message",
                        {
                            "id": None,
                            "username": "System",
                            "content": f"Error: Unrecognized category '{category}'. Please try again.",
                        },
                        room=room_name,
                    )
                    return

                next_section_and_step = transition.get("next_section_and_step", None)
                counts_as_attempt = transition.get("counts_as_attempt", True)

                # Emit the category to the frontend
                socketio.emit(
                    "chat_message",
                    {
                        "id": None,
                        "username": "System",
                        "content": f"Category: {category}",
                    },
                    room=room_name,
                )
                socketio.sleep(0.1)

                # Check metadata conditions for the current step
                if "metadata_conditions" in transition:
                    conditions_met = all(
                        activity_state.dict_metadata.get(key) == value
                        for key, value in transition["metadata_conditions"].items()
                    )
                    if not conditions_met:
                        # Emit a message indicating the conditions are not met
                        socketio.emit(
                            "chat_message",
                            {
                                "id": None,
                                "username": "System",
                                "content": "You do not have the required items to proceed.",
                            },
                            room=room_name,
                        )
                        # Remind the user of what they can do in the room
                        if "content_blocks" in step or "question" in step:
                            content_blocks = step.get("content_blocks", [])
                            question = step.get("question", "")
                            options_message = (
                                "\n\n".join(content_blocks) + "\n\n" + question
                            )

                            new_message = Message(
                                username="System",
                                content=options_message,
                                room_id=room.id,
                            )
                            db.session.add(new_message)
                            db.session.commit()

                            socketio.emit(
                                "chat_message",
                                {
                                    "id": new_message.id,
                                    "username": "System",
                                    "content": options_message,
                                },
                                room=room_name,
                            )
                        # exit early, the user may not pass ... yet.
                        return

                # this gives the llm context on what changed.
                new_metadata = {}

                # Track temporary metadata keys that last for a single turn.
                metadata_tmp_keys = []

                # Update metadata based on user actions
                if "metadata_add" in transition:
                    for key, value in transition["metadata_add"].items():
                        if value == "the-users-response":
                            value = user_response
                        elif value == "the-llms-response":
                            continue
                        elif isinstance(value, str):
                            if value.startswith("n+random(") and value.endswith(")"):
                                # Extract the range and apply the random increment
                                range_values = value[9:-1].split(",")
                                if len(range_values) == 2:
                                    x, y = map(int, range_values)
                                    value = activity_state.dict_metadata.get(
                                        key, 0
                                    ) + random.randint(x, y)
                            elif value.startswith("n+") or value.startswith("n-"):
                                # Extract the numeric part c and apply the operation +/-
                                c = int(value[1:])
                                if value.startswith("n+"):
                                    value = activity_state.dict_metadata.get(key, 0) + c
                                elif value.startswith("n-"):
                                    value = activity_state.dict_metadata.get(key, 0) - c
                        new_metadata[key] = value
                        activity_state.add_metadata(key, value)

                # Update metadata based on user actions
                if "metadata_tmp_add" in transition:
                    for key, value in transition["metadata_tmp_add"].items():
                        if value == "the-users-response":
                            value = user_response
                        elif value == "the-llms-response":
                            continue
                        elif isinstance(value, str):
                            if value.startswith("n+random(") and value.endswith(")"):
                                # Extract the range and apply the random increment
                                range_values = value[9:-1].split(",")
                                if len(range_values) == 2:
                                    x, y = map(int, range_values)
                                    value = activity_state.dict_metadata.get(
                                        key, 0
                                    ) + random.randint(x, y)
                            elif value.startswith("n+") or value.startswith("n-"):
                                # Extract the numeric part c and apply the operation +/-
                                c = int(value[1:])
                                if value.startswith("n+"):
                                    value = activity_state.dict_metadata.get(key, 0) + c
                                elif value.startswith("n-"):
                                    value = activity_state.dict_metadata.get(key, 0) - c
                        new_metadata[key] = value
                        metadata_tmp_keys.append(key)
                        activity_state.add_metadata(key, value)

                # Update metadata by appending values to lists
                if "metadata_append" in transition:
                    for key, value in transition["metadata_append"].items():
                        # Determine the value to append
                        if value == "the-users-response":
                            value_to_append = user_response
                        elif value == "the-llms-response":
                            continue  # Handle this after feedback
                        else:
                            value_to_append = value

                        # Ensure the key exists and is a list
                        current_value = activity_state.dict_metadata.get(key, [])
                        if not isinstance(current_value, list):
                            current_value = [current_value]

                        # Append the value to the list
                        if isinstance(value_to_append, list):
                            current_value.extend(value_to_append)
                        else:
                            current_value.append(value_to_append)

                        # Update the metadata
                        activity_state.add_metadata(key, current_value)

                # Update temporary metadata by appending values to lists
                if "metadata_tmp_append" in transition:
                    for key, value in transition["metadata_tmp_append"].items():
                        # Determine the value to append
                        if value == "the-users-response":
                            value_to_append = user_response
                        elif value == "the-llms-response":
                            continue  # Handle this after feedback
                        else:
                            value_to_append = value

                        # Ensure the key exists and is a list
                        current_value = activity_state.dict_metadata.get(key, [])
                        if not isinstance(current_value, list):
                            current_value = [current_value]

                        # Append the value to the list
                        if isinstance(value_to_append, list):
                            current_value.extend(value_to_append)
                        else:
                            current_value.append(value_to_append)

                        # Update the metadata
                        activity_state.add_metadata(key, current_value)

                        # Track temporary metadata keys
                        metadata_tmp_keys.append(key)

                if "metadata_remove" in transition:
                    for key in transition["metadata_remove"]:
                        activity_state.remove_metadata(key)

                # Handle metadata_random
                if "metadata_random" in transition:
                    random_key = random.choice(
                        list(transition["metadata_random"].keys())
                    )
                    random_value = transition["metadata_random"][random_key]
                    new_metadata[random_key] = random_value
                    activity_state.add_metadata(random_key, random_value)

                if "metadata_tmp_random" in transition:
                    random_key = random.choice(
                        list(transition["metadata_tmp_random"].keys())
                    )
                    random_value = transition["metadata_tmp_random"][random_key]
                    new_metadata[random_key] = random_value
                    metadata_tmp_keys.append(random_key)
                    activity_state.add_metadata(random_key, random_value)

                # Execute the post-script if it exists (supports both old and new naming)
                post_script = step.get("post_script") or step.get("processing_script")
                if post_script and (
                    transition.get("run_post_script", False)
                    or transition.get("run_processing_script", False)
                ):
                    print(f"DEBUG: Executing post-script")
                    result = (
                        execute_processing_script(
                            activity_state.dict_metadata, post_script
                        )
                        or {}
                    )

                    plot_image_base64 = result.pop("plot_image", None)

                    # Add the result to the temporary metadata for use in AI feedback
                    metadata_tmp_keys.append("processing_script_result")
                    activity_state.add_metadata("processing_script_result", result)

                    # Update metadata with results from the processing script
                    for key, value in result.get("metadata", {}).items():
                        activity_state.add_metadata(key, value)

                    # Check if processing script wants to override the transition
                    if "next_section_and_step" in result:
                        next_section_and_step = result["next_section_and_step"]
                        print(
                            f"DEBUG: Processing script overriding transition to: {next_section_and_step}"
                        )

                    # Check if the result contains a plot image
                    if plot_image_base64:
                        plot_image_html = f'<img alt="Plot Image" src="data:image/png;base64,{plot_image_base64}">'

                        if result.get("set_background", False):
                            socketio.emit(
                                "set_background",
                                {"image_data": plot_image_base64},
                                room=room_name,
                            )
                            socketio.sleep(0.1)
                        else:
                            # Save the plot image to the database
                            new_message = Message(
                                username=username,
                                content=plot_image_html,
                                room_id=room.id,
                            )
                            db.session.add(new_message)
                            db.session.commit()

                            # Emit the plot image to the frontend
                            socketio.emit(
                                "chat_message",
                                {
                                    "id": new_message.id,
                                    "username": username,
                                    "content": plot_image_html,
                                },
                                room=room_name,
                            )
                            socketio.sleep(0.1)

                if (
                    "metadata_clear" in transition
                    and transition["metadata_clear"] == True
                ):
                    activity_state.clear_metadata()

                print(activity_state.dict_metadata)

                # Commit the changes after the loop
                db.session.add(activity_state)
                db.session.commit()

                user_language = activity_state.dict_metadata.get("language", "English")

                # Emit the transition content blocks if they exist
                if "content_blocks" in transition:
                    transition_content = "\n\n".join(transition["content_blocks"])
                    translated_transition_content = translate_text(
                        transition_content, user_language
                    )
                    new_message = Message(
                        username="System",
                        content=translated_transition_content,
                        room_id=room.id,
                    )
                    db.session.add(new_message)
                    db.session.commit()

                    socketio.emit(
                        "chat_message",
                        {
                            "id": new_message.id,
                            "username": "System",
                            "content": translated_transition_content,
                        },
                        room=room_name,
                    )
                    socketio.sleep(0.1)

                # if "correct" or max_attempts reached.
                # Provide feedback based on the category

                # Handle feedback systems
                feedback_messages = []

                if "feedback_prompts" in step:
                    # New multi-prompt system - pass full metadata, let each prompt filter
                    multi_feedback_messages = provide_feedback_prompts(
                        transition,
                        category,
                        step["question"],
                        step["feedback_prompts"],
                        user_response,
                        user_language,
                        username,
                        json.dumps(activity_state.dict_metadata),  # Pass full metadata
                        json.dumps(new_metadata),
                        feedback_tokens_for_ai,  # Pass legacy tokens to be combined
                    )
                    feedback_messages.extend(multi_feedback_messages)
                elif feedback_tokens_for_ai:
                    # Legacy single feedback system - use transition-level filtering
                    feedback_metadata = activity_state.dict_metadata
                    if "metadata_feedback_filter" in transition:
                        filter_keys = transition["metadata_feedback_filter"]
                        feedback_metadata = {
                            k: v
                            for k, v in activity_state.dict_metadata.items()
                            if k in filter_keys
                        }

                    feedback = provide_feedback(
                        transition,
                        category,
                        step["question"],
                        feedback_tokens_for_ai,
                        user_response,
                        user_language,
                        username,
                        json.dumps(feedback_metadata),
                        json.dumps(new_metadata),
                    )
                    if feedback and feedback.strip():
                        feedback_messages.append(
                            {"name": "Feedback", "content": feedback}
                        )

                # Store and emit all feedback messages
                for feedback_msg in feedback_messages:
                    new_message = Message(
                        username=f"System ({feedback_msg['name'].title()})",
                        content=feedback_msg["content"],
                        room_id=room.id,
                    )
                    db.session.add(new_message)
                    db.session.commit()

                    socketio.emit(
                        "chat_message",
                        {
                            "id": new_message.id,
                            "username": f"System ({feedback_msg['name'].title()})",
                            "content": feedback_msg["content"],
                        },
                        room=room_name,
                    )
                    socketio.sleep(0.1)

                    # Add or append the LLM's response to the metadata
                    for key, value in transition.get("metadata_add", {}).items():
                        if value == "the-llms-response":
                            activity_state.add_metadata(key, feedback)

                    for key, value in transition.get("metadata_append", {}).items():
                        if value == "the-llms-response":
                            # Ensure the key exists and is a list
                            current_value = activity_state.dict_metadata.get(key, [])
                            if not isinstance(current_value, list):
                                current_value = [current_value]

                            # Append the feedback to the list
                            current_value.append(feedback)
                            activity_state.add_metadata(key, current_value)

                if (
                    category
                    not in [
                        "partial_understanding",
                        "limited_effort",
                        "asking_clarifying_questions",
                        "set_language",
                        "off_topic",
                    ]
                    or activity_state.attempts >= activity_state.max_attempts
                    or next_section_and_step  # Processing script override takes precedence
                ):
                    if next_section_and_step:
                        (
                            current_section_id,
                            current_step_id,
                        ) = next_section_and_step.split(":")
                        next_section = next(
                            s
                            for s in activity_content["sections"]
                            if s["section_id"] == current_section_id
                        )
                        next_step = next(
                            s
                            for s in next_section["steps"]
                            if s["step_id"] == current_step_id
                        )
                    else:
                        # Move to the next step or section
                        next_section, next_step = get_next_step(
                            activity_content, section["section_id"], step["step_id"]
                        )

                    if next_step:
                        activity_state.attempts = 0
                        activity_state.section_id = next_section["section_id"]
                        activity_state.step_id = next_step["step_id"]

                        db.session.add(activity_state)
                        db.session.commit()

                        # Loop through steps until a question is found or the end is reached
                        loop_through_steps_until_question(
                            activity_content, activity_state, room_name, username
                        )
                else:
                    # the user response is any bucket other than correct.
                    if counts_as_attempt:
                        activity_state.attempts += 1
                        db.session.add(activity_state)
                        db.session.commit()

                    # Emit the question again
                    question_content = step["question"]
                    translated_question_content = translate_text(
                        question_content, user_language
                    )
                    new_message = Message(
                        username="System (Question)",
                        content=translated_question_content,
                        room_id=room.id,
                    )
                    db.session.add(new_message)
                    db.session.commit()

                    socketio.emit(
                        "chat_message",
                        {
                            "id": new_message.id,
                            "username": "System",
                            "content": translated_question_content,
                        },
                        room=room_name,
                    )
                    socketio.sleep(0.1)

                # Check if the activity state still exists before removing temporary metadata
                try:
                    # Remove temporary metadata at the end of the turn
                    for key in metadata_tmp_keys:
                        activity_state.remove_metadata(key)

                    # Commit the changes after removing temporary metadata
                    db.session.add(activity_state)
                    db.session.commit()

                except InvalidRequestError:
                    # Handle the case where the activity state was deleted
                    # print("Activity state was deleted before commit.")
                    db.session.rollback()

            else:
                # Handle steps without a question
                loop_through_steps_until_question(
                    activity_content, activity_state, room_name, username
                )

        except Exception as e:
            import traceback

            msg = traceback.format_exc()
            socketio.emit(
                "chat_message",
                {
                    "id": None,
                    "username": "System",
                    "content": f"Error processing activity response: {e}\n\n{msg}",
                },
                room=room_name,
            )


def display_activity_info(room_name, username):
    with app.app_context():
        room = get_room(room_name)
        activity_state = ActivityState.query.filter_by(room_id=room.id).first()

        if not activity_state:
            socketio.emit(
                "chat_message",
                {
                    "id": None,
                    "username": "System",
                    "content": "No active activity found.",
                },
                room=room_name,
            )
            return

        # Load the activity content
        activity_content = get_activity_content(activity_state.s3_file_path)

        try:
            # Fetch the entire room history
            all_messages = (
                Message.query.filter_by(room_id=room.id)
                .order_by(Message.id.asc())
                .all()
            )
            chat_history = [
                {
                    "role": "system" if msg.username in SYSTEM_USERS else "user",
                    "username": msg.username,
                    "content": msg.content,
                }
                for msg in all_messages
                if not msg.is_base64_image()
            ]

            # Prepare the rubric for grading
            rubric = activity_content.get(
                "tokens_for_ai_rubric",
                """
                Grade the responses of all users based on the following criteria:
                - Accuracy: How correct is the response?
                - Completeness: Does the response fully address the question?
                - Clarity: Is the response clear and easy to understand?
                - Engagement: Is the response engaging and interesting?
                Provide a score out of 10 for each criterion and an overall grade for each user.
                Finally order each user by who is winning. Number of correct answers and accuracy & include an enumeration of the feats!
                Take into account how many attempts the user took to get a passing answer when ranking.
                Don't just try to give the user a "B" or 35/40, really figure out a good placement considering some people don't know how to type.
            """,
            )

            # Generate the grading using the AI
            grading_message = generate_grading(chat_history, rubric)

            # Store and emit the activity info
            info_message = f"Activity Info:\nCurrent Section: {activity_state.section_id}\nCurrent Step: {activity_state.step_id}\nAttempts: {activity_state.attempts}\n\n{grading_message}"
            new_message = Message(
                username="System", content=info_message, room_id=room.id
            )
            db.session.add(new_message)
            db.session.commit()

            socketio.emit(
                "chat_message",
                {
                    "id": new_message.id,
                    "username": "System",
                    "content": info_message,
                },
                room=room_name,
            )

        except Exception as e:
            socketio.emit(
                "chat_message",
                {
                    "id": None,
                    "username": "System",
                    "content": f"Error displaying activity info: {e}",
                },
                room=room_name,
            )
            # Debugging: Log exception
            print(f"Exception: {e}")


def generate_grading(chat_history, rubric):
    openai_client, model_name = get_openai_client_and_model()
    messages = [
        {
            "role": "system",
            "content": f"Using the following rubric, grade the responses in the chat history:\n\n{rubric}",
        },
        {
            "role": "user",
            "content": f"Chat History:\n\n{json.dumps(chat_history, indent=2)}",
        },
    ]

    try:
        completion = openai_client.chat.completions.create(
            model=model_name,
            messages=messages,
            max_tokens=1000,
            temperature=0.7,
            n=1,
        )
        grading = completion.choices[0].message.content.strip()
        return grading
    except Exception as e:
        return f"Error generating grading: {e}"


def get_next_step(activity_content, current_section_id, current_step_id):
    for section in activity_content["sections"]:
        if section["section_id"] == current_section_id:
            for i, step in enumerate(section["steps"]):
                if step["step_id"] == current_step_id:
                    if i + 1 < len(section["steps"]):
                        return section, section["steps"][i + 1]
                    else:
                        # Move to the next section
                        next_section_index = (
                            activity_content["sections"].index(section) + 1
                        )
                        if next_section_index < len(activity_content["sections"]):
                            next_section = activity_content["sections"][
                                next_section_index
                            ]
                            return next_section, next_section["steps"][0]
    return None, None


# Categorize the user's response.
def categorize_response(question, response, buckets, tokens_for_ai):
    openai_client, model_name = get_openai_client_and_model()
    bucket_list = ", ".join([str(bucket) for bucket in buckets])
    # Check if tokens_for_ai already includes format instructions (ANALYSIS/BUCKET format)
    if "ANALYSIS:" in tokens_for_ai and "BUCKET:" in tokens_for_ai:
        # YAML already specifies output format, don't override
        system_content = f"{tokens_for_ai}"
        user_content = f"Question: {question}\nResponse: {response}"
    else:
        # Use old simple format for backwards compatibility
        system_content = f"{tokens_for_ai} Categorize the following response into one of the following buckets: {bucket_list}. Return ONLY a bucket label."
        user_content = f"Question: {question}\nResponse: {response}\n\nCategory:"

    messages = [
        {
            "role": "system",
            "content": system_content,
        },
        {
            "role": "user",
            "content": user_content,
        },
    ]

    try:
        completion = openai_client.chat.completions.create(
            model=model_name,
            messages=messages,
            n=1,
            max_tokens=150,  # Increased for ANALYSIS + BUCKET format
            temperature=0,
        )
        full_response = completion.choices[0].message.content.strip()
        print(f"DEBUG BUCKET CATEGORIZATION: Full Hermes response: {full_response}")

        # Handle both ANALYSIS/BUCKET format and simple bucket response
        if "BUCKET:" in full_response:
            # New ANALYSIS/BUCKET format
            bucket_lines = [
                line for line in full_response.split("\n") if "BUCKET:" in line
            ]
            if bucket_lines:
                category = (
                    bucket_lines[0]
                    .split("BUCKET:")[1]
                    .strip()
                    .lower()
                    .replace(" ", "_")
                )
            else:
                category = full_response.lower().replace(" ", "_")
        elif "ANALYSIS:" in full_response:
            # Has analysis but no explicit BUCKET: line, try to extract from end
            lines = [line.strip() for line in full_response.split("\n") if line.strip()]
            if lines:
                category = lines[-1].lower().replace(" ", "_")
            else:
                category = full_response.lower().replace(" ", "_")
        else:
            # Simple bucket response (old format)
            category = full_response.lower().replace(" ", "_")

        print(f"DEBUG BUCKET CATEGORIZATION: Extracted category: {category}")
        return category
    except Exception as e:
        return f"Error: {e}"


# Generate AI feedback
def generate_ai_feedback(
    category,
    question,
    user_response,
    tokens_for_ai,
    username,
    json_metadata,
    json_new_metadata,
):
    openai_client, model_name = get_openai_client_and_model()
    messages = [
        {
            "role": "system",
            "content": f"{tokens_for_ai} Generate a human-readable feedback message based on the following:",
        },
        {
            "role": "user",
            "content": f"Username: {username}\nQuestion: {question}\nResponse: {user_response}\nCategory: {category}\nMetadata: {json_metadata}\n New Metadata: {json_new_metadata}",
        },
    ]

    try:
        completion = openai_client.chat.completions.create(
            model=model_name, messages=messages, max_tokens=1000, temperature=0.7, n=1
        )
        feedback = completion.choices[0].message.content.strip()
        return feedback
    except Exception as e:
        return f"Error: {e}"


def provide_feedback(
    transition,
    category,
    question,
    tokens_for_ai,
    user_response,
    user_language,
    username,
    json_metadata,
    json_new_metadata,
):
    feedback = ""
    if "ai_feedback" in transition:
        tokens_for_ai += f" You must provide the feedback in the user's language: {user_language}. {transition['ai_feedback'].get('tokens_for_ai', '')}."
        ai_feedback = generate_ai_feedback(
            category,
            question,
            user_response,
            tokens_for_ai,
            username,
            json_metadata,
            json_new_metadata,
        )
        feedback += f"\n\n{ai_feedback}"

    return feedback


def provide_feedback_prompts(
    transition,
    category,
    question,
    feedback_prompts,
    user_response,
    user_language,
    username,
    json_metadata,
    json_new_metadata,
    legacy_tokens_for_ai="",
):
    """Generate feedback from multiple prompts"""
    feedback_messages = []

    # Parse full metadata once for filtering
    full_metadata = json.loads(json_metadata)

    # Add user_response to metadata for filtering purposes
    full_metadata["user_response"] = user_response

    for prompt in feedback_prompts:
        prompt_name = prompt.get("name", "unnamed")
        tokens_for_ai = prompt.get("tokens_for_ai", "")

        # Apply per-prompt metadata filtering if specified
        prompt_metadata = full_metadata
        if "metadata_filter" in prompt:
            filter_keys = prompt["metadata_filter"]
            prompt_metadata = {
                k: v for k, v in full_metadata.items() if k in filter_keys
            }

            # Special debug for Ship Status
            if prompt_name == "Ship Status":
                print(f"DEBUG SHIP STATUS - filter_keys: {filter_keys}")
                print(f"DEBUG SHIP STATUS - filtered metadata: {prompt_metadata}")
                print(
                    f"DEBUG SHIP STATUS - user_sunk_ship_this_round = '{prompt_metadata.get('user_sunk_ship_this_round')}'"
                )
                print(
                    f"DEBUG SHIP STATUS - ai_sunk_ship_this_round = '{prompt_metadata.get('ai_sunk_ship_this_round')}'"
                )
        else:
            if prompt_name == "Ship Status":
                print(
                    f"DEBUG SHIP STATUS - NO metadata_filter, full metadata: {prompt_metadata}"
                )

        # Combine legacy tokens with prompt-specific tokens
        if legacy_tokens_for_ai:
            tokens_for_ai = legacy_tokens_for_ai + " " + tokens_for_ai

        # Add language instruction
        tokens_for_ai += (
            f" You must provide the feedback in the user's language: {user_language}."
        )

        # Add transition-specific AI feedback if present
        if "ai_feedback" in transition:
            tokens_for_ai += f" {transition['ai_feedback'].get('tokens_for_ai', '')}"

        # Determine user_response for this prompt based on metadata filtering
        filtered_user_response = user_response
        if (
            "metadata_filter" in prompt
            and "user_response" not in prompt["metadata_filter"]
        ):
            filtered_user_response = ""  # Remove user response if not in filter

        ai_feedback = generate_ai_feedback(
            category,
            question,
            filtered_user_response,
            tokens_for_ai,
            username,
            json.dumps(prompt_metadata),  # Use filtered metadata for this prompt
            json_new_metadata,
        )

        # Only add feedback if it has content and isn't exactly the STFU token
        if ai_feedback and ai_feedback.strip() and ai_feedback.strip() != "STFU":
            feedback_messages.append(
                {"name": prompt_name, "content": ai_feedback.strip()}
            )

    return feedback_messages


def translate_text(text, target_language):
    # Guard clause for default language
    target_language = target_language.lower().split()

    if "english" in target_language:
        return text

    openai_client, model_name = get_openai_client_and_model()
    messages = [
        {
            "role": "system",
            "content": f"Translate the following text to {target_language}. DO NOT add anything else extra to your translation. It should be as close to word for word the dame but translated. Don't start with 'Set_language:' DO NOT try to solve math questions, translate the text around it and use mathmatical notation like normal.",
        },
        {
            "role": "user",
            "content": text,
        },
    ]

    try:
        completion = openai_client.chat.completions.create(
            model=model_name, messages=messages, max_tokens=2000, temperature=0.7, n=1
        )
        translation = completion.choices[0].message.content.strip()
        return translation
    except Exception as e:
        return f"Error: {e}"


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
