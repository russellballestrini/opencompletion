"""File uploads: post a file into a room, fetch it back.

POST /api/rooms/<room_name>/attachments  (multipart, field "file")
GET  /attachments/<public_id>/<filename>

Both follow the room's rule (room_access_denied): a private room's files
are its owner's alone & answer 404 to anyone else. A file's address
carries a random public_id (never its row id, which counts up) & its name;
either wrong is a 404. Bytes live in
blob_store (SQLite today). Limits: OPENCOMPLETION_UPLOAD_MAX_MB per file
(default 7) & OPENCOMPLETION_UPLOAD_LIMIT uploads per address
("count/seconds", default 30/600).
"""

from urllib.parse import quote

from flask import Blueprint, Response, jsonify, redirect, render_template, request
from werkzeug.exceptions import RequestEntityTooLarge

import auth
from blob_store import (
    INLINE_TYPES,
    attachment_json,
    find_attachment,
    max_bytes,
    read_attachment,
    save_attachment,
)
from models import Attachment, Room, db
from routes.rooms import room_access_denied
from throttle import Throttle, client_ip

bp = Blueprint("files", __name__)

UPLOAD_THROTTLE = Throttle("OPENCOMPLETION_UPLOAD_LIMIT", default=(30, 600))
# Multipart framing around one file: headers, boundary, the filename.
MULTIPART_OVERHEAD = 64 * 1024


def _too_big():
    mb = max_bytes() / (1024 * 1024)
    return jsonify({"error": f"Files are limited to {mb:g} MB each."}), 413


@bp.route("/api/rooms/<room_name>/attachments", methods=["POST"])
def upload_attachment(room_name):
    """Store one file in a room you may post in; answer where it lives."""
    user = auth.get_current_user()
    room = Room.query.filter_by(name=room_name).first()
    if room is None or room_access_denied(room, user):
        return jsonify({"error": "No such chat"}), 404
    # Refuse an oversized body before reading a byte of it, & cap a body
    # that names no length (chunked) while it is parsed.
    request.max_content_length = max_bytes() + MULTIPART_OVERHEAD
    if (request.content_length or 0) > request.max_content_length:
        return _too_big()
    try:
        upload = request.files.get("file")
    except RequestEntityTooLarge:
        return _too_big()
    if upload is None:
        return jsonify({"error": "Choose a file to upload."}), 400
    data = upload.stream.read(max_bytes() + 1)
    if len(data) > max_bytes():
        return _too_big()
    if not data:
        return jsonify({"error": "That file is empty."}), 400

    retry_after = UPLOAD_THROTTLE.hit(client_ip())
    if retry_after:
        minutes = -(-retry_after // 60)
        response = jsonify(
            {
                "error": f"Too many uploads from your address: "
                f"{UPLOAD_THROTTLE.count} per {UPLOAD_THROTTLE.seconds // 60} "
                f"minutes. Try again in {minutes} minute"
                f"{'' if minutes == 1 else 's'}."
            }
        )
        response.headers["Retry-After"] = str(retry_after)
        return response, 429

    username = user.display_name if user else "guest"
    attachment = save_attachment(
        room.id, data, upload.filename, upload.mimetype, username
    )
    return jsonify(attachment_json(attachment)), 201


@bp.route("/attachments/<public_id>/<path:filename>")
def serve_attachment(public_id, filename):
    """A file, under its room's access rule. Safe types (images, audio,
    video, PDF, plain text) show inline; anything else, SVG & HTML
    included, downloads, so an upload never runs script on our origin."""
    attachment = find_attachment(public_id)
    if attachment is None:
        return jsonify({"error": "File not found"}), 404
    room = db.session.get(Room, attachment.room_id)
    if room is None:
        return jsonify({"error": "File not found"}), 404
    if room_access_denied(room, auth.get_current_user()):
        # A real file's random id, held by someone who may not open its
        # solo chat: say that, as the chat's own page does, so its owner
        # signed out (or whoever they sent the link to) is not told a file
        # that exists is missing (fox, 2026-11-10). The same answer for any
        # name, so nothing about the chat's files can be tried.
        return (
            render_template(
                "not_found.html",
                heading="This file is in someone's solo chat",
                detail="Only its owner can open it. If it is yours, sign in.",
            ),
            403,
        )
    if attachment.filename != filename:
        # A model writing links to several files it shared sometimes gives
        # one file's id to another's name (fox, 2026-11-10: phone_ring.mp3
        # carried phone_ring.wav's id & read as not found). The name is
        # what it meant: someone who may open this room is sent to this
        # room's file of that name, its newest. Any other name is a 404.
        named = (
            Attachment.query.filter_by(room_id=attachment.room_id, filename=filename)
            .order_by(Attachment.id.desc())
            .first()
        )
        if named is None:
            return jsonify({"error": "File not found"}), 404
        return redirect(named.url, code=302)

    etag = f'"{attachment.sha256}"'
    cache = "private, max-age=31536000, immutable"
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag, "Cache-Control": cache})
    data = read_attachment(attachment)
    if data is None:
        return jsonify({"error": "File not found"}), 404

    inline = attachment.mime_type in INLINE_TYPES
    disposition = "inline" if inline else "attachment"
    response = Response(
        data, mimetype=attachment.mime_type if inline else "application/octet-stream"
    )
    response.headers["Content-Disposition"] = (
        f"{disposition}; filename*=UTF-8''{quote(attachment.filename)}"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    if attachment.mime_type != "application/pdf":
        # Chrome's PDF viewer won't open under a sandbox; a PDF runs no
        # script on our origin, so it goes without.
        response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = cache
    return response
