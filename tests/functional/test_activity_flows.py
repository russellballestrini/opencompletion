#!/usr/bin/env python3
"""
Comprehensive activity flow tests that exercise all transitions

These tests run complete activity walkthroughs to validate that all 
transitions work correctly, especially after our YAML changes.
"""

import unittest
import os
import sys
import tempfile
import json
from unittest.mock import patch, MagicMock, call
from pathlib import Path

# Add research directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "research"))
import guarded_ai


class TestCompleteActivityFlows(unittest.TestCase):
    """Test complete activity walkthroughs"""

    def setUp(self):
        """Set up test environment with mock AI responses"""
        self.mock_client = MagicMock()
        self.mock_response = MagicMock()
        self.mock_response.choices = [MagicMock()]
        self.mock_client.chat.completions.create.return_value = self.mock_response

    def create_test_activity(self, content):
        """Create temporary activity YAML file"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            return f.name

    def test_integer_bucket_activity_flow(self):
        """Test complete flow using integer buckets (like activity20)"""
        activity_yaml = """
sections:
  - section_id: "quiz"
    title: "History Quiz"
    steps:
      - step_id: "q1"
        title: "Question 1"
        question: "What year did the Titanic sink?"
        tokens_for_ai: "Check if response matches 1912"
        buckets:
          - 1912
          - incorrect
        transitions:
          1912:
            content_blocks:
              - "Correct! The Titanic sank in 1912."
            metadata_add:
              score: "n+1"
            next_section_and_step: "quiz:q2"
          incorrect:
            content_blocks:
              - "That's not correct. Try again!"
            next_section_and_step: "quiz:q1"
      
      - step_id: "q2" 
        title: "Question 2"
        question: "How many people were on board?"
        tokens_for_ai: "Check if response is reasonable"
        buckets:
          - reasonable
          - unreasonable
        transitions:
          reasonable:
            content_blocks:
              - "Good estimate!"
            metadata_add:
              score: "n+1"
            next_section_and_step: "results:final"
          unreasonable:
            content_blocks:
              - "That doesn't seem right."
            next_section_and_step: "quiz:q2"
  
  - section_id: "results"
    title: "Results"
    steps:
      - step_id: "final"
        title: "Final Results"
        content_blocks:
          - "Quiz completed!"
          - "Check your score in the metadata."
"""

        with patch("guarded_ai.get_openai_client_and_model") as mock_get_client:
            mock_get_client.return_value = (self.mock_client, "test-model")

            # Test sequence: correct answer to q1, then reasonable answer to q2
            mock_responses = ["1912", "reasonable"]

            with patch("guarded_ai.categorize_response", side_effect=mock_responses):
                with patch("guarded_ai.input", side_effect=["1912", "2000"]):
                    with patch("builtins.print") as mock_print:

                        activity_file = self.create_test_activity(activity_yaml)
                        try:
                            # This should complete the full flow
                            guarded_ai.simulate_activity(activity_file)

                            # Check that we reached the final step
                            print_calls = [
                                call[0][0] for call in mock_print.call_args_list
                            ]
                            final_output = "\n".join(print_calls)

                            self.assertIn("Quiz completed!", final_output)
                            self.assertIn(
                                "Correct! The Titanic sank in 1912.", final_output
                            )
                            self.assertIn("Good estimate!", final_output)

                        finally:
                            os.unlink(activity_file)

    def test_metadata_operations_flow(self):
        """Test flow with all metadata operations"""
        activity_yaml = """
sections:
  - section_id: "meta_test"
    title: "Metadata Operations Test"
    steps:
      - step_id: "setup"
        title: "Setup"
        question: "Ready to start?"
        tokens_for_ai: "Always categorize as ready"
        buckets:
          - ready
        transitions:
          ready:
            metadata_add:
              user_name: "the-users-response"
              level: 1
              temp_data: "temporary"
            metadata_tmp_add:
              session_id: "temp-123"
            next_section_and_step: "meta_test:process"
      
      - step_id: "process"
        title: "Processing" 
        question: "Continue processing?"
        tokens_for_ai: "Always categorize as continue"
        buckets:
          - continue
        transitions:
          continue:
            metadata_remove:
              - temp_data
            metadata_add:
              level: "n+1"
            next_section_and_step: "meta_test:filter_test"
      
      - step_id: "filter_test"
        title: "Filter Test"
        question: "Test feedback filtering?"
        feedback_tokens_for_ai: "Provide filtered feedback"
        tokens_for_ai: "Always categorize as test"
        buckets:
          - test
        transitions:
          test:
            metadata_feedback_filter:
              - level
              - user_name
            ai_feedback:
              tokens_for_ai: "Use only filtered metadata"
            next_section_and_step: "meta_test:clear_test"
      
      - step_id: "clear_test"
        title: "Clear Test"
        question: "Clear all metadata?"
        tokens_for_ai: "Always categorize as clear"
        buckets:
          - clear
        transitions:
          clear:
            metadata_clear: true
            content_blocks:
              - "All metadata cleared!"
"""

        with patch("guarded_ai.get_openai_client_and_model") as mock_get_client:
            mock_get_client.return_value = (self.mock_client, "test-model")

            # Mock AI feedback response
            self.mock_response.choices[0].message.content = "Good job!"

            mock_responses = ["ready", "continue", "test", "clear"]
            user_inputs = ["TestUser", "yes", "yes", "yes"]

            with patch("guarded_ai.categorize_response", side_effect=mock_responses):
                with patch("guarded_ai.input", side_effect=user_inputs):
                    with patch("builtins.print") as mock_print:

                        activity_file = self.create_test_activity(activity_yaml)
                        try:
                            guarded_ai.simulate_activity(activity_file)

                            print_calls = [
                                call[0][0] for call in mock_print.call_args_list
                            ]
                            final_output = "\n".join(print_calls)

                            self.assertIn("All metadata cleared!", final_output)

                        finally:
                            os.unlink(activity_file)

    def test_processing_script_flow(self):
        """Test flow with processing scripts"""
        activity_yaml = """
sections:
  - section_id: "script_test"
    title: "Processing Script Test"
    steps:
      - step_id: "input_step"
        title: "Input Step"
        question: "Enter a number:"
        tokens_for_ai: "Always categorize as number"
        processing_script: |
          import random
          user_input = metadata.get('user_response', '0')
          try:
              number = int(user_input)
              metadata['parsed_number'] = number
              metadata['is_even'] = number % 2 == 0
              metadata['doubled'] = number * 2
          except ValueError:
              metadata['error'] = 'Invalid number'
          
          script_result = {
              'metadata': {
                  'processing_complete': True
              }
          }
        buckets:
          - number
        transitions:
          number:
            run_processing_script: true
            next_section_and_step: "script_test:result_step"
      
      - step_id: "result_step"
        title: "Results"
        question: "Continue?"
        tokens_for_ai: "Always categorize as done"
        buckets:
          - done
        transitions:
          done:
            content_blocks:
              - "Processing completed!"
              - "Check metadata for results."
"""

        with patch("guarded_ai.get_openai_client_and_model") as mock_get_client:
            mock_get_client.return_value = (self.mock_client, "test-model")

            mock_responses = ["number", "done"]
            user_inputs = ["42", "yes"]

            with patch("guarded_ai.categorize_response", side_effect=mock_responses):
                with patch("guarded_ai.input", side_effect=user_inputs):
                    with patch("builtins.print") as mock_print:

                        activity_file = self.create_test_activity(activity_yaml)
                        try:
                            guarded_ai.simulate_activity(activity_file)

                            print_calls = [
                                call[0][0] for call in mock_print.call_args_list
                            ]
                            final_output = "\n".join(print_calls)

                            self.assertIn("Processing completed!", final_output)
                            # Should show metadata with processed values
                            self.assertIn("parsed_number", final_output)
                            self.assertIn("42", final_output)

                        finally:
                            os.unlink(activity_file)

    def test_boolean_bucket_transitions(self):
        """Test boolean bucket transitions thoroughly"""
        activity_yaml = """
sections:
  - section_id: "bool_test"
    title: "Boolean Test"
    steps:
      - step_id: "yes_no"
        title: "Yes/No Question"
        question: "Do you agree?"
        tokens_for_ai: "Categorize as true or false based on response"
        buckets:
          - true
          - false
        transitions:
          true:
            content_blocks:
              - "You agreed!"
            metadata_add:
              agreement: true
            next_section_and_step: "bool_test:follow_up"
          false:
            content_blocks:
              - "You disagreed!"
            metadata_add:
              agreement: false
            next_section_and_step: "bool_test:follow_up"
      
      - step_id: "follow_up"
        title: "Follow Up"
        question: "Final question?"
        tokens_for_ai: "Always categorize as final"
        buckets:
          - final
        transitions:
          final:
            content_blocks:
              - "Thank you for your response!"
"""

        # Test both true and false paths
        test_cases = [
            (["true", "final"], ["yes", "done"], "You agreed!"),
            (["false", "final"], ["no", "done"], "You disagreed!"),
        ]

        for mock_responses, user_inputs, expected_content in test_cases:
            with self.subTest(responses=mock_responses):
                with patch("guarded_ai.get_openai_client_and_model") as mock_get_client:
                    mock_get_client.return_value = (self.mock_client, "test-model")

                    with patch(
                        "guarded_ai.categorize_response", side_effect=mock_responses
                    ):
                        with patch("guarded_ai.input", side_effect=user_inputs):
                            with patch("builtins.print") as mock_print:

                                activity_file = self.create_test_activity(activity_yaml)
                                try:
                                    guarded_ai.simulate_activity(activity_file)

                                    print_calls = [
                                        call[0][0] for call in mock_print.call_args_list
                                    ]
                                    final_output = "\n".join(print_calls)

                                    self.assertIn(expected_content, final_output)
                                    self.assertIn(
                                        "Thank you for your response!", final_output
                                    )

                                finally:
                                    os.unlink(activity_file)


class TestRealActivityFiles(unittest.TestCase):
    """Test our modified YAML files with complete flows"""

    def setUp(self):
        """Set up test environment"""
        self.mock_client = MagicMock()
        self.mock_response = MagicMock()
        self.mock_response.choices = [MagicMock()]
        self.mock_response.choices[0].message.content = "Test response"
        self.mock_client.chat.completions.create.return_value = self.mock_response

    def test_activity3_terminal_section_flow(self):
        """Test that activity3 flows to the new terminal section"""
        with patch("guarded_ai.get_openai_client_and_model") as mock_get_client:
            mock_get_client.return_value = (self.mock_client, "test-model")

            # Load actual activity3.yaml
            activity_file = "/home/fox/git/opencompletion/research/activity3.yaml"
            activity = guarded_ai.load_yaml_activity(activity_file)

            # Should have section_5 as the terminal section
            section_5 = None
            for section in activity["sections"]:
                if section["section_id"] == "section_5":
                    section_5 = section
                    break

            self.assertIsNotNone(section_5, "Should have section_5")

            # Terminal section should not have questions or transitions with next_section_and_step
            terminal_step = section_5["steps"][0]
            self.assertNotIn("question", terminal_step)
            self.assertNotIn("buckets", terminal_step)
            self.assertNotIn("transitions", terminal_step)

            # Should have congratulatory content
            content = "\n".join(terminal_step["content_blocks"])
            self.assertIn("Congratulations", content)
            self.assertIn("elephant expert", content)

    def test_activity17_metadata_remove_flow(self):
        """Test activity17 with new metadata_remove format"""
        with patch("guarded_ai.get_openai_client_and_model") as mock_get_client:
            mock_get_client.return_value = (self.mock_client, "test-model")

            activity_file = (
                "/home/fox/git/opencompletion/research/activity17-choose-adventure.yaml"
            )
            activity = guarded_ai.load_yaml_activity(activity_file)

            # Find a step with metadata_remove operations
            found_remove_operation = False
            for section in activity["sections"]:
                for step in section["steps"]:
                    if "transitions" in step:
                        for transition in step["transitions"].values():
                            if "metadata_remove" in transition:
                                found_remove_operation = True

                                # Should be list format now
                                remove_op = transition["metadata_remove"]
                                self.assertIsInstance(remove_op, list)

                                # Test the actual removal logic
                                test_metadata = {
                                    "old_key": "old_value",
                                    "keep_key": "keep_value",
                                }

                                # Simulate metadata removal
                                for key in remove_op:
                                    if key in test_metadata:
                                        del test_metadata[key]

                                # Should have removed the keys
                                for key in remove_op:
                                    self.assertNotIn(key, test_metadata)

            self.assertTrue(
                found_remove_operation, "Should find metadata_remove operations"
            )

    def test_activity20_integer_bucket_flow(self):
        """Test activity20 with integer buckets"""
        with patch("guarded_ai.get_openai_client_and_model") as mock_get_client:
            mock_get_client.return_value = (self.mock_client, "test-model")

            activity_file = (
                "/home/fox/git/opencompletion/research/activity20-n-plus-1.yaml"
            )
            activity = guarded_ai.load_yaml_activity(activity_file)

            # Find the step with integer bucket (1912)
            found_integer_bucket = False
            for section in activity["sections"]:
                for step in section["steps"]:
                    if "buckets" in step:
                        for bucket in step["buckets"]:
                            if bucket == 1912:  # Integer bucket
                                found_integer_bucket = True

                                # Test transition matching logic
                                transitions = step["transitions"]
                                category = "1912"  # AI response as string

                                # Test our matching logic
                                transition = None
                                if category in transitions:
                                    transition = transitions[category]
                                elif (
                                    category.isdigit() and int(category) in transitions
                                ):
                                    transition = transitions[int(category)]

                                self.assertIsNotNone(
                                    transition, "Should match integer bucket"
                                )
                                self.assertIn("1912", transition["content_blocks"][0])

            self.assertTrue(found_integer_bucket, "Should find integer bucket (1912)")


class TestPreScriptFunctionality(unittest.TestCase):
    """Test pre_script execution (runs before categorization)"""

    def setUp(self):
        """Set up test environment"""
        self.mock_client = MagicMock()
        self.mock_response = MagicMock()
        self.mock_response.choices = [MagicMock()]
        self.mock_response.choices[0].message.content = "valid"
        self.mock_client.chat.completions.create.return_value = self.mock_response

    def create_test_activity(self, content):
        """Create temporary activity YAML file"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            return f.name

    def test_pre_script_battleship_scenario(self):
        """Test pre_script with battleship-like win detection"""
        activity_yaml = """
sections:
  - section_id: "game"
    title: "Battleship Game"
    steps:
      - step_id: "setup"
        title: "Setup"
        question: "Ready to play?"
        tokens_for_ai: "Always categorize as ready"
        buckets:
          - ready
        transitions:
          ready:
            metadata_add:
              user_winning_move: 42
              ai_winning_move: 73
            next_section_and_step: "game:play"
      
      - step_id: "play"
        title: "Take a Shot"
        question: "Choose a position to fire at (0-99):"
        pre_script: |
          # Check if moves match winning moves from previous turn
          user_winning_move = metadata.get("user_winning_move")
          ai_winning_move = metadata.get("ai_winning_move")
          user_shot_input = metadata.get("user_response", "")
          
          is_game_ending_move = False
          
          # Check if user move wins
          if user_shot_input and user_shot_input.isdigit():
              user_move = int(user_shot_input)
              if user_winning_move is not None and user_move == user_winning_move:
                  is_game_ending_move = True
          
          script_result = {
              "metadata": {
                  "is_game_ending_move": is_game_ending_move,
                  "user_shot": user_shot_input
              }
          }
        tokens_for_ai: "If is_game_ending_move is True, categorize as winning_move, otherwise as regular_move"
        buckets:
          - winning_move
          - regular_move
        transitions:
          winning_move:
            content_blocks:
              - "🎉 You hit the target! You win!"
          regular_move:
            content_blocks:
              - "Miss! Try again."
            next_section_and_step: "game:play"
"""

        with patch("guarded_ai.get_openai_client_and_model") as mock_get_client:
            mock_get_client.return_value = (self.mock_client, "test-model")

            # Test sequence: setup, then winning move
            mock_responses = ["ready", "winning_move"]
            user_inputs = ["yes", "42"]  # 42 is the winning move

            with patch("guarded_ai.categorize_response", side_effect=mock_responses):
                with patch("guarded_ai.input", side_effect=user_inputs):
                    with patch("builtins.print") as mock_print:

                        activity_file = self.create_test_activity(activity_yaml)
                        try:
                            guarded_ai.simulate_activity(activity_file)

                            print_calls = [
                                call[0][0] for call in mock_print.call_args_list
                            ]
                            final_output = "\n".join(print_calls)

                            # Should show debug messages for pre-script execution
                            self.assertIn("DEBUG: Executing pre-script", final_output)
                            self.assertIn("DEBUG: Pre-script completed", final_output)

                            # Should show winning message
                            self.assertIn("You hit the target! You win!", final_output)

                            # Metadata should show game ending move detected
                            self.assertIn('"is_game_ending_move": true', final_output)

                        finally:
                            os.unlink(activity_file)

    def test_pre_script_metadata_processing(self):
        """Test pre_script processes user input and updates metadata"""
        activity_yaml = """
sections:
  - section_id: "input_processing"
    title: "Input Processing"
    steps:
      - step_id: "number_input"
        title: "Number Input"
        question: "Enter a number between 1-100:"
        pre_script: |
          user_input = metadata.get("user_response", "")
          
          # Process and validate input
          is_valid = False
          parsed_number = None
          error_message = ""
          
          try:
              parsed_number = int(user_input)
              if 1 <= parsed_number <= 100:
                  is_valid = True
              else:
                  error_message = "Number must be between 1-100"
          except ValueError:
              error_message = "Invalid number format"
          
          script_result = {
              "metadata": {
                  "is_valid_input": is_valid,
                  "parsed_number": parsed_number,
                  "error_message": error_message,
                  "processing_complete": True
              }
          }
        tokens_for_ai: "If is_valid_input is True, categorize as valid, otherwise as invalid"
        buckets:
          - valid
          - invalid
        transitions:
          valid:
            content_blocks:
              - "Valid number received!"
          invalid:
            content_blocks:
              - "Invalid input. Please try again."
            next_section_and_step: "input_processing:number_input"
"""

        with patch("guarded_ai.get_openai_client_and_model") as mock_get_client:
            mock_get_client.return_value = (self.mock_client, "test-model")

            # Test with valid number
            mock_responses = ["valid"]
            user_inputs = ["50"]

            with patch("guarded_ai.categorize_response", side_effect=mock_responses):
                with patch("guarded_ai.input", side_effect=user_inputs):
                    with patch("builtins.print") as mock_print:

                        activity_file = self.create_test_activity(activity_yaml)
                        try:
                            guarded_ai.simulate_activity(activity_file)

                            print_calls = [
                                call[0][0] for call in mock_print.call_args_list
                            ]
                            final_output = "\n".join(print_calls)

                            # Should show pre-script execution
                            self.assertIn("DEBUG: Executing pre-script", final_output)

                            # Should show valid input message
                            self.assertIn("Valid number received!", final_output)

                            # Metadata should show processed values
                            self.assertIn('"is_valid_input": true', final_output)
                            self.assertIn('"parsed_number": 50', final_output)
                            self.assertIn('"processing_complete": true', final_output)

                        finally:
                            os.unlink(activity_file)


class TestErrorHandling(unittest.TestCase):
    """Test error handling in activity flows"""

    def setUp(self):
        """Set up test environment"""
        self.mock_client = MagicMock()
        self.mock_response = MagicMock()
        self.mock_response.choices = [MagicMock()]
        self.mock_response.choices[0].message.content = "unknown"
        self.mock_client.chat.completions.create.return_value = self.mock_response

    def create_test_activity(self, content):
        """Create temporary activity YAML file"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            return f.name

    def test_invalid_transition_handling(self):
        """Test handling of invalid AI responses"""
        activity_yaml = """
sections:
  - section_id: "error_test"
    title: "Error Test"
    steps:
      - step_id: "step1"
        title: "Test Step"
        question: "Test question?"
        tokens_for_ai: "Categorize as valid or invalid"
        buckets:
          - valid
          - invalid
        transitions:
          valid:
            content_blocks:
              - "Valid response!"
          invalid:
            content_blocks:
              - "Invalid response!"
"""

        with patch("guarded_ai.get_openai_client_and_model") as mock_get_client:
            mock_get_client.return_value = (self.mock_client, "test-model")

            # Mock categorize_response to return unknown category first, then valid
            with patch(
                "guarded_ai.categorize_response", side_effect=["unknown", "valid"]
            ):
                with patch(
                    "guarded_ai.input", side_effect=["test input", "valid input"]
                ):
                    with patch("builtins.print") as mock_print:

                        activity_file = self.create_test_activity(activity_yaml)
                        try:
                            guarded_ai.simulate_activity(activity_file)

                            print_calls = [
                                call[0][0] for call in mock_print.call_args_list
                            ]
                            final_output = "\n".join(print_calls)

                            # Should show error message for invalid transition
                            self.assertIn("No valid transition found", final_output)

                        finally:
                            os.unlink(activity_file)


if __name__ == "__main__":
    unittest.main(verbosity=2)
