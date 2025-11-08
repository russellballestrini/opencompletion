#!/usr/bin/env python3
"""
pytest configuration and fixtures for OpenCompletion testing

Sets up common test environment variables and fixtures used across all tests.
"""

import os
import pytest
import tempfile
from unittest.mock import patch, MagicMock
from pathlib import Path

# Set up test environment variables immediately at import time
TEST_ENV_VARS = {
    "MODEL_ENDPOINT_1": "https://test.api",
    "MODEL_NAME_1": "test-model",
    "MODEL_KEY_1": "test-key",
    "TESTING": "1",
}

# Apply environment variables immediately for import
os.environ.update(TEST_ENV_VARS)


@pytest.fixture(scope="session", autouse=True)
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
    mock_response = {"Body": MagicMock()}
    mock_response["Body"].read.return_value.decode.return_value = "test: content"
    mock_client.get_object.return_value = mock_response
    return mock_client


@pytest.fixture(scope="function")
def test_app():
    """Create a test Flask app with in-memory database"""
    # Import here to avoid circular dependencies
    import app as app_module
    from models import db

    # Create a temporary directory for instance path
    with tempfile.TemporaryDirectory() as tmpdir:
        app_module.app.config["TESTING"] = True
        app_module.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        app_module.app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        app_module.app.config["WTF_CSRF_ENABLED"] = False
        app_module.app.instance_path = tmpdir

        with app_module.app.app_context():
            # Recreate all tables with test config
            db.drop_all()
            db.create_all()
            yield app_module.app
            db.session.remove()
            db.drop_all()
