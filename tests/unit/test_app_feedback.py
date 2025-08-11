#!/usr/bin/env python3
"""
Unit tests for app.py feedback functions.

Tests the feedback generation functions including:
- Legacy provide_feedback function
- New provide_feedback_prompts function
- Both systems integration
- Metadata filtering
- Language handling
"""

import unittest
from unittest.mock import patch, MagicMock, call
import sys
import json
from pathlib import Path

# Add parent directory to path to import app functions
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class TestAppFeedback(unittest.TestCase):
    """Test cases for app.py feedback functions"""

    def setUp(self):
        """Set up test fixtures"""
        self.sample_transition = {
            "ai_feedback": {"tokens_for_ai": "Additional transition instructions"},
            "metadata_feedback_filter": ["shot_location", "hit_result", "ship_sunk"],
        }

        self.sample_metadata = {
            "shot_location": "A5",
            "hit_result": "hit",
            "ship_sunk": "destroyer",
            "private_info": "should_be_filtered",
            "player_health": 100,
        }

        self.sample_new_metadata = {"new_shot": "B3", "new_result": "miss"}

    def test_provide_feedback_import(self):
        """Test that we can import the provide_feedback function"""
        try:
            from app import provide_feedback

            self.assertTrue(callable(provide_feedback))
        except ImportError as e:
            self.fail(f"Could not import provide_feedback: {e}")

    def test_provide_feedback_prompts_import(self):
        """Test that we can import the provide_feedback_prompts function"""
        try:
            from app import provide_feedback_prompts

            self.assertTrue(callable(provide_feedback_prompts))
        except ImportError as e:
            self.fail(f"Could not import provide_feedback_prompts: {e}")

    @patch("app.get_openai_client_and_model")
    def test_provide_feedback_legacy(self, mock_get_client):
        """Test legacy provide_feedback function"""
        # Import here to avoid issues if module is not available
        try:
            from app import provide_feedback
        except ImportError:
            self.skipTest("app module not available for testing")

        # Setup mock
        mock_client = MagicMock()
        mock_completion = MagicMock()
        mock_completion.choices[0].message.content = "Great shot! You hit the target."
        mock_client.chat.completions.create.return_value = mock_completion
        mock_get_client.return_value = (mock_client, "test-model")

        # Test data
        transition = self.sample_transition
        category = "hit"
        question = "Where do you want to shoot?"
        feedback_tokens_for_ai = "Provide battleship feedback"
        user_response = "A5"
        user_language = "English"
        username = "testuser"
        json_metadata = json.dumps(self.sample_metadata)
        json_new_metadata = json.dumps(self.sample_new_metadata)

        # Call function
        feedback = provide_feedback(
            transition,
            category,
            question,
            feedback_tokens_for_ai,
            user_response,
            user_language,
            username,
            json_metadata,
            json_new_metadata,
        )

        # Verify result
        self.assertIn("Great shot! You hit the target.", feedback)

        # Verify client was called
        mock_client.chat.completions.create.assert_called_once()
        call_args = mock_client.chat.completions.create.call_args[1]

        # Check that system message includes language and transition instructions
        system_message = call_args["messages"][0]["content"]
        self.assertIn("English", system_message)
        self.assertIn("Additional transition instructions", system_message)

    @patch("app.get_openai_client_and_model")
    def test_provide_feedback_prompts_multi(self, mock_get_client):
        """Test provide_feedback_prompts with multiple prompts"""
        try:
            from app import provide_feedback_prompts
        except ImportError:
            self.skipTest("app module not available for testing")

        # Setup mock to return different responses for each prompt
        mock_client = MagicMock()
        mock_completion_1 = MagicMock()
        mock_completion_1.choices[0].message.content = (
            "Your shot at A5 was a hit! Enemy shot at B3 missed."
        )
        mock_completion_2 = MagicMock()
        mock_completion_2.choices[0].message.content = (
            "The enemy's destroyer has been sunk!"
        )

        mock_client.chat.completions.create.side_effect = [
            mock_completion_1,
            mock_completion_2,
        ]
        mock_get_client.return_value = (mock_client, "test-model")

        # Test data
        transition = self.sample_transition
        category = "valid_move"
        question = "Where do you want to shoot?"
        feedback_prompts = [
            {
                "name": "hit_miss_feedback",
                "tokens_for_ai": "Report the hit/miss results for both players this turn",
            },
            {
                "name": "ship_sinking_feedback",
                "tokens_for_ai": "Report any ships that were sunk this turn",
            },
        ]
        user_response = "A5"
        user_language = "English"
        username = "testuser"
        json_metadata = json.dumps(self.sample_metadata)
        json_new_metadata = json.dumps(self.sample_new_metadata)

        # Call function
        feedback_messages = provide_feedback_prompts(
            transition,
            category,
            question,
            feedback_prompts,
            user_response,
            user_language,
            username,
            json_metadata,
            json_new_metadata,
            "",
        )

        # Verify results
        self.assertEqual(len(feedback_messages), 2)

        # Check first feedback message
        self.assertEqual(feedback_messages[0]["name"], "hit_miss_feedback")
        self.assertIn("Your shot at A5 was a hit", feedback_messages[0]["content"])

        # Check second feedback message
        self.assertEqual(feedback_messages[1]["name"], "ship_sinking_feedback")
        self.assertIn("destroyer has been sunk", feedback_messages[1]["content"])

        # Verify client was called twice
        self.assertEqual(mock_client.chat.completions.create.call_count, 2)

    @patch("app.get_openai_client_and_model")
    def test_provide_feedback_with_filtered_metadata(self, mock_get_client):
        """Test that provide_feedback works correctly with pre-filtered metadata"""
        try:
            from app import provide_feedback
        except ImportError:
            self.skipTest("app module not available for testing")

        # Setup mock
        mock_client = MagicMock()
        mock_completion = MagicMock()
        mock_completion.choices[0].message.content = "Filtered feedback"
        mock_client.chat.completions.create.return_value = mock_completion
        mock_get_client.return_value = (mock_client, "test-model")

        # Simulate app.py behavior: filter metadata before calling provide_feedback
        filtered_metadata = {
            k: v
            for k, v in self.sample_metadata.items()
            if k in self.sample_transition["metadata_feedback_filter"]
        }

        provide_feedback(
            self.sample_transition,
            "test",
            "Question?",
            "tokens",
            "response",
            "English",
            "user",
            json.dumps(filtered_metadata),
            json.dumps({}),
        )

        # Check that user message contains only filtered metadata
        call_args = mock_client.chat.completions.create.call_args[1]
        user_message = call_args["messages"][1]["content"]

        # Should contain filtered fields
        self.assertIn("shot_location", user_message)
        self.assertIn("hit_result", user_message)
        self.assertIn("ship_sunk", user_message)

        # Should NOT contain unfiltered fields (because we pre-filtered)
        self.assertNotIn("private_info", user_message)
        self.assertNotIn("player_health", user_message)

    @patch("app.get_openai_client_and_model")
    def test_provide_feedback_no_filter(self, mock_get_client):
        """Test feedback when no metadata filter is specified"""
        try:
            from app import provide_feedback
        except ImportError:
            self.skipTest("app module not available for testing")

        # Setup mock
        mock_client = MagicMock()
        mock_completion = MagicMock()
        mock_completion.choices[0].message.content = "Unfiltered feedback"
        mock_client.chat.completions.create.return_value = mock_completion
        mock_get_client.return_value = (mock_client, "test-model")

        # Call function without metadata filter
        transition = {
            "ai_feedback": {"tokens_for_ai": "Generate feedback"}
        }  # No metadata_feedback_filter

        provide_feedback(
            transition,
            "test",
            "Question?",
            "tokens",
            "response",
            "English",
            "user",
            json.dumps(self.sample_metadata),
            json.dumps({}),
        )

        # Check that user message contains all metadata
        call_args = mock_client.chat.completions.create.call_args[1]
        user_message = call_args["messages"][1]["content"]

        # Should contain all metadata fields when no filter is applied
        self.assertIn("private_info", user_message)
        self.assertIn("player_health", user_message)

    @patch("app.get_openai_client_and_model")
    def test_provide_feedback_error_handling(self, mock_get_client):
        """Test error handling in feedback functions"""
        try:
            from app import provide_feedback
        except ImportError:
            self.skipTest("app module not available for testing")

        # Setup mock to raise exception
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("API Error")
        mock_get_client.return_value = (mock_client, "test-model")

        # Call function
        feedback = provide_feedback(
            {"ai_feedback": {"tokens_for_ai": "Generate feedback"}},
            "test",
            "Question?",
            "tokens",
            "response",
            "English",
            "user",
            json.dumps({}),
            json.dumps({}),
        )

        # Should handle error gracefully
        self.assertIn("Error", feedback)

    @patch("app.get_openai_client_and_model")
    def test_provide_feedback_prompts_filter_empty(self, mock_get_client):
        """Test feedback_prompts with empty results filtered out"""
        try:
            from app import provide_feedback_prompts
        except ImportError:
            self.skipTest("app module not available for testing")

        # Setup mock to return mixed results including empty
        mock_client = MagicMock()
        mock_completion_1 = MagicMock()
        mock_completion_1.choices[0].message.content = ""  # Empty result
        mock_completion_2 = MagicMock()
        mock_completion_2.choices[0].message.content = (
            "   "  # Whitespace only (should be filtered)
        )
        mock_completion_3 = MagicMock()
        mock_completion_3.choices[0].message.content = "Valid feedback"  # Valid result

        mock_client.chat.completions.create.side_effect = [
            mock_completion_1,
            mock_completion_2,
            mock_completion_3,
        ]
        mock_get_client.return_value = (mock_client, "test-model")

        # Test data
        feedback_prompts = [
            {"name": "empty", "tokens_for_ai": "Empty prompt"},
            {"name": "whitespace", "tokens_for_ai": "Whitespace prompt"},
            {"name": "valid", "tokens_for_ai": "Valid prompt"},
        ]

        feedback_messages = provide_feedback_prompts(
            {},
            "test",
            "Question?",
            feedback_prompts,
            "response",
            "English",
            "user",
            json.dumps({}),
            json.dumps({}),
            "",
        )

        # Should only return valid feedback (empty and whitespace filtered out)
        self.assertEqual(len(feedback_messages), 1)
        self.assertEqual(feedback_messages[0]["name"], "valid")
        self.assertEqual(feedback_messages[0]["content"], "Valid feedback")

    @patch("app.get_openai_client_and_model")
    def test_provide_feedback_prompts_per_prompt_metadata_filtering(
        self, mock_get_client
    ):
        """Test that each prompt gets its own filtered metadata"""
        try:
            from app import provide_feedback_prompts
        except ImportError:
            self.skipTest("app module not available for testing")

        # Setup mock to return different responses
        mock_client = MagicMock()
        mock_completion_1 = MagicMock()
        mock_completion_1.choices[0].message.content = (
            "Shot feedback with hit/miss data"
        )
        mock_completion_2 = MagicMock()
        mock_completion_2.choices[0].message.content = "Ship feedback with sinking data"

        mock_client.chat.completions.create.side_effect = [
            mock_completion_1,
            mock_completion_2,
        ]
        mock_get_client.return_value = (mock_client, "test-model")

        # Test data with mixed metadata
        full_metadata = {
            "user_shot": "A5",
            "user_hit_result": "hit",
            "ai_shot": "B3",
            "ai_hit_result": "miss",
            "user_sunk_ship_this_round": "Destroyer",
            "ai_sunk_ship_this_round": None,
            "game_over": False,
            "extra_field": "should_not_appear",
        }

        feedback_prompts = [
            {
                "name": "shot_report",
                "tokens_for_ai": "Report hit/miss",
                "metadata_filter": [
                    "user_shot",
                    "user_hit_result",
                    "ai_shot",
                    "ai_hit_result",
                ],
            },
            {
                "name": "ship_status",
                "tokens_for_ai": "Report ship sinking",
                "metadata_filter": [
                    "user_sunk_ship_this_round",
                    "ai_sunk_ship_this_round",
                ],
            },
        ]

        feedback_messages = provide_feedback_prompts(
            {},
            "test",
            "Question?",
            feedback_prompts,
            "response",
            "English",
            "user",
            json.dumps(full_metadata),
            json.dumps({}),
            "",
        )

        # Verify both prompts got responses
        self.assertEqual(len(feedback_messages), 2)
        self.assertEqual(feedback_messages[0]["name"], "shot_report")
        self.assertEqual(feedback_messages[1]["name"], "ship_status")

        # Verify the first prompt only got shot-related metadata
        first_call_args = mock_client.chat.completions.create.call_args_list[0][1]
        first_user_message = first_call_args["messages"][1]["content"]
        self.assertIn("user_shot", first_user_message)
        self.assertIn("user_hit_result", first_user_message)
        self.assertIn("ai_shot", first_user_message)
        self.assertIn("ai_hit_result", first_user_message)
        self.assertNotIn("user_sunk_ship_this_round", first_user_message)
        self.assertNotIn("extra_field", first_user_message)

        # Verify the second prompt only got ship-related metadata
        second_call_args = mock_client.chat.completions.create.call_args_list[1][1]
        second_user_message = second_call_args["messages"][1]["content"]
        self.assertIn("user_sunk_ship_this_round", second_user_message)
        self.assertIn("ai_sunk_ship_this_round", second_user_message)
        self.assertNotIn("user_shot", second_user_message)
        self.assertNotIn("extra_field", second_user_message)

    @patch("app.get_openai_client_and_model")
    def test_ship_status_metadata_filtering_debug(self, mock_get_client):
        """Debug test to check if Ship Status is getting only the right metadata"""
        try:
            from app import provide_feedback_prompts
        except ImportError:
            self.skipTest("app module not available for testing")

        # Setup mock
        mock_client = MagicMock()
        mock_completion = MagicMock()
        mock_completion.choices[0].message.content = "Test response"
        mock_client.chat.completions.create.return_value = mock_completion
        mock_get_client.return_value = (mock_client, "test-model")

        # Test data mimicking the actual battleship scenario
        full_metadata = {
            "user_shot": "46",  # This should NOT appear in Ship Status
            "ai_shot": "49",  # This should NOT appear in Ship Status
            "user_hit_result": "hit",
            "ai_hit_result": "miss",
            "user_sunk_ship_this_round": "Destroyer",  # This SHOULD appear
            "ai_sunk_ship_this_round": None,  # This SHOULD appear
            "game_over": False,
            "extra_stuff": "should not appear anywhere",
        }

        # Exact structure from battleship YAML
        feedback_prompts = [
            {
                "name": "Shot Report",
                "tokens_for_ai": "🎯 Report ONLY the hit/miss results",
                "metadata_filter": [
                    "user_shot",
                    "ai_shot",
                    "user_hit_result",
                    "ai_hit_result",
                ],
            },
            {
                "name": "Ship Status",
                "tokens_for_ai": "You are the Ship Destruction Oracle",
                "metadata_filter": [
                    "user_sunk_ship_this_round",
                    "ai_sunk_ship_this_round",
                ],
            },
        ]

        # Call the function
        provide_feedback_prompts(
            {},
            "valid_move",
            "Question?",
            feedback_prompts,
            "46",
            "English",
            "user",
            json.dumps(full_metadata),
            json.dumps({}),
            "",
        )

        # Check what metadata each prompt actually received
        self.assertEqual(mock_client.chat.completions.create.call_count, 2)

        # First call should be Shot Report
        shot_report_call = mock_client.chat.completions.create.call_args_list[0][1]
        shot_report_metadata = shot_report_call["messages"][1]["content"]

        print("=== SHOT REPORT METADATA ===")
        print(shot_report_metadata)

        # Shot Report should have shot data but NOT ship destruction data
        self.assertIn("user_shot", shot_report_metadata)
        self.assertIn("46", shot_report_metadata)
        self.assertNotIn("user_sunk_ship_this_round", shot_report_metadata)
        self.assertNotIn("Destroyer", shot_report_metadata)

        # Second call should be Ship Status
        ship_status_call = mock_client.chat.completions.create.call_args_list[1][1]
        ship_status_metadata = ship_status_call["messages"][1]["content"]

        print("=== SHIP STATUS METADATA ===")
        print(ship_status_metadata)

        # Ship Status should have ship destruction data but NOT shot data
        self.assertIn("user_sunk_ship_this_round", ship_status_metadata)
        self.assertIn("Destroyer", ship_status_metadata)
        self.assertNotIn("user_shot", ship_status_metadata)
        self.assertNotIn("46", ship_status_metadata)
        self.assertNotIn("extra_stuff", ship_status_metadata)

    def test_provide_feedback_prompts_language_injection(self):
        """Test that language instructions are properly added to prompts"""
        try:
            from app import provide_feedback_prompts
        except ImportError:
            self.skipTest("app module not available for testing")

        with patch("app.get_openai_client_and_model") as mock_get_client:
            mock_client = MagicMock()
            mock_completion = MagicMock()
            mock_completion.choices[0].message.content = "Feedback in Spanish"
            mock_client.chat.completions.create.return_value = mock_completion
            mock_get_client.return_value = (mock_client, "test-model")

            feedback_prompts = [{"name": "test", "tokens_for_ai": "Base prompt"}]

            # Test with Spanish language
            provide_feedback_prompts(
                {},
                "test",
                "Question?",
                feedback_prompts,
                "response",
                "Spanish",
                "user",
                json.dumps({}),
                json.dumps({}),
                "",
            )

            # Check that system message includes Spanish language instruction
            call_args = mock_client.chat.completions.create.call_args[1]
            system_message = call_args["messages"][0]["content"]
            self.assertIn("Spanish", system_message)
            self.assertIn("Base prompt", system_message)

    @patch("app.get_openai_client_and_model")
    def test_provide_feedback_transition_tokens(self, mock_get_client):
        """Test that transition ai_feedback tokens are included"""
        try:
            from app import provide_feedback_prompts
        except ImportError:
            self.skipTest("app module not available for testing")

        mock_client = MagicMock()
        mock_completion = MagicMock()
        mock_completion.choices[0].message.content = "Enhanced feedback"
        mock_client.chat.completions.create.return_value = mock_completion
        mock_get_client.return_value = (mock_client, "test-model")

        transition = {
            "ai_feedback": {"tokens_for_ai": "Be more dramatic in your feedback"}
        }

        feedback_prompts = [{"name": "test", "tokens_for_ai": "Base prompt"}]

        provide_feedback_prompts(
            transition,
            "test",
            "Question?",
            feedback_prompts,
            "response",
            "English",
            "user",
            json.dumps({}),
            json.dumps({}),
            "",
        )

        # Check that system message includes both base and transition tokens
        call_args = mock_client.chat.completions.create.call_args[1]
        system_message = call_args["messages"][0]["content"]
        self.assertIn("Base prompt", system_message)
        self.assertIn("Be more dramatic in your feedback", system_message)

    @patch("app.get_openai_client_and_model")
    def test_user_response_filtering_with_metadata_filter(self, mock_get_client):
        """Test that user_response is filtered correctly using metadata_filter approach"""
        try:
            from app import provide_feedback_prompts
        except ImportError:
            self.skipTest("app module not available for testing")

        # Setup mock
        mock_client = MagicMock()
        mock_completion = MagicMock()
        mock_completion.choices[0].message.content = "Response for prompt"
        mock_client.chat.completions.create.return_value = mock_completion
        mock_get_client.return_value = (mock_client, "test-model")

        # Test feedback prompts - one that includes user_response, one that doesn't
        feedback_prompts = [
            {
                "name": "Shot Report",
                "tokens_for_ai": "Report shot positions",
                "metadata_filter": [
                    "user_shot",
                    "user_response",
                ],  # Includes user_response
            },
            {
                "name": "Ship Status",
                "tokens_for_ai": "Report ship status",
                "metadata_filter": ["ship_status"],  # Does NOT include user_response
            },
        ]

        metadata = {"user_shot": "35", "ship_status": "intact"}

        user_response = "I choose position 35"

        provide_feedback_prompts(
            {},
            "valid_move",
            "Choose position?",
            feedback_prompts,
            user_response,
            "English",
            "user",
            json.dumps(metadata),
            json.dumps({}),
            "",
        )

        # Should have 2 calls
        self.assertEqual(mock_client.chat.completions.create.call_count, 2)

        # First call (Shot Report) should have user_response
        first_call = mock_client.chat.completions.create.call_args_list[0][1]
        first_user_message = first_call["messages"][1]["content"]
        self.assertIn(
            "I choose position 35", first_user_message
        )  # user_response should be present

        # Second call (Ship Status) should NOT have user_response
        second_call = mock_client.chat.completions.create.call_args_list[1][1]
        second_user_message = second_call["messages"][1]["content"]
        self.assertEqual(
            second_user_message.count("I choose position 35"), 0
        )  # user_response should be empty/filtered

    @patch("app.get_openai_client_and_model")
    def test_skip_condition_all_null(self, mock_get_client):
        """Test skip_condition 'all_null' skips prompts when all metadata values are null"""
        try:
            from app import provide_feedback_prompts
        except ImportError:
            self.skipTest("app module not available for testing")

        # Mock client (should not be called for skipped prompts)
        mock_client = MagicMock()
        mock_get_client.return_value = (mock_client, "test-model")

        feedback_prompts = [
            {
                "name": "Ship Status",
                "tokens_for_ai": "Report ship destruction",
                "metadata_filter": ["user_sunk_ship", "ai_sunk_ship"],
                "skip_condition": "all_null"
            }
        ]

        # Test with all null values - should skip
        metadata_all_null = {
            "user_sunk_ship": None,
            "ai_sunk_ship": None
        }

        feedback_messages = provide_feedback_prompts(
            {},
            "test",
            "Question?", 
            feedback_prompts,
            "response",
            "English",
            "user",
            json.dumps(metadata_all_null),
            json.dumps({}),
            ""
        )

        # Should be empty (prompt was skipped)
        self.assertEqual(len(feedback_messages), 0)
        # Client should not have been called
        self.assertEqual(mock_client.chat.completions.create.call_count, 0)

    @patch("app.get_openai_client_and_model")
    def test_skip_condition_all_null_with_values(self, mock_get_client):
        """Test skip_condition 'all_null' does NOT skip when values exist"""
        try:
            from app import provide_feedback_prompts
        except ImportError:
            self.skipTest("app module not available for testing")

        # Mock client to return valid response
        mock_client = MagicMock()
        mock_completion = MagicMock()
        mock_completion.choices[0].message.content = "Ship destroyed!"
        mock_client.chat.completions.create.return_value = mock_completion
        mock_get_client.return_value = (mock_client, "test-model")

        feedback_prompts = [
            {
                "name": "Ship Status",
                "tokens_for_ai": "Report ship destruction",
                "metadata_filter": ["user_sunk_ship", "ai_sunk_ship"],
                "skip_condition": "all_null"
            }
        ]

        # Test with actual values - should NOT skip
        metadata_with_values = {
            "user_sunk_ship": "Destroyer",
            "ai_sunk_ship": None
        }

        feedback_messages = provide_feedback_prompts(
            {},
            "test",
            "Question?",
            feedback_prompts, 
            "response",
            "English",
            "user",
            json.dumps(metadata_with_values),
            json.dumps({}),
            ""
        )

        # Should have feedback (prompt was NOT skipped)
        self.assertEqual(len(feedback_messages), 1)
        self.assertEqual(feedback_messages[0]["name"], "Ship Status")
        self.assertEqual(feedback_messages[0]["content"], "Ship destroyed!")
        # Client should have been called
        self.assertEqual(mock_client.chat.completions.create.call_count, 1)

    @patch("app.get_openai_client_and_model")
    def test_skip_condition_all_false(self, mock_get_client):
        """Test skip_condition 'all_false' skips when all metadata values are False"""
        try:
            from app import provide_feedback_prompts
        except ImportError:
            self.skipTest("app module not available for testing")

        mock_client = MagicMock()
        mock_get_client.return_value = (mock_client, "test-model")

        feedback_prompts = [
            {
                "name": "Game Over",
                "tokens_for_ai": "Report game over",
                "metadata_filter": ["game_over", "user_wins", "ai_wins"],
                "skip_condition": "all_false"
            }
        ]

        # Test with all false values - should skip
        metadata_all_false = {
            "game_over": False,
            "user_wins": False,
            "ai_wins": False
        }

        feedback_messages = provide_feedback_prompts(
            {},
            "test",
            "Question?",
            feedback_prompts,
            "response", 
            "English",
            "user",
            json.dumps(metadata_all_false),
            json.dumps({}),
            ""
        )

        # Should be empty (prompt was skipped)
        self.assertEqual(len(feedback_messages), 0)
        self.assertEqual(mock_client.chat.completions.create.call_count, 0)

    @patch("app.get_openai_client_and_model")
    def test_skip_condition_all_true(self, mock_get_client):
        """Test skip_condition 'all_true' skips when all metadata values are True"""
        try:
            from app import provide_feedback_prompts
        except ImportError:
            self.skipTest("app module not available for testing")

        mock_client = MagicMock()
        mock_get_client.return_value = (mock_client, "test-model")

        feedback_prompts = [
            {
                "name": "All True Test",
                "tokens_for_ai": "Test prompt",
                "metadata_filter": ["flag1", "flag2", "flag3"],
                "skip_condition": "all_true"
            }
        ]

        # Test with all true values - should skip
        metadata_all_true = {
            "flag1": True,
            "flag2": True, 
            "flag3": True
        }

        feedback_messages = provide_feedback_prompts(
            {},
            "test",
            "Question?",
            feedback_prompts,
            "response",
            "English", 
            "user",
            json.dumps(metadata_all_true),
            json.dumps({}),
            ""
        )

        # Should be empty (prompt was skipped)
        self.assertEqual(len(feedback_messages), 0)
        self.assertEqual(mock_client.chat.completions.create.call_count, 0)

    @patch("app.get_openai_client_and_model")
    def test_skip_condition_mixed_values(self, mock_get_client):
        """Test skip_condition does NOT skip when values are mixed"""
        try:
            from app import provide_feedback_prompts
        except ImportError:
            self.skipTest("app module not available for testing")

        # Mock client to return valid response
        mock_client = MagicMock()
        mock_completion = MagicMock()
        mock_completion.choices[0].message.content = "Mixed values feedback"
        mock_client.chat.completions.create.return_value = mock_completion
        mock_get_client.return_value = (mock_client, "test-model")

        feedback_prompts = [
            {
                "name": "Mixed Test",
                "tokens_for_ai": "Mixed test prompt", 
                "metadata_filter": ["val1", "val2", "val3"],
                "skip_condition": "all_false"
            }
        ]

        # Test with mixed values - should NOT skip
        metadata_mixed = {
            "val1": False,
            "val2": True,  # Mixed with False - should NOT skip
            "val3": False
        }

        feedback_messages = provide_feedback_prompts(
            {},
            "test",
            "Question?",
            feedback_prompts,
            "response",
            "English",
            "user", 
            json.dumps(metadata_mixed),
            json.dumps({}),
            ""
        )

        # Should have feedback (prompt was NOT skipped due to mixed values)
        self.assertEqual(len(feedback_messages), 1)
        self.assertEqual(feedback_messages[0]["name"], "Mixed Test")
        self.assertEqual(feedback_messages[0]["content"], "Mixed values feedback")
        self.assertEqual(mock_client.chat.completions.create.call_count, 1)

    @patch("app.get_openai_client_and_model")
    def test_skip_condition_battleship_scenario(self, mock_get_client):
        """Test the real battleship scenario that was causing hallucinations"""
        try:
            from app import provide_feedback_prompts
        except ImportError:
            self.skipTest("app module not available for testing")

        mock_client = MagicMock()
        mock_get_client.return_value = (mock_client, "test-model")

        feedback_prompts = [
            {
                "name": "Shot Report",
                "tokens_for_ai": "Report shot results",
                "metadata_filter": ["user_shot", "ai_shot", "user_hit_result", "ai_hit_result"]
                # No skip condition - always runs
            },
            {
                "name": "Ship Status", 
                "tokens_for_ai": "Report ship destruction",
                "metadata_filter": ["user_sunk_ship_this_round", "ai_sunk_ship_this_round"],
                "skip_condition": "all_null"  # Skip when no ships sunk
            },
            {
                "name": "Game Over",
                "tokens_for_ai": "Report game over",
                "metadata_filter": ["game_over", "user_wins", "ai_wins"],
                "skip_condition": "all_false"  # Skip when game not over
            }
        ]

        # Real scenario: shots taken, no ships sunk, game continues
        real_battleship_metadata = {
            "user_shot": 23,
            "ai_shot": 46, 
            "user_hit_result": "hit",
            "ai_hit_result": "hit",
            "user_sunk_ship_this_round": None,  # No ship sunk
            "ai_sunk_ship_this_round": None,    # No ship sunk  
            "game_over": False,
            "user_wins": False,
            "ai_wins": False
        }

        # Mock only Shot Report response (others should be skipped)
        mock_completion = MagicMock()
        mock_completion.choices[0].message.content = "🎯 Your shot at 23: hit! AI shot at 46: hit!"
        mock_client.chat.completions.create.return_value = mock_completion

        feedback_messages = provide_feedback_prompts(
            {},
            "test",
            "Choose position",
            feedback_prompts,
            "23",
            "English",
            "user",
            json.dumps(real_battleship_metadata),
            json.dumps({}),
            ""
        )

        # Should only have Shot Report (other two skipped)
        self.assertEqual(len(feedback_messages), 1)
        self.assertEqual(feedback_messages[0]["name"], "Shot Report")
        self.assertIn("🎯", feedback_messages[0]["content"])
        
        # Only one API call should have been made (Ship Status and Game Over skipped)
        self.assertEqual(mock_client.chat.completions.create.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
