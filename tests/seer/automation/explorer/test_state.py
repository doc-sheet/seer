"""Tests for Explorer state management."""

import pytest

from seer.automation.explorer.models import Artifact, ExplorerStatus, Message, SeerRunState
from seer.automation.explorer.state import DbStateRunTypes, ExplorerRunState
from seer.db import DbRunMemory, DbRunState, Session


@pytest.fixture
def cleanup_explorer_runs():
    """Clean up explorer runs after tests."""
    yield
    with Session() as session:
        session.query(DbRunMemory).delete()
        session.query(DbRunState).filter(DbRunState.type == "explorer").delete()
        session.commit()


class TestExplorerRunState:
    def test_create_run(self, cleanup_explorer_runs):
        """Test creating a new explorer run."""
        state = ExplorerRunState.create(
            organization_id=123,
            category_key="issue",
            category_value="456",
            metadata={"extra": "data"},
        )

        assert state.run_id is not None
        assert state.run_id > 0

        # Verify state was persisted
        retrieved = ExplorerRunState.get(state.run_id)
        assert retrieved is not None

        run_state = retrieved.get_state()
        assert run_state.run_id == state.run_id
        assert run_state.status == ExplorerStatus.PROCESSING
        assert run_state.metadata["organization_id"] == 123
        assert run_state.metadata["category_key"] == "issue"

    def test_get_nonexistent_run(self, cleanup_explorer_runs):
        """Test getting a run that doesn't exist."""
        result = ExplorerRunState.get(999999)
        assert result is None

    def test_get_wrong_type(self, cleanup_explorer_runs):
        """Test getting a run with wrong type returns None."""
        # Create a run with a different type
        with Session() as session:
            db_state = DbRunState(
                value={"run_id": -1},
                type="autofix",  # Different type
            )
            session.add(db_state)
            session.commit()
            wrong_id = db_state.id

        result = ExplorerRunState.get(wrong_id)
        assert result is None

    def test_list_runs(self, cleanup_explorer_runs):
        """Test listing explorer runs."""
        # Create some runs
        state1 = ExplorerRunState.create(
            organization_id=1, category_key="issue", category_value="100"
        )
        ExplorerRunState.create(organization_id=1, category_key="issue", category_value="200")
        ExplorerRunState.create(organization_id=2, category_key="issue", category_value="300")

        # List all runs for org 1
        runs = ExplorerRunState.list(organization_id=1)
        assert len(runs) == 2

        # List runs for specific category
        runs = ExplorerRunState.list(organization_id=1, category_value="100")
        assert len(runs) == 1
        assert runs[0].run_id == state1.run_id

    def test_add_message(self, cleanup_explorer_runs):
        """Test adding messages to conversation history."""
        state = ExplorerRunState.create(organization_id=1)

        # Add user message
        user_msg = Message(role="user", content="What's wrong?")
        block1 = state.add_message(user_msg)
        assert block1.message.content == "What's wrong?"

        # Add assistant response
        assistant_msg = Message(role="assistant", content="The issue is...")
        state.add_message(assistant_msg)

        # Verify history
        memory = state.get_memory()
        assert len(memory) == 2
        assert memory[0].message.role == "user"
        assert memory[1].message.role == "assistant"

    def test_add_message_with_artifacts(self, cleanup_explorer_runs):
        """Test adding messages with artifacts."""
        state = ExplorerRunState.create(organization_id=1)

        msg = Message(role="assistant", content="Found the root cause")
        artifact = Artifact(key="root_cause", data={"cause": "null pointer"})
        block = state.add_message(msg, artifacts=[artifact])

        assert len(block.artifacts) == 1
        assert block.artifacts[0].key == "root_cause"

        # Verify in memory
        memory = state.get_memory()
        assert len(memory[0].artifacts) == 1

    def test_set_artifact(self, cleanup_explorer_runs):
        """Test setting an artifact on the most recent message."""
        state = ExplorerRunState.create(organization_id=1)

        # Add a message first
        msg = Message(role="assistant", content="Analyzing...")
        state.add_message(msg)

        # Set artifact
        artifact = state.set_artifact("solution", {"fix": "Add null check"}, reason="Found fix")

        assert artifact.key == "solution"
        assert artifact.data["fix"] == "Add null check"

    def test_set_status(self, cleanup_explorer_runs):
        """Test setting run status."""
        state = ExplorerRunState.create(organization_id=1)

        assert state.get_state().status == ExplorerStatus.PROCESSING

        state.set_status(ExplorerStatus.COMPLETED)
        assert state.get_state().status == ExplorerStatus.COMPLETED

        state.set_status(ExplorerStatus.ERROR)
        assert state.get_state().status == ExplorerStatus.ERROR

    def test_set_loading(self, cleanup_explorer_runs):
        """Test setting loading state."""
        state = ExplorerRunState.create(organization_id=1)

        # Add a message
        msg = Message(role="assistant", content="Working...")
        state.add_message(msg)

        # Set loading
        state.set_loading(True)
        run_state = state.get_state()
        assert run_state.blocks[-1].loading is True

        state.set_loading(False)
        run_state = state.get_state()
        assert run_state.blocks[-1].loading is False

    def test_update_context_manager(self, cleanup_explorer_runs):
        """Test atomic updates via context manager."""
        state = ExplorerRunState.create(organization_id=1)

        with state.update() as run_state:
            run_state.status = ExplorerStatus.COMPLETED
            run_state.metadata["updated"] = True

        # Verify changes persisted
        updated = state.get_state()
        assert updated.status == ExplorerStatus.COMPLETED
        assert updated.metadata["updated"] is True

    def test_to_seer_run_state(self, cleanup_explorer_runs):
        """Test converting to SeerRunState for API response."""
        state = ExplorerRunState.create(organization_id=1, metadata={"test": True})

        msg = Message(role="user", content="Hello")
        state.add_message(msg)
        state.set_status(ExplorerStatus.COMPLETED)

        seer_state = state.to_seer_run_state()

        assert isinstance(seer_state, SeerRunState)
        assert seer_state.run_id == state.run_id
        assert seer_state.status == ExplorerStatus.COMPLETED
        assert len(seer_state.blocks) == 1


class TestDbStateRunTypes:
    def test_explorer_type(self):
        assert DbStateRunTypes.EXPLORER.value == "explorer"
