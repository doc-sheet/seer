"""
State management for Explorer runs.

Uses DbRunState with type="explorer" to store run metadata and state.
Conversation history is stored in DbRunMemory.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import logging
from enum import Enum
from typing import Any

from sqlalchemy import select

from seer.automation.explorer.models import (
    Artifact,
    ExplorerRunInfo,
    ExplorerStatus,
    MemoryBlock,
    Message,
    SeerRunState,
)
from seer.db import DbRunMemory, DbRunState, Session

logger = logging.getLogger(__name__)


class DbStateRunTypes(str, Enum):
    """Run types stored in DbRunState."""

    EXPLORER = "explorer"


@dataclasses.dataclass
class ExplorerRunState:
    """
    State wrapper for Explorer runs.

    Provides methods to create, retrieve, and update Explorer run state
    stored in DbRunState and DbRunMemory.
    """

    run_id: int
    type: DbStateRunTypes = DbStateRunTypes.EXPLORER

    @classmethod
    def create(
        cls,
        *,
        organization_id: int | None = None,
        category_key: str | None = None,
        category_value: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "ExplorerRunState":
        """Create a new Explorer run."""
        now = datetime.datetime.now(datetime.UTC)

        initial_state = SeerRunState(
            run_id=-1,  # Will be set after insert
            blocks=[],
            status=ExplorerStatus.PROCESSING,
            updated_at=now.isoformat(),
            metadata=metadata or {},
        )

        # Store category info in metadata
        state_metadata = metadata or {}
        if organization_id is not None:
            state_metadata["organization_id"] = organization_id
        if category_key is not None:
            state_metadata["category_key"] = category_key
        if category_value is not None:
            state_metadata["category_value"] = category_value

        initial_state.metadata = state_metadata

        with Session() as session:
            db_state = DbRunState(
                value=initial_state.model_dump(mode="json"),
                type=cls.type.value,
            )
            session.add(db_state)
            session.flush()

            # Update run_id in state
            initial_state.run_id = db_state.id
            db_state.value = initial_state.model_dump(mode="json")
            session.merge(db_state)

            # Create empty memory
            db_memory = DbRunMemory(run_id=db_state.id, value=[])
            session.add(db_memory)

            session.commit()

            return cls(run_id=db_state.id)

    @classmethod
    def get(cls, run_id: int) -> "ExplorerRunState | None":
        """Get an existing Explorer run by ID."""
        with Session() as session:
            db_state = session.get(DbRunState, run_id)
            if db_state is None:
                return None
            if db_state.type != cls.type.value:
                logger.warning(f"Run {run_id} has type {db_state.type}, expected {cls.type.value}")
                return None
            return cls(run_id=run_id)

    @classmethod
    def list(
        cls,
        *,
        organization_id: int | None = None,
        category_key: str | None = None,
        category_value: str | None = None,
        limit: int = 50,
    ) -> list[ExplorerRunInfo]:
        """List Explorer runs, optionally filtered by organization and category."""
        with Session() as session:
            query = select(DbRunState).where(DbRunState.type == cls.type.value)

            # Order by most recent first
            query = query.order_by(DbRunState.updated_at.desc()).limit(limit)

            results = session.execute(query).scalars().all()

            runs = []
            for db_state in results:
                state = SeerRunState.model_validate(db_state.value)

                # Filter by organization/category if specified
                if organization_id is not None:
                    if state.metadata and state.metadata.get("organization_id") != organization_id:
                        continue

                if category_key is not None:
                    if state.metadata and state.metadata.get("category_key") != category_key:
                        continue

                if category_value is not None:
                    if state.metadata and state.metadata.get("category_value") != category_value:
                        continue

                runs.append(
                    ExplorerRunInfo(
                        run_id=db_state.id,
                        created_at=db_state.created_at.isoformat(),
                        updated_at=db_state.updated_at.isoformat(),
                        status=state.status,
                        category_key=state.metadata.get("category_key") if state.metadata else None,
                        category_value=(
                            state.metadata.get("category_value") if state.metadata else None
                        ),
                    )
                )

            return runs

    def get_state(self) -> SeerRunState:
        """Get the current state of the run."""
        with Session() as session:
            db_state = session.get(DbRunState, self.run_id)
            if db_state is None:
                raise ValueError(f"No state found for run {self.run_id}")
            return SeerRunState.model_validate(db_state.value)

    def get_memory(self) -> list[MemoryBlock]:
        """Get the conversation history."""
        with Session() as session:
            db_memory = session.get(DbRunMemory, self.run_id)
            if db_memory is None:
                return []
            blocks_data = db_memory.value if isinstance(db_memory.value, list) else []
            return [MemoryBlock.model_validate(block) for block in blocks_data]

    @contextlib.contextmanager
    def update(self):
        """
        Context manager for atomically updating the run state.

        Uses SELECT FOR UPDATE to ensure thread safety.
        """
        with Session() as session:
            db_state = session.execute(
                select(DbRunState).where(DbRunState.id == self.run_id).with_for_update()
            ).scalar_one_or_none()

            if db_state is None:
                raise ValueError(f"No state found for run {self.run_id}")

            state = SeerRunState.model_validate(db_state.value)
            yield state

            # Update timestamps
            state.updated_at = datetime.datetime.now(datetime.UTC).isoformat()
            db_state.value = state.model_dump(mode="json")
            db_state.updated_at = datetime.datetime.now(datetime.UTC)

            session.merge(db_state)
            session.commit()

    def add_message(self, message: Message, artifacts: list[Artifact] | None = None) -> MemoryBlock:
        """Add a message to the conversation history."""
        block = MemoryBlock(
            message=message,
            artifacts=artifacts or [],
        )

        with Session() as session:
            # Get or create memory
            db_memory = session.get(DbRunMemory, self.run_id)
            if db_memory is None:
                db_memory = DbRunMemory(run_id=self.run_id, value=[])
                session.add(db_memory)
                session.flush()

            # Add block to memory - create new list to trigger SQLAlchemy change detection
            existing_blocks = list(db_memory.value) if isinstance(db_memory.value, list) else []
            existing_blocks.append(block.model_dump(mode="json"))
            db_memory.value = existing_blocks  # Assign new list

            session.commit()

        # Also update state blocks
        with self.update() as state:
            state.blocks.append(block)

        return block

    def set_artifact(self, key: str, data: dict[str, Any] | None, reason: str = "") -> Artifact:
        """Set an artifact on the most recent assistant message."""
        artifact = Artifact(key=key, data=data, reason=reason)

        with Session() as session:
            db_memory = session.get(DbRunMemory, self.run_id)
            if db_memory is None:
                raise ValueError(f"No memory found for run {self.run_id}")

            blocks = db_memory.value if isinstance(db_memory.value, list) else []

            # Find the most recent assistant message and add artifact
            for block_data in reversed(blocks):
                if block_data.get("message", {}).get("role") == "assistant":
                    if "artifacts" not in block_data:
                        block_data["artifacts"] = []
                    block_data["artifacts"].append(artifact.model_dump(mode="json"))
                    break

            db_memory.value = blocks
            session.merge(db_memory)
            session.commit()

        # Also update state blocks
        with self.update() as state:
            for block in reversed(state.blocks):
                if block.message.role == "assistant":
                    block.artifacts.append(artifact)
                    break

        return artifact

    def set_status(self, status: ExplorerStatus):
        """Set the run status."""
        with self.update() as state:
            state.status = status

    def set_loading(self, loading: bool):
        """Set loading state on the most recent block."""
        with self.update() as state:
            if state.blocks:
                state.blocks[-1].loading = loading

    def to_seer_run_state(self) -> SeerRunState:
        """Convert to SeerRunState for API response."""
        return self.get_state()
