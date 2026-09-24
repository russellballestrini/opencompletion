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
from models import Room

bp = Blueprint("pages", __name__)


@bp.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(current_app.root_path, "static"), "favicon.ico"
    )


@bp.route("/")
def index():
    total_public_rooms = Room.query.filter_by(
        is_private=False, is_archived=False
    ).count()
    total_private_rooms = 0
    user = auth.get_current_user()
    if user:
        total_private_rooms = Room.query.filter_by(
            is_private=True, is_archived=False, owner_id=user.id
        ).count()

    stats = {
        "total_public_rooms": total_public_rooms,
        "total_private_rooms": total_private_rooms,
    }

    return render_template("index.html", stats=stats, user=user)


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
