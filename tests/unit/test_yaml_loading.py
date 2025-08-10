#!/usr/bin/env python3
"""
Unit tests for activity YAML loading and parsing functionality

Tests the core YAML loading functions in both app.py and guarded_ai.py
to ensure they handle valid YAML, invalid syntax, missing fields,
malformed structure, and edge cases correctly.
"""

import unittest
import tempfile
import os
import sys
from pathlib import Path
import yaml

# Add research directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "research"))
import guarded_ai


class TestYAMLLoading(unittest.TestCase):
    """Test YAML loading functionality"""

    def create_test_yaml_file(self, content):
        """Create temporary YAML file with given content"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            return f.name

    def test_valid_yaml_loading(self):
        """Test loading valid YAML activity file"""
        valid_yaml = """
sections:
  - section_id: "test_section"
    title: "Test Section"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        content_blocks:
          - "Welcome to the test!"
        question: "Ready?"
        tokens_for_ai: "Categorize as ready or not"
        buckets:
          - ready
          - not_ready
        transitions:
          ready:
            content_blocks:
              - "Great!"
            next_section_and_step: "test_section:step_2"
          not_ready:
            content_blocks:
              - "Take your time."
      - step_id: "step_2"
        title: "Final Step"
        content_blocks:
          - "All done!"
"""

        yaml_file = self.create_test_yaml_file(valid_yaml)
        try:
            activity = guarded_ai.load_yaml_activity(yaml_file)

            # Verify basic structure
            self.assertIn("sections", activity)
            self.assertEqual(len(activity["sections"]), 1)

            section = activity["sections"][0]
            self.assertEqual(section["section_id"], "test_section")
            self.assertEqual(section["title"], "Test Section")
            self.assertEqual(len(section["steps"]), 2)

            # Verify first step
            step1 = section["steps"][0]
            self.assertEqual(step1["step_id"], "step_1")
            self.assertEqual(step1["title"], "Test Step")
            self.assertIn("content_blocks", step1)
            self.assertIn("question", step1)
            self.assertIn("buckets", step1)
            self.assertIn("transitions", step1)

            # Verify transitions
            self.assertIn("ready", step1["transitions"])
            self.assertIn("not_ready", step1["transitions"])

        finally:
            os.unlink(yaml_file)

    def test_invalid_yaml_syntax(self):
        """Test handling of invalid YAML syntax"""
        invalid_yaml = """
sections:
  - section_id: "test"
    title: "Test"
    steps:
      - step_id: "step1"
        title: [invalid: yaml: syntax
"""

        yaml_file = self.create_test_yaml_file(invalid_yaml)
        try:
            with self.assertRaises(yaml.YAMLError):
                guarded_ai.load_yaml_activity(yaml_file)
        finally:
            os.unlink(yaml_file)

    def test_missing_file(self):
        """Test handling of missing YAML file"""
        with self.assertRaises(FileNotFoundError):
            guarded_ai.load_yaml_activity("/nonexistent/path/file.yaml")

    def test_empty_yaml_file(self):
        """Test handling of empty YAML file"""
        yaml_file = self.create_test_yaml_file("")
        try:
            activity = guarded_ai.load_yaml_activity(yaml_file)
            self.assertIsNone(activity)
        finally:
            os.unlink(yaml_file)

    def test_yaml_with_missing_sections(self):
        """Test YAML without required sections field"""
        incomplete_yaml = """
title: "Test Activity"
description: "A test activity"
"""

        yaml_file = self.create_test_yaml_file(incomplete_yaml)
        try:
            activity = guarded_ai.load_yaml_activity(yaml_file)
            # Should load but won't have sections
            self.assertNotIn("sections", activity)
            self.assertIn("title", activity)
        finally:
            os.unlink(yaml_file)

    def test_yaml_with_empty_sections(self):
        """Test YAML with empty sections list"""
        empty_sections_yaml = """
sections: []
"""

        yaml_file = self.create_test_yaml_file(empty_sections_yaml)
        try:
            activity = guarded_ai.load_yaml_activity(yaml_file)
            self.assertIn("sections", activity)
            self.assertEqual(len(activity["sections"]), 0)
        finally:
            os.unlink(yaml_file)

    def test_yaml_with_malformed_section_structure(self):
        """Test YAML with malformed section structure"""
        malformed_yaml = """
sections:
  - section_id: "test"
    # Missing title
    steps: "not_a_list"  # Should be a list
"""

        yaml_file = self.create_test_yaml_file(malformed_yaml)
        try:
            activity = guarded_ai.load_yaml_activity(yaml_file)
            # Should load but structure will be wrong
            section = activity["sections"][0]
            self.assertEqual(section["steps"], "not_a_list")  # String instead of list
            self.assertNotIn("title", section)
        finally:
            os.unlink(yaml_file)

    def test_yaml_with_integer_and_boolean_buckets(self):
        """Test YAML with integer and boolean bucket values"""
        mixed_buckets_yaml = """
sections:
  - section_id: "quiz"
    title: "Quiz Section"
    steps:
      - step_id: "question1"
        title: "Year Question"
        question: "What year?"
        tokens_for_ai: "Categorize response"
        buckets:
          - 1912
          - 2000
          - incorrect
        transitions:
          1912:
            content_blocks:
              - "Correct year!"
          2000:
            content_blocks:
              - "Wrong year!"
          incorrect:
            content_blocks:
              - "Invalid input!"
      - step_id: "question2"
        title: "Yes/No Question"
        question: "Do you agree?"
        tokens_for_ai: "Categorize response"
        buckets:
          - true
          - false
        transitions:
          true:
            content_blocks:
              - "You agreed!"
          false:
            content_blocks:
              - "You disagreed!"
"""

        yaml_file = self.create_test_yaml_file(mixed_buckets_yaml)
        try:
            activity = guarded_ai.load_yaml_activity(yaml_file)

            # Check integer buckets
            step1 = activity["sections"][0]["steps"][0]
            self.assertIn(1912, step1["buckets"])
            self.assertIn(2000, step1["buckets"])
            self.assertIn("incorrect", step1["buckets"])

            # Check transitions with integer keys
            self.assertIn(1912, step1["transitions"])
            self.assertIn(2000, step1["transitions"])

            # Check boolean buckets
            step2 = activity["sections"][0]["steps"][1]
            self.assertIn(True, step2["buckets"])
            self.assertIn(False, step2["buckets"])

            # Check transitions with boolean keys
            self.assertIn(True, step2["transitions"])
            self.assertIn(False, step2["transitions"])

        finally:
            os.unlink(yaml_file)

    def test_yaml_with_metadata_operations(self):
        """Test YAML with various metadata operation formats"""
        metadata_yaml = """
sections:
  - section_id: "metadata_test"
    title: "Metadata Test"
    steps:
      - step_id: "operations"
        title: "Metadata Operations"
        question: "Test?"
        tokens_for_ai: "Always test"
        buckets:
          - test
        transitions:
          test:
            metadata_add:
              user_name: "the-users-response"
              score: "n+1"
              level: 5
            metadata_remove:
              - old_key
              - temp_data
            metadata_clear: true
            metadata_feedback_filter:
              - score
              - level
"""

        yaml_file = self.create_test_yaml_file(metadata_yaml)
        try:
            activity = guarded_ai.load_yaml_activity(yaml_file)

            transition = activity["sections"][0]["steps"][0]["transitions"]["test"]

            # Check metadata_add operations
            self.assertIn("metadata_add", transition)
            self.assertEqual(
                transition["metadata_add"]["user_name"], "the-users-response"
            )
            self.assertEqual(transition["metadata_add"]["score"], "n+1")
            self.assertEqual(transition["metadata_add"]["level"], 5)

            # Check metadata_remove is list format
            self.assertIn("metadata_remove", transition)
            self.assertIsInstance(transition["metadata_remove"], list)
            self.assertIn("old_key", transition["metadata_remove"])
            self.assertIn("temp_data", transition["metadata_remove"])

            # Check metadata_clear
            self.assertEqual(transition["metadata_clear"], True)

            # Check metadata_feedback_filter
            self.assertIn("metadata_feedback_filter", transition)
            self.assertIsInstance(transition["metadata_feedback_filter"], list)

        finally:
            os.unlink(yaml_file)

    def test_yaml_with_processing_scripts(self):
        """Test YAML with processing and pre-scripts"""
        script_yaml = """
sections:
  - section_id: "script_test"
    title: "Script Test"
    steps:
      - step_id: "with_scripts"
        title: "Scripts Step"
        question: "Enter data:"
        pre_script: |
          user_input = metadata.get("user_response", "")
          script_result = {
              "metadata": {
                  "processed_input": user_input.upper()
              }
          }
        processing_script: |
          processed = metadata.get("processed_input", "")
          script_result = {
              "metadata": {
                  "final_result": f"Result: {processed}"
              }
          }
        tokens_for_ai: "Categorize as valid"
        buckets:
          - valid
        transitions:
          valid:
            run_processing_script: true
            content_blocks:
              - "Processing completed!"
"""

        yaml_file = self.create_test_yaml_file(script_yaml)
        try:
            activity = guarded_ai.load_yaml_activity(yaml_file)

            step = activity["sections"][0]["steps"][0]

            # Check scripts are loaded as strings
            self.assertIn("pre_script", step)
            self.assertIsInstance(step["pre_script"], str)
            self.assertIn("user_input", step["pre_script"])

            self.assertIn("processing_script", step)
            self.assertIsInstance(step["processing_script"], str)
            self.assertIn("processed", step["processing_script"])

            # Check transition has run_processing_script flag
            transition = step["transitions"]["valid"]
            self.assertTrue(transition["run_processing_script"])

        finally:
            os.unlink(yaml_file)

    def test_yaml_with_nested_structures(self):
        """Test YAML with complex nested structures"""
        nested_yaml = """
sections:
  - section_id: "complex"
    title: "Complex Section"
    steps:
      - step_id: "nested"
        title: "Nested Step"
        question: "Complex question?"
        tokens_for_ai: "Complex categorization"
        buckets:
          - option_a
          - option_b
        transitions:
          option_a:
            content_blocks:
              - "First block"
              - "Second block"
              - "Third block"
            metadata_add:
              nested_data:
                sub_field: "value"
                number: 42
                list_field:
                  - "item1"
                  - "item2"
            metadata_conditions:
              required_field: "required_value"
              level: 5
            ai_feedback:
              tokens_for_ai: "Provide detailed feedback"
          option_b:
            content_blocks:
              - "Alternative path"
            next_section_and_step: "complex:final"
      - step_id: "final"
        title: "Final"
        content_blocks:
          - "Done!"
"""

        yaml_file = self.create_test_yaml_file(nested_yaml)
        try:
            activity = guarded_ai.load_yaml_activity(yaml_file)

            step = activity["sections"][0]["steps"][0]
            transition_a = step["transitions"]["option_a"]

            # Check nested metadata structure
            nested_data = transition_a["metadata_add"]["nested_data"]
            self.assertEqual(nested_data["sub_field"], "value")
            self.assertEqual(nested_data["number"], 42)
            self.assertIsInstance(nested_data["list_field"], list)
            self.assertEqual(len(nested_data["list_field"]), 2)

            # Check metadata conditions
            conditions = transition_a["metadata_conditions"]
            self.assertEqual(conditions["required_field"], "required_value")
            self.assertEqual(conditions["level"], 5)

            # Check AI feedback structure
            ai_feedback = transition_a["ai_feedback"]
            self.assertIn("tokens_for_ai", ai_feedback)

        finally:
            os.unlink(yaml_file)


class TestActivityYAMLStructureValidation(unittest.TestCase):
    """Test validation of loaded YAML structure"""

    def create_test_yaml_file(self, content):
        """Create temporary YAML file with given content"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            return f.name

    def test_step_id_uniqueness_within_section(self):
        """Test that step IDs are unique within a section"""
        duplicate_step_yaml = """
sections:
  - section_id: "test"
    title: "Test"
    steps:
      - step_id: "step1"
        title: "First"
        content_blocks:
          - "First step"
      - step_id: "step1"  # Duplicate!
        title: "Second"
        content_blocks:
          - "Second step"
"""

        yaml_file = self.create_test_yaml_file(duplicate_step_yaml)
        try:
            activity = guarded_ai.load_yaml_activity(yaml_file)

            # Should load, but we can detect duplicates
            step_ids = [step["step_id"] for step in activity["sections"][0]["steps"]]
            unique_step_ids = set(step_ids)

            self.assertNotEqual(len(step_ids), len(unique_step_ids))  # Has duplicates

        finally:
            os.unlink(yaml_file)

    def test_section_id_uniqueness(self):
        """Test that section IDs are unique"""
        duplicate_section_yaml = """
sections:
  - section_id: "same"
    title: "First Section"
    steps:
      - step_id: "step1"
        title: "Step 1"
        content_blocks:
          - "Content 1"
  - section_id: "same"  # Duplicate!
    title: "Second Section"
    steps:
      - step_id: "step1"
        title: "Step 1"
        content_blocks:
          - "Content 2"
"""

        yaml_file = self.create_test_yaml_file(duplicate_section_yaml)
        try:
            activity = guarded_ai.load_yaml_activity(yaml_file)

            # Should load, but we can detect duplicates
            section_ids = [section["section_id"] for section in activity["sections"]]
            unique_section_ids = set(section_ids)

            self.assertNotEqual(
                len(section_ids), len(unique_section_ids)
            )  # Has duplicates

        finally:
            os.unlink(yaml_file)

    def test_transition_references(self):
        """Test that transitions reference valid section:step combinations"""
        invalid_reference_yaml = """
sections:
  - section_id: "section1"
    title: "Section 1"
    steps:
      - step_id: "step1"
        title: "Step 1"
        question: "Continue?"
        tokens_for_ai: "Categorize"
        buckets:
          - "yes"
        transitions:
          "yes":
            next_section_and_step: "nonexistent:step1"  # Invalid reference
"""

        yaml_file = self.create_test_yaml_file(invalid_reference_yaml)
        try:
            activity = guarded_ai.load_yaml_activity(yaml_file)

            # YAML loads successfully but reference is invalid
            step = activity["sections"][0]["steps"][0]
            self.assertIn("transitions", step)
            self.assertIn("yes", step["transitions"])

            transition = step["transitions"]["yes"]
            next_ref = transition["next_section_and_step"]
            section_id, step_id = next_ref.split(":")

            # Check if referenced section exists
            referenced_section = None
            for section in activity["sections"]:
                if section["section_id"] == section_id:
                    referenced_section = section
                    break

            self.assertIsNone(referenced_section)  # Should not exist

        finally:
            os.unlink(yaml_file)

    def test_bucket_transition_consistency(self):
        """Test that all buckets have corresponding transitions"""
        inconsistent_yaml = """
sections:
  - section_id: "test"
    title: "Test"
    steps:
      - step_id: "step1"
        title: "Step 1"
        question: "Choose option:"
        tokens_for_ai: "Categorize"
        buckets:
          - option_a
          - option_b
          - option_c
        transitions:
          option_a:
            content_blocks:
              - "Option A selected"
          option_b:
            content_blocks:
              - "Option B selected"
          # Missing option_c transition!
"""

        yaml_file = self.create_test_yaml_file(inconsistent_yaml)
        try:
            activity = guarded_ai.load_yaml_activity(yaml_file)

            step = activity["sections"][0]["steps"][0]
            buckets = set(step["buckets"])
            transition_keys = set(step["transitions"].keys())

            # Check for missing transitions
            missing_transitions = buckets - transition_keys
            self.assertTrue(
                len(missing_transitions) > 0
            )  # Should have missing transitions
            self.assertIn("option_c", missing_transitions)

        finally:
            os.unlink(yaml_file)


class TestRealYAMLFiles(unittest.TestCase):
    """Test loading of real YAML files from the project"""

    def test_load_existing_activity_files(self):
        """Test loading existing activity files"""
        research_dir = Path(__file__).parent.parent.parent / "research"
        yaml_files = list(research_dir.glob("activity*.yaml"))

        self.assertTrue(len(yaml_files) > 0, "Should find activity YAML files")

        for yaml_file in yaml_files[:5]:  # Test first 5 files
            with self.subTest(file=yaml_file.name):
                try:
                    activity = guarded_ai.load_yaml_activity(str(yaml_file))

                    # Basic structure checks
                    self.assertIsInstance(activity, dict)
                    self.assertIn("sections", activity)
                    self.assertIsInstance(activity["sections"], list)

                    if activity["sections"]:
                        section = activity["sections"][0]
                        self.assertIn("section_id", section)
                        self.assertIn("steps", section)
                        self.assertIsInstance(section["steps"], list)

                        if section["steps"]:
                            step = section["steps"][0]
                            self.assertIn("step_id", step)

                except Exception as e:
                    self.fail(f"Failed to load {yaml_file.name}: {e}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
