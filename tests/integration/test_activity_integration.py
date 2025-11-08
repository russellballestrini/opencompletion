#!/usr/bin/env python3
"""
Integration tests for activity.py functions that require Flask app context and database

Tests complete workflows with real Flask environment:
- start_activity: Starting an activity session
- handle_activity_response: Processing user responses
- cancel_activity: Canceling an activity
- display_activity_metadata: Showing metadata
- loop_through_steps_until_question: Step navigation
"""

import unittest
import json
import tempfile
from unittest.mock import patch, MagicMock
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class TestActivityIntegration(unittest.TestCase):
    """Integration tests for activity.py with Flask app context"""

    def setUp(self):
        """Set up test Flask application with in-memory database"""
        import app as app_module
        from models import db
        from flask_sqlalchemy import SQLAlchemy

        self.app_module = app_module
        self.db = db

        # Create a fresh Flask app for testing
        from flask import Flask
        test_app = Flask(__name__)
        test_app.config["TESTING"] = True
        test_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        test_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        test_app.config["LOCAL_ACTIVITIES"] = True
        test_app.config["WTF_CSRF_ENABLED"] = False
        test_app.config["SECRET_KEY"] = "test-secret"

        # Initialize db with test app
        db.init_app(test_app)

        # Replace the global app temporarily
        self.original_app = app_module.app
        app_module.app = test_app

        self.client = test_app.test_client()
        self.app_context = test_app.app_context()
        self.app_context.push()

        # Create tables
        db.create_all()

    def tearDown(self):
        """Clean up test environment"""
        self.db.session.remove()
        try:
            self.db.drop_all()
        except:
            pass
        self.app_context.pop()

        # Restore original app
        self.app_module.app = self.original_app

    def create_test_activity_file(self):
        """Create a test activity YAML file"""
        from models import Room
        import activity

        # Create a test room
        room = Room(name="test_room")
        self.db.session.add(room)
        self.db.session.commit()

        # Create minimal activity content
        activity_content = """
default_max_attempts_per_step: 3
tokens_for_ai_rubric: "Test rubric"

sections:
  - section_id: "section_1"
    title: "Test Section"
    steps:
      - step_id: "step_1"
        type: "question"
        question: "What is 2+2?"
        buckets:
          - bucket_name: "correct"
            bucket_criteria: "Answer is 4"
        transitions:
          correct:
            ai_feedback:
              tokens_for_ai: "Provide encouragement"
            next_section_id: "section_1"
            next_step_id: "step_2"
      - step_id: "step_2"
        type: "info"
        display_text: "Great job!"
"""
        # Write to research directory
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.yaml', dir='research', delete=False
        ) as f:
            f.write(activity_content)
            return f.name.replace('research/', ''), room

    @patch('activity.socketio')
    @patch('activity.get_openai_client_and_model')
    def test_start_activity(self, mock_get_client, mock_socketio):
        """Test starting an activity creates proper state"""
        from models import ActivityState
        import activity

        # Create test activity
        filename, room = self.create_test_activity_file()

        # Mock AI client
        mock_client = MagicMock()
        mock_get_client.return_value = (mock_client, "gpt-4")

        # Start activity
        activity.start_activity(room.name, f"research/{filename}", "alice")

        # Verify ActivityState was created
        state = ActivityState.query.filter_by(room_id=room.id).first()
        self.assertIsNotNone(state)
        self.assertEqual(state.section_id, "section_1")
        self.assertEqual(state.step_id, "step_1")
        self.assertEqual(state.attempts, 0)

    @patch('activity.socketio')
    def test_cancel_activity(self, mock_socketio):
        """Test canceling an activity"""
        from models import ActivityState, Room
        import activity

        # Create room and activity state
        room = Room(name="test_room")
        self.db.session.add(room)
        self.db.session.commit()

        state = ActivityState(
            room_id=room.id,
            section_id="test_section",
            step_id="test_step",
            s3_file_path="test.yaml"
        )
        self.db.session.add(state)
        self.db.session.commit()

        # Cancel activity
        activity.cancel_activity(room.name, "alice")

        # Verify state was deleted
        remaining_state = ActivityState.query.filter_by(room_id=room.id).first()
        self.assertIsNone(remaining_state)

        # Verify socket event was emitted
        mock_socketio.emit.assert_called()

    @patch('activity.socketio')
    def test_display_activity_metadata(self, mock_socketio):
        """Test displaying activity metadata"""
        from models import ActivityState, Room
        import activity

        # Create room and activity state with metadata
        room = Room(name="test_room")
        self.db.session.add(room)
        self.db.session.commit()

        state = ActivityState(
            room_id=room.id,
            section_id="test_section",
            step_id="test_step",
            s3_file_path="test.yaml"
        )
        state.add_metadata("score", 100)
        state.add_metadata("level", 5)
        self.db.session.add(state)
        self.db.session.commit()

        # Display metadata
        activity.display_activity_metadata(room.name, "alice")

        # Verify emit was called with metadata
        mock_socketio.emit.assert_called()
        call_args = mock_socketio.emit.call_args
        self.assertIn("activity_metadata", str(call_args))

    @patch('activity.socketio')
    @patch('activity.get_openai_client_and_model')
    def test_handle_activity_response_correct_answer(self, mock_get_client, mock_socketio):
        """Test handling a correct answer advances to next step"""
        from models import ActivityState
        import activity

        # Create test activity
        filename, room = self.create_test_activity_file()

        # Create activity state
        state = ActivityState(
            room_id=room.id,
            section_id="section_1",
            step_id="step_1",
            s3_file_path=f"research/{filename}"
        )
        self.db.session.add(state)
        self.db.session.commit()

        # Mock AI client for categorization and feedback
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content.strip.return_value = "correct"
        mock_client.chat.completions.create.return_value = mock_response
        mock_get_client.return_value = (mock_client, "gpt-4")

        # Handle response
        activity.handle_activity_response(room.name, "4", "alice")

        # Verify state advanced to next step
        updated_state = ActivityState.query.filter_by(room_id=room.id).first()
        self.assertEqual(updated_state.step_id, "step_2")

    @patch('activity.socketio')
    @patch('activity.get_openai_client_and_model')
    def test_handle_activity_response_increments_attempts(self, mock_get_client, mock_socketio):
        """Test that incorrect answers increment attempt counter"""
        from models import ActivityState
        import activity

        # Create test activity
        filename, room = self.create_test_activity_file()

        # Create activity state
        state = ActivityState(
            room_id=room.id,
            section_id="section_1",
            step_id="step_1",
            s3_file_path=f"research/{filename}"
        )
        self.db.session.add(state)
        self.db.session.commit()

        initial_attempts = state.attempts

        # Mock AI to return incorrect answer
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content.strip.return_value = "incorrect"
        mock_client.chat.completions.create.return_value = mock_response
        mock_get_client.return_value = (mock_client, "gpt-4")

        # Handle response
        activity.handle_activity_response(room.name, "5", "alice")

        # Verify attempts incremented
        updated_state = ActivityState.query.filter_by(room_id=room.id).first()
        self.assertEqual(updated_state.attempts, initial_attempts + 1)
        # Should still be on same step
        self.assertEqual(updated_state.step_id, "step_1")

    @patch('activity.socketio')
    def test_execute_processing_script_with_metadata_operations(self, mock_socketio):
        """Test processing script that modifies metadata"""
        from models import ActivityState, Room
        import activity

        # Create room and state
        room = Room(name="test_room")
        self.db.session.add(room)
        self.db.session.commit()

        state = ActivityState(
            room_id=room.id,
            section_id="test",
            step_id="test",
            s3_file_path="test.yaml"
        )
        state.add_metadata("counter", 0)
        self.db.session.add(state)
        self.db.session.commit()

        # Execute script that increments counter
        metadata = state.dict_metadata
        script = """
metadata['counter'] = metadata.get('counter', 0) + 1
script_result = metadata['counter']
"""
        result = activity.execute_processing_script(metadata, script)

        self.assertEqual(result, 1)

    @patch('activity.socketio')
    @patch('activity.get_openai_client_and_model')
    def test_loop_through_steps_until_question(self, mock_get_client, mock_socketio):
        """Test looping through info steps until reaching a question"""
        from models import ActivityState
        import activity

        # Create activity with multiple info steps before question
        activity_content = """
default_max_attempts_per_step: 3

sections:
  - section_id: "intro"
    steps:
      - step_id: "info_1"
        type: "info"
        display_text: "Welcome!"
      - step_id: "info_2"
        type: "info"
        display_text: "Let's begin"
      - step_id: "question_1"
        type: "question"
        question: "Ready?"
        buckets:
          - bucket_name: "yes"
"""
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.yaml', dir='research', delete=False
        ) as f:
            f.write(activity_content)
            filename = f.name.replace('research/', '')

        # Create room
        from models import Room
        room = Room(name="test_room")
        self.db.session.add(room)
        self.db.session.commit()

        # Create state at first info step
        state = ActivityState(
            room_id=room.id,
            section_id="intro",
            step_id="info_1",
            s3_file_path=f"research/{filename}"
        )
        self.db.session.add(state)
        self.db.session.commit()

        # Load activity content
        content = activity.get_activity_content(f"research/{filename}")

        # Mock AI client
        mock_client = MagicMock()
        mock_get_client.return_value = (mock_client, "gpt-4")

        # Loop through steps
        activity.loop_through_steps_until_question(
            content, state, room.name, "alice"
        )

        # Should have advanced to question_1
        updated_state = ActivityState.query.filter_by(room_id=room.id).first()
        self.assertEqual(updated_state.step_id, "question_1")

        # Should have emitted info messages for info_1 and info_2
        self.assertGreaterEqual(mock_socketio.emit.call_count, 2)


class TestActivityMetadataOperations(unittest.TestCase):
    """Integration tests for metadata operations in activities"""

    def setUp(self):
        """Set up test Flask application"""
        import app as app_module
        from models import db
        from flask import Flask

        self.app_module = app_module
        self.db = db

        # Create test app
        test_app = Flask(__name__)
        test_app.config["TESTING"] = True
        test_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        test_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        test_app.config["SECRET_KEY"] = "test"

        db.init_app(test_app)

        self.original_app = app_module.app
        app_module.app = test_app

        self.app_context = test_app.app_context()
        self.app_context.push()

        db.create_all()

    def tearDown(self):
        """Clean up"""
        self.db.session.remove()
        try:
            self.db.drop_all()
        except:
            pass
        self.app_context.pop()
        self.app_module.app = self.original_app

    def test_activity_state_metadata_persistence(self):
        """Test that metadata persists across database operations"""
        from models import ActivityState, Room

        # Create room
        room = Room(name="test_room")
        self.db.session.add(room)
        self.db.session.commit()

        # Create state with metadata
        state = ActivityState(
            room_id=room.id,
            section_id="test",
            step_id="test",
            s3_file_path="test.yaml"
        )
        state.add_metadata("score", 100)
        state.add_metadata("level", 5)
        state.add_metadata("items", ["sword", "shield"])
        self.db.session.add(state)
        self.db.session.commit()

        # Retrieve from database
        retrieved_state = ActivityState.query.filter_by(room_id=room.id).first()
        metadata = retrieved_state.dict_metadata

        self.assertEqual(metadata["score"], 100)
        self.assertEqual(metadata["level"], 5)
        self.assertEqual(metadata["items"], ["sword", "shield"])

    def test_metadata_update_and_remove(self):
        """Test updating and removing metadata"""
        from models import ActivityState, Room

        room = Room(name="test_room")
        self.db.session.add(room)
        self.db.session.commit()

        state = ActivityState(
            room_id=room.id,
            section_id="test",
            step_id="test",
            s3_file_path="test.yaml"
        )
        state.add_metadata("temp", "value")
        state.add_metadata("keep", "important")
        self.db.session.add(state)
        self.db.session.commit()

        # Remove temp metadata
        state.remove_metadata("temp")
        self.db.session.commit()

        # Verify
        retrieved_state = ActivityState.query.filter_by(room_id=room.id).first()
        metadata = retrieved_state.dict_metadata

        self.assertNotIn("temp", metadata)
        self.assertEqual(metadata["keep"], "important")


if __name__ == "__main__":
    unittest.main()
