"""Tests for Explorer API endpoints."""

from unittest.mock import patch

import pytest
from flask import Flask

from seer.automation.explorer.models import ExplorerStatus
from seer.automation.explorer.state import ExplorerRunState as ExplorerRunStateClass
from seer.configuration import AppConfig
from seer.db import DbRunMemory, DbRunState, Session
from seer.dependency_injection import resolve


@pytest.fixture
def cleanup_explorer_runs():
    """Clean up explorer runs after tests."""
    yield
    with Session() as session:
        session.query(DbRunMemory).delete()
        session.query(DbRunState).filter(DbRunState.type == "explorer").delete()
        session.commit()


@pytest.fixture
def app():
    """Create Flask test app."""
    from seer.app import blueprint

    app = Flask(__name__)
    app.register_blueprint(blueprint)
    return app


@pytest.fixture
def client(app):
    """Create test client."""
    app_config = resolve(AppConfig)
    app_config.IGNORE_API_AUTH = True
    return app.test_client()


class TestExplorerRunsEndpoint:
    def test_list_runs_empty(self, client, cleanup_explorer_runs):
        """Test listing runs when none exist."""
        response = client.post(
            "/v1/automation/explorer/runs",
            json={"organization_id": 1},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["runs"] == []

    def test_list_runs_with_results(self, client, cleanup_explorer_runs):
        """Test listing runs with results."""
        # Create some runs
        ExplorerRunStateClass.create(organization_id=1)
        ExplorerRunStateClass.create(organization_id=1)

        response = client.post(
            "/v1/automation/explorer/runs",
            json={"organization_id": 1},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert len(data["runs"]) == 2


class TestExplorerChatEndpoint:
    def test_chat_no_api_key(self, client, cleanup_explorer_runs):
        """Test chat endpoint without API key configured."""
        app_config = resolve(AppConfig)
        original_key = app_config.ANTHROPIC_API_KEY
        app_config.ANTHROPIC_API_KEY = None

        with patch.dict("os.environ", {}, clear=True):
            with patch.dict("os.environ", {"DATABASE_URL": "test", "CELERY_BROKER_URL": "test"}):
                response = client.post(
                    "/v1/automation/explorer/chat",
                    json={
                        "organization_id": 1,
                        "query": "Help me",
                    },
                )

        # Restore
        app_config.ANTHROPIC_API_KEY = original_key

        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "not_available"
        assert "ANTHROPIC_API_KEY" in data["message"]

    @patch("seer.app.process_explorer_chat")
    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    def test_chat_new_run(self, mock_task, client, cleanup_explorer_runs):
        """Test starting a new chat run."""
        app_config = resolve(AppConfig)
        app_config.ANTHROPIC_API_KEY = "test-key"

        response = client.post(
            "/v1/automation/explorer/chat",
            json={
                "organization_id": 1,
                "query": "What's wrong?",
            },
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "processing"
        assert data["run_id"] is not None

        # Verify task was queued
        mock_task.delay.assert_called_once()

    @patch("seer.app.process_explorer_chat")
    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    def test_chat_existing_run(self, mock_task, client, cleanup_explorer_runs):
        """Test continuing an existing chat run."""
        app_config = resolve(AppConfig)
        app_config.ANTHROPIC_API_KEY = "test-key"

        # Create a run first
        state = ExplorerRunStateClass.create(organization_id=1)

        response = client.post(
            "/v1/automation/explorer/chat",
            json={
                "run_id": state.run_id,
                "query": "More help please",
            },
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "processing"
        assert data["run_id"] == state.run_id

    @patch("seer.app.process_explorer_chat")
    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    def test_chat_run_not_found(self, mock_task, client, cleanup_explorer_runs):
        """Test chat with non-existent run ID."""
        app_config = resolve(AppConfig)
        app_config.ANTHROPIC_API_KEY = "test-key"

        response = client.post(
            "/v1/automation/explorer/chat",
            json={
                "run_id": 999999,
                "query": "Help",
            },
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "error"
        assert "not found" in data["message"]

    @patch("seer.app.process_explorer_chat")
    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    def test_chat_no_query(self, mock_task, client, cleanup_explorer_runs):
        """Test chat without query."""
        app_config = resolve(AppConfig)
        app_config.ANTHROPIC_API_KEY = "test-key"

        response = client.post(
            "/v1/automation/explorer/chat",
            json={
                "organization_id": 1,
                # No query provided
            },
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "error"
        assert "No query" in data["message"]


class TestExplorerStateEndpoint:
    def test_get_state_success(self, client, cleanup_explorer_runs):
        """Test getting run state."""
        state = ExplorerRunStateClass.create(organization_id=1)

        response = client.post(
            "/v1/automation/explorer/state",
            json={"run_id": state.run_id},
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "ok"
        assert data["session"]["run_id"] == state.run_id

    def test_get_state_not_found(self, client, cleanup_explorer_runs):
        """Test getting non-existent run state."""
        response = client.post(
            "/v1/automation/explorer/state",
            json={"run_id": 999999},
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "not_found"
        assert data["session"] is None


class TestExplorerUpdateEndpoint:
    def test_update_cancel(self, client, cleanup_explorer_runs):
        """Test canceling a run."""
        state = ExplorerRunStateClass.create(organization_id=1)

        response = client.post(
            "/v1/automation/explorer/update",
            json={
                "run_id": state.run_id,
                "update_type": "cancel",
            },
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "ok"

        # Verify run was canceled
        run_state = state.get_state()
        assert run_state.status == ExplorerStatus.COMPLETED

    def test_update_not_found(self, client, cleanup_explorer_runs):
        """Test updating non-existent run."""
        response = client.post(
            "/v1/automation/explorer/update",
            json={
                "run_id": 999999,
                "update_type": "cancel",
            },
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "error"
        assert "not found" in data["message"]
