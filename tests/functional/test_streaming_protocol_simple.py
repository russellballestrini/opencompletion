#!/usr/bin/env python3
"""
Simplified functional tests for streaming message protocol

Tests the critical streaming functionality with a focus on the new protocol
that separates username/model from content for cleaner TTS processing.
"""

import unittest
import json
import sys
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path

# Add parent directory to path to import the app
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class StreamingProtocolSimpleTest(unittest.TestCase):
    """Test streaming message protocol with simplified mocking"""

    def setUp(self):
        """Set up test fixtures"""
        self.username = "testuser"
        self.room_name = "test_room"
        self.model_name = "test-model-v1"
        self.test_content = ["Hello", " world", "!"]

        # Track emitted messages
        self.emitted_messages = []

    def _mock_socketio_emit(self, event_type, data, **kwargs):
        """Capture socketio.emit calls"""
        self.emitted_messages.append(
            {"event": event_type, "data": data, "kwargs": kwargs}
        )

    def test_openai_streaming_new_protocol_format(self):
        """Test that OpenAI streaming uses the new protocol format"""

        # Mock OpenAI streaming chunks
        mock_chunks = []
        for content in self.test_content:
            chunk = MagicMock()
            chunk.choices = [MagicMock()]
            chunk.choices[0].delta.content = content
            mock_chunks.append(chunk)

        mock_client = MagicMock()
        mock_completion = MagicMock()
        mock_completion.__iter__.return_value = iter(mock_chunks)
        mock_client.chat.completions.create.return_value = mock_completion

        # Mock dependencies and import app
        with patch.dict(
            "sys.modules",
            {
                "gevent": MagicMock(),
                "flask_socketio": MagicMock(),
                "boto3": MagicMock(),
                "openai": MagicMock(),
                "together": MagicMock(),
                "models": MagicMock(),
            },
        ), patch.dict(
            "os.environ",
            {"MODEL_ENDPOINT_0": "https://test.api", "MODEL_API_KEY_0": "test-key"},
        ):
            import app

            # Mock all the necessary components
            mock_room = MagicMock()
            mock_room.name = self.room_name
            mock_message = MagicMock()
            mock_message.id = 123

            with patch("app.get_room", return_value=mock_room), patch(
                "app.get_openai_client_and_model",
                return_value=(mock_client, self.model_name),
            ), patch("app.socketio.emit", side_effect=self._mock_socketio_emit), patch(
                "app.db.session.add"
            ), patch(
                "app.db.session.commit"
            ), patch(
                "app.db.session.query"
            ) as mock_query, patch(
                "app.Message", return_value=mock_message
            ):

                mock_query.return_value.filter.return_value.one_or_none.return_value = (
                    mock_message
                )

                # Execute the function
                app.chat_gpt(self.username, self.room_name, self.model_name)

        # Verify the new protocol format
        message_chunks = [
            msg for msg in self.emitted_messages if msg["event"] == "message_chunk"
        ]

        # Should have content chunks + completion signal
        self.assertGreater(len(message_chunks), len(self.test_content))

        # First chunk should have new protocol fields
        first_chunk = message_chunks[0]
        first_data = first_chunk["data"]

        # Verify new protocol structure
        required_fields = ["id", "content", "username", "model_name", "is_first_chunk"]
        for field in required_fields:
            self.assertIn(field, first_data, f"Missing required field: {field}")

        # Verify field values
        self.assertEqual(first_data["username"], self.username)
        self.assertEqual(first_data["model_name"], self.model_name)
        self.assertTrue(first_data["is_first_chunk"])
        self.assertEqual(first_data["content"], self.test_content[0])
        self.assertEqual(first_data["id"], 123)

        # Subsequent content chunks should be simpler (no metadata)
        for i in range(1, len(self.test_content)):
            if i < len(message_chunks):
                chunk_data = message_chunks[i]["data"]
                # Should have id and content, but not the metadata fields
                self.assertIn("id", chunk_data)
                self.assertIn("content", chunk_data)
                self.assertNotIn("username", chunk_data)
                self.assertNotIn("model_name", chunk_data)
                self.assertNotIn("is_first_chunk", chunk_data)

        # Should have completion signal
        completion_chunks = [
            msg for msg in message_chunks if msg["data"].get("is_complete")
        ]
        self.assertEqual(len(completion_chunks), 1)

        completion_data = completion_chunks[0]["data"]
        self.assertTrue(completion_data["is_complete"])
        self.assertEqual(completion_data["content"], "")

    def test_protocol_consistency_across_models(self):
        """Test that all streaming models use consistent protocol"""

        # Just test OpenAI for now to keep test simple
        self.emitted_messages.clear()

        mock_client = self._setup_openai_mock()

        with patch.dict(
            "sys.modules",
            {
                "gevent": MagicMock(),
                "flask_socketio": MagicMock(),
                "boto3": MagicMock(),
                "openai": MagicMock(),
                "together": MagicMock(),
                "models": MagicMock(),
            },
        ), patch.dict(
            "os.environ",
            {"MODEL_ENDPOINT_0": "https://test.api", "MODEL_API_KEY_0": "test-key"},
        ):
            import app

            mock_room = MagicMock()
            mock_room.name = self.room_name
            mock_message = MagicMock()
            mock_message.id = 999

            with patch("app.get_room", return_value=mock_room), patch(
                "app.socketio.emit", side_effect=self._mock_socketio_emit
            ), patch("app.db.session.add"), patch("app.db.session.commit"), patch(
                "app.db.session.query"
            ) as mock_query, patch(
                "app.Message", return_value=mock_message
            ), patch(
                "app.get_openai_client_and_model",
                return_value=(mock_client, self.model_name),
            ):

                mock_query.return_value.filter.return_value.one_or_none.return_value = (
                    mock_message
                )

                # Execute the function
                app.chat_gpt(self.username, self.room_name, self.model_name)

        # Verify consistent protocol
        message_chunks = [
            msg for msg in self.emitted_messages if msg["event"] == "message_chunk"
        ]

        if len(message_chunks) > 0:
            first_chunk = message_chunks[0]["data"]

            # Should use the new protocol
            protocol_fields = ["username", "model_name", "is_first_chunk"]
            for field in protocol_fields:
                self.assertIn(field, first_chunk, f"Missing protocol field: {field}")

    def _setup_openai_mock(self):
        """Setup OpenAI-specific mocks"""
        mock_chunks = []
        for content in self.test_content:
            chunk = MagicMock()
            chunk.choices = [MagicMock()]
            chunk.choices[0].delta.content = content
            mock_chunks.append(chunk)

        mock_client = MagicMock()
        mock_completion = MagicMock()
        mock_completion.__iter__.return_value = iter(mock_chunks)
        mock_client.chat.completions.create.return_value = mock_completion
        return mock_client

    def _setup_bedrock_mock(self):
        """Setup Bedrock-specific mocks"""
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
        return mock_client

    def test_protocol_separates_content_from_metadata(self):
        """Test that content is separate from username/model metadata"""

        test_message = "This is test content"

        # Mock single chunk
        mock_chunk = MagicMock()
        mock_chunk.choices = [MagicMock()]
        mock_chunk.choices[0].delta.content = test_message

        mock_client = MagicMock()
        mock_completion = MagicMock()
        mock_completion.__iter__.return_value = iter([mock_chunk])
        mock_client.chat.completions.create.return_value = mock_completion

        with patch.dict(
            "sys.modules",
            {
                "gevent": MagicMock(),
                "flask_socketio": MagicMock(),
                "boto3": MagicMock(),
                "openai": MagicMock(),
                "together": MagicMock(),
                "models": MagicMock(),
            },
        ), patch.dict(
            "os.environ",
            {"MODEL_ENDPOINT_0": "https://test.api", "MODEL_API_KEY_0": "test-key"},
        ):
            import app

            mock_room = MagicMock()
            mock_room.name = self.room_name
            mock_message = MagicMock()
            mock_message.id = 555

            with patch("app.get_room", return_value=mock_room), patch(
                "app.get_openai_client_and_model",
                return_value=(mock_client, self.model_name),
            ), patch("app.socketio.emit", side_effect=self._mock_socketio_emit), patch(
                "app.db.session.add"
            ), patch(
                "app.db.session.commit"
            ), patch(
                "app.db.session.query"
            ) as mock_query, patch(
                "app.Message", return_value=mock_message
            ):

                mock_query.return_value.filter.return_value.one_or_none.return_value = (
                    mock_message
                )

                app.chat_gpt(self.username, self.room_name, self.model_name)

        # Find the first chunk
        message_chunks = [
            msg for msg in self.emitted_messages if msg["event"] == "message_chunk"
        ]
        self.assertGreater(len(message_chunks), 0)

        first_chunk = message_chunks[0]["data"]

        # Critical test: content should NOT contain the old format
        content = first_chunk["content"]
        self.assertEqual(content, test_message)  # Should be pure content
        self.assertNotIn(
            f"**{self.username}", content
        )  # Should not have old markdown format
        self.assertNotIn(
            f"({self.model_name})", content
        )  # Should not have model name in content

        # Metadata should be in separate fields
        self.assertEqual(first_chunk["username"], self.username)
        self.assertEqual(first_chunk["model_name"], self.model_name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
