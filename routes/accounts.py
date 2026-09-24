"""Sign in by email code, sign out & display-name changes (JSON API)."""

from flask import Blueprint, jsonify, request, session

import auth
from models import User, db

bp = Blueprint("accounts", __name__)


# Authentication endpoints
@bp.route("/auth/send-otp", methods=["POST"])
def send_otp():
    """Send OTP to user's email"""
    data = request.get_json(silent=True) or {}
    email = data.get("email", "").strip().lower()

    if not email:
        return jsonify({"error": "Email is required"}), 400

    # Basic email validation
    if "@" not in email or "." not in email.split("@")[1]:
        return jsonify({"error": "Invalid email address"}), 400

    # Create OTP token
    otp_token = auth.create_otp_token(email)

    # Send OTP via email; `delivery` tells the page where it really went.
    delivery = auth.send_otp_email(email, otp_token.otp_code)
    if delivery:
        return jsonify({"success": True, "delivery": delivery, "email": email})
    else:
        return jsonify({"error": "Failed to send OTP email"}), 500


@bp.route("/auth/verify-otp", methods=["POST"])
def verify_otp():
    """Verify OTP code and check if user exists"""
    data = request.get_json(silent=True) or {}
    email = data.get("email", "").strip().lower()
    otp_code = data.get("otp_code", "").strip()

    if not email or not otp_code:
        return jsonify({"error": "Email and OTP code are required"}), 400

    # Verify OTP
    otp_token = auth.verify_otp(email, otp_code)
    if not otp_token:
        return (
            jsonify(
                {
                    "error": "Invalid or expired code. After 5 wrong tries, request a new one."
                }
            ),
            400,
        )

    # Check if user exists
    user = auth.get_or_create_user(email)

    if user:
        # Existing user - log them in
        auth.login_user(user)
        return jsonify(
            {
                "success": True,
                "needs_display_name": False,
                "user": {"email": user.email, "display_name": user.display_name},
            }
        )
    else:
        # New user - needs to claim display name
        # Store email in session temporarily
        session["pending_email"] = email
        return jsonify({"success": True, "needs_display_name": True, "email": email})


@bp.route("/auth/claim-name", methods=["POST"])
def claim_name():
    """Claim display name for new user (after OTP verification)"""
    data = request.get_json(silent=True) or {}
    display_name = data.get("display_name", "").strip()
    email = session.get("pending_email")

    if not email:
        return jsonify({"error": "No pending email verification"}), 400

    if not display_name:
        return jsonify({"error": "Display name is required"}), 400

    # Validate display name (alphanumeric, underscores, hyphens only, 3-50 chars)
    import re

    if not re.match(r"^[a-zA-Z0-9_-]{3,50}$", display_name):
        return (
            jsonify(
                {
                    "error": "Display name must be 3-50 characters (letters, numbers, underscores, hyphens only)"
                }
            ),
            400,
        )

    # Create user
    user, error = auth.create_user(email, display_name)
    if error:
        return jsonify({"error": error}), 400

    # Log in user
    auth.login_user(user)

    # Clear pending email
    session.pop("pending_email", None)

    return jsonify(
        {
            "success": True,
            "user": {"email": user.email, "display_name": user.display_name},
        }
    )


@bp.route("/auth/status", methods=["GET"])
def auth_status():
    """Get current authentication status"""
    user = auth.get_current_user()
    if user:
        return jsonify(
            {
                "authenticated": True,
                "user": {"email": user.email, "display_name": user.display_name},
            }
        )
    else:
        return jsonify({"authenticated": False})


@bp.route("/auth/logout", methods=["POST"])
def logout():
    """Log out current user"""
    auth.logout_user()
    return jsonify({"success": True})


@bp.route("/api/check-username", methods=["GET"])
def check_username():
    """Check if username is available"""
    username = request.args.get("username", "").strip()

    if not username:
        return jsonify({"available": False, "error": "Username is required"}), 400

    # Validate format
    import re

    if not re.match(r"^[a-zA-Z0-9_-]{3,50}$", username):
        return jsonify({"available": False, "error": "Invalid format"}), 400

    # Check if username exists
    existing_user = User.query.filter_by(display_name=username).first()

    return jsonify({"available": existing_user is None})


@bp.route("/api/update-username", methods=["POST"])
@auth.require_auth
def update_username():
    """Update user's display name"""
    user = auth.get_current_user()
    data = request.get_json(silent=True) or {}
    new_username = data.get("new_username", "").strip()

    if not new_username:
        return jsonify({"error": "Username is required"}), 400

    # Validate format
    import re

    if not re.match(r"^[a-zA-Z0-9_-]{3,50}$", new_username):
        return (
            jsonify(
                {
                    "error": "Username must be 3-50 characters (letters, numbers, underscores, hyphens only)"
                }
            ),
            400,
        )

    # Check if username is already taken
    existing_user = User.query.filter_by(display_name=new_username).first()
    if existing_user and existing_user.id != user.id:
        return jsonify({"error": "Username is already taken"}), 400

    # Update username
    user.display_name = new_username
    db.session.commit()

    return jsonify(
        {
            "success": True,
            "user": {"email": user.email, "display_name": user.display_name},
        }
    )
