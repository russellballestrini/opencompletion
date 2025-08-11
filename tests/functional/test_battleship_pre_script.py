#!/usr/bin/env python3
"""
Test that battleship pre_script functionality works with actual YAML files
"""

import unittest
import os
import sys
from unittest.mock import patch, MagicMock
from pathlib import Path

# Add research directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "research"))
import guarded_ai


class TestBattleshipPreScript(unittest.TestCase):
    """Test actual battleship YAML files with pre_script"""

    def setUp(self):
        """Set up test environment"""
        self.mock_client = MagicMock()
        self.mock_response = MagicMock()
        self.mock_response.choices = [MagicMock()]
        self.mock_response.choices[0].message.content = "Test response"
        self.mock_client.chat.completions.create.return_value = self.mock_response

    def test_battleship_yaml_has_pre_script(self):
        """Test that battleship YAML loads and has pre_script"""
        activity_file = (
            Path(__file__).parent.parent.parent
            / "research"
            / "activity29-battleship.yaml"
        )
        activity = guarded_ai.load_yaml_activity(str(activity_file))

        # Find step with pre_script
        found_pre_script = False
        pre_script_content = ""

        for section in activity["sections"]:
            for step in section["steps"]:
                if "pre_script" in step:
                    found_pre_script = True
                    pre_script_content = step["pre_script"]

                    # Should contain win detection logic
                    self.assertIn("user_winning_move", pre_script_content)
                    self.assertIn("ai_winning_move", pre_script_content)
                    self.assertIn("is_game_ending_move", pre_script_content)
                    self.assertIn("user_shot_input", pre_script_content)
                    break

            if found_pre_script:
                break

        self.assertTrue(found_pre_script, "Battleship YAML should have pre_script")

    def test_battleship_pre_script_execution_simulation(self):
        """Test simulated battleship pre_script execution"""
        activity_file = (
            Path(__file__).parent.parent.parent
            / "research"
            / "activity29-battleship.yaml"
        )
        activity = guarded_ai.load_yaml_activity(str(activity_file))

        # Find the step with pre_script (step_2)
        step_with_pre_script = None
        for section in activity["sections"]:
            for step in section["steps"]:
                if step.get("step_id") == "step_2" and "pre_script" in step:
                    step_with_pre_script = step
                    break

        self.assertIsNotNone(step_with_pre_script, "Should find step_2 with pre_script")

        # Test pre_script logic manually
        pre_script = step_with_pre_script["pre_script"]

        # Simulate metadata with winning move setup
        test_metadata = {
            "user_winning_move": 42,
            "ai_winning_move": 73,
            "user_response": "42",  # User enters winning move
        }

        # Execute the pre_script
        result = guarded_ai.execute_processing_script(test_metadata, pre_script)

        # Should detect winning move
        self.assertTrue(result.get("metadata", {}).get("is_game_ending_move", False))

        # Test with non-winning move
        test_metadata["user_response"] = "25"
        result = guarded_ai.execute_processing_script(test_metadata, pre_script)

        # Should NOT detect winning move
        self.assertFalse(result.get("metadata", {}).get("is_game_ending_move", False))

    def test_testship_yaml_has_pre_script(self):
        """Test that testship YAML also has pre_script"""
        activity_file = (
            Path(__file__).parent.parent.parent
            / "research"
            / "activity29-testship.yaml"
        )
        activity = guarded_ai.load_yaml_activity(str(activity_file))

        # Should also have pre_script (same structure as battleship)
        found_pre_script = False

        for section in activity["sections"]:
            for step in section["steps"]:
                if "pre_script" in step:
                    found_pre_script = True
                    break

        self.assertTrue(found_pre_script, "Testship YAML should have pre_script")


if __name__ == "__main__":
    unittest.main(verbosity=2)
