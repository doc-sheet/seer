"""
Celery tasks for Explorer functionality.
"""

from __future__ import annotations

import logging
from typing import Any

import sentry_sdk
from celery.exceptions import SoftTimeLimitExceeded

from celery_app.app import celery_app
from seer.automation.explorer.agent import ExplorerAgent
from seer.automation.explorer.models import ExplorerStatus, ToolDefinition
from seer.automation.explorer.state import ExplorerRunState

logger = logging.getLogger(__name__)


@celery_app.task(time_limit=300, soft_time_limit=280)
def process_explorer_chat(
    run_id: int,
    query: str,
    artifact_key: str | None = None,
    artifact_schema: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
):
    """
    Celery task to process an Explorer chat message.

    This task:
    1. Loads the run state
    2. Creates an ExplorerAgent
    3. Processes the user's message
    4. Updates state with the response

    Args:
        run_id: The Explorer run ID
        query: The user's query/message
        artifact_key: Optional key for extracting a specific artifact
        artifact_schema: Optional JSON schema for the artifact
        tools: Optional list of custom tool definitions (as dicts)
        metadata: Optional metadata about the request
    """
    logger.info(f"Processing Explorer chat for run {run_id}")

    try:
        # Load state
        state = ExplorerRunState.get(run_id)
        if state is None:
            logger.error(f"Run {run_id} not found")
            return

        # Convert tool dicts to ToolDefinition objects
        tool_definitions = None
        if tools:
            tool_definitions = [ToolDefinition.model_validate(t) for t in tools]

        # Get organization_id from state metadata
        current_state = state.get_state()
        organization_id = (
            current_state.metadata.get("organization_id") if current_state.metadata else None
        )

        # Create agent and process message
        agent = ExplorerAgent(state=state, organization_id=organization_id)
        agent.process_message(
            query=query,
            artifact_key=artifact_key,
            artifact_schema=artifact_schema,
            tools=tool_definitions,
            metadata=metadata,
        )

        logger.info(f"Explorer chat completed for run {run_id}")

    except SoftTimeLimitExceeded:
        logger.error(f"Explorer chat timed out for run {run_id}")
        try:
            state = ExplorerRunState.get(run_id)
            if state:
                state.set_status(ExplorerStatus.ERROR)
                state.set_loading(False)
        except Exception:
            pass
        sentry_sdk.capture_message(f"Explorer chat timed out for run {run_id}")

    except Exception as e:
        logger.exception(f"Error processing Explorer chat for run {run_id}: {e}")
        sentry_sdk.capture_exception(e)

        try:
            state = ExplorerRunState.get(run_id)
            if state:
                state.set_status(ExplorerStatus.ERROR)
                state.set_loading(False)
        except Exception:
            pass
