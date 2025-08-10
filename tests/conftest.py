#!/usr/bin/env python3
"""
pytest configuration and fixtures for OpenCompletion testing

Sets up common test environment variables and fixtures used across all tests.
"""

import os
import pytest
from unittest.mock import patch, MagicMock

# Set up test environment variables immediately at import time
TEST_ENV_VARS = {
    'MODEL_ENDPOINT_1': 'https://test.api',
    'MODEL_NAME_1': 'test-model', 
    'MODEL_KEY_1': 'test-key'
}

# Apply environment variables immediately for import
os.environ.update(TEST_ENV_VARS)

@pytest.fixture(scope='session', autouse=True)
def setup_test_environment():
    """Set up test environment variables for all tests"""
    with patch.dict(os.environ, TEST_ENV_VARS):
        yield

@pytest.fixture
def mock_openai_client():
    """Mock OpenAI client for testing"""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.choices[0].message.content.strip.return_value = "test response"
    mock_client.chat.completions.create.return_value = mock_response
    return mock_client

@pytest.fixture
def mock_s3_client():
    """Mock S3 client for testing"""
    mock_client = MagicMock()
    mock_response = {
        'Body': MagicMock()
    }
    mock_response['Body'].read.return_value.decode.return_value = "test: content"
    mock_client.get_object.return_value = mock_response
    return mock_client