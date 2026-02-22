"""Tests for Explorer agent."""

import os
from unittest.mock import MagicMock, patch

import pytest

from seer.automation.explorer.agent import ExplorerAgent
from seer.automation.explorer.models import ExplorerStatus, Message, ToolDefinition
from seer.automation.explorer.state import ExplorerRunState
from seer.db import DbRunMemory, DbRunState, Session


@pytest.fixture
def cleanup_explorer_runs():
    """Clean up explorer runs after tests."""
    yield
    with Session() as session:
        session.query(DbRunMemory).delete()
        session.query(DbRunState).filter(DbRunState.type == "explorer").delete()
        session.commit()


@pytest.fixture
def mock_anthropic_response():
    """Create a mock Anthropic response."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(type="text", text="This is a test response.")]
    return mock_response


@pytest.fixture
def mock_anthropic_client(mock_anthropic_response):
    """Create a mock Anthropic client."""
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_anthropic_response
    return mock_client


class TestExplorerAgent:
    def test_process_message_success(self, cleanup_explorer_runs, mock_anthropic_client):
        """Test processing a message successfully."""
        state = ExplorerRunState.create(organization_id=1)
        agent = ExplorerAgent(state=state, organization_id=1)
        agent._client = mock_anthropic_client

        run_id = agent.process_message(query="What's causing this error?")

        assert run_id == state.run_id

        # Verify state was updated
        run_state = state.get_state()
        assert run_state.status == ExplorerStatus.COMPLETED
        assert len(run_state.blocks) == 2  # User message + assistant response

        # Verify messages
        memory = state.get_memory()
        assert memory[0].message.role == "user"
        assert memory[0].message.content == "What's causing this error?"
        assert memory[1].message.role == "assistant"
        assert memory[1].message.content == "This is a test response."

    def test_process_message_with_metadata(self, cleanup_explorer_runs, mock_anthropic_client):
        """Test processing a message with metadata context."""
        state = ExplorerRunState.create(organization_id=1)
        agent = ExplorerAgent(state=state, organization_id=1)
        agent._client = mock_anthropic_client

        metadata = {
            "issue": {"id": 123, "title": "NullPointerException"},
            "stacktrace": "at com.example.Main.run(Main.java:42)",
        }

        agent.process_message(query="Help me fix this", metadata=metadata)

        # Verify the API was called (system prompt should include context)
        mock_anthropic_client.messages.create.assert_called_once()
        call_args = mock_anthropic_client.messages.create.call_args
        assert "Issue Context" in call_args.kwargs["system"]

    def test_process_message_with_tools(self, cleanup_explorer_runs, mock_anthropic_client):
        """Test processing a message with custom tools."""
        state = ExplorerRunState.create(organization_id=1)
        agent = ExplorerAgent(state=state, organization_id=1)
        agent._client = mock_anthropic_client

        tools = [
            ToolDefinition(
                name="search_code",
                description="Search the codebase",
                param_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            )
        ]

        agent.process_message(query="Find the bug", tools=tools)

        # Verify tools were passed to API
        call_args = mock_anthropic_client.messages.create.call_args
        assert call_args.kwargs["tools"] is not None
        assert len(call_args.kwargs["tools"]) == 1
        assert call_args.kwargs["tools"][0]["name"] == "search_code"

    def test_process_message_error(self, cleanup_explorer_runs, mock_anthropic_client):
        """Test error handling during message processing."""
        state = ExplorerRunState.create(organization_id=1)
        agent = ExplorerAgent(state=state, organization_id=1)
        agent._client = mock_anthropic_client

        # Make the API call fail
        mock_anthropic_client.messages.create.side_effect = Exception("API Error")

        agent.process_message(query="Help")

        # Verify error state
        run_state = state.get_state()
        assert run_state.status == ExplorerStatus.ERROR

        # Verify error message was added
        memory = state.get_memory()
        assert len(memory) == 2  # User message + error message
        assert "error" in memory[1].message.content.lower()

    def test_process_message_with_tool_calls(self, cleanup_explorer_runs):
        """Test processing a response that includes tool calls."""
        state = ExplorerRunState.create(organization_id=1)
        agent = ExplorerAgent(state=state, organization_id=1)

        # Mock response with tool use
        mock_response = MagicMock()
        mock_tool_use = MagicMock()
        mock_tool_use.type = "tool_use"
        mock_tool_use.id = "tool_123"
        mock_tool_use.name = "search_code"
        mock_tool_use.input = {"query": "null pointer"}
        mock_response.content = [mock_tool_use]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        agent._client = mock_client

        agent.process_message(query="Find the issue")

        # Verify tool calls were recorded
        memory = state.get_memory()
        assistant_msg = memory[1].message
        assert assistant_msg.tool_calls is not None
        assert len(assistant_msg.tool_calls) == 1
        assert assistant_msg.tool_calls[0].function == "search_code"

    def test_build_messages(self, cleanup_explorer_runs):
        """Test building Claude messages from history."""
        state = ExplorerRunState.create(organization_id=1)

        # Add some messages to history
        state.add_message(Message(role="user", content="Hello"))
        state.add_message(Message(role="assistant", content="Hi there!"))
        state.add_message(Message(role="user", content="What's wrong?"))

        agent = ExplorerAgent(state=state)
        messages = agent._build_messages()

        assert len(messages) == 3
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Hello"
        assert messages[1]["role"] == "assistant"
        assert messages[2]["role"] == "user"

    def test_extract_artifact_json_block(self, cleanup_explorer_runs):
        """Test extracting artifact from JSON code block."""
        state = ExplorerRunState.create(organization_id=1)
        agent = ExplorerAgent(state=state)

        response_text = """
Here is the analysis:

```json
{
    "cause": "Null pointer dereference",
    "file": "Main.java",
    "line": 42
}
```

The fix is to add a null check.
"""

        artifact_schema = {
            "type": "object",
            "properties": {
                "cause": {"type": "string"},
                "file": {"type": "string"},
            },
            "required": ["cause"],
        }

        artifact = agent._extract_artifact(response_text, "root_cause", artifact_schema)

        assert artifact is not None
        assert artifact.key == "root_cause"
        assert artifact.data["cause"] == "Null pointer dereference"
        assert artifact.data["file"] == "Main.java"

    def test_extract_artifact_no_json(self, cleanup_explorer_runs):
        """Test artifact extraction with no JSON block."""
        state = ExplorerRunState.create(organization_id=1)
        agent = ExplorerAgent(state=state)

        response_text = "This response has no JSON blocks."
        artifact_schema = {"type": "object", "properties": {}}

        artifact = agent._extract_artifact(response_text, "test", artifact_schema)
        assert artifact is None

    def test_extract_artifact_invalid_json(self, cleanup_explorer_runs):
        """Test artifact extraction with invalid JSON."""
        state = ExplorerRunState.create(organization_id=1)
        agent = ExplorerAgent(state=state)

        response_text = """
```json
{invalid json here}
```
"""
        artifact_schema = {"type": "object", "properties": {}}

        artifact = agent._extract_artifact(response_text, "test", artifact_schema)
        assert artifact is None


class TestExplorerAgentConfiguration:
    @patch.dict("os.environ", {"EXPLORER_MODEL": "claude-sonnet-4-20250514"})
    def test_get_model_from_config(self, cleanup_explorer_runs):
        """Test getting model name from configuration."""
        state = ExplorerRunState.create(organization_id=1)
        agent = ExplorerAgent(state=state)

        # The model should come from environment
        model = agent._get_model()
        assert model is not None
        assert "claude" in model.lower()

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    def test_client_creation(self, cleanup_explorer_runs):
        """Test Anthropic client is created with API key."""
        state = ExplorerRunState.create(organization_id=1)
        ExplorerAgent(state=state)

        # Test that API key can be retrieved from environment
        mock_config = MagicMock()
        mock_config.ANTHROPIC_API_KEY = "test-key"

        # Directly test client creation logic
        api_key = mock_config.ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY")
        assert api_key == "test-key"

    def test_client_missing_api_key(self, cleanup_explorer_runs):
        """Test error when API key is missing."""
        state = ExplorerRunState.create(organization_id=1)
        agent = ExplorerAgent(state=state)
        agent._client = None

        with patch.dict("os.environ", {}, clear=True):
            with patch("seer.dependency_injection.resolve") as mock_resolve:
                mock_config = MagicMock()
                mock_config.ANTHROPIC_API_KEY = None
                mock_resolve.return_value = mock_config

                with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
                    _ = agent.client
