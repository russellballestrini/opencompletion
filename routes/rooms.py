"""Rooms: browse, create, fork, archive & delete, search, history
downloads, and our one rule for who may open a private room."""

import json
import os

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

import auth
from models import ActivityState, Message, Room, UserSession, db
from routes import DEPS

bp = Blueprint("rooms", __name__)


@bp.route("/browse")
def browse_rooms():
    """Browse all rooms (public and user's private rooms)"""
    user = auth.get_current_user()

    # Get public rooms ordered by last updated
    public_rooms = (
        Room.query.filter_by(is_private=False, is_archived=False)
        .order_by(Room.updated_at.desc())
        .all()
    )

    # Get user's private rooms if authenticated
    private_rooms = []
    if user:
        private_rooms = (
            Room.query.filter_by(is_private=True, is_archived=False, owner_id=user.id)
            .order_by(Room.updated_at.desc())
            .all()
        )

    return render_template(
        "browse.html", public_rooms=public_rooms, private_rooms=private_rooms, user=user
    )


_model_map_refreshed_at = 0.0


@bp.route("/api/activities", methods=["GET"])
def get_activities():
    """Return the list of available activities."""
    activities = []

    if current_app.config.get("LOCAL_ACTIVITIES"):
        # List local activity files from research directory
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


@bp.route("/api/rooms", methods=["GET"])
def get_rooms_api():
    """Get list of rooms (public or user's private rooms)"""
    user = auth.get_current_user()

    # Get public rooms
    public_rooms = (
        Room.query.filter_by(is_private=False, is_archived=False)
        .order_by(Room.id.desc())
        .all()
    )

    # Get private rooms if authenticated
    private_rooms = []
    if user:
        private_rooms = (
            Room.query.filter_by(is_private=True, is_archived=False, owner_id=user.id)
            .order_by(Room.id.desc())
            .all()
        )

    return jsonify(
        {
            "public_rooms": [
                {
                    "id": r.id,
                    "name": r.name,
                    "title": r.title,
                    "active_users_count": len(r.get_active_users()),
                }
                for r in public_rooms
            ],
            "private_rooms": [
                {
                    "id": r.id,
                    "name": r.name,
                    "title": r.title,
                    "active_users_count": len(r.get_active_users()),
                }
                for r in private_rooms
            ],
        }
    )


def emit_room_list_update(room, room_data):
    """Tell sidebars about a new or retitled room.

    A public room goes to every client. A private room's name & title are
    its owner's business: they go only to our clients inside that room,
    never to the whole site.
    """
    if room.is_private:
        current_app.extensions["socketio"].emit(
            "update_room_list", room_data, room=room.name
        )
    else:
        current_app.extensions["socketio"].emit(
            "update_room_list", room_data, room=None
        )


@bp.route("/api/rooms/create", methods=["POST"])
def create_room_api():
    """Create a new room"""
    user = auth.get_current_user()
    data = request.get_json() or {}
    room_name = data.get("name", "").strip()
    is_private = data.get("is_private", False)

    if not room_name:
        return jsonify({"error": "Room name is required"}), 400

    # Private rooms require authentication
    if is_private and not user:
        return (
            jsonify({"error": "Authentication required to create private rooms"}),
            401,
        )

    # Check if room already exists
    existing_room = Room.query.filter_by(name=room_name).first()
    if existing_room:
        return jsonify({"error": "Room name already exists"}), 400

    # Create room
    new_room = Room()
    new_room.name = room_name
    new_room.is_private = is_private
    new_room.owner_id = user.id if user else None

    db.session.add(new_room)
    db.session.commit()

    # Broadcast new room to all users so it appears in sidebar
    new_room_data = {
        "id": new_room.id,
        "name": new_room.name,
        "title": new_room.title,
        "is_private": new_room.is_private,
        "is_new": True,  # Flag to indicate this is a new room, not an update
    }
    emit_room_list_update(new_room, new_room_data)

    return jsonify(
        {
            "success": True,
            "room": {
                "id": new_room.id,
                "name": new_room.name,
                "is_private": new_room.is_private,
            },
        }
    )


@bp.route("/api/rooms/<int:room_id>/fork", methods=["POST"])
def fork_room(room_id):
    """Fork a room (authenticated users can fork to private or public)"""
    user = auth.get_current_user()
    data = request.get_json() or {}
    make_private = data.get("private", False)

    # Get source room
    source_room = Room.query.get(room_id)
    if not source_room:
        return jsonify({"error": "Room not found"}), 404

    # Private rooms can only be forked by their owner
    if source_room.is_private:
        if not user or source_room.owner_id != user.id:
            return jsonify({"error": "Cannot fork private rooms you do not own"}), 403

    # Private rooms require authentication
    if make_private and not user:
        return (
            jsonify({"error": "Authentication required to create private rooms"}),
            401,
        )

    # Generate new room name
    base_name = f"{source_room.name}_fork"
    new_name = base_name
    counter = 1
    while Room.query.filter_by(name=new_name).first():
        new_name = f"{base_name}_{counter}"
        counter += 1

    # Create forked room
    new_room = Room()
    new_room.name = new_name
    new_room.title = f"Fork of {source_room.title or source_room.name}"
    new_room.is_private = make_private
    new_room.owner_id = user.id if user else None
    new_room.forked_from_id = source_room.id

    db.session.add(new_room)
    db.session.commit()

    # Copy messages from source room
    source_messages = Message.query.filter_by(room_id=source_room.id).all()
    for msg in source_messages:
        new_msg = Message(
            username=msg.username, content=msg.content, room_id=new_room.id
        )
        db.session.add(new_msg)

    db.session.commit()

    return jsonify(
        {
            "success": True,
            "room": {
                "id": new_room.id,
                "name": new_room.name,
                "title": new_room.title,
                "is_private": new_room.is_private,
            },
        }
    )


@bp.route("/api/rooms/<int:room_id>/archive", methods=["POST"])
@auth.require_auth
def archive_room(room_id):
    """Archive a room (owner only)"""
    user = auth.get_current_user()
    room = Room.query.get(room_id)

    if not room:
        return jsonify({"error": "Room not found"}), 404

    if room.owner_id != user.id:
        return jsonify({"error": "Only room owner can archive rooms"}), 403

    room.is_archived = True
    db.session.commit()

    return jsonify({"success": True})


@bp.route("/api/rooms/<int:room_id>/delete", methods=["DELETE"])
@auth.require_auth
def delete_room(room_id):
    """Delete a room (owner only)"""
    user = auth.get_current_user()
    room = Room.query.get(room_id)

    if not room:
        return jsonify({"error": "Room not found"}), 404

    if room.owner_id != user.id:
        return jsonify({"error": "Only room owner can delete rooms"}), 403

    # Delete all messages in the room
    Message.query.filter_by(room_id=room.id).delete()

    # Delete activity state if any
    ActivityState.query.filter_by(room_id=room.id).delete()

    # Delete user sessions
    UserSession.query.filter_by(room_id=room.id).delete()

    # Delete the room
    db.session.delete(room)
    db.session.commit()

    return jsonify({"success": True})


@bp.route("/download_chat_history", methods=["GET"])
def download_chat_history():
    room_name = request.args.get("room_name")
    room = Room.query.filter_by(name=room_name).first()

    # A private room answers 404 like a missing one: its name stays secret.
    if not room or room_access_denied(room, auth.get_current_user()):
        return jsonify({"error": "Room not found"}), 404

    messages = Message.query.filter_by(room_id=room.id).all()

    if not messages:
        return jsonify({"error": "No messages found"}), 404

    chat_history = [
        {
            "role": "system" if message.username in DEPS["system_users"] else "user",
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


@bp.route("/download_chat_history_md", methods=["GET"])
def download_chat_history_md():
    room_name = request.args.get("room_name")
    room = Room.query.filter_by(name=room_name).first()

    # A private room answers 404 like a missing one: its name stays secret.
    if not room or room_access_denied(room, auth.get_current_user()):
        return jsonify({"error": "Room not found"}), 404

    messages = Message.query.filter_by(room_id=room.id).all()

    if not messages:
        return jsonify({"error": "No messages found"}), 404

    # Access system users from the existing context
    chat_history_md = []
    toc = []
    for index, message in enumerate(messages):
        if not message.is_base64_image():  # Correctly call the method
            role = "System" if message.username in DEPS["system_users"] else "User"
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


@bp.route("/search")
def search_page():
    user = auth.get_current_user()
    keywords = request.args.get("keywords", "").strip()
    if not keywords:
        return render_template(
            "search.html", keywords="", results=[], user=user, error=None
        )

    search_results = search_messages(keywords, user)

    # Exactly one hit: go straight to that room, keeping model & voice.
    if len(search_results) == 1:
        redirect_params = {
            param: request.args[param]
            for param in ("model", "voice")
            if request.args.get(param)
        }
        return redirect(
            url_for("chat", room_name=search_results[0]["room_name"], **redirect_params)
        )

    return render_template(
        "search.html", keywords=keywords, results=search_results, user=user, error=None
    )


def search_messages(keywords, user=None):
    """Rank rooms by keyword hits in their messages.

    Only rooms the caller may open are searched: public rooms that are not
    archived, plus the caller's own private rooms. A search must never
    reveal that someone else's private room exists.
    """
    sanitized_keywords = []
    for keyword in keywords.lower().split():
        # Keep word characters only & cap the length; the query is
        # parameterized either way, this just bounds the LIKE patterns.
        sanitized_keyword = "".join(c for c in keyword if c.isalnum() or c in "-_")[:50]
        if sanitized_keyword:
            sanitized_keywords.append(sanitized_keyword)

    if not sanitized_keywords:
        return []

    visible = db.and_(Room.is_private.is_(False), Room.is_archived.is_(False))
    if user:
        visible = db.or_(
            visible,
            db.and_(
                Room.is_private.is_(True),
                Room.is_archived.is_(False),
                Room.owner_id == user.id,
            ),
        )

    rows = (
        db.session.query(Message.content, Room)
        .join(Room, Message.room_id == Room.id)
        .filter(visible)
        .filter(
            db.or_(
                *[
                    Message.content.ilike(f"%{keyword}%")
                    for keyword in sanitized_keywords
                ]
            )
        )
        .all()
    )

    search_results = {}
    for content, room in rows:
        score = sum(content.lower().count(keyword) for keyword in sanitized_keywords)
        result = search_results.setdefault(
            room.id,
            {
                "room_id": room.id,
                "name": room.name,
                "title": room.title,
                "room_name": room.name,
                "room_title": room.title,
                "is_private": room.is_private,
                "score": 0,
            },
        )
        result["score"] += score

    return sorted(search_results.values(), key=lambda r: r["score"], reverse=True)


def room_access_denied(room, user):
    """True when `user` may not see or post in `room`.

    Private rooms belong to their owner alone; every socket event & download
    applies our same rule the /chat page does, so knowing a private room's
    name never opens it.
    """
    return bool(room and room.is_private and (not user or room.owner_id != user.id))
