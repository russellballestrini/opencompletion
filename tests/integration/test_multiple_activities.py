#!/usr/bin/env python3
"""
Integration tests that run against multiple activity files

These tests validate that all activity YAML files in the project
can be loaded, validated, and executed without errors after our changes.
"""

import unittest
import os
import sys
import glob
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add research directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "research"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import guarded_ai
from activity_yaml_validator import ActivityYAMLValidator


class TestMultipleActivityFiles(unittest.TestCase):
    """Integration tests across multiple activity files"""

    def setUp(self):
        """Set up test environment"""
        self.research_dir = Path(__file__).parent.parent.parent / "research"
        self.activity_files = list(self.research_dir.glob("activity*.yaml"))
        self.validator = ActivityYAMLValidator()

        # Mock OpenAI client for testing
        self.mock_client = MagicMock()
        self.mock_response = MagicMock()
        self.mock_response.choices = [MagicMock()]
        self.mock_response.choices[0].message.content = "valid_response"
        self.mock_client.chat.completions.create.return_value = self.mock_response

    def test_all_activity_files_load_successfully(self):
        """Test that all activity YAML files load without errors"""
        self.assertTrue(len(self.activity_files) > 0, "Should find activity files")

        failed_files = []

        for activity_file in self.activity_files:
            with self.subTest(file=activity_file.name):
                try:
                    activity = guarded_ai.load_yaml_activity(str(activity_file))
                    self.assertIsInstance(activity, dict)
                    self.assertIn("sections", activity)
                except Exception as e:
                    failed_files.append((activity_file.name, str(e)))

        if failed_files:
            failure_msg = "Failed to load files:\n" + "\n".join(
                f"  - {name}: {error}" for name, error in failed_files
            )
            self.fail(failure_msg)

    def test_all_activity_files_pass_validation(self):
        """Test that all activity files pass our validator"""
        validation_errors = {}

        for activity_file in self.activity_files:
            with self.subTest(file=activity_file.name):
                try:
                    is_valid, errors, warnings = self.validator.validate_file(
                        str(activity_file)
                    )
                    if errors:
                        validation_errors[activity_file.name] = errors
                except Exception as e:
                    validation_errors[activity_file.name] = [f"Validation failed: {e}"]

        if validation_errors:
            failure_msg = "Validation errors found:\n"
            for filename, errors in validation_errors.items():
                failure_msg += f"\n{filename}:\n"
                for error in errors[:5]:  # Show first 5 errors
                    failure_msg += f"  - {error}\n"
                if len(errors) > 5:
                    failure_msg += f"  ... and {len(errors) - 5} more errors\n"
            self.fail(failure_msg)

    def test_activity_files_have_required_structure(self):
        """Test that all activity files have the required basic structure"""
        structural_issues = {}

        for activity_file in self.activity_files:
            issues = []

            try:
                activity = guarded_ai.load_yaml_activity(str(activity_file))

                # Check basic structure
                if "sections" not in activity:
                    issues.append("Missing 'sections' field")
                elif not isinstance(activity["sections"], list):
                    issues.append("'sections' is not a list")
                elif len(activity["sections"]) == 0:
                    issues.append("Empty sections list")
                else:
                    # Check each section
                    for i, section in enumerate(activity["sections"]):
                        if "section_id" not in section:
                            issues.append(f"Section {i} missing 'section_id'")
                        if "steps" not in section:
                            issues.append(f"Section {i} missing 'steps'")
                        elif not isinstance(section["steps"], list):
                            issues.append(f"Section {i} 'steps' is not a list")
                        elif len(section["steps"]) == 0:
                            issues.append(f"Section {i} has empty steps list")
                        else:
                            # Check each step
                            for j, step in enumerate(section["steps"]):
                                if "step_id" not in step:
                                    issues.append(
                                        f"Section {i} Step {j} missing 'step_id'"
                                    )

                if issues:
                    structural_issues[activity_file.name] = issues

            except Exception as e:
                structural_issues[activity_file.name] = [f"Failed to analyze: {e}"]

        if structural_issues:
            failure_msg = "Structural issues found:\n"
            for filename, issues in structural_issues.items():
                failure_msg += f"\n{filename}:\n"
                for issue in issues:
                    failure_msg += f"  - {issue}\n"
            self.fail(failure_msg)

    def test_modified_files_specific_checks(self):
        """Test specific checks for files we modified"""

        # Test activity3 has the new terminal section
        activity3_path = self.research_dir / "activity3.yaml"
        if activity3_path.exists():
            activity3 = guarded_ai.load_yaml_activity(str(activity3_path))
            section_ids = [s["section_id"] for s in activity3["sections"]]
            self.assertIn("section_5", section_ids, "activity3 should have section_5")

            # Find section_5 and verify it's terminal
            section_5 = next(
                s for s in activity3["sections"] if s["section_id"] == "section_5"
            )
            terminal_step = section_5["steps"][0]
            self.assertNotIn(
                "question", terminal_step, "Terminal step should not have question"
            )
            self.assertNotIn(
                "buckets", terminal_step, "Terminal step should not have buckets"
            )
            self.assertNotIn(
                "transitions",
                terminal_step,
                "Terminal step should not have transitions",
            )

        # Test activity17 has metadata_remove in list format
        activity17_path = self.research_dir / "activity17-choose-adventure.yaml"
        if activity17_path.exists():
            activity17 = guarded_ai.load_yaml_activity(str(activity17_path))
            found_metadata_remove = False

            for section in activity17["sections"]:
                for step in section["steps"]:
                    if "transitions" in step:
                        for transition in step["transitions"].values():
                            if "metadata_remove" in transition:
                                found_metadata_remove = True
                                self.assertIsInstance(
                                    transition["metadata_remove"],
                                    list,
                                    "metadata_remove should be a list",
                                )

            self.assertTrue(
                found_metadata_remove,
                "activity17 should have metadata_remove operations",
            )

        # Test activity20 has integer buckets
        activity20_path = self.research_dir / "activity20-n-plus-1.yaml"
        if activity20_path.exists():
            activity20 = guarded_ai.load_yaml_activity(str(activity20_path))
            found_integer_bucket = False

            for section in activity20["sections"]:
                for step in section["steps"]:
                    if "buckets" in step:
                        for bucket in step["buckets"]:
                            if isinstance(bucket, int):
                                found_integer_bucket = True
                                # Check that transitions exist for integer buckets
                                self.assertIn("transitions", step)
                                # Should have transition for the integer or its string equivalent
                                has_transition = (
                                    bucket in step["transitions"]
                                    or str(bucket) in step["transitions"]
                                )
                                self.assertTrue(
                                    has_transition,
                                    f"Integer bucket {bucket} should have corresponding transition",
                                )

            self.assertTrue(
                found_integer_bucket, "activity20 should have integer buckets"
            )

        # Test battleship files have pre_script
        for battleship_file in [
            "activity29-battleship.yaml",
            "activity29-testship.yaml",
        ]:
            battleship_path = self.research_dir / battleship_file
            if battleship_path.exists():
                battleship = guarded_ai.load_yaml_activity(str(battleship_path))
                found_pre_script = False

                for section in battleship["sections"]:
                    for step in section["steps"]:
                        if "pre_script" in step:
                            found_pre_script = True
                            self.assertIsInstance(step["pre_script"], str)
                            # Should contain win detection logic
                            self.assertIn("user_winning_move", step["pre_script"])
                            self.assertIn("is_game_ending_move", step["pre_script"])

                self.assertTrue(
                    found_pre_script, f"{battleship_file} should have pre_script"
                )

    def test_bucket_transition_consistency_across_files(self):
        """Test that all files have consistent bucket-transition mappings"""
        inconsistent_files = {}

        for activity_file in self.activity_files:
            inconsistencies = []

            try:
                activity = guarded_ai.load_yaml_activity(str(activity_file))

                for section in activity["sections"]:
                    for step in section["steps"]:
                        if "buckets" in step and "transitions" in step:
                            # Check if this step actually has boolean buckets
                            has_boolean_buckets = any(
                                isinstance(b, bool) for b in step["buckets"]
                            )
                            has_integer_buckets = any(
                                isinstance(b, int) for b in step["buckets"]
                            )

                            if has_boolean_buckets or has_integer_buckets:
                                # Skip consistency check for boolean/integer buckets as they have special handling
                                # The matching logic in guarded_ai.py handles these conversions
                                continue

                            # For string buckets, check normal consistency
                            buckets = set(str(b) for b in step["buckets"])
                            transitions = set(
                                str(k) for k in step["transitions"].keys()
                            )

                            # Check for missing transitions
                            missing_transitions = buckets - transitions
                            if missing_transitions:
                                inconsistencies.append(
                                    f"Section {section['section_id']} Step {step['step_id']}: "
                                    f"Missing transitions for buckets: {missing_transitions}"
                                )

                            # Check for extra transitions (less critical)
                            extra_transitions = transitions - buckets
                            # Filter out boolean conversions and integer conversions
                            significant_extras = []
                            for extra in extra_transitions:
                                # Skip if it's a boolean conversion
                                if extra.lower() in ["true", "false"] and any(
                                    isinstance(b, bool) for b in step["buckets"]
                                ):
                                    continue
                                # Skip if it's an integer conversion
                                if extra.isdigit() and any(
                                    isinstance(b, int) and str(b) == extra
                                    for b in step["buckets"]
                                ):
                                    continue
                                significant_extras.append(extra)

                            if significant_extras:
                                inconsistencies.append(
                                    f"Section {section['section_id']} Step {step['step_id']}: "
                                    f"Extra transitions without buckets: {significant_extras}"
                                )

                if inconsistencies:
                    inconsistent_files[activity_file.name] = inconsistencies

            except Exception as e:
                inconsistent_files[activity_file.name] = [f"Failed to check: {e}"]

        if inconsistent_files:
            failure_msg = "Bucket-transition inconsistencies found:\n"
            for filename, inconsistencies in inconsistent_files.items():
                failure_msg += f"\n{filename}:\n"
                for inconsistency in inconsistencies:
                    failure_msg += f"  - {inconsistency}\n"
            self.fail(failure_msg)

    def test_activity_initialization_simulation(self):
        """Test that activities can be initialized for simulation without errors"""
        initialization_errors = {}
        warnings = {}

        with patch("guarded_ai.get_openai_client_and_model") as mock_get_client:
            mock_get_client.return_value = (self.mock_client, "test-model")

            for activity_file in self.activity_files:
                try:
                    activity = guarded_ai.load_yaml_activity(str(activity_file))

                    # Test that we can access the first section and step
                    if activity["sections"]:
                        first_section = activity["sections"][0]
                        if first_section["steps"]:
                            first_step = first_section["steps"][0]

                            # Test that required fields are accessible
                            step_id = first_step["step_id"]
                            self.assertIsInstance(step_id, str)

                            # If step has content_blocks, they should be a list
                            if "content_blocks" in first_step:
                                self.assertIsInstance(
                                    first_step["content_blocks"], list
                                )

                            # If step has question, test categorization setup
                            if "question" in first_step:
                                self.assertIn("buckets", first_step)

                                # tokens_for_ai is optional but recommended
                                if "tokens_for_ai" not in first_step:
                                    warnings[activity_file.name] = (
                                        "Missing tokens_for_ai field (recommended for AI categorization)"
                                    )

                                self.assertIn("transitions", first_step)

                                # Test that categorization inputs are valid
                                buckets = first_step["buckets"]
                                self.assertIsInstance(buckets, list)
                                self.assertTrue(len(buckets) > 0)

                except Exception as e:
                    initialization_errors[activity_file.name] = str(e)

        # Report warnings (but don't fail)
        if warnings:
            print(f"\n=== Initialization Warnings ===")
            for filename, warning in warnings.items():
                print(f"  - {filename}: {warning}")

        # Only fail on actual errors
        if initialization_errors:
            failure_msg = "Activity initialization errors:\n"
            for filename, error in initialization_errors.items():
                failure_msg += f"  - {filename}: {error}\n"
            self.fail(failure_msg)

    def test_metadata_operations_syntax_across_files(self):
        """Test that all metadata operations use correct syntax"""
        syntax_errors = {}

        for activity_file in self.activity_files:
            errors = []

            try:
                activity = guarded_ai.load_yaml_activity(str(activity_file))

                for section in activity["sections"]:
                    for step in section["steps"]:
                        if "transitions" in step:
                            for transition_name, transition in step[
                                "transitions"
                            ].items():

                                # Check metadata_remove format
                                if "metadata_remove" in transition:
                                    metadata_remove = transition["metadata_remove"]
                                    if not isinstance(metadata_remove, list):
                                        errors.append(
                                            f"Section {section['section_id']} Step {step['step_id']} "
                                            f"Transition {transition_name}: metadata_remove should be a list, "
                                            f"got {type(metadata_remove).__name__}"
                                        )

                                # Check metadata_add values
                                if "metadata_add" in transition:
                                    metadata_add = transition["metadata_add"]
                                    if not isinstance(metadata_add, dict):
                                        errors.append(
                                            f"Section {section['section_id']} Step {step['step_id']} "
                                            f"Transition {transition_name}: metadata_add should be a dict"
                                        )

                                # Check metadata_clear format
                                if "metadata_clear" in transition:
                                    metadata_clear = transition["metadata_clear"]
                                    if not isinstance(metadata_clear, bool):
                                        errors.append(
                                            f"Section {section['section_id']} Step {step['step_id']} "
                                            f"Transition {transition_name}: metadata_clear should be boolean"
                                        )

                                # Check metadata_feedback_filter format
                                if "metadata_feedback_filter" in transition:
                                    metadata_filter = transition[
                                        "metadata_feedback_filter"
                                    ]
                                    if not isinstance(metadata_filter, list):
                                        errors.append(
                                            f"Section {section['section_id']} Step {step['step_id']} "
                                            f"Transition {transition_name}: metadata_feedback_filter should be a list"
                                        )

                if errors:
                    syntax_errors[activity_file.name] = errors

            except Exception as e:
                syntax_errors[activity_file.name] = [f"Failed to check syntax: {e}"]

        if syntax_errors:
            failure_msg = "Metadata operation syntax errors found:\n"
            for filename, errors in syntax_errors.items():
                failure_msg += f"\n{filename}:\n"
                for error in errors:
                    failure_msg += f"  - {error}\n"
            self.fail(failure_msg)


class TestActivityFileStatistics(unittest.TestCase):
    """Collect statistics about activity files for reporting"""

    def setUp(self):
        """Set up test environment"""
        self.research_dir = Path(__file__).parent.parent.parent / "research"
        self.activity_files = list(self.research_dir.glob("activity*.yaml"))

    def test_report_activity_file_statistics(self):
        """Generate a report of activity file statistics"""
        stats = {
            "total_files": len(self.activity_files),
            "total_sections": 0,
            "total_steps": 0,
            "files_with_pre_script": 0,
            "files_with_processing_script": 0,
            "files_with_integer_buckets": 0,
            "files_with_boolean_buckets": 0,
            "files_with_metadata_operations": 0,
        }

        for activity_file in self.activity_files:
            try:
                activity = guarded_ai.load_yaml_activity(str(activity_file))

                stats["total_sections"] += len(activity["sections"])

                has_pre_script = False
                has_processing_script = False
                has_integer_buckets = False
                has_boolean_buckets = False
                has_metadata_ops = False

                for section in activity["sections"]:
                    stats["total_steps"] += len(section["steps"])

                    for step in section["steps"]:
                        if "pre_script" in step:
                            has_pre_script = True

                        if "processing_script" in step:
                            has_processing_script = True

                        if "buckets" in step:
                            for bucket in step["buckets"]:
                                if isinstance(bucket, int):
                                    has_integer_buckets = True
                                if isinstance(bucket, bool):
                                    has_boolean_buckets = True

                        if "transitions" in step:
                            for transition in step["transitions"].values():
                                if any(
                                    key.startswith("metadata_")
                                    for key in transition.keys()
                                ):
                                    has_metadata_ops = True

                if has_pre_script:
                    stats["files_with_pre_script"] += 1
                if has_processing_script:
                    stats["files_with_processing_script"] += 1
                if has_integer_buckets:
                    stats["files_with_integer_buckets"] += 1
                if has_boolean_buckets:
                    stats["files_with_boolean_buckets"] += 1
                if has_metadata_ops:
                    stats["files_with_metadata_operations"] += 1

            except Exception as e:
                print(f"Warning: Could not analyze {activity_file.name}: {e}")

        # Print the statistics (this will show in test output)
        print(f"\n=== Activity File Statistics ===")
        print(f"Total files: {stats['total_files']}")
        print(f"Total sections: {stats['total_sections']}")
        print(f"Total steps: {stats['total_steps']}")
        print(f"Files with pre_script: {stats['files_with_pre_script']}")
        print(f"Files with processing_script: {stats['files_with_processing_script']}")
        print(f"Files with integer buckets: {stats['files_with_integer_buckets']}")
        print(f"Files with boolean buckets: {stats['files_with_boolean_buckets']}")
        print(
            f"Files with metadata operations: {stats['files_with_metadata_operations']}"
        )

        # Test passes if we successfully collected statistics
        self.assertGreater(stats["total_files"], 0)
        self.assertGreater(stats["total_sections"], 0)
        self.assertGreater(stats["total_steps"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
