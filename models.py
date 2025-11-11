from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta

import tiktoken

import json

db = SQLAlchemy()


class User(db.Model):
    """User model for authentication and ownership"""
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(50), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    last_login = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    owned_rooms = db.relationship('Room', backref='owner', lazy='dynamic', foreign_keys='Room.owner_id')

    def __repr__(self):
        return f'<User {self.display_name} ({self.email})>'


class OTPToken(db.Model):
    """One-Time Password tokens for email authentication"""
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, index=True)
    otp_code = db.Column(db.String(6), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False, nullable=False)

    def __init__(self, email, otp_code, expiration_minutes=10):
        self.email = email
        self.otp_code = otp_code
        self.created_at = datetime.utcnow()
        self.expires_at = self.created_at + timedelta(minutes=expiration_minutes)
        self.used = False

    def is_valid(self):
        """Check if the OTP is still valid (not used and not expired)"""
        return not self.used and datetime.utcnow() < self.expires_at

    def __repr__(self):
        return f'<OTPToken {self.email} expires_at={self.expires_at}>'


class Room(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, unique=True)
    title = db.Column(db.String(128), nullable=True)
    active_users = db.Column(db.Text, default="")  # Store as a comma-separated string
    inactive_users = db.Column(db.Text, default="")  # Store as a comma-separated string
    is_private = db.Column(db.Boolean, default=False, nullable=False, index=True)
    is_archived = db.Column(db.Boolean, default=False, nullable=False, index=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    forked_from_id = db.Column(db.Integer, db.ForeignKey('room.id'), nullable=True)

    def add_user(self, username):
        active_users = set(self.active_users.split(",")) if self.active_users else set()
        inactive_users = (
            set(self.inactive_users.split(",")) if self.inactive_users else set()
        )

        # Move from inactive to active if necessary
        if username in inactive_users:
            inactive_users.discard(username)

        active_users.add(username)
        self.active_users = ",".join(sorted(active_users))
        self.inactive_users = ",".join(sorted(inactive_users))

    def remove_user(self, username):
        active_users = set(self.active_users.split(",")) if self.active_users else set()
        inactive_users = (
            set(self.inactive_users.split(",")) if self.inactive_users else set()
        )

        if username in active_users:
            active_users.discard(username)
            inactive_users.add(username)  # Move to inactive users

        self.active_users = ",".join(sorted(active_users))
        self.inactive_users = ",".join(sorted(inactive_users))

    def get_active_users(self):
        return self.active_users.split(",") if self.active_users else []

    def get_inactive_users(self):
        return self.inactive_users.split(",") if self.inactive_users else []


class UserSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(128), unique=True, nullable=False)
    username = db.Column(db.String(128))
    room_name = db.Column(db.String(128))
    room_id = db.Column(db.Integer)


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(128), nullable=False)
    content = db.Column(db.String(1024), nullable=False)
    token_count = db.Column(db.Integer)
    room_id = db.Column(db.Integer, db.ForeignKey("room.id"), nullable=False)

    def __init__(self, username, content, room_id):
        self.username = username
        self.content = content
        self.room_id = room_id
        self.count_tokens()

    def count_tokens(self):
        if self.token_count is None:
            if self.is_base64_image():
                self.token_count = 0
            else:
                encoding = tiktoken.encoding_for_model("gpt-4")
                self.token_count = len(encoding.encode(self.content))
        return self.token_count

    def is_base64_image(self):
        return (
            '<img src="data:image/jpeg;base64,' in self.content
            or '<img alt="Plot Image" src="data:image/png;base64,' in self.content
        )


class ActivityState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey("room.id"), nullable=False)
    section_id = db.Column(db.String(128), nullable=False)
    step_id = db.Column(db.String(128), nullable=False)
    attempts = db.Column(db.Integer, default=0)
    max_attempts = db.Column(db.Integer, default=3)
    s3_file_path = db.Column(db.String(256), nullable=False)
    json_metadata = db.Column(db.UnicodeText, default="{}")

    @property
    def dict_metadata(self):
        return json.loads(self.json_metadata) if self.json_metadata else {}

    @dict_metadata.setter
    def dict_metadata(self, value):
        self.json_metadata = json.dumps(value)

    def add_metadata(self, key, value):
        metadata = self.dict_metadata
        metadata[key] = value
        self.dict_metadata = metadata

    def remove_metadata(self, key):
        metadata = self.dict_metadata
        if key in metadata:
            del metadata[key]
        self.dict_metadata = metadata

    def clear_metadata(self):
        self.dict_metadata = {}
