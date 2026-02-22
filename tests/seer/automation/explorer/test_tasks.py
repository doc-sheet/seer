"""Tests for Explorer Celery tasks."""

from unittest.mock import MagicMock, patch

import pytest

from seer.automation.explorer.models import ExplorerStatus
from seer.automation.explorer.state import ExplorerRunState
from seer.automation.explorer.tasks import process_explorer_chat
from seer.db import DbRunMemory, DbRunState, Session


@pytest.fixture
def cleanup_explorer_runs():
    """Clean up explorer runs after tests."""
    yield
    with Session() as session:
        session.query(DbRunMemory).delete()
        session.query(DbRunState).filter(DbRunState.type == "explorer").delete()
        session.commit()


class TestProcessExplorerChat:
    @patch("seer.automation.explorer.tasks.ExplorerAgent")
    def test_process_chat_success(self, mock_agent_class, cleanup_explorer_runs):
        """Test successful chat processing."""
        state = ExplorerRunState.create(organization_id=1)

        mock_agent = MagicMock()
        mock_agent_class.return_value = mock_agent

        process_explorer_chat(
            run_id=state.run_id,
            query="What's the issue?",
        )

        # Verify agent was created and process_message was called
        mock_agent_class.assert_called_once()
        mock_agent.process_message.assert_called_once_with(
            query="What's the issue?",
            artifact_key=None,
            artifact_schema=None,
            tools=None,
            metadata=None,
        )

    @patch("seer.automation.explorer.tasks.ExplorerAgent")
    def test_process_chat_with_artifact(self, mock_agent_class, cleanup_explorer_runs):
        """Test chat processing with artifact extraction."""
        state = ExplorerRunState.create(organization_id=1)

        mock_agent = MagicMock()
        mock_agent_class.return_value = mock_agent

        artifact_schema = {"type": "object", "properties": {"cause": {"type": "string"}}}

        process_explorer_chat(
            run_id=state.run_id,
            query="Find the root cause",
            artifact_key="root_cause",
            artifact_schema=artifact_schema,
        )

        mock_agent.process_message.assert_called_once_with(
            query="Find the root cause",
            artifact_key="root_cause",
            artifact_schema=artifact_schema,
            tools=None,
            metadata=None,
        )

    @patch("seer.automation.explorer.tasks.ExplorerAgent")
    def test_process_chat_with_tools(self, mock_agent_class, cleanup_explorer_runs):
        """Test chat processing with custom tools."""
        state = ExplorerRunState.create(organization_id=1)

        mock_agent = MagicMock()
        mock_agent_class.return_value = mock_agent

        tools = [
            {"name": "search", "description": "Search code", "param_schema": {}},
        ]

        process_explorer_chat(
            run_id=state.run_id,
            query="Search for the bug",
            tools=tools,
        )

        # Verify tools were converted to ToolDefinition objects
        call_args = mock_agent.process_message.call_args
        assert call_args.kwargs["tools"] is not None
        assert len(call_args.kwargs["tools"]) == 1

    def test_process_chat_run_not_found(self, cleanup_explorer_runs):
        """Test handling of non-existent run."""
        # Should not raise, just log error
        process_explorer_chat(
            run_id=999999,
            query="Test",
        )

    @patch("seer.automation.explorer.tasks.ExplorerAgent")
    def test_process_chat_exception(self, mock_agent_class, cleanup_explorer_runs):
        """Test error handling during processing."""
        state = ExplorerRunState.create(organization_id=1)

        mock_agent = MagicMock()
        mock_agent.process_message.side_effect = Exception("Processing error")
        mock_agent_class.return_value = mock_agent

        # Should not raise, but should set error status
        process_explorer_chat(
            run_id=state.run_id,
            query="Test",
        )

        # Verify error state was set
        run_state = state.get_state()
        assert run_state.status == ExplorerStatus.ERROR

    @patch("seer.automation.explorer.tasks.ExplorerAgent")
    def test_process_chat_with_metadata(self, mock_agent_class, cleanup_explorer_runs):
        """Test chat processing with metadata."""
        state = ExplorerRunState.create(organization_id=1)

        mock_agent = MagicMock()
        mock_agent_class.return_value = mock_agent

        metadata = {"issue": {"id": 123}, "stacktrace": "..."}

        process_explorer_chat(
            run_id=state.run_id,
            query="Analyze this",
            metadata=metadata,
        )

        call_args = mock_agent.process_message.call_args
        assert call_args.kwargs["metadata"] == metadata
