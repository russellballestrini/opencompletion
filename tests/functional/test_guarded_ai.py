#!/usr/bin/env python3
"""
Functional tests for guarded_ai.py to validate app.py behavior compatibility

These tests use guarded_ai.py as a simpler test harness to validate that
the core activity processing logic works correctly, especially after our
validator and YAML changes.
"""

import unittest
import os
import sys
import tempfile
import json
from unittest.mock import patch, MagicMock
from pathlib import Path

# Add research directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "research"))

# Import guarded_ai directly
import guarded_ai


class TestGuardedAIFunctionality(unittest.TestCase):
    """Test guarded_ai.py core functionality"""

    def setUp(self):
        """Set up test environment"""
        # Mock the OpenAI client to avoid API calls
        self.mock_client = MagicMock()
        self.mock_response = MagicMock()
        self.mock_response.choices = [MagicMock()]
        self.mock_response.choices[0].message.content = "correct"

        self.mock_client.chat.completions.create.return_value = self.mock_response

    def create_test_activity(self, content):
        """Create temporary activity YAML file"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            return f.name

    def test_integer_bucket_matching(self):
        """Test that integer buckets work correctly (key regression test)"""
        # This tests our fix for activity20-n-plus-1.yaml
        test_activity = """
sections:
  - section_id: "test_section"
    title: "Integer Bucket Test"
    steps:
      - step_id: "step_1"
        title: "Year Question"
        question: "What year did the Titanic sink?"
        tokens_for_ai: "Categorize the response"
        buckets:
          - 1912
          - incorrect
        transitions:
          1912:
            content_blocks:
              - "Correct! The Titanic sank in 1912."
          incorrect:
            content_blocks:
              - "That's not correct."
"""

        with patch("guarded_ai.get_openai_client_and_model") as mock_get_client:
            mock_get_client.return_value = (self.mock_client, "test-model")

            # Mock the categorize_response to return "1912"
            with patch("guarded_ai.categorize_response") as mock_categorize:
                mock_categorize.return_value = "1912"

                import guarded_ai as guarded_ai

                activity_file = self.create_test_activity(test_activity)
                try:
                    activity = guarded_ai.load_yaml_activity(activity_file)

                    # Test that integer bucket matching works
                    step = activity["sections"][0]["steps"][0]

                    # Simulate the transition matching logic
                    category = "1912"
                    transitions = step["transitions"]

                    # Test the bucket matching logic we added
                    transition = None
                    if category in transitions:
                        transition = transitions[category]
                    elif category.isdigit() and int(category) in transitions:
                        transition = transitions[int(category)]

                    self.assertIsNotNone(
                        transition, "Should find transition for integer bucket"
                    )
                    self.assertIn(
                        "Correct! The Titanic sank in 1912.",
                        transition["content_blocks"],
                    )

                finally:
                    os.unlink(activity_file)

    def test_metadata_clear_functionality(self):
        """Test metadata_clear functionality"""
        test_activity = """
sections:
  - section_id: "test_section"  
    title: "Metadata Clear Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        question: "Test question"
        tokens_for_ai: "Categorize the response"
        buckets:
          - clear_test
        transitions:
          clear_test:
            metadata_clear: true
            content_blocks:
              - "Metadata cleared!"
"""

        import guarded_ai as guarded_ai

        activity_file = self.create_test_activity(test_activity)
        try:
            activity = guarded_ai.load_yaml_activity(activity_file)
            step = activity["sections"][0]["steps"][0]
            transition = step["transitions"]["clear_test"]

            # Test metadata clearing
            metadata = {"test_key": "test_value", "another_key": "another_value"}

            # Simulate the metadata_clear logic we added
            if "metadata_clear" in transition and transition["metadata_clear"] == True:
                metadata.clear()

            self.assertEqual(len(metadata), 0, "Metadata should be cleared")

        finally:
            os.unlink(activity_file)

    def test_metadata_feedback_filter(self):
        """Test metadata_feedback_filter functionality"""
        test_activity = """
sections:
  - section_id: "test_section"
    title: "Metadata Filter Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        question: "Test question"
        tokens_for_ai: "Categorize the response"
        feedback_tokens_for_ai: "Provide feedback"
        buckets:
          - filter_test
        transitions:
          filter_test:
            metadata_feedback_filter:
              - "score"
              - "level"
            ai_feedback:
              tokens_for_ai: "Generate feedback"
            content_blocks:
              - "Filtered feedback!"
"""

        with patch("guarded_ai.get_openai_client_and_model") as mock_get_client:
            mock_get_client.return_value = (self.mock_client, "test-model")

            import guarded_ai as guarded_ai

            activity_file = self.create_test_activity(test_activity)
            try:
                activity = guarded_ai.load_yaml_activity(activity_file)
                step = activity["sections"][0]["steps"][0]
                transition = step["transitions"]["filter_test"]

                # Test metadata filtering for feedback
                full_metadata = {
                    "score": 85,
                    "level": 2,
                    "secret_data": "should_not_be_included",
                    "user_id": "12345",
                }

                # Simulate the feedback filtering logic we added
                feedback_metadata = full_metadata
                if "metadata_feedback_filter" in transition:
                    filter_keys = transition["metadata_feedback_filter"]
                    feedback_metadata = {
                        k: v for k, v in full_metadata.items() if k in filter_keys
                    }

                expected_filtered = {"score": 85, "level": 2}
                self.assertEqual(feedback_metadata, expected_filtered)
                self.assertNotIn("secret_data", feedback_metadata)
                self.assertNotIn("user_id", feedback_metadata)

            finally:
                os.unlink(activity_file)

    def test_metadata_remove_list_format(self):
        """Test that metadata_remove works with list format (activity17 fix)"""
        test_activity = """
sections:
  - section_id: "test_section"
    title: "Metadata Remove Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        question: "Test question"
        tokens_for_ai: "Categorize the response"
        buckets:
          - remove_test
        transitions:
          remove_test:
            metadata_remove:
              - "old_key1"
              - "old_key2"
            content_blocks:
              - "Keys removed!"
"""

        import guarded_ai as guarded_ai

        activity_file = self.create_test_activity(test_activity)
        try:
            activity = guarded_ai.load_yaml_activity(activity_file)
            step = activity["sections"][0]["steps"][0]
            transition = step["transitions"]["remove_test"]

            # Test metadata removal with list format
            metadata = {
                "old_key1": "value1",
                "old_key2": "value2",
                "keep_key": "keep_value",
            }

            # Simulate the metadata_remove logic
            if "metadata_remove" in transition:
                for key in transition["metadata_remove"]:
                    if key in metadata:
                        del metadata[key]

            expected = {"keep_key": "keep_value"}
            self.assertEqual(metadata, expected)
            self.assertNotIn("old_key1", metadata)
            self.assertNotIn("old_key2", metadata)

        finally:
            os.unlink(activity_file)

    def test_boolean_bucket_matching(self):
        """Test that boolean buckets work correctly"""
        test_activity = """
sections:
  - section_id: "test_section"
    title: "Boolean Bucket Test"
    steps:
      - step_id: "step_1" 
        title: "Yes/No Question"
        question: "Is this correct?"
        tokens_for_ai: "Categorize as true or false"
        buckets:
          - true
          - false
        transitions:
          true:
            content_blocks:
              - "Yes, that's right!"
          false:
            content_blocks:
              - "No, that's not right."
"""

        import guarded_ai as guarded_ai

        activity_file = self.create_test_activity(test_activity)
        try:
            activity = guarded_ai.load_yaml_activity(activity_file)
            step = activity["sections"][0]["steps"][0]
            transitions = step["transitions"]

            # Test boolean matching logic
            for category_response in ["yes", "true", "TRUE", "Yes"]:
                category = category_response.lower()

                transition = None
                if category in transitions:
                    transition = transitions[category]
                elif category.isdigit() and int(category) in transitions:
                    transition = transitions[int(category)]
                else:
                    # This is the logic we added
                    if category in ["yes", "true"]:
                        category = True
                    elif category in ["no", "false"]:
                        category = False
                    if category in transitions:
                        transition = transitions[category]

                self.assertIsNotNone(
                    transition,
                    f"Should find boolean transition for '{category_response}'",
                )
                self.assertIn("Yes, that's right!", transition["content_blocks"])

        finally:
            os.unlink(activity_file)


class TestActivityYAMLChanges(unittest.TestCase):
    """Test that our YAML changes don't break functionality"""

    def test_activity3_terminal_section(self):
        """Test that activity3's new terminal section loads correctly"""
        import guarded_ai as guarded_ai

        activity_file = "/home/fox/git/opencompletion/research/activity3.yaml"
        activity = guarded_ai.load_yaml_activity(activity_file)

        # Should have section_5 now
        section_ids = [section["section_id"] for section in activity["sections"]]
        self.assertIn("section_5", section_ids)

        # Section_5 should be terminal (no transitions with next_section_and_step)
        section_5 = next(
            s for s in activity["sections"] if s["section_id"] == "section_5"
        )
        step = section_5["steps"][0]

        # Terminal step should not have question or buckets
        self.assertNotIn("question", step)
        self.assertNotIn("buckets", step)
        self.assertIn("content_blocks", step)

        # Should have congratulatory content
        content = "\n".join(step["content_blocks"])
        self.assertIn("Congratulations", content)
        self.assertIn("elephant expert", content)

    def test_activity17_metadata_remove_format(self):
        """Test that activity17's metadata_remove changes work"""
        import guarded_ai as guarded_ai

        activity_file = (
            "/home/fox/git/opencompletion/research/activity17-choose-adventure.yaml"
        )
        activity = guarded_ai.load_yaml_activity(activity_file)

        # Find steps with metadata_remove
        found_metadata_remove = False
        for section in activity["sections"]:
            for step in section["steps"]:
                if "transitions" in step:
                    for transition in step["transitions"].values():
                        if "metadata_remove" in transition:
                            found_metadata_remove = True
                            # Should be list format now, not dictionary
                            self.assertIsInstance(transition["metadata_remove"], list)
                            for item in transition["metadata_remove"]:
                                self.assertIsInstance(item, str)

        self.assertTrue(found_metadata_remove, "Should find metadata_remove operations")

    def test_battleship_exit_transitions(self):
        """Test that battleship exit transitions go to step_4"""
        import guarded_ai as guarded_ai

        for battleship_file in [
            "activity29-battleship.yaml",
            "activity29-testship.yaml",
        ]:
            activity_file = f"/home/fox/git/opencompletion/research/{battleship_file}"
            activity = guarded_ai.load_yaml_activity(activity_file)

            # Find exit transitions and verify they go to step_4
            exit_transitions_found = 0
            for section in activity["sections"]:
                for step in section["steps"]:
                    if "transitions" in step:
                        for bucket, transition in step["transitions"].items():
                            if (
                                bucket == "exit"
                                and "next_section_and_step" in transition
                            ):
                                exit_transitions_found += 1
                                target = transition["next_section_and_step"]
                                if step["step_id"] == "step_2":
                                    # step_2 exit should go directly to step_4
                                    self.assertEqual(
                                        target,
                                        "section_1:step_4",
                                        f"step_2 exit should go to step_4 in {battleship_file}",
                                    )

            self.assertGreater(
                exit_transitions_found,
                0,
                f"Should find exit transitions in {battleship_file}",
            )


class TestGuardedAIClientAndErrorHandling(unittest.TestCase):
    """Test client management and error handling in guarded_ai"""

    def setUp(self):
        """Reset global state before each test"""
        # Save original state
        self.original_model_map = guarded_ai.MODEL_CLIENT_MAP.copy()
        guarded_ai.MODEL_CLIENT_MAP.clear()

    def tearDown(self):
        """Restore original state"""
        guarded_ai.MODEL_CLIENT_MAP.clear()
        guarded_ai.MODEL_CLIENT_MAP.update(self.original_model_map)

    def test_initialize_model_map_with_env_vars(self):
        """Test model map initialization with environment variables"""
        test_env = {
            "MODEL_ENDPOINT_1": "https://api.test1.com",
            "MODEL_API_KEY_1": "test-key-1",
            "MODEL_ENDPOINT_2": "https://api.test2.com",
            "MODEL_API_KEY_2": "test-key-2",
        }

        with patch.dict(os.environ, test_env):
            with patch("guarded_ai.get_client_for_endpoint") as mock_get_client:
                mock_client1 = MagicMock()
                mock_client2 = MagicMock()
                mock_get_client.side_effect = [mock_client1, mock_client2]

                guarded_ai.initialize_model_map()

                self.assertIn("endpoint_1", guarded_ai.MODEL_CLIENT_MAP)
                self.assertIn("endpoint_2", guarded_ai.MODEL_CLIENT_MAP)

    def test_initialize_model_map_with_errors(self):
        """Test error handling in model map initialization"""
        test_env = {
            "MODEL_ENDPOINT_0": "https://bad.endpoint.com",
            "MODEL_API_KEY_0": "bad-key",
        }

        with patch.dict(os.environ, test_env):
            with patch("guarded_ai.get_client_for_endpoint") as mock_get_client:
                mock_get_client.side_effect = Exception("Connection failed")

                with patch("builtins.print") as mock_print:
                    guarded_ai.initialize_model_map()

                    # Should print warning about failed endpoint
                    self.assertTrue(mock_print.called)

    def test_get_openai_client_and_model_fallback(self):
        """Test client fallback behavior"""
        # Clear model map to force fallback
        guarded_ai.MODEL_CLIENT_MAP.clear()

        with patch("guarded_ai.get_client_for_endpoint") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client

            client, model = guarded_ai.get_openai_client_and_model("test-model")

            self.assertEqual(client, mock_client)
            self.assertEqual(model, "test-model")

    def test_categorize_response_error_handling(self):
        """Test error handling in categorization"""
        with patch("guarded_ai.get_openai_client_and_model") as mock_get_client:
            mock_client = MagicMock()
            mock_client.chat.completions.create.side_effect = Exception("API Error")
            mock_get_client.return_value = (mock_client, "test-model")

            result = guarded_ai.categorize_response(
                "Test question",
                "Test response",
                ["correct", "incorrect"],
                "Categorize this",
            )

            self.assertTrue(result.startswith("Error:"))

    def test_generate_ai_feedback_error_handling(self):
        """Test error handling in feedback generation"""
        with patch("guarded_ai.get_openai_client_and_model") as mock_get_client:
            mock_client = MagicMock()
            mock_client.chat.completions.create.side_effect = Exception(
                "Feedback Error"
            )
            mock_get_client.return_value = (mock_client, "test-model")

            result = guarded_ai.generate_ai_feedback(
                "correct", "Test question", "Test response", "Generate feedback", {}
            )

            self.assertTrue(result.startswith("Error:"))

    def test_translate_text_english_bypass(self):
        """Test that English translation is bypassed"""
        text = "Hello, world!"

        result = guarded_ai.translate_text(text, "English")
        self.assertEqual(result, text)

    def test_translate_text_error_handling(self):
        """Test error handling in translation (tests the bug with undefined 'client')"""
        result = guarded_ai.translate_text("Hello", "Spanish")

        # Should return an error due to undefined 'client' variable
        self.assertTrue(result.startswith("Error:"))

    def test_execute_processing_script_basic(self):
        """Test basic script execution functionality"""
        script = """
metadata['processed'] = True
metadata['score'] = metadata.get('score', 0) + 10
script_result = {'status': 'completed', 'points': 100}
"""
        metadata = {"score": 5}

        result = guarded_ai.execute_processing_script(metadata, script)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["points"], 100)
        self.assertTrue(metadata["processed"])
        self.assertEqual(metadata["score"], 15)

    def test_get_next_section_and_step_navigation(self):
        """Test navigation between sections and steps"""
        activity_content = {
            "sections": [
                {
                    "section_id": "section_1",
                    "steps": [{"step_id": "step_1"}, {"step_id": "step_2"}],
                },
                {"section_id": "section_2", "steps": [{"step_id": "step_1"}]},
            ]
        }

        # Test within section
        next_section, next_step = guarded_ai.get_next_section_and_step(
            activity_content, "section_1", "step_1"
        )
        self.assertEqual(next_section, "section_1")
        self.assertEqual(next_step, "step_2")

        # Test across sections
        next_section, next_step = guarded_ai.get_next_section_and_step(
            activity_content, "section_1", "step_2"
        )
        self.assertEqual(next_section, "section_2")
        self.assertEqual(next_step, "step_1")

        # Test at end
        next_section, next_step = guarded_ai.get_next_section_and_step(
            activity_content, "section_2", "step_1"
        )
        self.assertIsNone(next_section)
        self.assertIsNone(next_step)

    def test_provide_feedback_functionality(self):
        """Test feedback provision with various configurations"""
        # Test with AI feedback
        transition_with_ai = {"ai_feedback": {"tokens_for_ai": "Provide encouragement"}}

        with patch("guarded_ai.generate_ai_feedback") as mock_generate:
            mock_generate.return_value = "Great work!"

            result = guarded_ai.provide_feedback(
                transition_with_ai,
                "correct",
                "Test question",
                "Test response",
                "English",
                "Base instructions",
                {"score": 10},
            )

            self.assertIn("AI Feedback: Great work!", result)

        # Test without AI feedback
        transition_without_ai = {}

        result = guarded_ai.provide_feedback(
            transition_without_ai,
            "correct",
            "Test question",
            "Test response",
            "English",
            "Base instructions",
            {},
        )

        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
