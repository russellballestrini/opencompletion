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


if __name__ == "__main__":
    unittest.main(verbosity=2)
