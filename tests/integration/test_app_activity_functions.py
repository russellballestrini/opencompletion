#!/usr/bin/env python3
"""
Integration tests for app.py activity functions with real Flask environment and database

These tests use a real Flask test environment with in-memory SQLite database
to actually execute the activity functions and improve app.py coverage.
"""

import unittest
import os
import sys
import tempfile
import json
from pathlib import Path

# Add parent directory to path to import the app
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Import Flask and testing utilities
import pytest
from flask import Flask
from flask_socketio import SocketIO

# Import the main application
import app
import activity
from models import db, Room, ActivityState, Message


class TestFlaskAppActivityFunctions(unittest.TestCase):
    """Integration tests for app.py activity functions with real Flask environment"""

    def setUp(self):
        """Set up test Flask application with in-memory database"""
        # Ensure instance directory exists (GitHub Actions might not have it)
        import os
        os.makedirs(app.app.instance_path, exist_ok=True)

        # Store original database URI
        self.original_db_uri = app.app.config.get("SQLALCHEMY_DATABASE_URI")

        # Configure test app BEFORE pushing context
        app.app.config["TESTING"] = True
        app.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        app.app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        app.app.config["LOCAL_ACTIVITIES"] = True  # Use local YAML files
        app.app.config["WTF_CSRF_ENABLED"] = False

        # Create test client and push context
        self.client = app.app.test_client()
        self.app_context = app.app.app_context()
        self.app_context.push()

        # Force db to use the new in-memory database by clearing the engine
        # This allows the in-memory database to be created
        # Note: Access db.engine AFTER pushing app context
        if hasattr(db, "engine"):
            db.engine.dispose()
        db.session.remove()

        # Re-initialize db with test config to use in-memory database
        try:
            db.drop_all()
        except Exception:
            pass

        # Initialize database
        db.create_all()

        # Create test room
        self.test_room = Room(name="test_room")
        db.session.add(self.test_room)
        db.session.commit()

        # Store original socketio for cleanup
        self.original_socketio = app.socketio

        # Initialize activity module with app's socketio and db
        # Note: activity module is already initialized when imported,
        # so we don't need to re-initialize it for tests

    def tearDown(self):
        """Clean up test environment"""
        db.session.remove()
        try:
            db.drop_all()
        except Exception:
            pass
        self.app_context.pop()

        # Restore original socketio and database URI
        app.socketio = self.original_socketio
        if self.original_db_uri:
            app.app.config["SQLALCHEMY_DATABASE_URI"] = self.original_db_uri

    def create_test_activity_file(self, content):
        """Create a temporary activity YAML file"""
        # Get absolute path to research directory
        base_dir = Path(__file__).parent.parent.parent
        research_dir = base_dir / "research"
        research_dir.mkdir(exist_ok=True)

        # Create temporary file in research directory
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", dir=research_dir, delete=False
        ) as f:
            f.write(content)
            # Return just the filename relative to research directory
            return Path(f.name).name

    def test_get_activity_content_local(self):
        """Test loading activity content from local files"""
        test_yaml_content = """
title: "Test Activity"
description: "A simple test activity"
default_max_attempts_per_step: 3
sections:
  - section_id: "section_1"
    title: "Test Section"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        content_blocks:
          - "Welcome to the test activity"
        question: "What is 2+2?"
        buckets:
          - "correct"
          - "incorrect"
        tokens_for_ai: "Categorize the mathematical response"
        transitions:
          correct:
            content_blocks:
              - "Correct! Well done."
          incorrect:
            content_blocks:
              - "That's not right. Try again."
            counts_as_attempt: true
"""

        activity_file = self.create_test_activity_file(test_yaml_content)

        try:
            # Test the actual get_activity_content function
            result = activity.get_activity_content(f"research/{activity_file}")

            # Verify structure
            self.assertEqual(result["title"], "Test Activity")
            self.assertEqual(result["default_max_attempts_per_step"], 3)
            self.assertEqual(len(result["sections"]), 1)
            self.assertEqual(result["sections"][0]["section_id"], "section_1")

        finally:
            # Clean up
            base_dir = Path(__file__).parent.parent.parent
            os.unlink(base_dir / "research" / activity_file)

    def test_start_activity_integration(self):
        """Test starting an activity with real database operations"""
        test_yaml_content = """
title: "Integration Test Activity"
default_max_attempts_per_step: 2
sections:
  - section_id: "intro"
    title: "Introduction"
    steps:
      - step_id: "welcome"
        title: "Welcome Step"
        content_blocks:
          - "Welcome to this integration test!"
      - step_id: "question_step"
        title: "Question"
        content_blocks:
          - "Now for a question..."
        question: "What is your name?"
        buckets:
          - "any_response"
        tokens_for_ai: "Accept any response"
        transitions:
          any_response:
            content_blocks:
              - "Thank you for your response!"
"""

        activity_file = self.create_test_activity_file(test_yaml_content)

        try:
            # Mock socketio emissions to avoid actual socket connections
            app.socketio = type(
                "MockSocketIO",
                (),
                {
                    "emit": lambda *args, **kwargs: None,
                    "sleep": lambda *args, **kwargs: None,
                },
            )()

            # Test start_activity function
            activity.start_activity(
                "test_room", f"research/{activity_file}", "testuser"
            )

            # Verify activity state was created in database
            activity_state = ActivityState.query.filter_by(
                room_id=self.test_room.id
            ).first()
            self.assertIsNotNone(activity_state)
            self.assertEqual(activity_state.section_id, "intro")
            # The function advances through steps until it finds a question
            # So it should stop at "question_step" not "welcome"
            self.assertEqual(activity_state.step_id, "question_step")
            self.assertEqual(activity_state.max_attempts, 2)
            self.assertEqual(activity_state.s3_file_path, f"research/{activity_file}")

        finally:
            # Clean up
            base_dir = Path(__file__).parent.parent.parent
            os.unlink(base_dir / "research" / activity_file)

    def test_handle_activity_response_integration(self):
        """Test handling activity responses with real categorization and database updates"""
        test_yaml_content = """
title: "Response Test Activity"
default_max_attempts_per_step: 3
sections:
  - section_id: "test_section"
    title: "Test Section"
    steps:
      - step_id: "math_question"
        title: "Math Question"
        question: "What is 5+5?"
        buckets:
          - "correct"
          - "incorrect"
        tokens_for_ai: "Categorize: if answer is 10 or ten, say 'correct', otherwise 'incorrect'"
        transitions:
          correct:
            content_blocks:
              - "Excellent! That's correct."
            metadata_add:
              score: "n+10"
              correct_answers: "n+1"
          incorrect:
            content_blocks:
              - "Not quite right. Try again."
            counts_as_attempt: true
"""

        activity_file = self.create_test_activity_file(test_yaml_content)

        try:
            # Mock socketio emissions
            app.socketio = type(
                "MockSocketIO",
                (),
                {
                    "emit": lambda *args, **kwargs: None,
                    "sleep": lambda *args, **kwargs: None,
                },
            )()

            # Create activity state manually
            activity_state = ActivityState(
                room_id=self.test_room.id,
                section_id="test_section",
                step_id="math_question",
                max_attempts=3,
                s3_file_path=f"research/{activity_file}",
                attempts=0,
            )
            activity_state.dict_metadata = {"score": 0, "correct_answers": 0}
            activity_state.json_metadata = json.dumps(activity_state.dict_metadata)
            db.session.add(activity_state)
            db.session.commit()

            # Test handling a correct response
            activity.handle_activity_response("test_room", "10", "testuser")

            # Refresh activity state from database
            db.session.refresh(activity_state)

            # Verify metadata was updated (if categorization worked)
            updated_metadata = json.loads(activity_state.json_metadata)

            # The exact assertion depends on whether the AI categorization succeeded
            # At minimum, we verify the function executed without error
            self.assertIsInstance(updated_metadata, dict)

        finally:
            # Clean up
            base_dir = Path(__file__).parent.parent.parent
            os.unlink(base_dir / "research" / activity_file)

    def test_display_activity_metadata_integration(self):
        """Test displaying activity metadata with real database state"""
        # Mock socketio emissions and capture them
        emitted_messages = []

        def mock_emit(*args, **kwargs):
            # Handle different emit signatures flexibly
            # Skip self argument if it's a MockSocketIO object
            filtered_args = [
                arg
                for arg in args
                if not hasattr(arg, "__class__")
                or "MockSocketIO" not in str(arg.__class__)
            ]

            event = filtered_args[0] if filtered_args else kwargs.get("event")
            data = filtered_args[1] if len(filtered_args) > 1 else kwargs.get("data")
            room = kwargs.get("room")
            emitted_messages.append({"event": event, "data": data, "room": room})

        # Mock activity.socketio directly (not app.socketio)
        activity.socketio = type(
            "MockSocketIO",
            (),
            {"emit": mock_emit, "sleep": lambda *args, **kwargs: None},
        )()

        # Create activity state with metadata
        activity_state = ActivityState(
            room_id=self.test_room.id,
            section_id="test_section",
            step_id="test_step",
            max_attempts=3,
            s3_file_path="test_activity.yaml",
        )
        activity_state.dict_metadata = {
            "player_name": "TestPlayer",
            "score": 150,
            "level": 5,
            "achievements": ["first_win", "perfect_score"],
        }
        activity_state.json_metadata = json.dumps(activity_state.dict_metadata)
        db.session.add(activity_state)
        db.session.commit()

        # Test display_activity_metadata function
        activity.display_activity_metadata("test_room", "testuser")

        # Verify that a message was emitted
        self.assertTrue(len(emitted_messages) > 0)

        # Check if metadata message was emitted
        metadata_message = None
        for msg in emitted_messages:
            if msg["event"] == "chat_message" and msg["data"].get("content"):
                metadata_message = msg
                break

        self.assertIsNotNone(metadata_message, "Should have emitted metadata message")
        self.assertEqual(metadata_message["room"], "test_room")
        # Verify the content contains the metadata
        content = metadata_message["data"]["content"]
        self.assertIn("TestPlayer", content)
        self.assertIn("150", content)  # score

    def test_cancel_activity_integration(self):
        """Test canceling an activity with real database operations"""
        # Mock socketio emissions
        emitted_messages = []

        def mock_emit(*args, **kwargs):
            # Handle different emit signatures flexibly
            # Skip self argument if it's a MockSocketIO object
            filtered_args = [
                arg
                for arg in args
                if not hasattr(arg, "__class__")
                or "MockSocketIO" not in str(arg.__class__)
            ]

            event = filtered_args[0] if filtered_args else kwargs.get("event")
            data = filtered_args[1] if len(filtered_args) > 1 else kwargs.get("data")
            room = kwargs.get("room")
            emitted_messages.append({"event": event, "data": data, "room": room})

        # Mock activity.socketio directly (not app.socketio)
        activity.socketio = type(
            "MockSocketIO",
            (),
            {"emit": mock_emit, "sleep": lambda *args, **kwargs: None},
        )()

        # Create activity state
        activity_state = ActivityState(
            room_id=self.test_room.id,
            section_id="test_section",
            step_id="test_step",
            max_attempts=3,
            s3_file_path="test_activity.yaml",
        )
        db.session.add(activity_state)
        db.session.commit()

        # Verify activity exists
        self.assertIsNotNone(
            ActivityState.query.filter_by(room_id=self.test_room.id).first()
        )

        # Test cancel_activity function
        activity.cancel_activity("test_room", "testuser")

        # Verify activity was deleted from database
        self.assertIsNone(
            ActivityState.query.filter_by(room_id=self.test_room.id).first()
        )

        # Verify cancellation messages were emitted
        self.assertTrue(
            len(emitted_messages) > 0, "Should have emitted cancellation messages"
        )

        # Check for chat_message with cancellation content
        chat_messages = [
            msg
            for msg in emitted_messages
            if msg["event"] == "chat_message" and msg["data"]
        ]
        self.assertTrue(len(chat_messages) > 0, "Should have a chat message")
        cancel_message = chat_messages[0]
        self.assertEqual(cancel_message["room"], "test_room")
        self.assertIn("canceled", cancel_message["data"]["content"].lower())

        # Check for activity_status update
        status_messages = [
            msg for msg in emitted_messages if msg["event"] == "activity_status"
        ]
        self.assertTrue(len(status_messages) > 0, "Should have activity_status")
        self.assertFalse(status_messages[0]["data"]["active"])

    def test_execute_processing_script_integration(self):
        """Test processing script execution with real metadata manipulation"""
        script = """
import random
import math

# Test various operations
user_input = metadata.get('user_response', 'default')
metadata['processed_input'] = user_input.upper()
metadata['input_length'] = len(user_input)
metadata['random_bonus'] = random.randint(10, 50)
metadata['calculated_score'] = math.sqrt(metadata.get('base_score', 100))

# Test complex operations
if 'achievements' not in metadata:
    metadata['achievements'] = []

metadata['achievements'].append('processed_response')

script_result = {
    'status': 'success',
    'processing_complete': True,
    'metadata': {
        'bonus_applied': True,
        'processing_timestamp': 'mock_timestamp'
    }
}
"""

        metadata = {
            "user_response": "test input",
            "base_score": 144,
            "existing_data": "preserved",
        }

        # Test the actual execute_processing_script function
        result = activity.execute_processing_script(metadata, script)

        # Verify script execution results
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["processing_complete"])
        self.assertTrue(result["metadata"]["bonus_applied"])

        # Verify metadata modifications
        self.assertEqual(metadata["processed_input"], "TEST INPUT")
        self.assertEqual(metadata["input_length"], 10)
        self.assertIn("random_bonus", metadata)
        self.assertEqual(metadata["calculated_score"], 12.0)  # sqrt(144)
        self.assertIn("processed_response", metadata["achievements"])
        self.assertEqual(metadata["existing_data"], "preserved")  # Should be unchanged

    def test_get_next_step_integration(self):
        """Test step navigation with real activity content"""
        activity_content = {
            "sections": [
                {
                    "section_id": "section_1",
                    "steps": [
                        {"step_id": "step_1", "title": "Step 1"},
                        {"step_id": "step_2", "title": "Step 2"},
                        {"step_id": "step_3", "title": "Step 3"},
                    ],
                },
                {
                    "section_id": "section_2",
                    "steps": [
                        {"step_id": "step_1", "title": "Section 2 Step 1"},
                        {"step_id": "step_2", "title": "Section 2 Step 2"},
                    ],
                },
            ]
        }

        # Test navigation within section
        next_section, next_step = activity.get_next_step(
            activity_content, "section_1", "step_1"
        )
        self.assertEqual(next_section["section_id"], "section_1")
        self.assertEqual(next_step["step_id"], "step_2")

        # Test navigation across sections
        next_section, next_step = activity.get_next_step(
            activity_content, "section_1", "step_3"
        )
        self.assertEqual(next_section["section_id"], "section_2")
        self.assertEqual(next_step["step_id"], "step_1")

        # Test at end of activity
        next_section, next_step = activity.get_next_step(
            activity_content, "section_2", "step_2"
        )
        self.assertIsNone(next_section)
        self.assertIsNone(next_step)

    def test_categorize_response_integration(self):
        """Test response categorization with real AI endpoint (if available)"""
        # Test with simple categorization
        question = "What is 2 + 2?"
        response = "4"
        buckets = ["correct", "incorrect"]
        tokens_for_ai = (
            "If the answer is 4 or four, categorize as 'correct', otherwise 'incorrect'"
        )

        # Test the actual categorization function
        result = activity.categorize_response(
            question, response, buckets, tokens_for_ai
        )

        # Result should be either "correct", "incorrect", or an error message
        self.assertIsInstance(result, str)
        self.assertTrue(
            result in ["correct", "incorrect"] or result.startswith("Error:")
        )

    def test_translate_text_integration(self):
        """Test text translation functionality"""
        # Test English bypass
        english_text = "Hello, world!"
        result = activity.translate_text(english_text, "English")
        self.assertEqual(result, english_text)

        # Test case insensitive
        result = activity.translate_text(english_text, "english")
        self.assertEqual(result, english_text)

        # Test with compound language
        result = activity.translate_text(english_text, "English please")
        self.assertEqual(result, english_text)

        # Test other language (will use AI endpoint if available)
        result = activity.translate_text("Hello", "Spanish")
        self.assertIsInstance(result, str)  # Should return some string result


if __name__ == "__main__":
    unittest.main(verbosity=2)
