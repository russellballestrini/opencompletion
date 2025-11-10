#!/usr/bin/env python3
"""
Functional tests for streaming message protocol

Tests the critical streaming functionality that sends real-time messages
via websockets, including the new protocol that separates username/model
from content for cleaner TTS processing.
"""

import unittest
import tempfile
import json
import sys
import threading
import time
from unittest.mock import Mock, patch, MagicMock, call
from pathlib import Path
from queue import Queue

# Add parent directory to path to import the app
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class StreamingProtocolTest(unittest.TestCase):
    """Test streaming message protocol and websocket emissions"""

    def setUp(self):
        """Set up test fixtures with mocked dependencies"""
        self.username = "testuser"
        self.room_name = "test_room"
        self.model_name = "test-model-v1"
        self.test_content = ["Hello", " world", "!", " How", " are", " you?"]

        # Mock external dependencies
        self.mock_socketio = MagicMock()
        self.mock_db = MagicMock()
        self.mock_room = MagicMock()
        self.mock_room.name = self.room_name

        # Track emitted messages
        self.emitted_messages = []
        self.mock_socketio.emit.side_effect = self._capture_emit

    def _capture_emit(self, event_type, data, **kwargs):
        """Capture socketio.emit calls for verification"""
        self.emitted_messages.append(
            {"event": event_type, "data": data, "kwargs": kwargs}
        )

    def test_openai_streaming_protocol(self):
        """Test OpenAI/GPT streaming with new protocol format"""

        # Mock OpenAI streaming response
        mock_chunks = []
        for i, content in enumerate(self.test_content):
            chunk = MagicMock()
            chunk.choices = [MagicMock()]
            chunk.choices[0].delta.content = content
            mock_chunks.append(chunk)

        mock_client = MagicMock()
        mock_completion = MagicMock()
        mock_completion.__iter__.return_value = iter(mock_chunks)
        mock_client.chat.completions.create.return_value = mock_completion

        # Import and patch app with mocks
        with patch.dict(
            "sys.modules",
            {
                "gevent": MagicMock(),
                "flask_socketio": self.mock_socketio,
                "boto3": MagicMock(),
                "openai": MagicMock(),
                "together": MagicMock(),
                "models": MagicMock(),
            },
        ):
            # Mock environment variables to avoid startup error
            with patch.dict(
                "os.environ",
                {
                    "MODEL_ENDPOINT_0": "https://test.api.com",
                    "MODEL_API_KEY_0": "test-key",
                },
            ):
                import app

                # Mock message creation
                mock_message = MagicMock()
                mock_message.id = 123
                mock_message.content = ""

                # Mock database and room operations
                with patch.object(app.db.session, "add"), patch.object(
                    app.db.session, "commit"
                ), patch.object(app.db.session, "query") as mock_query, patch.object(
                    app, "get_room", return_value=self.mock_room
                ), patch.object(
                    app,
                    "get_openai_client_and_model",
                    return_value=(mock_client, self.model_name),
                ), patch.object(
                    app, "socketio", self.mock_socketio
                ), patch(
                    "app.Message", return_value=mock_message
                ):

                    mock_query.return_value.filter.return_value.one_or_none.return_value = (
                        mock_message
                    )

                    # Execute the streaming function
                    app.chat_gpt(self.username, self.room_name, self.model_name)

                    # Update content to simulate accumulation
                    mock_message.content = "".join(self.test_content)

        # Verify the streaming protocol
        message_chunks = [
            msg for msg in self.emitted_messages if msg["event"] == "message_chunk"
        ]

        # Should have one chunk per content piece plus completion signal
        expected_chunks = len(self.test_content) + 1  # +1 for completion
        self.assertEqual(len(message_chunks), expected_chunks)

        # First chunk should have the new protocol format
        first_chunk = message_chunks[0]
        self.assertEqual(first_chunk["data"]["content"], self.test_content[0])
        self.assertEqual(first_chunk["data"]["username"], self.username)
        self.assertEqual(first_chunk["data"]["model_name"], self.model_name)
        self.assertTrue(first_chunk["data"]["is_first_chunk"])
        self.assertEqual(first_chunk["data"]["id"], 123)

        # Subsequent content chunks should be simple format
        for i in range(1, len(self.test_content)):
            chunk = message_chunks[i]
            self.assertEqual(chunk["data"]["content"], self.test_content[i])
            self.assertEqual(chunk["data"]["id"], 123)
            # Should not have username/model in subsequent chunks
            self.assertNotIn("username", chunk["data"])
            self.assertNotIn("model_name", chunk["data"])
            self.assertNotIn("is_first_chunk", chunk["data"])

        # Final chunk should be completion signal
        completion_chunk = message_chunks[-1]
        self.assertEqual(completion_chunk["data"]["content"], "")
        self.assertTrue(completion_chunk["data"]["is_complete"])
        self.assertEqual(completion_chunk["data"]["id"], 123)

    def test_bedrock_streaming_protocol(self):
        """Test AWS Bedrock/Claude streaming with new protocol"""

        # Mock Bedrock streaming response
        mock_events = []
        for content in self.test_content:
            event = {
                "chunk": {
                    "bytes": json.dumps(
                        {
                            "type": "content_block_delta",
                            "delta": {"type": "text_delta", "text": content},
                        }
                    ).encode()
                }
            }
            mock_events.append(event)

        mock_client = MagicMock()
        mock_response = {"body": iter(mock_events)}
        mock_client.invoke_model_with_response_stream.return_value = mock_response

        # Import and test
        with patch.dict(
            "sys.modules",
            {
                "gevent": MagicMock(),
                "flask_socketio": self.mock_socketio,
                "boto3": MagicMock(),
                "openai": MagicMock(),
                "together": MagicMock(),
                "models": MagicMock(),
            },
        ):
            with patch.dict(
                "os.environ",
                {
                    "MODEL_ENDPOINT_0": "https://test.api.com",
                    "MODEL_API_KEY_0": "test-key",
                },
            ):
                import app

                # Mock message creation
                mock_message = MagicMock()
                mock_message.id = 456
                mock_message.content = ""

                with patch.object(app.db.session, "add"), patch.object(
                    app.db.session, "commit"
                ), patch.object(app.db.session, "query") as mock_query, patch.object(
                    app, "get_room", return_value=self.mock_room
                ), patch(
                    "boto3.client", return_value=mock_client
                ), patch.object(
                    app, "socketio", self.mock_socketio
                ), patch(
                    "app.Message", return_value=mock_message
                ):

                    mock_query.return_value.filter.return_value.one_or_none.return_value = (
                        mock_message
                    )

                    # Execute Bedrock streaming
                    app.chat_claude(self.username, self.room_name, self.model_name)

                    # Update content to simulate accumulation
                    mock_message.content = "".join(self.test_content)

        # Verify Bedrock streaming protocol
        message_chunks = [
            msg for msg in self.emitted_messages if msg["event"] == "message_chunk"
        ]

        # Should have content chunks plus completion
        expected_chunks = len(self.test_content) + 1
        self.assertEqual(len(message_chunks), expected_chunks)

        # First chunk verification
        first_chunk = message_chunks[0]
        self.assertEqual(first_chunk["data"]["username"], self.username)
        self.assertEqual(first_chunk["data"]["model_name"], self.model_name)
        self.assertTrue(first_chunk["data"]["is_first_chunk"])

    def test_llama_streaming_protocol(self):
        """Test Llama.cpp streaming with new protocol"""

        # Mock Llama streaming response
        mock_chunks = []
        for content in self.test_content:
            chunk = {"choices": [{"delta": {"content": content}}]}
            mock_chunks.append(chunk)

        mock_model = MagicMock()
        mock_model.create_chat_completion.return_value = iter(mock_chunks)

        with patch.dict(
            "sys.modules",
            {
                "gevent": MagicMock(),
                "flask_socketio": self.mock_socketio,
                "boto3": MagicMock(),
                "openai": MagicMock(),
                "together": MagicMock(),
                "models": MagicMock(),
                "llama_cpp": MagicMock(),
            },
        ):
            with patch.dict(
                "os.environ",
                {
                    "MODEL_ENDPOINT_0": "https://test.api.com",
                    "MODEL_API_KEY_0": "test-key",
                },
            ):
                import app

                # Mock message creation
                mock_message = MagicMock()
                mock_message.id = 789
                mock_message.content = ""

                with patch.object(app.db.session, "add"), patch.object(
                    app.db.session, "commit"
                ), patch.object(app.db.session, "query") as mock_query, patch.object(
                    app, "get_room", return_value=self.mock_room
                ), patch.object(
                    app, "socketio", self.mock_socketio
                ), patch(
                    "app.Message", return_value=mock_message
                ):

                    mock_query.return_value.filter.return_value.one_or_none.return_value = (
                        mock_message
                    )

                    # Mock llama_cpp model loading
                    with patch("llama_cpp.Llama", return_value=mock_model):
                        app.chat_llama(self.username, self.room_name, self.model_name)

                    # Update content to simulate accumulation
                    mock_message.content = "".join(self.test_content)

        # Verify Llama streaming protocol
        message_chunks = [
            msg for msg in self.emitted_messages if msg["event"] == "message_chunk"
        ]

        # Verify protocol consistency across all models
        self.assertGreater(len(message_chunks), 0)
        first_chunk = message_chunks[0]
        self.assertEqual(first_chunk["data"]["username"], self.username)
        self.assertEqual(first_chunk["data"]["model_name"], self.model_name)
        self.assertTrue(first_chunk["data"]["is_first_chunk"])

    def test_streaming_protocol_backwards_compatibility(self):
        """Test that the new protocol maintains expected behavior"""

        # Mock a simple streaming scenario
        content_chunks = ["Hello", " there!"]

        mock_chunks = []
        for content in content_chunks:
            chunk = MagicMock()
            chunk.choices = [MagicMock()]
            chunk.choices[0].delta.content = content
            mock_chunks.append(chunk)

        mock_client = MagicMock()
        mock_completion = MagicMock()
        mock_completion.__iter__.return_value = iter(mock_chunks)
        mock_client.chat.completions.create.return_value = mock_completion

        with patch.dict(
            "sys.modules",
            {
                "gevent": MagicMock(),
                "flask_socketio": self.mock_socketio,
                "boto3": MagicMock(),
                "openai": MagicMock(),
                "together": MagicMock(),
                "models": MagicMock(),
            },
        ):
            with patch.dict(
                "os.environ",
                {
                    "MODEL_ENDPOINT_0": "https://test.api.com",
                    "MODEL_API_KEY_0": "test-key",
                },
            ):
                import app

                with patch.object(app.db.session, "add"), patch.object(
                    app.db.session, "commit"
                ), patch.object(app.db.session, "query") as mock_query, patch.object(
                    app, "get_room", return_value=self.mock_room
                ), patch.object(
                    app,
                    "get_openai_client_and_model",
                    return_value=(mock_client, self.model_name),
                ), patch.object(
                    app, "socketio", self.mock_socketio
                ):

                    mock_message = MagicMock()
                    mock_message.id = 999
                    mock_query.return_value.filter.return_value.one_or_none.return_value = (
                        None
                    )
                    app.Message.return_value = mock_message

                    app.chat_gpt(self.username, self.room_name, self.model_name)

        # Verify key properties of the new protocol
        message_chunks = [
            msg for msg in self.emitted_messages if msg["event"] == "message_chunk"
        ]

        # All chunks should have an ID
        for chunk in message_chunks:
            self.assertIn("id", chunk["data"])
            self.assertEqual(chunk["data"]["id"], 999)

        # First chunk should have metadata fields
        first_chunk = message_chunks[0]
        required_first_chunk_fields = [
            "id",
            "content",
            "username",
            "model_name",
            "is_first_chunk",
        ]
        for field in required_first_chunk_fields:
            self.assertIn(
                field, first_chunk["data"], f"Missing required field: {field}"
            )

        # Content chunks should be minimal
        for i in range(1, len(content_chunks)):
            chunk = message_chunks[i]
            # Should only have id and content
            self.assertEqual(set(chunk["data"].keys()), {"id", "content"})

        # Completion chunk should have is_complete
        completion_chunk = message_chunks[-1]
        self.assertTrue(completion_chunk["data"].get("is_complete", False))

    def test_streaming_content_accumulation(self):
        """Test that streaming content is properly accumulated"""

        test_chunks = ["The", " quick", " brown", " fox"]
        expected_full_content = "".join(test_chunks)

        mock_chunks = []
        for content in test_chunks:
            chunk = MagicMock()
            chunk.choices = [MagicMock()]
            chunk.choices[0].delta.content = content
            mock_chunks.append(chunk)

        mock_client = MagicMock()
        mock_completion = MagicMock()
        mock_completion.__iter__.return_value = iter(mock_chunks)
        mock_client.chat.completions.create.return_value = mock_completion

        with patch.dict(
            "sys.modules",
            {
                "gevent": MagicMock(),
                "flask_socketio": self.mock_socketio,
                "boto3": MagicMock(),
                "openai": MagicMock(),
                "together": MagicMock(),
                "models": MagicMock(),
            },
        ):
            with patch.dict(
                "os.environ",
                {
                    "MODEL_ENDPOINT_0": "https://test.api.com",
                    "MODEL_API_KEY_0": "test-key",
                },
            ):
                import app

                mock_message = MagicMock()
                mock_message.id = 555
                mock_message.content = ""

                with patch.object(app.db.session, "add"), patch.object(
                    app.db.session, "commit"
                ), patch.object(app.db.session, "query") as mock_query, patch.object(
                    app, "get_room", return_value=self.mock_room
                ), patch.object(
                    app,
                    "get_openai_client_and_model",
                    return_value=(mock_client, self.model_name),
                ), patch.object(
                    app, "socketio", self.mock_socketio
                ), patch(
                    "app.Message", return_value=mock_message
                ):

                    mock_query.return_value.filter.return_value.one_or_none.return_value = (
                        mock_message
                    )

                    app.chat_gpt(self.username, self.room_name, self.model_name)

        # Verify that content was properly accumulated in the database
        # The message content should be the full accumulated text
        self.assertEqual(mock_message.content, expected_full_content)

        # Verify individual chunks were sent correctly
        message_chunks = [
            msg
            for msg in self.emitted_messages
            if msg["event"] == "message_chunk" and msg["data"].get("content")
        ]

        # Each chunk should contain its piece of content
        for i, chunk in enumerate(message_chunks[:-1]):  # Exclude completion chunk
            if i < len(test_chunks):
                self.assertEqual(chunk["data"]["content"], test_chunks[i])

    def test_error_handling_in_streaming(self):
        """Test error handling during streaming operations"""

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("API Error")

        with patch.dict(
            "sys.modules",
            {
                "gevent": MagicMock(),
                "flask_socketio": self.mock_socketio,
                "boto3": MagicMock(),
                "openai": MagicMock(),
                "together": MagicMock(),
                "models": MagicMock(),
            },
        ):
            with patch.dict(
                "os.environ",
                {
                    "MODEL_ENDPOINT_0": "https://test.api.com",
                    "MODEL_API_KEY_0": "test-key",
                },
            ):
                import app

                mock_message = MagicMock()
                mock_message.id = 444

                with patch.object(app.db.session, "add"), patch.object(
                    app.db.session, "commit"
                ), patch.object(app.db.session, "query") as mock_query, patch.object(
                    app, "get_room", return_value=self.mock_room
                ), patch.object(
                    app,
                    "get_openai_client_and_model",
                    return_value=(mock_client, self.model_name),
                ), patch.object(
                    app, "socketio", self.mock_socketio
                ), patch(
                    "app.Message", return_value=mock_message
                ):

                    mock_query.return_value.filter.return_value.one_or_none.return_value = (
                        mock_message
                    )

                    # Should not raise exception, should handle gracefully
                    try:
                        app.chat_gpt(self.username, self.room_name, self.model_name)
                    except Exception as e:
                        self.fail(
                            f"Streaming should handle errors gracefully, but got: {e}"
                        )

                    # Should still send chat_message on error
                    error_messages = [
                        msg
                        for msg in self.emitted_messages
                        if msg["event"] == "chat_message"
                    ]
                    self.assertEqual(len(error_messages), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
