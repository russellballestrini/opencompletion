#!/usr/bin/env python3
"""
Universal YAML Validator for Activity Configurations

This module provides comprehensive validation for activity YAML files,
particularly battleship configurations and other interactive activities.
It validates structure, syntax, Python code blocks, and logical consistency.
"""

import yaml
import ast
import re
import sys
import argparse
from typing import Dict, List, Any, Optional, Tuple
from pathlib import Path


class ValidationError(Exception):
    """Custom exception for validation errors"""

    pass


class ActivityYAMLValidator:
    """
    Comprehensive validator for activity YAML configurations

    Validates:
    - YAML syntax and structure
    - Required fields and schema compliance
    - Python code blocks (processing_script, pre_script)
    - Logic flow and transitions
    - Battleship-specific rules
    - Token limits and AI prompt structures
    """

    def __init__(self):
        self.errors = []
        self.warnings = []
        self.current_file = None

    def validate_file(self, file_path: str) -> Tuple[bool, List[str], List[str]]:
        """
        Validate a YAML file and return results

        Returns:
            Tuple of (is_valid, errors, warnings)
        """
        self.errors = []
        self.warnings = []
        self.current_file = file_path

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            # Parse YAML
            try:
                data = yaml.safe_load(content)
            except yaml.YAMLError as e:
                self.errors.append(f"YAML syntax error: {e}")
                return False, self.errors, self.warnings

            # Validate structure
            self._validate_structure(data)

            # Validate sections
            if "sections" in data:
                self._validate_sections(data["sections"])

            # Validate universal activity rules
            self._validate_activity_rules(data)

            # Validate Python code blocks
            self._validate_python_code(data)

            # Validate logic flow
            self._validate_logic_flow(data)

            return len(self.errors) == 0, self.errors, self.warnings

        except Exception as e:
            self.errors.append(f"Unexpected error: {e}")
            return False, self.errors, self.warnings

    def _validate_structure(self, data: Dict[str, Any]):
        """Validate basic YAML structure"""
        if not isinstance(data, dict):
            self.errors.append("Root level must be a dictionary")
            return

        # Check required top-level fields
        required_fields = ["sections"]
        for field in required_fields:
            if field not in data:
                self.errors.append(f"Missing required field: {field}")

        # Validate optional fields
        if "default_max_attempts_per_step" in data:
            if (
                not isinstance(data["default_max_attempts_per_step"], int)
                or data["default_max_attempts_per_step"] < 1
            ):
                self.errors.append(
                    "default_max_attempts_per_step must be a positive integer"
                )

        if "tokens_for_ai_rubric" in data:
            if not isinstance(data["tokens_for_ai_rubric"], str):
                self.errors.append("tokens_for_ai_rubric must be a string")

    def _validate_sections(self, sections: List[Dict[str, Any]]):
        """Validate sections structure"""
        if not isinstance(sections, list):
            self.errors.append("sections must be a list")
            return

        if not sections:
            self.errors.append("At least one section is required")
            return

        section_ids = set()
        for i, section in enumerate(sections):
            if not isinstance(section, dict):
                self.errors.append(f"Section {i} must be a dictionary")
                continue

            # Validate section structure
            self._validate_section(section, i)

            # Check for duplicate section IDs
            if "section_id" in section:
                if section["section_id"] in section_ids:
                    self.errors.append(f"Duplicate section_id: {section['section_id']}")
                section_ids.add(section["section_id"])

    def _validate_section(self, section: Dict[str, Any], section_index: int):
        """Validate individual section"""
        required_fields = ["section_id", "title", "steps"]
        for field in required_fields:
            if field not in section:
                self.errors.append(
                    f"Section {section_index}: Missing required field '{field}'"
                )

        if "steps" in section:
            self._validate_steps(
                section["steps"], section.get("section_id", f"section_{section_index}")
            )

    def _validate_steps(self, steps: List[Dict[str, Any]], section_id: str):
        """Validate steps within a section"""
        if not isinstance(steps, list):
            self.errors.append(f"Section {section_id}: steps must be a list")
            return

        if not steps:
            self.errors.append(f"Section {section_id}: At least one step is required")
            return

        step_ids = set()
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                self.errors.append(
                    f"Section {section_id}, step {i}: Must be a dictionary"
                )
                continue

            self._validate_step(step, section_id, i)

            # Check for duplicate step IDs
            if "step_id" in step:
                if step["step_id"] in step_ids:
                    self.errors.append(
                        f"Section {section_id}: Duplicate step_id '{step['step_id']}'"
                    )
                step_ids.add(step["step_id"])

    def _validate_step(self, step: Dict[str, Any], section_id: str, step_index: int):
        """Validate individual step"""
        step_id = step.get("step_id", f"step_{step_index}")

        # Required fields
        required_fields = ["step_id", "title"]
        for field in required_fields:
            if field not in step:
                self.errors.append(
                    f"Section {section_id}, step {step_id}: Missing required field '{field}'"
                )

        # Validate content_blocks or question
        has_content = "content_blocks" in step
        has_question = "question" in step

        if not has_content and not has_question:
            self.errors.append(
                f"Section {section_id}, step {step_id}: Must have either 'content_blocks' or 'question'"
            )

        if has_content:
            self._validate_content_blocks(step["content_blocks"], section_id, step_id)

        if has_question:
            self._validate_question_step(step, section_id, step_id)

    def _validate_content_blocks(
        self, content_blocks: List[str], section_id: str, step_id: str
    ):
        """Validate content blocks"""
        if not isinstance(content_blocks, list):
            self.errors.append(
                f"Section {section_id}, step {step_id}: content_blocks must be a list"
            )
            return

        for i, block in enumerate(content_blocks):
            if not isinstance(block, str):
                self.errors.append(
                    f"Section {section_id}, step {step_id}: content_blocks[{i}] must be a string"
                )

    def _validate_question_step(
        self, step: Dict[str, Any], section_id: str, step_id: str
    ):
        """Validate question-type step"""
        if "question" in step and not isinstance(step["question"], str):
            self.errors.append(
                f"Section {section_id}, step {step_id}: 'question' must be a string"
            )

        # Validate AI tokens
        if "tokens_for_ai" in step:
            if not isinstance(step["tokens_for_ai"], str):
                self.errors.append(
                    f"Section {section_id}, step {step_id}: 'tokens_for_ai' must be a string"
                )

        if "feedback_tokens_for_ai" in step:
            if not isinstance(step["feedback_tokens_for_ai"], str):
                self.errors.append(
                    f"Section {section_id}, step {step_id}: 'feedback_tokens_for_ai' must be a string"
                )

        # Validate feedback_prompts (new multi-prompt system)
        if "feedback_prompts" in step:
            self._validate_feedback_prompts(
                step["feedback_prompts"], section_id, step_id
            )

        # Validate buckets and transitions
        if "buckets" in step:
            self._validate_buckets(step["buckets"], section_id, step_id)

        if "transitions" in step:
            self._validate_transitions(
                step["transitions"], step.get("buckets", []), section_id, step_id
            )

    def _validate_feedback_prompts(
        self, feedback_prompts: List[Dict[str, Any]], section_id: str, step_id: str
    ):
        """Validate feedback_prompts structure"""
        if not isinstance(feedback_prompts, list):
            self.errors.append(
                f"Section {section_id}, step {step_id}: 'feedback_prompts' must be a list"
            )
            return

        if len(feedback_prompts) == 0:
            self.errors.append(
                f"Section {section_id}, step {step_id}: 'feedback_prompts' cannot be empty"
            )
            return

        prompt_names = set()
        for i, prompt in enumerate(feedback_prompts):
            if not isinstance(prompt, dict):
                self.errors.append(
                    f"Section {section_id}, step {step_id}: feedback_prompts[{i}] must be a dictionary"
                )
                continue

            # Required fields for each prompt
            required_fields = ["name", "tokens_for_ai"]
            for field in required_fields:
                if field not in prompt:
                    self.errors.append(
                        f"Section {section_id}, step {step_id}: feedback_prompts[{i}] missing required field '{field}'"
                    )

            # Validate name uniqueness
            if "name" in prompt:
                if not isinstance(prompt["name"], str):
                    self.errors.append(
                        f"Section {section_id}, step {step_id}: feedback_prompts[{i}].name must be a string"
                    )
                else:
                    if prompt["name"] in prompt_names:
                        self.errors.append(
                            f"Section {section_id}, step {step_id}: duplicate feedback prompt name '{prompt['name']}'"
                        )
                    prompt_names.add(prompt["name"])

            # Validate tokens_for_ai
            if "tokens_for_ai" in prompt:
                if not isinstance(prompt["tokens_for_ai"], str):
                    self.errors.append(
                        f"Section {section_id}, step {step_id}: feedback_prompts[{i}].tokens_for_ai must be a string"
                    )
                # Check for STFU token usage (informational)
                elif "STFU" in prompt["tokens_for_ai"]:
                    # This is valid - STFU token is used to suppress empty feedback messages
                    pass

            # Validate metadata_filter (optional)
            if "metadata_filter" in prompt:
                if not isinstance(prompt["metadata_filter"], list):
                    self.errors.append(
                        f"Section {section_id}, step {step_id}: feedback_prompts[{i}].metadata_filter must be a list"
                    )
                else:
                    for j, filter_key in enumerate(prompt["metadata_filter"]):
                        if not isinstance(filter_key, str):
                            self.errors.append(
                                f"Section {section_id}, step {step_id}: feedback_prompts[{i}].metadata_filter[{j}] must be a string"
                            )

    def _validate_buckets(self, buckets: List[str], section_id: str, step_id: str):
        """Validate buckets list"""
        if not isinstance(buckets, list):
            self.errors.append(
                f"Section {section_id}, step {step_id}: 'buckets' must be a list"
            )
            return

        if not buckets:
            self.warnings.append(
                f"Section {section_id}, step {step_id}: Empty buckets list"
            )
            return

        for i, bucket in enumerate(buckets):
            if not isinstance(bucket, (str, int, bool)):
                self.errors.append(
                    f"Section {section_id}, step {step_id}: buckets[{i}] must be a string, integer, or boolean"
                )

    def _validate_transitions(
        self,
        transitions: Dict[str, Any],
        buckets: List[Any],
        section_id: str,
        step_id: str,
    ):
        """Validate transitions dictionary"""
        if not isinstance(transitions, dict):
            self.errors.append(
                f"Section {section_id}, step {step_id}: 'transitions' must be a dictionary"
            )
            return

        # Check that all buckets have corresponding transitions
        for bucket in buckets:
            if bucket not in transitions:
                self.errors.append(
                    f"Section {section_id}, step {step_id}: Missing transition for bucket '{bucket}'"
                )

        # Check for unused transitions
        for transition_key in transitions:
            if transition_key not in buckets:
                self.warnings.append(
                    f"Section {section_id}, step {step_id}: Unused transition '{transition_key}'"
                )

        # Validate each transition
        for bucket, transition in transitions.items():
            self._validate_transition(transition, bucket, section_id, step_id)

    def _validate_transition(
        self, transition: Dict[str, Any], bucket: str, section_id: str, step_id: str
    ):
        """Validate individual transition"""
        if not isinstance(transition, dict):
            self.errors.append(
                f"Section {section_id}, step {step_id}, bucket {bucket}: Transition must be a dictionary"
            )
            return

        # Validate next_section_and_step format
        if "next_section_and_step" in transition:
            next_step = transition["next_section_and_step"]
            if not isinstance(next_step, str):
                self.errors.append(
                    f"Section {section_id}, step {step_id}, bucket {bucket}: 'next_section_and_step' must be a string"
                )
            elif ":" not in next_step:
                self.errors.append(
                    f"Section {section_id}, step {step_id}, bucket {bucket}: 'next_section_and_step' must be in format 'section_id:step_id'"
                )

        # Validate metadata operations
        metadata_fields = [
            "metadata_add",
            "metadata_tmp_add",
            "metadata_remove",
            "metadata_clear",
            "metadata_feedback_filter",
        ]
        for field in metadata_fields:
            if field in transition:
                if field == "metadata_clear":
                    if not isinstance(transition[field], bool):
                        self.errors.append(
                            f"Section {section_id}, step {step_id}, bucket {bucket}: '{field}' must be boolean"
                        )
                elif field == "metadata_feedback_filter":
                    if not isinstance(transition[field], list):
                        self.errors.append(
                            f"Section {section_id}, step {step_id}, bucket {bucket}: '{field}' must be a list"
                        )
                    else:
                        for item in transition[field]:
                            if not isinstance(item, str):
                                self.errors.append(
                                    f"Section {section_id}, step {step_id}, bucket {bucket}: '{field}' items must be strings"
                                )
                elif field == "metadata_remove":
                    if isinstance(transition[field], str):
                        # Single key to remove
                        pass
                    elif isinstance(transition[field], list):
                        # List of keys to remove
                        for item in transition[field]:
                            if not isinstance(item, str):
                                self.errors.append(
                                    f"Section {section_id}, step {step_id}, bucket {bucket}: '{field}' list items must be strings"
                                )
                    else:
                        self.errors.append(
                            f"Section {section_id}, step {step_id}, bucket {bucket}: '{field}' must be a string or list of strings"
                        )
                else:
                    if not isinstance(transition[field], dict):
                        self.errors.append(
                            f"Section {section_id}, step {step_id}, bucket {bucket}: '{field}' must be a dictionary"
                        )

        # Validate other transition fields
        if "run_processing_script" in transition:
            if not isinstance(transition["run_processing_script"], bool):
                self.errors.append(
                    f"Section {section_id}, step {step_id}, bucket {bucket}: 'run_processing_script' must be boolean"
                )

        if "ai_feedback" in transition:
            ai_feedback = transition["ai_feedback"]
            if not isinstance(ai_feedback, dict):
                self.errors.append(
                    f"Section {section_id}, step {step_id}, bucket {bucket}: 'ai_feedback' must be a dictionary"
                )
            elif "tokens_for_ai" in ai_feedback and not isinstance(
                ai_feedback["tokens_for_ai"], str
            ):
                self.errors.append(
                    f"Section {section_id}, step {step_id}, bucket {bucket}: ai_feedback.tokens_for_ai must be a string"
                )

        if "content_blocks" in transition:
            if not isinstance(transition["content_blocks"], list):
                self.errors.append(
                    f"Section {section_id}, step {step_id}, bucket {bucket}: 'content_blocks' must be a list"
                )
            else:
                for i, block in enumerate(transition["content_blocks"]):
                    if not isinstance(block, str):
                        self.errors.append(
                            f"Section {section_id}, step {step_id}, bucket {bucket}: content_blocks[{i}] must be a string"
                        )

    def _validate_python_code(self, data: Dict[str, Any]):
        """Validate Python code blocks in scripts"""

        def validate_code_block(code: str, location: str):
            if not code or not isinstance(code, str):
                return

            try:
                # Parse the code to check for syntax errors
                ast.parse(code)
            except SyntaxError as e:
                self.errors.append(f"{location}: Python syntax error - {e}")
            except Exception as e:
                self.errors.append(f"{location}: Python parsing error - {e}")

            # Check for common issues
            self._check_python_code_quality(code, location)

        # Recursively find and validate all Python code blocks
        self._find_and_validate_scripts(data, validate_code_block)

    def _find_and_validate_scripts(self, obj: Any, validator, path: str = "root"):
        """Recursively find and validate Python scripts"""
        if isinstance(obj, dict):
            for key, value in obj.items():
                current_path = f"{path}.{key}"
                if key in ["processing_script", "pre_script"] and isinstance(
                    value, str
                ):
                    validator(value, current_path)
                else:
                    self._find_and_validate_scripts(value, validator, current_path)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                self._find_and_validate_scripts(item, validator, f"{path}[{i}]")

    def _check_python_code_quality(self, code: str, location: str):
        """Check Python code for common issues and best practices"""
        lines = code.split("\n")

        # Check for empty except blocks
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("except"):
                # Look for the next non-empty line
                next_line_idx = i + 1
                while next_line_idx < len(lines) and not lines[next_line_idx].strip():
                    next_line_idx += 1

                if next_line_idx < len(lines):
                    next_line = lines[next_line_idx].strip()
                    if next_line == "pass":
                        self.warnings.append(
                            f"{location} line {i+1}: Empty except block with only 'pass'"
                        )

        # Check for potential security issues
        dangerous_patterns = [
            ("exec(", "Use of exec() can be dangerous"),
            ("eval(", "Use of eval() can be dangerous"),
            ("__import__(", "Dynamic imports should be used carefully"),
        ]

        for pattern, message in dangerous_patterns:
            if pattern in code:
                self.warnings.append(f"{location}: {message}")

        # Check for proper indentation in else blocks
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped == "else:":
                # Check if the next non-empty line exists and is properly indented
                next_line_idx = i + 1
                while next_line_idx < len(lines) and not lines[next_line_idx].strip():
                    next_line_idx += 1

                if next_line_idx >= len(lines):
                    self.errors.append(
                        f"{location} line {i+1}: 'else:' block has no content"
                    )
                elif next_line_idx < len(lines):
                    next_line = lines[next_line_idx]
                    if not next_line.strip():
                        continue  # Skip empty lines
                    # Check if it's just a comment
                    if next_line.strip().startswith("#") and next_line_idx + 1 < len(
                        lines
                    ):
                        following_line_idx = next_line_idx + 1
                        while (
                            following_line_idx < len(lines)
                            and not lines[following_line_idx].strip()
                        ):
                            following_line_idx += 1
                        if following_line_idx >= len(lines) or lines[
                            following_line_idx
                        ].strip().startswith("#"):
                            self.errors.append(
                                f"{location} line {i+1}: 'else:' block contains only comments - add 'pass' statement"
                            )

    def _validate_activity_rules(self, data: Dict[str, Any]):
        """Validate universal activity rules"""
        if "sections" not in data:
            return

        sections = data["sections"]

        # Find truly terminal steps (last step of last section with no transitions)
        for section_idx, section in enumerate(sections):
            if "steps" not in section:
                continue

            steps = section["steps"]
            if not steps:
                continue

            # Check if this is the last section
            is_last_section = section_idx == len(sections) - 1

            for step_idx, step in enumerate(steps):
                step_id = step.get("step_id", "unknown")
                section_id = section.get("section_id", "unknown")

                # Check if this is the last step in the section
                is_last_step_in_section = step_idx == len(steps) - 1

                # A step is truly terminal only if:
                # 1. It's the last step of the last section AND has no transitions with next_section_and_step
                # OR
                # 2. All its transitions explicitly end the activity (no next_section_and_step anywhere)
                is_terminal = False

                if "transitions" in step:
                    # Check if any transition continues the flow
                    has_continuing_transition = False
                    for transition in step["transitions"].values():
                        if "next_section_and_step" in transition:
                            has_continuing_transition = True
                            break

                    # If this is the last step of the last section and has no continuing transitions
                    if (
                        is_last_section
                        and is_last_step_in_section
                        and not has_continuing_transition
                    ):
                        is_terminal = True
                elif is_last_section and is_last_step_in_section:
                    # No transitions at all and it's the last step of the last section
                    is_terminal = True

                # Only validate true terminal steps
                if is_terminal:
                    if "question" in step:
                        self.errors.append(
                            f"Section {section_id}, step {step_id}: Final/terminal steps cannot have questions"
                        )

                    if "buckets" in step and step["buckets"]:
                        self.errors.append(
                            f"Section {section_id}, step {step_id}: Final/terminal steps should not have buckets"
                        )

        # Validate metadata_feedback_filter usage
        self._validate_metadata_filters(data)

        # Validate pre_script usage
        self._validate_pre_scripts(data)

    def _validate_metadata_filters(self, data: Dict[str, Any]):
        """Validate metadata_feedback_filter usage"""
        if "sections" not in data:
            return

        for section in data["sections"]:
            if "steps" not in section:
                continue

            section_id = section.get("section_id", "unknown")
            for step in section["steps"]:
                step_id = step.get("step_id", "unknown")
                if "transitions" not in step:
                    continue

                for bucket, transition in step["transitions"].items():
                    if "metadata_feedback_filter" in transition:
                        # Check if step has feedback_tokens_for_ai
                        if "feedback_tokens_for_ai" not in step:
                            self.warnings.append(
                                f"Section {section_id}, step {step_id}: metadata_feedback_filter used but no feedback_tokens_for_ai defined"
                            )

    def _validate_pre_scripts(self, data: Dict[str, Any]):
        """Validate pre_script usage"""
        if "sections" not in data:
            return

        for section in data["sections"]:
            if "steps" not in section:
                continue

            section_id = section.get("section_id", "unknown")
            for step in section["steps"]:
                step_id = step.get("step_id", "unknown")

                if "pre_script" in step:
                    # Check if step has a question (pre_script should be used with questions)
                    if "question" not in step:
                        self.warnings.append(
                            f"Section {section_id}, step {step_id}: pre_script typically used with question steps"
                        )

                    # Validate pre_script is a string
                    if not isinstance(step["pre_script"], str):
                        self.errors.append(
                            f"Section {section_id}, step {step_id}: pre_script must be a string"
                        )

    def _validate_logic_flow(self, data: Dict[str, Any]):
        """Validate logical flow and transitions between steps"""
        if "sections" not in data:
            return

        # Build a map of all available steps
        all_steps = {}
        for section in data["sections"]:
            section_id = section.get("section_id")
            if not section_id or "steps" not in section:
                continue

            for step in section["steps"]:
                step_id = step.get("step_id")
                if step_id:
                    all_steps[f"{section_id}:{step_id}"] = step

        # Validate all transition targets
        for section in data["sections"]:
            section_id = section.get("section_id")
            if not section_id or "steps" not in section:
                continue

            for step in section["steps"]:
                step_id = step.get("step_id")
                if not step_id or "transitions" not in step:
                    continue

                for bucket, transition in step["transitions"].items():
                    if "next_section_and_step" in transition:
                        target = transition["next_section_and_step"]
                        if target not in all_steps:
                            self.errors.append(
                                f"Section {section_id}, step {step_id}: Invalid transition target '{target}'"
                            )


def main():
    """Command line interface for the validator"""
    parser = argparse.ArgumentParser(description="Validate activity YAML files")
    parser.add_argument("files", nargs="+", help="YAML files to validate")
    parser.add_argument(
        "--strict", action="store_true", help="Treat warnings as errors"
    )
    parser.add_argument("--quiet", action="store_true", help="Only show errors")

    args = parser.parse_args()

    validator = ActivityYAMLValidator()
    total_errors = 0
    total_warnings = 0

    for file_path in args.files:
        if not Path(file_path).exists():
            print(f"❌ File not found: {file_path}")
            total_errors += 1
            continue

        if not args.quiet:
            print(f"\n📄 Validating: {file_path}")
            print("=" * 50)

        is_valid, errors, warnings = validator.validate_file(file_path)

        if errors:
            print(f"❌ {len(errors)} error(s):")
            for error in errors:
                print(f"   • {error}")
            total_errors += len(errors)

        if warnings and not args.quiet:
            print(f"⚠️  {len(warnings)} warning(s):")
            for warning in warnings:
                print(f"   • {warning}")
            total_warnings += len(warnings)

        if is_valid and not warnings:
            print(f"✅ {file_path} is valid!")
        elif is_valid:
            print(f"✅ {file_path} is valid (with warnings)")
        else:
            print(f"❌ {file_path} has errors")

    # Summary
    if not args.quiet:
        print(f"\n📊 Summary:")
        print(f"   Files checked: {len(args.files)}")
        print(f"   Errors: {total_errors}")
        print(f"   Warnings: {total_warnings}")

    # Exit code
    exit_code = 0
    if total_errors > 0:
        exit_code = 1
    elif args.strict and total_warnings > 0:
        exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
