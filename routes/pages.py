"""Pages people open: home, sign in, profile, our style guide, favicon."""

import os
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    current_app,
    render_template,
    request,
    send_from_directory,
)

import auth
from blob_store import max_bytes as max_upload_bytes
from models import Room

bp = Blueprint("pages", __name__)


@bp.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(current_app.root_path, "static"), "favicon.ico"
    )


@bp.app_context_processor
def brand():
    """Our name atop every page: OPENCOMPLETION_BRAND (say "uncloseai")
    lets one deployment carry its own, OpenCompletion otherwise."""
    limit = max_upload_bytes()
    return {
        "brand_name": os.environ.get("OPENCOMPLETION_BRAND") or "OpenCompletion",
        # Our composer refuses a file over this before uploading it.
        "max_upload_bytes": limit,
        "max_upload_mb": f"{limit / (1024 * 1024):g}",
    }


@bp.app_context_processor
def sidebar():
    """Every page's sidebar (_app_sidebar.html) lists our chats; a route
    that passes its own `user` still decides who that is."""
    return sidebar_chats(auth.get_current_user())


# How many chats each sidebar list shows; /browse has every room.
SIDEBAR_CHATS = 30


def sidebar_chats(user):
    """What _app_sidebar.html lists, on our home & every chat: rooms you
    own (private ones too), newest first, and public rooms. Never anyone
    else's private room."""
    your_chats = []
    if user:
        your_chats = (
            Room.query.filter_by(is_archived=False, owner_id=user.id)
            .order_by(Room.updated_at.desc(), Room.id.desc())
            .limit(SIDEBAR_CHATS)
            .all()
        )
    public_chats = (
        Room.query.filter_by(is_private=False, is_archived=False)
        .order_by(Room.updated_at.desc(), Room.id.desc())
        .limit(SIDEBAR_CHATS)
        .all()
    )
    return {"user": user, "your_chats": your_chats, "public_chats": public_chats}


@bp.route("/")
def index():
    """Our home: an empty chat. The first message makes a room (private,
    or a multiplayer chatroom) & opens it."""
    return render_template("home.html", user=auth.get_current_user())


def safe_next_url(value):
    """A same-site path to return to after sign in, else home.

    Only a single leading slash is accepted, so `//evil.example` and
    `https://...` can never turn our sign-in page into an open redirect.
    """
    # Browsers drop tabs & newlines inside URLs, so "/\t/evil.example" would
    # become "//evil.example": refuse any control character or backslash.
    if not value or any(ord(c) < 0x20 or ord(c) == 0x7F or c == "\\" for c in value):
        return "/"
    parts = urlsplit(value)
    if value.startswith("/") and not value.startswith("//") and not parts.netloc:
        if not parts.scheme:
            return value
    return "/"


@bp.route("/auth")
def auth_page():
    """Authentication page"""
    return render_template(
        "auth.html",
        user=auth.get_current_user(),
        next_url=safe_next_url(request.args.get("next", "")),
    )


@bp.route("/styleguide")
def styleguide():
    """Our living style guide: every shared component, rendered by our CSS.

    docs/STYLEGUIDE.md explains the rules; this page shows them & is what
    tests/functional/test_ui_contract.py loads at phone & desktop widths.
    """
    return render_template("styleguide.html", user=auth.get_current_user())


@bp.route("/profile")
def profile_page():
    """Profile settings page — works for both authenticated users and guests."""
    user = auth.get_current_user()
    return render_template("profile.html", user=user)
