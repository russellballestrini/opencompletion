from flask_sqlalchemy import SQLAlchemy

import tiktoken

import json

db = SQLAlchemy()


class Room(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, unique=True)
    title = db.Column(db.String(128), nullable=True)
    active_users = db.Column(db.Text, default="")  # Store as a comma-separated string
    inactive_users = db.Column(db.Text, default="")  # Store as a comma-separated string

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
