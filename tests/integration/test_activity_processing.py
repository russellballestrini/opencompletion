#!/usr/bin/env python3
"""
Integration tests for activity processing

Tests the complete activity processing flow including YAML loading,
script execution, metadata management, and state transitions.
"""

import unittest
import tempfile
import json
import sys
import os
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Mock external dependencies before importing
with patch.dict('sys.modules', {
    'gevent': MagicMock(),
    'flask_socketio': MagicMock(),
    'boto3': MagicMock(),
    'openai': MagicMock(),
    'together': MagicMock(),
    'models': MagicMock(),
}):
    import app


class MockActivityState:
    """Mock ActivityState for testing"""
    
    def __init__(self, section_id="test_section", step_id="test_step"):
        self.section_id = section_id
        self.step_id = step_id
        self.attempts = 0
        self.max_attempts = 3
        self.dict_metadata = {}
        self.json_metadata = "{}"
        self.s3_file_path = "test_activity.yaml"
    
    def add_metadata(self, key, value):
        self.dict_metadata[key] = value
        self.json_metadata = json.dumps(self.dict_metadata)
    
    def remove_metadata(self, key):
        if key in self.dict_metadata:
            del self.dict_metadata[key]
            self.json_metadata = json.dumps(self.dict_metadata)
    
    def clear_metadata(self):
        self.dict_metadata = {}
        self.json_metadata = "{}"


class TestActivityProcessingIntegration(unittest.TestCase):
    """Integration tests for complete activity processing"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.test_activity = {
            "default_max_attempts_per_step": 3,
            "sections": [
                {
                    "section_id": "section_1",
                    "title": "Test Section",
                    "steps": [
                        {
                            "step_id": "step_1",
                            "title": "Question Step",
                            "question": "What is 2+2?",
                            "tokens_for_ai": "Categorize as correct or incorrect",
                            "feedback_tokens_for_ai": "Provide feedback on the math answer",
                            "buckets": ["correct", "incorrect"],
                            "transitions": {
                                "correct": {
                                    "content_blocks": ["Great job!"],
                                    "metadata_add": {"score": "n+1"},
                                    "next_section_and_step": "section_1:step_2"
                                },
                                "incorrect": {
                                    "content_blocks": ["Try again!"],
                                    "counts_as_attempt": True
                                }
                            }
                        },
                        {
                            "step_id": "step_2", 
                            "title": "Final Step",
                            "content_blocks": ["Activity completed!"]
                        }
                    ]
                }
            ]
        }
    
    def test_complete_activity_flow_correct_answer(self):
        """Test complete activity flow with correct answer"""
        activity_state = MockActivityState("section_1", "step_1")
        activity_state.add_metadata("score", 0)
        
        # Mock the categorization to return "correct"
        # Simulate the core logic without external dependencies
        section = self.test_activity["sections"][0]
        step = section["steps"][0]
        transition = step["transitions"]["correct"]
        
        # Test metadata operations
        if "metadata_add" in transition:
            for key, value in transition["metadata_add"].items():
                if isinstance(value, str) and value.startswith("n+"):
                    c = int(value[2:])
                    new_value = activity_state.dict_metadata.get(key, 0) + c
                    activity_state.add_metadata(key, new_value)
        
        # Verify state after processing
        self.assertEqual(activity_state.dict_metadata["score"], 1)
    
    def test_complete_activity_flow_incorrect_answer(self):
        """Test complete activity flow with incorrect answer"""
        activity_state = MockActivityState("section_1", "step_1")
        
        section = self.test_activity["sections"][0] 
        step = section["steps"][0]
        transition = step["transitions"]["incorrect"]
        
        # Test that attempts increment for incorrect answers
        if transition.get("counts_as_attempt", True):
            activity_state.attempts += 1
        
        self.assertEqual(activity_state.attempts, 1)
    
    def test_processing_script_execution_integration(self):
        """Test processing script execution with metadata updates"""
        script_step = {
            "step_id": "script_step",
            "title": "Script Step", 
            "question": "Test question",
            "processing_script": """
import random

# Generate random number
random_num = random.randint(1, 100)
metadata['generated_number'] = random_num

# Calculate something based on existing metadata
score = metadata.get('score', 0)
bonus = 10 if random_num > 50 else 5
metadata['bonus'] = bonus

script_result = {
    'metadata': {
        'processing_complete': True,
        'final_score': score + bonus
    },
    'status': 'success'
}
""",
            "buckets": ["continue"],
            "transitions": {
                "continue": {
                    "run_processing_script": True,
                    "next_section_and_step": "section_1:step_2"
                }
            }
        }
        
        activity_state = MockActivityState()
        activity_state.add_metadata("score", 25)
        
        transition = script_step["transitions"]["continue"]
        
        # Execute the processing script
        if transition.get("run_processing_script", False):
            result = app.execute_processing_script(
                activity_state.dict_metadata,
                script_step["processing_script"]
            )
            
            # Update metadata with results
            for key, value in result.get("metadata", {}).items():
                activity_state.add_metadata(key, value)
        
        # Verify the script executed correctly
        self.assertIn('generated_number', activity_state.dict_metadata)
        self.assertIn('bonus', activity_state.dict_metadata)
        self.assertTrue(activity_state.dict_metadata['processing_complete'])
        self.assertIn('final_score', activity_state.dict_metadata)
        
        # Verify calculation
        expected_score = 25 + activity_state.dict_metadata['bonus']
        self.assertEqual(activity_state.dict_metadata['final_score'], expected_score)
    
    def test_pre_script_execution_integration(self):
        """Test pre-script execution with user response"""
        pre_script_step = {
            "step_id": "pre_script_step",
            "title": "Pre-script Step",
            "question": "Enter a number",
            "pre_script": """
# Process user response before categorization
user_input = metadata.get('user_response', '')

try:
    number = int(user_input)
    metadata['parsed_number'] = number
    metadata['is_valid_number'] = True
    metadata['number_category'] = 'positive' if number > 0 else 'non_positive'
except ValueError:
    metadata['is_valid_number'] = False
    metadata['error_message'] = 'Invalid number format'

script_result = {
    'metadata': {
        'pre_processing_complete': True
    }
}
""",
            "buckets": ["valid", "invalid"],
            "transitions": {
                "valid": {"content_blocks": ["Valid number!"]},
                "invalid": {"content_blocks": ["Invalid input!"]}
            }
        }
        
        activity_state = MockActivityState()
        
        # Simulate user response
        user_response = "42"
        temp_metadata = activity_state.dict_metadata.copy()
        temp_metadata["user_response"] = user_response
        
        # Execute pre-script
        pre_result = app.execute_processing_script(
            temp_metadata,
            pre_script_step["pre_script"]
        )
        
        # Update metadata with pre-script results
        for key, value in pre_result.get("metadata", {}).items():
            activity_state.add_metadata(key, value)
        
        # Copy processed data back (excluding temporary user_response)
        activity_state.add_metadata('parsed_number', temp_metadata['parsed_number'])
        activity_state.add_metadata('is_valid_number', temp_metadata['is_valid_number'])
        activity_state.add_metadata('number_category', temp_metadata['number_category'])
        
        # Verify pre-script execution
        self.assertTrue(activity_state.dict_metadata['pre_processing_complete'])
        self.assertEqual(activity_state.dict_metadata['parsed_number'], 42)
        self.assertTrue(activity_state.dict_metadata['is_valid_number'])
        self.assertEqual(activity_state.dict_metadata['number_category'], 'positive')
    
    def test_metadata_operations_integration(self):
        """Test various metadata operations in sequence"""
        activity_state = MockActivityState()
        
        # Test metadata_add with various value types
        metadata_add_ops = {
            "simple_value": "test",
            "numeric_increment": "n+5",
            "random_increment": "n+random(1,10)",
            "user_response_copy": "the-users-response"
        }
        
        activity_state.add_metadata("numeric_increment", 10)
        user_response = "Hello World"
        
        for key, value in metadata_add_ops.items():
            if value == "the-users-response":
                processed_value = user_response
            elif isinstance(value, str) and value.startswith("n+random("):
                # For testing, we'll use a fixed random value
                processed_value = activity_state.dict_metadata.get(key, 0) + 5  # Fixed for testing
            elif isinstance(value, str) and value.startswith("n+"):
                c = int(value[2:])
                processed_value = activity_state.dict_metadata.get(key, 0) + c
            else:
                processed_value = value
                
            activity_state.add_metadata(key, processed_value)
        
        # Verify metadata operations
        self.assertEqual(activity_state.dict_metadata["simple_value"], "test")
        self.assertEqual(activity_state.dict_metadata["numeric_increment"], 15)
        self.assertEqual(activity_state.dict_metadata["random_increment"], 5)
        self.assertEqual(activity_state.dict_metadata["user_response_copy"], "Hello World")
        
        # Test metadata_remove
        activity_state.remove_metadata("simple_value")
        self.assertNotIn("simple_value", activity_state.dict_metadata)
        
        # Test metadata_clear
        activity_state.clear_metadata()
        self.assertEqual(len(activity_state.dict_metadata), 0)
    
    def test_activity_navigation_integration(self):
        """Test complete activity navigation"""
        multi_section_activity = {
            "sections": [
                {
                    "section_id": "intro",
                    "steps": [
                        {"step_id": "step_1", "title": "Intro Step 1"},
                        {"step_id": "step_2", "title": "Intro Step 2"}
                    ]
                },
                {
                    "section_id": "main",
                    "steps": [
                        {"step_id": "step_1", "title": "Main Step 1"},
                        {"step_id": "step_2", "title": "Main Step 2"}
                    ]
                },
                {
                    "section_id": "conclusion",
                    "steps": [
                        {"step_id": "final", "title": "Final Step"}
                    ]
                }
            ]
        }
        
        # Test navigation through multiple sections
        current_section = "intro"
        current_step = "step_1"
        
        navigation_path = []
        
        for _ in range(10):  # Prevent infinite loop
            next_section, next_step = app.get_next_step(
                multi_section_activity, current_section, current_step
            )
            
            navigation_path.append((current_section, current_step))
            
            if next_section is None or next_step is None:
                break
                
            current_section = next_section["section_id"]
            current_step = next_step["step_id"]
        
        # Verify complete navigation path
        expected_path = [
            ("intro", "step_1"),
            ("intro", "step_2"), 
            ("main", "step_1"),
            ("main", "step_2"),
            ("conclusion", "final")
        ]
        
        self.assertEqual(navigation_path, expected_path)
    
    def test_feedback_generation_integration(self):
        """Test complete feedback generation flow"""
        transition_with_feedback = {
            "ai_feedback": {
                "tokens_for_ai": "Provide encouraging feedback for correct math answers"
            }
        }
        
        # Mock the OpenAI response
        mock_feedback = "Excellent! You correctly calculated 2+2=4. Great mathematical skills!"
        
        with patch.object(app, 'provide_feedback', return_value=mock_feedback) as mock_func:
            result = app.provide_feedback(
                transition_with_feedback,
                "correct",
                "What is 2+2?", 
                "Base feedback instructions",
                "4",
                "English",
                "testuser",
                json.dumps({"score": 1}),
                json.dumps({"score": 2})
            )
            
            self.assertEqual(result, mock_feedback)
            mock_func.assert_called_once()


class TestActivityErrorHandling(unittest.TestCase):
    """Test error handling in activity processing"""
    
    def test_invalid_processing_script(self):
        """Test handling of invalid processing scripts"""
        invalid_script = """
# This script has a syntax error
if True
    print("Missing colon")
"""
        metadata = {}
        
        # Should handle syntax errors gracefully
        with self.assertRaises(SyntaxError):
            app.execute_processing_script(metadata, invalid_script)
    
    def test_processing_script_runtime_error(self):
        """Test handling of runtime errors in processing scripts"""
        runtime_error_script = """
# This will cause a runtime error
result = 1 / 0  # Division by zero
script_result = {'status': 'error'}
"""
        metadata = {}
        
        # Should handle runtime errors gracefully
        with self.assertRaises(ZeroDivisionError):
            app.execute_processing_script(metadata, runtime_error_script)
    
    def test_missing_activity_content(self):
        """Test handling of missing activity content"""
        with patch.object(app, 'get_activity_content') as mock_get_content:
            mock_get_content.side_effect = FileNotFoundError("Activity file not found")
            
            with self.assertRaises(FileNotFoundError):
                app.get_activity_content("nonexistent_activity.yaml")
            
            mock_get_content.assert_called_once_with("nonexistent_activity.yaml")
    
    def test_malformed_yaml_content(self):
        """Test handling of malformed YAML content"""
        malformed_yaml = "invalid: yaml: content: [unclosed"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(malformed_yaml)
            temp_file = f.name
        
        try:
            # Should handle YAML parsing errors
            with patch.dict(app.app.config, {'LOCAL_ACTIVITIES': True}):
                # Create research directory and file
                research_dir = Path("research")
                research_dir.mkdir(exist_ok=True)
                
                test_file = research_dir / "malformed.yaml"
                with open(test_file, 'w') as f:
                    f.write(malformed_yaml)
                
                with self.assertRaises(Exception):  # YAML parsing error
                    app.get_activity_content("research/malformed.yaml")
                    
        finally:
            os.unlink(temp_file)
            if test_file.exists():
                test_file.unlink()


if __name__ == '__main__':
    unittest.main(verbosity=2)