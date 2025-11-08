#!/usr/bin/env python3
"""
Integration tests for app.py with real Flask routes and Socket.IO

Tests Flask routes, request handling, and basic app functionality
"""

import unittest
import json
from unittest.mock import patch, MagicMock
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class TestAppUtilityFunctionsIntegration(unittest.TestCase):
    """Integration tests for app.py utility functions with dependencies"""

    def test_group_consecutive_roles_integration(self):
        """Test grouping messages by role"""
        from app import group_consecutive_roles

        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "user", "content": "How are you?"},
            {"role": "assistant", "content": "I'm fine"},
            {"role": "assistant", "content": "Thanks for asking"},
            {"role": "user", "content": "Great"},
        ]

        grouped = group_consecutive_roles(messages)

        self.assertEqual(len(grouped), 3)
        self.assertEqual(grouped[0]["role"], "user")
        self.assertIn("Hello", grouped[0]["content"])
        self.assertIn("How are you?", grouped[0]["content"])


class TestDatabaseModelsIntegration(unittest.TestCase):
    """Integration tests for database models with real Flask app"""

    def setUp(self):
        """Set up test database"""
        import app as app_module
        from models import db
        from flask import Flask

        test_app = Flask(__name__)
        test_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        test_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        test_app.config["TESTING"] = True

        db.init_app(test_app)

        self.app_context = test_app.app_context()
        self.app_context.push()

        db.create_all()
        self.db = db

    def tearDown(self):
        """Clean up"""
        self.db.session.remove()
        try:
            self.db.drop_all()
        except:
            pass
        self.app_context.pop()

    def test_room_user_workflow(self):
        """Test complete room and user workflow"""
        from models import Room

        # Create room
        room = Room(name="game_room", title="Game Room")
        self.db.session.add(room)
        self.db.session.commit()

        # Add users
        room.add_user("alice")
        room.add_user("bob")
        room.add_user("charlie")
        self.db.session.commit()

        # Verify users added
        self.assertEqual(len(room.get_active_users()), 3)

        # Remove a user
        room.remove_user("bob")
        self.db.session.commit()

        # Verify bob moved to inactive
        active = room.get_active_users()
        inactive = room.get_inactive_users()

        self.assertNotIn("bob", active)
        self.assertIn("bob", inactive)
        self.assertEqual(len(active), 2)

    def test_message_persistence(self):
        """Test message storage and retrieval"""
        from models import Room, Message

        # Create room
        room = Room(name="chat_room")
        self.db.session.add(room)
        self.db.session.commit()

        # Create messages
        msg1 = Message("alice", "Hello world", room.id)
        msg2 = Message("bob", "Hi there", room.id)
        self.db.session.add(msg1)
        self.db.session.add(msg2)
        self.db.session.commit()

        # Retrieve messages
        messages = Message.query.filter_by(room_id=room.id).all()

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].username, "alice")
        self.assertEqual(messages[1].username, "bob")

    def test_activity_state_workflow(self):
        """Test activity state management workflow"""
        from models import Room, ActivityState

        # Create room
        room = Room(name="activity_room")
        self.db.session.add(room)
        self.db.session.commit()

        # Create activity state
        state = ActivityState(
            room_id=room.id,
            section_id="intro",
            step_id="step_1",
            s3_file_path="activity.yaml",
            attempts=0,
            max_attempts=3
        )
        self.db.session.add(state)
        self.db.session.commit()

        # Add metadata
        state.add_metadata("score", 0)
        state.add_metadata("level", 1)
        self.db.session.commit()

        # Progress through activity
        state.step_id = "step_2"
        state.attempts = 1
        state.add_metadata("score", 10)
        self.db.session.commit()

        # Retrieve and verify
        retrieved = ActivityState.query.filter_by(room_id=room.id).first()
        self.assertEqual(retrieved.step_id, "step_2")
        self.assertEqual(retrieved.attempts, 1)
        self.assertEqual(retrieved.dict_metadata["score"], 10)


if __name__ == "__main__":
    unittest.main()
