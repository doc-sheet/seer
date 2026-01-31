"""
Explorer Agent for LLM-powered chat functionality.

This agent processes user queries about issues and provides AI-powered responses
using Anthropic Claude.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import anthropic
import sentry_sdk

from seer.automation.explorer.models import (
    Artifact,
    ExplorerStatus,
    Message,
    ToolCall,
    ToolDefinition,
)
from seer.automation.explorer.prompts import get_explorer_system_prompt
from seer.automation.explorer.state import ExplorerRunState
from seer.configuration import AppConfig

logger = logging.getLogger(__name__)


class ExplorerAgent:
    """
    Agent that processes Explorer chat messages using Claude.

    Handles:
    - Loading conversation history from state
    - Building system prompts with user context
    - Calling Claude API
    - Parsing artifacts from responses
    - Updating state with results
    """

    def __init__(
        self,
        state: ExplorerRunState,
        organization_id: int | None = None,
    ):
        self.state = state
        self.organization_id = organization_id
        self._client: anthropic.Anthropic | None = None

    @property
    def client(self) -> anthropic.Anthropic:
        """Get or create the Anthropic client."""
        if self._client is None:
            # Try config first, then environment variable
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            try:
                from seer.dependency_injection import resolve

                app_config = resolve(AppConfig)
                api_key = app_config.ANTHROPIC_API_KEY or api_key
            except Exception:
                pass  # Use environment variable if injection fails

            if not api_key:
                raise ValueError(
                    "ANTHROPIC_API_KEY is required for Explorer functionality. "
                    "Set it in configuration or as an environment variable."
                )
            self._client = anthropic.Anthropic(api_key=api_key)
        return self._client

    def _get_model(self) -> str:
        """Get the model name from configuration."""
        model = os.environ.get("EXPLORER_MODEL")
        if model:
            return model

        try:
            from seer.dependency_injection import resolve

            app_config = resolve(AppConfig)
            if app_config.EXPLORER_MODEL:
                return app_config.EXPLORER_MODEL
        except Exception:
            pass  # Use default if injection fails

        return "claude-sonnet-4-20250514"

    def process_message(
        self,
        query: str,
        artifact_key: str | None = None,
        artifact_schema: dict[str, Any] | None = None,
        tools: list[ToolDefinition] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """
        Process a user message and generate a response.

        Args:
            query: The user's query/message
            artifact_key: Optional key for extracting a specific artifact
            artifact_schema: Optional JSON schema for the artifact
            tools: Optional list of custom tool definitions
            metadata: Optional metadata about the request

        Returns:
            The run_id
        """
        try:
            # Add user message to history
            user_message = Message(role="user", content=query)
            self.state.add_message(user_message)

            # Set loading state
            self.state.set_loading(True)

            # Build messages from history
            messages = self._build_messages()

            # Build system prompt
            system_prompt = get_explorer_system_prompt(
                artifact_key=artifact_key,
                artifact_schema=artifact_schema,
            )

            # Add context from metadata if available
            if metadata:
                context_parts = []
                if "issue" in metadata:
                    context_parts.append(
                        f"## Issue Context\n{json.dumps(metadata['issue'], indent=2)}"
                    )
                if "stacktrace" in metadata:
                    context_parts.append(f"## Stack Trace\n{metadata['stacktrace']}")
                if context_parts:
                    system_prompt += "\n\n" + "\n\n".join(context_parts)

            # Call Claude
            response = self._call_claude(
                messages=messages,
                system_prompt=system_prompt,
                tools=tools,
            )

            # Parse artifacts from response if requested
            artifacts = []
            if artifact_key and artifact_schema:
                extracted = self._extract_artifact(
                    response.content or "",
                    artifact_key,
                    artifact_schema,
                )
                if extracted:
                    artifacts.append(extracted)

            # Add assistant response to history
            assistant_message = Message(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls,
            )
            self.state.add_message(assistant_message, artifacts=artifacts)

            # Update status
            self.state.set_status(ExplorerStatus.COMPLETED)
            self.state.set_loading(False)

        except Exception as e:
            logger.exception(f"Error processing Explorer message: {e}")
            sentry_sdk.capture_exception(e)

            # Add error message
            error_message = Message(
                role="assistant",
                content=f"I encountered an error while processing your request: {str(e)}",
            )
            self.state.add_message(error_message)
            self.state.set_status(ExplorerStatus.ERROR)
            self.state.set_loading(False)

        return self.state.run_id

    def _build_messages(self) -> list[dict[str, Any]]:
        """Build Claude messages from conversation history."""
        messages = []
        memory_blocks = self.state.get_memory()

        for block in memory_blocks:
            msg = block.message
            if msg.role == "user":
                messages.append({"role": "user", "content": msg.content or ""})
            elif msg.role == "assistant":
                if msg.tool_calls:
                    # Handle tool use messages
                    content = []
                    if msg.content:
                        content.append({"type": "text", "text": msg.content})
                    for tool_call in msg.tool_calls:
                        content.append(
                            {
                                "type": "tool_use",
                                "id": tool_call.id or "",
                                "name": tool_call.function,
                                "input": json.loads(tool_call.args) if tool_call.args else {},
                            }
                        )
                    messages.append({"role": "assistant", "content": content})
                else:
                    messages.append({"role": "assistant", "content": msg.content or ""})
            elif msg.role == "tool":
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.tool_call_id or "",
                                "content": msg.content or "",
                            }
                        ],
                    }
                )

        return messages

    def _call_claude(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
        tools: list[ToolDefinition] | None = None,
    ) -> Message:
        """Call Claude API and return the response as a Message."""
        # Build tools if provided
        claude_tools = None
        if tools:
            claude_tools = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.param_schema or {"type": "object", "properties": {}},
                }
                for tool in tools
            ]

        # Get model from configuration
        model = self._get_model()

        try:
            response = self.client.messages.create(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                messages=messages,
                tools=claude_tools if claude_tools else anthropic.NOT_GIVEN,
            )

            # Convert response to Message
            message = Message(role="assistant")

            for block in response.content:
                if block.type == "text":
                    message.content = block.text
                elif block.type == "tool_use":
                    if not message.tool_calls:
                        message.tool_calls = []
                    message.tool_calls.append(
                        ToolCall(
                            id=block.id,
                            function=block.name,
                            args=json.dumps(block.input),
                        )
                    )

            return message

        except anthropic.APIError as e:
            logger.error(f"Anthropic API error: {e}")
            raise

    def _extract_artifact(
        self,
        response_text: str,
        artifact_key: str,
        artifact_schema: dict[str, Any],
    ) -> Artifact | None:
        """
        Try to extract an artifact from the response text.

        This is a simple extraction that looks for JSON blocks in the response.
        More sophisticated extraction could be added based on the schema.
        """
        try:
            # Look for JSON code blocks
            import re

            json_pattern = r"```(?:json)?\s*\n?(.*?)\n?```"
            matches = re.findall(json_pattern, response_text, re.DOTALL)

            for match in matches:
                try:
                    data = json.loads(match.strip())
                    # Basic schema validation - check if required fields are present
                    if "properties" in artifact_schema:
                        required = artifact_schema.get("required", [])
                        if all(key in data for key in required):
                            return Artifact(
                                key=artifact_key,
                                data=data,
                                reason="Extracted from response",
                            )
                except json.JSONDecodeError:
                    continue

            # If no JSON block found, try to extract structured data from the response
            # This is a fallback for simpler cases
            return None

        except Exception as e:
            logger.warning(f"Failed to extract artifact: {e}")
            return None
