#!/usr/bin/env python3
"""
Unit tests for the activity_yaml_validator.py module.

Tests all validation features including:
- YAML syntax validation
- Structure validation
- Metadata operations validation
- Python code validation
- Logic flow validation
- Terminal step validation
"""

import unittest
import tempfile
import os
import sys
from pathlib import Path

# Add parent directory to path to import the validator
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from activity_yaml_validator import ActivityYAMLValidator, ValidationError


class TestActivityYAMLValidator(unittest.TestCase):
    """Test cases for ActivityYAMLValidator"""

    def setUp(self):
        """Set up test fixtures"""
        self.validator = ActivityYAMLValidator()

    def create_temp_yaml(self, content: str) -> str:
        """Create a temporary YAML file with given content"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            return f.name

    def tearDown(self):
        """Clean up any temporary files"""
        # Clean up is handled by tempfile
        pass

    def test_valid_yaml_passes(self):
        """Test that a valid YAML file passes validation"""
        valid_yaml = """
default_max_attempts_per_step: 3
tokens_for_ai_rubric: "Test rubric"

sections:
  - section_id: "section_1"
    title: "Test Section"
    steps:
      - step_id: "step_1"
        title: "Question Step"
        question: "What do you want?"
        tokens_for_ai: "Categorize response"
        feedback_tokens_for_ai: "Provide feedback"
        buckets:
          - valid
          - invalid
        transitions:
          valid:
            content_blocks:
              - "Great!"
            next_section_and_step: "section_1:step_2"
          invalid:
            content_blocks:
              - "Try again"
            next_section_and_step: "section_1:step_1"
      
      - step_id: "step_2"
        title: "Final Step"
        content_blocks:
          - "All done!"
"""
        temp_file = self.create_temp_yaml(valid_yaml)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertTrue(is_valid)
            self.assertEqual(len(errors), 0)
        finally:
            os.unlink(temp_file)

    def test_yaml_syntax_error(self):
        """Test that YAML syntax errors are caught"""
        invalid_yaml = """
sections:
  - section_id: "test"
    title: "Test"
    steps:
      - step_id: "step1"
        title: "Test Step"
        content_blocks:
          - "Test"
        invalid_key: [unclosed list
"""
        temp_file = self.create_temp_yaml(invalid_yaml)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertFalse(is_valid)
            self.assertGreater(len(errors), 0)
            self.assertIn("YAML syntax error", errors[0])
        finally:
            os.unlink(temp_file)

    def test_missing_required_fields(self):
        """Test that missing required fields are caught"""
        missing_sections = """
default_max_attempts_per_step: 3
"""
        temp_file = self.create_temp_yaml(missing_sections)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertFalse(is_valid)
            self.assertIn("Missing required field: sections", errors)
        finally:
            os.unlink(temp_file)

    def test_invalid_field_types(self):
        """Test that invalid field types are caught"""
        invalid_types = """
default_max_attempts_per_step: "should_be_integer"
tokens_for_ai_rubric: 123

sections:
  - section_id: "test"
    title: "Test"
    steps: "should_be_list"
"""
        temp_file = self.create_temp_yaml(invalid_types)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertFalse(is_valid)
            self.assertTrue(
                any("must be a positive integer" in error for error in errors)
            )
            self.assertTrue(any("must be a string" in error for error in errors))
        finally:
            os.unlink(temp_file)

    def test_duplicate_ids(self):
        """Test that duplicate section and step IDs are caught"""
        duplicate_ids = """
sections:
  - section_id: "duplicate"
    title: "First Section"
    steps:
      - step_id: "step_duplicate"
        title: "First Step"
        content_blocks:
          - "Content"
      - step_id: "step_duplicate"
        title: "Second Step"
        content_blocks:
          - "More content"
  
  - section_id: "duplicate"
    title: "Second Section"
    steps:
      - step_id: "step_1"
        title: "Step"
        content_blocks:
          - "Content"
"""
        temp_file = self.create_temp_yaml(duplicate_ids)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertFalse(is_valid)
            self.assertTrue(any("Duplicate section_id" in error for error in errors))
            self.assertTrue(any("Duplicate step_id" in error for error in errors))
        finally:
            os.unlink(temp_file)

    def test_terminal_step_validation(self):
        """Test that terminal steps cannot have questions or buckets"""
        terminal_with_question = """
sections:
  - section_id: "section_1"
    title: "First Section"
    steps:
      - step_id: "step_1"
        title: "First Step"
        content_blocks:
          - "This step is fine"
      - step_id: "step_2"
        title: "Also fine"
        question: "Questions are OK in non-terminal steps"
        buckets: ["yes", "no"]
        transitions:
          yes:
            content_blocks: ["Good"]
            next_section_and_step: "section_2:step_1"
          no:
            content_blocks: ["Try again"]
  - section_id: "section_2"
    title: "Last Section"
    steps:
      - step_id: "step_1"
        title: "Not terminal - has another step after"
        question: "This is OK"
        buckets: ["answer"]
        transitions:
          answer:
            content_blocks: ["Continue"]
      - step_id: "step_2"
        title: "This is the real terminal step"
        question: "This is invalid"
        buckets:
          - some_bucket
        transitions:
          some_bucket:
            content_blocks:
              - "Done"
            # No next_section_and_step and last step of last section = terminal
"""
        temp_file = self.create_temp_yaml(terminal_with_question)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertFalse(is_valid)
            # Should only flag the last step of the last section
            terminal_errors = [e for e in errors if "Final/terminal" in e]
            self.assertEqual(
                len(terminal_errors), 2
            )  # One for question, one for buckets
            self.assertTrue(
                any(
                    "section_2" in error and "step_2" in error
                    for error in terminal_errors
                )
            )
        finally:
            os.unlink(temp_file)

    def test_metadata_operations_validation(self):
        """Test validation of metadata operations"""
        metadata_test = """
sections:
  - section_id: "section_1"
    title: "Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        question: "Test?"
        buckets:
          - test
        transitions:
          test:
            metadata_clear: "should_be_boolean"
            metadata_feedback_filter: "should_be_list"
            metadata_remove: 123
            metadata_add: "should_be_dict"
            next_section_and_step: "section_1:step_2"
      
      - step_id: "step_2"
        title: "Final"
        content_blocks:
          - "Done"
"""
        temp_file = self.create_temp_yaml(metadata_test)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertFalse(is_valid)
            self.assertTrue(
                any("metadata_clear' must be boolean" in error for error in errors)
            )
            self.assertTrue(
                any(
                    "metadata_feedback_filter' must be a list" in error
                    for error in errors
                )
            )
            self.assertTrue(
                any(
                    "metadata_remove' must be a string or list of strings" in error
                    for error in errors
                )
            )
            self.assertTrue(
                any("metadata_add' must be a dictionary" in error for error in errors)
            )
        finally:
            os.unlink(temp_file)

    def test_valid_metadata_operations(self):
        """Test that valid metadata operations pass"""
        valid_metadata = """
sections:
  - section_id: "section_1"
    title: "Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        question: "Test?"
        feedback_tokens_for_ai: "Provide feedback"
        buckets:
          - test
        transitions:
          test:
            metadata_clear: true
            metadata_feedback_filter:
              - "field1"
              - "field2"
            metadata_remove: "single_field"
            metadata_add:
              new_field: "value"
            next_section_and_step: "section_1:step_2"
      
      - step_id: "step_2"
        title: "Test Step 2"
        question: "Another test?"
        buckets:
          - test2
        transitions:
          test2:
            metadata_remove:
              - "field1"
              - "field2"
            next_section_and_step: "section_1:step_3"
      
      - step_id: "step_3"
        title: "Final"
        content_blocks:
          - "Done"
"""
        temp_file = self.create_temp_yaml(valid_metadata)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertTrue(is_valid)
            self.assertEqual(len(errors), 0)
        finally:
            os.unlink(temp_file)

    def test_python_syntax_validation(self):
        """Test that Python syntax errors in scripts are caught"""
        python_syntax_error = """
sections:
  - section_id: "section_1"
    title: "Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        question: "Test?"
        pre_script: |
          if True  # Missing colon
              print("error")
        processing_script: |
          def invalid_function(
              # Missing closing parenthesis
              pass
        buckets:
          - test
        transitions:
          test:
            next_section_and_step: "section_1:step_2"
      
      - step_id: "step_2"
        title: "Final"
        content_blocks:
          - "Done"
"""
        temp_file = self.create_temp_yaml(python_syntax_error)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertFalse(is_valid)
            self.assertTrue(any("Python syntax error" in error for error in errors))
        finally:
            os.unlink(temp_file)

    def test_invalid_transitions(self):
        """Test validation of transition references"""
        invalid_transitions = """
sections:
  - section_id: "section_1"
    title: "Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        question: "Test?"
        buckets:
          - valid_bucket
          - another_bucket
        transitions:
          valid_bucket:
            next_section_and_step: "nonexistent_section:step_1"
          another_bucket:
            next_section_and_step: "invalid_format"
          unused_transition:
            content_blocks:
              - "This transition has no corresponding bucket"
"""
        temp_file = self.create_temp_yaml(invalid_transitions)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertFalse(is_valid)
            # Should have errors for invalid transition targets and missing transitions
            self.assertTrue(
                any("Invalid transition target" in error for error in errors)
            )
            self.assertTrue(
                any(
                    "must be in format 'section_id:step_id'" in error
                    for error in errors
                )
            )
            # Should have warnings for unused transitions
            self.assertTrue(any("Unused transition" in warning for warning in warnings))
        finally:
            os.unlink(temp_file)

    def test_metadata_feedback_filter_warning(self):
        """Test warning when metadata_feedback_filter used without feedback_tokens_for_ai"""
        metadata_filter_no_feedback = """
sections:
  - section_id: "section_1"
    title: "Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        question: "Test?"
        buckets:
          - test
        transitions:
          test:
            metadata_feedback_filter:
              - "field1"
            next_section_and_step: "section_1:step_2"
      
      - step_id: "step_2"
        title: "Final"
        content_blocks:
          - "Done"
"""
        temp_file = self.create_temp_yaml(metadata_filter_no_feedback)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertTrue(is_valid)  # Should be valid but with warning
            self.assertTrue(
                any(
                    "metadata_feedback_filter used but no feedback_tokens_for_ai"
                    in warning
                    for warning in warnings
                )
            )
        finally:
            os.unlink(temp_file)

    def test_pre_script_warning(self):
        """Test warning when pre_script used without question"""
        pre_script_no_question = """
sections:
  - section_id: "section_1"
    title: "Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        content_blocks:
          - "Content"
        pre_script: |
          print("This is unusual without a question")
"""
        temp_file = self.create_temp_yaml(pre_script_no_question)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertTrue(is_valid)  # Should be valid but with warning
            self.assertTrue(
                any(
                    "pre_script typically used with question steps" in warning
                    for warning in warnings
                )
            )
        finally:
            os.unlink(temp_file)

    def test_empty_else_block_detection(self):
        """Test detection of empty else blocks in Python code"""
        empty_else_block = """
sections:
  - section_id: "section_1"
    title: "Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        question: "Test?"
        processing_script: |
          if condition:
              do_something()
          else:
              # Only comments here, should trigger error
        buckets:
          - test
        transitions:
          test:
            next_section_and_step: "section_1:step_2"
      
      - step_id: "step_2"
        title: "Final"
        content_blocks:
          - "Done"
"""
        temp_file = self.create_temp_yaml(empty_else_block)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            # This should detect the empty else block
            self.assertTrue(
                any("'else:' block contains only comments" in error for error in errors)
            )
        finally:
            os.unlink(temp_file)

    def test_content_blocks_validation(self):
        """Test validation of content_blocks structure"""
        invalid_content_blocks = """
sections:
  - section_id: "section_1"
    title: "Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        content_blocks: "should_be_list"
      
      - step_id: "step_2"
        title: "Another Test"
        content_blocks:
          - "Valid string"
          - 123  # Should be string
          - "Another valid string"
"""
        temp_file = self.create_temp_yaml(invalid_content_blocks)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertFalse(is_valid)
            self.assertTrue(
                any("content_blocks must be a list" in error for error in errors)
            )
            self.assertTrue(any("must be a string" in error for error in errors))
        finally:
            os.unlink(temp_file)

    def test_transition_fields_validation(self):
        """Test validation of various transition fields"""
        invalid_transition_fields = """
sections:
  - section_id: "section_1"
    title: "Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        question: "Test?"
        buckets:
          - test
        transitions:
          test:
            run_processing_script: "should_be_boolean"
            ai_feedback: "should_be_dict"
            content_blocks: "should_be_list"
            next_section_and_step: "section_1:step_2"
      
      - step_id: "step_2"
        title: "Another Test"
        question: "Test?"
        buckets:
          - test2
        transitions:
          test2:
            ai_feedback:
              tokens_for_ai: 123  # Should be string
            content_blocks:
              - "Valid"
              - 456  # Should be string
            next_section_and_step: "section_1:step_3"
      
      - step_id: "step_3"
        title: "Final"
        content_blocks:
          - "Done"
"""
        temp_file = self.create_temp_yaml(invalid_transition_fields)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertFalse(is_valid)
            self.assertTrue(
                any(
                    "run_processing_script' must be boolean" in error
                    for error in errors
                )
            )
            self.assertTrue(
                any("ai_feedback' must be a dictionary" in error for error in errors)
            )
            self.assertTrue(
                any("tokens_for_ai must be a string" in error for error in errors)
            )
            self.assertTrue(
                any("content_blocks' must be a list" in error for error in errors)
            )
        finally:
            os.unlink(temp_file)

    def test_using_existing_failing_fixture(self):
        """Test using the existing failing fixture we created"""
        fixture_path = "tests/fixtures/test_invalid.yaml"
        if os.path.exists(fixture_path):
            is_valid, errors, warnings = self.validator.validate_file(fixture_path)
            self.assertFalse(is_valid)
            self.assertGreater(len(errors), 0)
            # Should catch the YAML syntax error we know is in there
            self.assertTrue(any("YAML syntax error" in error for error in errors))

    def test_feedback_prompts_validation(self):
        """Test validation of feedback_prompts structure"""
        valid_feedback_prompts = """
sections:
  - section_id: "section_1"
    title: "Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        question: "Test?"
        feedback_prompts:
          - name: "hit_miss"
            tokens_for_ai: "Report hit/miss for both players"
          - name: "ship_sinking"
            tokens_for_ai: "Report any ship sinking events"
        buckets:
          - test
        transitions:
          test:
            next_section_and_step: "section_1:step_2"
      
      - step_id: "step_2"
        title: "Final"
        content_blocks:
          - "Done"
"""
        temp_file = self.create_temp_yaml(valid_feedback_prompts)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertTrue(is_valid)
            self.assertEqual(len(errors), 0)
        finally:
            os.unlink(temp_file)

    def test_invalid_feedback_prompts(self):
        """Test validation of invalid feedback_prompts structure"""
        invalid_feedback_prompts = """
sections:
  - section_id: "section_1"
    title: "Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        question: "Test?"
        feedback_prompts: "should_be_list"
        buckets:
          - test
        transitions:
          test:
            next_section_and_step: "section_1:step_2"
      
      - step_id: "step_2"
        title: "Test Step 2"
        question: "Another test?"
        feedback_prompts: []  # Empty list should error
        buckets:
          - test2
        transitions:
          test2:
            next_section_and_step: "section_1:step_3"
      
      - step_id: "step_3"
        title: "Test Step 3"
        question: "Third test?"
        feedback_prompts:
          - "should_be_dict"
          - name: "valid_name"
            # Missing tokens_for_ai
          - name: "duplicate"
            tokens_for_ai: "First prompt"
          - name: "duplicate"  # Duplicate name
            tokens_for_ai: "Second prompt"
          - name: 123  # Invalid name type
            tokens_for_ai: "Valid tokens"
          - name: "valid_name2"
            tokens_for_ai: 456  # Invalid tokens type
        buckets:
          - test3
        transitions:
          test3:
            content_blocks: ["Done"]
"""
        temp_file = self.create_temp_yaml(invalid_feedback_prompts)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertFalse(is_valid)

            # Check for specific error types
            self.assertTrue(
                any("feedback_prompts' must be a list" in error for error in errors)
            )
            self.assertTrue(
                any("feedback_prompts' cannot be empty" in error for error in errors)
            )
            self.assertTrue(any("must be a dictionary" in error for error in errors))
            self.assertTrue(any("missing required field" in error for error in errors))
            self.assertTrue(
                any("duplicate feedback prompt name" in error for error in errors)
            )
            self.assertTrue(any("name must be a string" in error for error in errors))
            self.assertTrue(
                any("tokens_for_ai must be a string" in error for error in errors)
            )
        finally:
            os.unlink(temp_file)

    def test_both_feedback_systems(self):
        """Test that both feedback_tokens_for_ai and feedback_prompts can be used together"""
        both_feedback_systems = """
sections:
  - section_id: "section_1"
    title: "Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        question: "Test?"
        feedback_tokens_for_ai: "Legacy feedback system"
        feedback_prompts:
          - name: "new_system_1"
            tokens_for_ai: "New system prompt 1"
          - name: "new_system_2"
            tokens_for_ai: "New system prompt 2"
        buckets:
          - test
        transitions:
          test:
            next_section_and_step: "section_1:step_2"
      
      - step_id: "step_2"
        title: "Final"
        content_blocks:
          - "Done"
"""
        temp_file = self.create_temp_yaml(both_feedback_systems)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertTrue(is_valid, f"Should be valid but got errors: {errors}")
            self.assertEqual(len(errors), 0)
        finally:
            os.unlink(temp_file)

    def test_cli_integration(self):
        """Test the command line interface"""
        import subprocess
        import sys

        # Test with valid battleship YAML
        result = subprocess.run(
            [
                sys.executable,
                "activity_yaml_validator.py",
                "research/activity29-battleship.yaml",
            ],
            capture_output=True,
            text=True,
            cwd=".",
        )

        # Should succeed (exit code 0) despite warnings
        self.assertEqual(result.returncode, 0)
        self.assertIn("valid", result.stdout.lower())

        # Create a YAML file that will have warnings (pre_script without question)
        warning_yaml = """
sections:
  - section_id: "test_section"
    title: "Test Section"
    steps:
      - step_id: "step1"
        title: "Step with pre_script but no question"
        content_blocks:
          - "This step has pre_script but no question - should generate warning"
        pre_script: |
          # This pre_script without a question should generate a warning
          metadata['test'] = 'value'
          script_result = {'metadata': {}}
"""

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(warning_yaml)
            warning_file = f.name

        try:
            # Test with --strict flag (warnings become errors)
            result = subprocess.run(
                [
                    sys.executable,
                    "activity_yaml_validator.py",
                    warning_file,
                    "--strict",
                ],
                capture_output=True,
                text=True,
                cwd=".",
            )

            # Should fail (exit code 1) because warnings become errors in strict mode
            self.assertEqual(
                result.returncode,
                1,
                f"Expected strict mode to fail with warnings. Output: {result.stdout}",
            )

        finally:
            os.unlink(warning_file)

    def test_jinja2_control_structures_rejected(self):
        """Test that Jinja2 control structures are rejected"""
        jinja2_control_yaml = """
sections:
  - section_id: "section_1"
    title: "Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        content_blocks:
          - "Valid content"
          - "{% if score > 80 %}High score{% else %}Low score{% endif %}"
        question: "Test question {% for item in items %}{{item}}{% endfor %}"
        tokens_for_ai: |
          {% if attempts_remaining == 1 %}
            Last chance
          {% else %}
            Keep trying
          {% endif %}
        buckets:
          - test
        transitions:
          test:
            content_blocks: ["Done"]
"""
        temp_file = self.create_temp_yaml(jinja2_control_yaml)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertFalse(is_valid)
            # Should have multiple errors for different Jinja2 control structures
            jinja2_errors = [e for e in errors if "Jinja2" in e]
            self.assertGreater(len(jinja2_errors), 0)
            # Check that error messages mention the right thing
            self.assertTrue(any("NOT supported" in error for error in jinja2_errors))
            self.assertTrue(
                any(
                    "show_if" in error or "pre-compute" in error
                    for error in jinja2_errors
                )
            )
        finally:
            os.unlink(temp_file)

    def test_handlebars_control_structures_rejected(self):
        """Test that Handlebars control structures are rejected"""
        handlebars_yaml = """
sections:
  - section_id: "section_1"
    title: "Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        content_blocks:
          - "{{#if premium}}Premium content{{else}}Free content{{/if}}"
          - "{{#each items}}Item: {{name}}{{/each}}"
        question: "{{#unless answered}}Please answer{{/unless}}"
        feedback_tokens_for_ai: "{{#if correct}}Good job{{else}}Try again{{/if}}"
        buckets:
          - test
        transitions:
          test:
            ai_feedback:
              tokens_for_ai: "{{#with user}}Hello {{name}}{{/with}}"
            content_blocks: ["Done"]
"""
        temp_file = self.create_temp_yaml(handlebars_yaml)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertFalse(is_valid)
            # Should have multiple errors for different Handlebars control structures
            handlebars_errors = [e for e in errors if "Handlebars" in e]
            self.assertGreater(len(handlebars_errors), 0)
            # Check that error messages mention the right thing
            self.assertTrue(
                any("NOT supported" in error for error in handlebars_errors)
            )
        finally:
            os.unlink(temp_file)

    def test_valid_substitutions_allowed(self):
        """Test that valid {{variable}} substitutions are allowed"""
        valid_substitutions_yaml = """
sections:
  - section_id: "section_1"
    title: "Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        content_blocks:
          - "Hello {{username}}!"
          - "Score: {{metadata.score}}"
          - "Attempt {{current_attempt}} of {{max_attempts}}"
          - "You have {{attempts_remaining}} attempts left"
        question: "Ready {{username}}? Try {{current_attempt}}"
        tokens_for_ai: |
          User {{username}} is on attempt {{current_attempt}}.
          Their score is {{metadata.score}}.
        feedback_tokens_for_ai: |
          Provide feedback to {{username}}.
          Reference their {{metadata.last_answer}}.
        buckets:
          - test
        transitions:
          test:
            ai_feedback:
              tokens_for_ai: "Great job {{username}}! Score: {{metadata.score}}"
            content_blocks:
              - "Well done {{username}}!"
              - "Final score: {{metadata.score}}"
            next_section_and_step: "section_1:step_2"

      - step_id: "step_2"
        title: "Final"
        content_blocks:
          - "Goodbye {{username}}!"
"""
        temp_file = self.create_temp_yaml(valid_substitutions_yaml)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertTrue(
                is_valid,
                f"Valid substitutions should be allowed but got errors: {errors}",
            )
            self.assertEqual(len(errors), 0)
        finally:
            os.unlink(temp_file)

    def test_control_structures_in_hints(self):
        """Test that control structures in hints are rejected"""
        hints_with_control_yaml = """
sections:
  - section_id: "section_1"
    title: "Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        question: "What is 2+2?"
        hints:
          - attempt: 2
            text: "{% if score > 50 %}Think harder{% else %}You can do it{% endif %}"
          - attempt: 3
            text: "{{#if last_try}}This is your last chance{{/if}}"
        buckets:
          - test
        transitions:
          test:
            content_blocks: ["Done"]
"""
        temp_file = self.create_temp_yaml(hints_with_control_yaml)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertFalse(is_valid)
            # Should catch control structures in hints
            hint_errors = [e for e in errors if "hints" in e]
            self.assertGreater(len(hint_errors), 0)
        finally:
            os.unlink(temp_file)

    def test_control_structures_in_feedback_prompts(self):
        """Test that control structures in feedback_prompts are rejected"""
        feedback_prompts_control_yaml = """
sections:
  - section_id: "section_1"
    title: "Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        question: "Test?"
        feedback_prompts:
          - name: "status"
            tokens_for_ai: "{% if health > 50 %}Healthy{% else %}Injured{% endif %}"
          - name: "items"
            tokens_for_ai: "{{#each inventory}}{{item}}{{/each}}"
        buckets:
          - test
        transitions:
          test:
            content_blocks: ["Done"]
"""
        temp_file = self.create_temp_yaml(feedback_prompts_control_yaml)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertFalse(is_valid)
            # Should catch control structures in feedback_prompts
            feedback_errors = [e for e in errors if "feedback_prompts" in e]
            self.assertGreater(len(feedback_errors), 0)
        finally:
            os.unlink(temp_file)

    def test_control_structures_in_conditional_content_blocks(self):
        """Test that control structures in conditional content_blocks are rejected"""
        conditional_blocks_yaml = """
sections:
  - section_id: "section_1"
    title: "Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        content_blocks:
          - text: "{% if score > 90 %}Excellent!{% endif %}"
            show_if:
              score_gte: 90
          - text: "{{#if premium}}Premium user{{/if}}"
            show_if:
              premium: true
        question: "Test?"
        buckets:
          - test
        transitions:
          test:
            content_blocks:
              - text: "{% for i in range(5) %}Step {{i}}{% endfor %}"
            next_section_and_step: "section_1:step_2"

      - step_id: "step_2"
        title: "Final"
        content_blocks:
          - "Done"
"""
        temp_file = self.create_temp_yaml(conditional_blocks_yaml)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertFalse(is_valid)
            # Should catch control structures in conditional content blocks
            control_errors = [e for e in errors if "Jinja2" in e or "Handlebars" in e]
            self.assertGreater(len(control_errors), 0)
        finally:
            os.unlink(temp_file)

    def test_mixed_valid_and_invalid_templates(self):
        """Test file with both valid substitutions and invalid control structures"""
        mixed_yaml = """
sections:
  - section_id: "section_1"
    title: "Test"
    steps:
      - step_id: "step_1"
        title: "Test Step"
        content_blocks:
          - "Hello {{username}}!"  # VALID
          - "Score: {{metadata.score}}"  # VALID
          - "{% if score > 80 %}High{% else %}Low{% endif %}"  # INVALID
        question: "Ready {{username}}?"  # VALID
        tokens_for_ai: |
          User {{username}} on attempt {{current_attempt}}.  # VALID
          {% if attempts_remaining == 1 %}Last chance{% endif %}  # INVALID
        buckets:
          - test
        transitions:
          test:
            content_blocks: ["Done"]
"""
        temp_file = self.create_temp_yaml(mixed_yaml)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertFalse(is_valid)
            # Should only have errors for the control structures, not the valid substitutions
            control_errors = [e for e in errors if "Jinja2" in e or "Handlebars" in e]
            self.assertGreater(len(control_errors), 0)
            # Should have exactly 2 errors (one for content_block, one for tokens_for_ai)
            self.assertEqual(len(control_errors), 2)
        finally:
            os.unlink(temp_file)

    def test_various_jinja2_statements(self):
        """Test detection of various Jinja2 statement types"""
        various_jinja2_yaml = """
sections:
  - section_id: "section_1"
    title: "Test"
    steps:
      - step_id: "step_1"
        title: "Test with various Jinja2"
        content_blocks:
          - "{% if x %}test{% endif %}"
          - "{% for item in list %}{{item}}{% endfor %}"
          - "{% elif condition %}branch{% endif %}"
          - "{% else %}default{% endif %}"
          - "{% set var = value %}"
          - "{% block content %}test{% endblock %}"
        question: "Test?"
        buckets:
          - test
        transitions:
          test:
            content_blocks: ["Done"]
"""
        temp_file = self.create_temp_yaml(various_jinja2_yaml)
        try:
            is_valid, errors, warnings = self.validator.validate_file(temp_file)
            self.assertFalse(is_valid)
            # Should catch all the different Jinja2 statement types
            jinja2_errors = [e for e in errors if "Jinja2" in e]
            # Should have multiple errors for different statements
            self.assertGreaterEqual(len(jinja2_errors), 5)
        finally:
            os.unlink(temp_file)


if __name__ == "__main__":
    # Run the tests
    unittest.main(verbosity=2)
