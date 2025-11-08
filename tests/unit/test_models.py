#!/usr/bin/env python3
"""
Comprehensive unit tests for models.py

Tests all database models:
- Room: user management, active/inactive tracking
- UserSession: session tracking
- Message: message storage, token counting, image detection
- ActivityState: state management, metadata operations
"""

import unittest
import json
from unittest.mock import patch, MagicMock
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class TestRoomModel(unittest.TestCase):
    """Test cases for Room model"""

    def setUp(self):
        """Set up test fixtures"""
        # Import here to avoid issues
        from models import Room
        self.Room = Room

    def create_room(self, name="test_room", title=None):
        """Helper to create a room instance"""
        room = self.Room()
        room.name = name
        room.title = title
        room.active_users = ""
        room.inactive_users = ""
        return room

    def test_room_creation(self):
        """Test creating a room"""
        room = self.create_room("test_room", "Test Room")
        self.assertEqual(room.name, "test_room")
        self.assertEqual(room.title, "Test Room")
        self.assertEqual(room.active_users, "")
        self.assertEqual(room.inactive_users, "")

    def test_add_first_user(self):
        """Test adding the first user to a room"""
        room = self.create_room()
        room.add_user("alice")

        self.assertEqual(room.active_users, "alice")
        self.assertEqual(room.inactive_users, "")
        self.assertEqual(room.get_active_users(), ["alice"])
        self.assertEqual(room.get_inactive_users(), [])

    def test_add_multiple_users(self):
        """Test adding multiple users to a room"""
        room = self.create_room()
        room.add_user("alice")
        room.add_user("bob")
        room.add_user("charlie")

        active = room.get_active_users()
        self.assertEqual(len(active), 3)
        self.assertIn("alice", active)
        self.assertIn("bob", active)
        self.assertIn("charlie", active)

    def test_add_duplicate_user(self):
        """Test adding the same user twice"""
        room = self.create_room()
        room.add_user("alice")
        room.add_user("alice")

        active = room.get_active_users()
        self.assertEqual(len(active), 1)
        self.assertEqual(active, ["alice"])

    def test_remove_user(self):
        """Test removing a user from active to inactive"""
        room = self.create_room()
        room.add_user("alice")
        room.add_user("bob")
        room.remove_user("alice")

        active = room.get_active_users()
        inactive = room.get_inactive_users()

        self.assertNotIn("alice", active)
        self.assertIn("bob", active)
        self.assertIn("alice", inactive)

    def test_remove_nonexistent_user(self):
        """Test removing a user that doesn't exist"""
        room = self.create_room()
        room.add_user("alice")
        room.remove_user("bob")  # User not in room

        active = room.get_active_users()
        self.assertEqual(active, ["alice"])

    def test_reactivate_inactive_user(self):
        """Test moving a user from inactive back to active"""
        room = self.create_room()
        room.add_user("alice")
        room.remove_user("alice")  # Move to inactive

        self.assertIn("alice", room.get_inactive_users())

        room.add_user("alice")  # Reactivate

        self.assertIn("alice", room.get_active_users())
        self.assertNotIn("alice", room.get_inactive_users())

    def test_get_active_users_empty(self):
        """Test getting active users when none exist"""
        room = self.create_room()
        self.assertEqual(room.get_active_users(), [])

    def test_get_inactive_users_empty(self):
        """Test getting inactive users when none exist"""
        room = self.create_room()
        self.assertEqual(room.get_inactive_users(), [])

    def test_users_sorted(self):
        """Test that users are stored in sorted order"""
        room = self.create_room()
        room.add_user("charlie")
        room.add_user("alice")
        room.add_user("bob")

        # Check they're sorted
        self.assertEqual(room.active_users, "alice,bob,charlie")


class TestUserSessionModel(unittest.TestCase):
    """Test cases for UserSession model"""

    def setUp(self):
        """Set up test fixtures"""
        from models import UserSession
        self.UserSession = UserSession

    def test_user_session_creation(self):
        """Test creating a user session"""
        session = self.UserSession()
        session.session_id = "test_session_123"
        session.username = "alice"
        session.room_name = "test_room"
        session.room_id = 1

        self.assertEqual(session.session_id, "test_session_123")
        self.assertEqual(session.username, "alice")
        self.assertEqual(session.room_name, "test_room")
        self.assertEqual(session.room_id, 1)


class TestMessageModel(unittest.TestCase):
    """Test cases for Message model"""

    def setUp(self):
        """Set up test fixtures"""
        from models import Message
        self.Message = Message

    def test_message_creation(self):
        """Test creating a message"""
        with patch('models.tiktoken.encoding_for_model') as mock_encoding:
            mock_enc = MagicMock()
            mock_enc.encode.return_value = [1, 2, 3, 4, 5]  # 5 tokens
            mock_encoding.return_value = mock_enc

            msg = self.Message("alice", "Hello world", 1)

            self.assertEqual(msg.username, "alice")
            self.assertEqual(msg.content, "Hello world")
            self.assertEqual(msg.room_id, 1)
            self.assertEqual(msg.token_count, 5)

    def test_count_tokens(self):
        """Test token counting for text messages"""
        with patch('models.tiktoken.encoding_for_model') as mock_encoding:
            mock_enc = MagicMock()
            mock_enc.encode.return_value = [1, 2, 3]  # 3 tokens
            mock_encoding.return_value = mock_enc

            msg = self.Message("alice", "Test message", 1)
            count = msg.count_tokens()

            self.assertEqual(count, 3)
            mock_encoding.assert_called_with("gpt-4")

    def test_count_tokens_cached(self):
        """Test that token count is cached after first calculation"""
        with patch('models.tiktoken.encoding_for_model') as mock_encoding:
            mock_enc = MagicMock()
            mock_enc.encode.return_value = [1, 2, 3]
            mock_encoding.return_value = mock_enc

            msg = self.Message("alice", "Test", 1)
            msg.count_tokens()  # First call
            msg.count_tokens()  # Second call

            # Should only encode once (cached)
            self.assertEqual(mock_enc.encode.call_count, 1)

    def test_is_base64_image_jpeg(self):
        """Test detecting JPEG base64 images"""
        content = '<img src="data:image/jpeg;base64,/9j/4AAQSkZJRg...">'
        msg = self.Message("alice", content, 1)

        self.assertTrue(msg.is_base64_image())

    def test_is_base64_image_png(self):
        """Test detecting PNG base64 images"""
        content = '<img alt="Plot Image" src="data:image/png;base64,iVBORw0KGgo...">'
        msg = self.Message("alice", content, 1)

        self.assertTrue(msg.is_base64_image())

    def test_is_not_base64_image(self):
        """Test that regular text is not detected as image"""
        msg = self.Message("alice", "Regular text message", 1)

        self.assertFalse(msg.is_base64_image())

    def test_image_token_count_is_zero(self):
        """Test that images have zero token count"""
        content = '<img src="data:image/jpeg;base64,/9j/4AAQSkZJRg...">'

        with patch('models.tiktoken.encoding_for_model') as mock_encoding:
            msg = self.Message("alice", content, 1)

            self.assertEqual(msg.token_count, 0)
            # Should not call encoding for images
            mock_encoding.assert_not_called()


class TestActivityStateModel(unittest.TestCase):
    """Test cases for ActivityState model"""

    def setUp(self):
        """Set up test fixtures"""
        from models import ActivityState
        self.ActivityState = ActivityState

    def create_activity_state(self):
        """Helper to create an activity state instance"""
        state = self.ActivityState()
        state.room_id = 1
        state.section_id = "section_1"
        state.step_id = "step_1"
        state.attempts = 0
        state.max_attempts = 3
        state.s3_file_path = "test_activity.yaml"
        state.json_metadata = "{}"
        return state

    def test_activity_state_creation(self):
        """Test creating an activity state"""
        state = self.create_activity_state()

        self.assertEqual(state.room_id, 1)
        self.assertEqual(state.section_id, "section_1")
        self.assertEqual(state.step_id, "step_1")
        self.assertEqual(state.attempts, 0)
        self.assertEqual(state.max_attempts, 3)
        self.assertEqual(state.s3_file_path, "test_activity.yaml")

    def test_dict_metadata_getter_empty(self):
        """Test getting empty metadata as dict"""
        state = self.create_activity_state()

        metadata = state.dict_metadata
        self.assertEqual(metadata, {})
        self.assertIsInstance(metadata, dict)

    def test_dict_metadata_getter_with_data(self):
        """Test getting metadata with data"""
        state = self.create_activity_state()
        state.json_metadata = json.dumps({"score": 100, "level": 5})

        metadata = state.dict_metadata
        self.assertEqual(metadata["score"], 100)
        self.assertEqual(metadata["level"], 5)

    def test_dict_metadata_setter(self):
        """Test setting metadata as dict"""
        state = self.create_activity_state()

        state.dict_metadata = {"user_name": "alice", "score": 50}

        # Check it's stored as JSON
        self.assertIsInstance(state.json_metadata, str)
        # Check it can be retrieved
        metadata = state.dict_metadata
        self.assertEqual(metadata["user_name"], "alice")
        self.assertEqual(metadata["score"], 50)

    def test_add_metadata(self):
        """Test adding individual metadata items"""
        state = self.create_activity_state()

        state.add_metadata("player_health", 100)
        state.add_metadata("enemy_health", 80)

        metadata = state.dict_metadata
        self.assertEqual(metadata["player_health"], 100)
        self.assertEqual(metadata["enemy_health"], 80)

    def test_add_metadata_overwrites_existing(self):
        """Test that adding metadata with same key overwrites"""
        state = self.create_activity_state()

        state.add_metadata("score", 50)
        state.add_metadata("score", 100)  # Overwrite

        metadata = state.dict_metadata
        self.assertEqual(metadata["score"], 100)

    def test_remove_metadata(self):
        """Test removing metadata items"""
        state = self.create_activity_state()
        state.dict_metadata = {"a": 1, "b": 2, "c": 3}

        state.remove_metadata("b")

        metadata = state.dict_metadata
        self.assertNotIn("b", metadata)
        self.assertEqual(metadata["a"], 1)
        self.assertEqual(metadata["c"], 3)

    def test_remove_nonexistent_metadata(self):
        """Test removing metadata that doesn't exist"""
        state = self.create_activity_state()
        state.dict_metadata = {"a": 1}

        # Should not raise error
        state.remove_metadata("nonexistent")

        metadata = state.dict_metadata
        self.assertEqual(metadata, {"a": 1})

    def test_clear_metadata(self):
        """Test clearing all metadata"""
        state = self.create_activity_state()
        state.dict_metadata = {"a": 1, "b": 2, "c": 3}

        state.clear_metadata()

        metadata = state.dict_metadata
        self.assertEqual(metadata, {})

    def test_metadata_supports_nested_structures(self):
        """Test that metadata can store nested structures"""
        state = self.create_activity_state()

        complex_data = {
            "user": {"name": "alice", "score": 100},
            "game": {"level": 5, "items": ["sword", "shield"]},
        }
        state.dict_metadata = complex_data

        metadata = state.dict_metadata
        self.assertEqual(metadata["user"]["name"], "alice")
        self.assertEqual(metadata["game"]["items"], ["sword", "shield"])

    def test_metadata_none_handling(self):
        """Test handling None in json_metadata"""
        state = self.create_activity_state()
        state.json_metadata = None

        # Should return empty dict, not error
        metadata = state.dict_metadata
        self.assertEqual(metadata, {})


if __name__ == "__main__":
    unittest.main()
