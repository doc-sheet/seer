"""Tests for Explorer models."""

from seer.automation.explorer.models import (
    Artifact,
    ConduitParams,
    ExplorerChatRequest,
    ExplorerChatResponse,
    ExplorerStateResponse,
    ExplorerStatus,
    MemoryBlock,
    Message,
    SeerRunState,
    ToolCall,
    ToolDefinition,
)


class TestExplorerStatus:
    def test_terminal_statuses(self):
        terminal = ExplorerStatus.terminal()
        assert ExplorerStatus.COMPLETED in terminal
        assert ExplorerStatus.ERROR in terminal
        assert ExplorerStatus.PROCESSING not in terminal
        assert ExplorerStatus.AWAITING_USER_INPUT not in terminal


class TestMessage:
    def test_user_message(self):
        msg = Message(role="user", content="Hello")
        assert msg.role == "user"
        assert msg.content == "Hello"
        assert msg.tool_calls is None

    def test_assistant_message_with_tool_calls(self):
        tool_call = ToolCall(id="tc_1", function="search", args='{"query": "test"}')
        msg = Message(role="assistant", content="Let me search", tool_calls=[tool_call])
        assert msg.role == "assistant"
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].function == "search"


class TestMemoryBlock:
    def test_memory_block_creation(self):
        msg = Message(role="user", content="Test")
        block = MemoryBlock(message=msg)
        assert block.message.content == "Test"
        assert block.loading is False
        assert block.artifacts == []
        assert block.id is not None  # Auto-generated UUID

    def test_memory_block_with_artifacts(self):
        msg = Message(role="assistant", content="Result")
        artifact = Artifact(key="root_cause", data={"cause": "null pointer"}, reason="Found")
        block = MemoryBlock(message=msg, artifacts=[artifact])
        assert len(block.artifacts) == 1
        assert block.artifacts[0].key == "root_cause"


class TestSeerRunState:
    def test_minimal_state(self):
        state = SeerRunState(run_id=1)
        assert state.run_id == 1
        assert state.status == ExplorerStatus.PROCESSING
        assert state.blocks == []

    def test_full_state(self):
        msg = Message(role="user", content="Help me")
        block = MemoryBlock(message=msg)
        state = SeerRunState(
            run_id=123,
            blocks=[block],
            status=ExplorerStatus.COMPLETED,
            metadata={"org_id": 1},
        )
        assert state.run_id == 123
        assert len(state.blocks) == 1
        assert state.status == ExplorerStatus.COMPLETED


class TestExplorerChatRequest:
    def test_basic_request(self):
        req = ExplorerChatRequest(
            organization_id=1,
            query="What's wrong?",
        )
        assert req.get_query() == "What's wrong?"
        assert req.run_id is None

    def test_request_with_message_alias(self):
        req = ExplorerChatRequest(
            organization_id=1,
            message="What's wrong?",  # Using message instead of query
        )
        assert req.get_query() == "What's wrong?"

    def test_request_with_tools(self):
        tool = ToolDefinition(
            name="search_code",
            description="Search the codebase",
            param_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        )
        req = ExplorerChatRequest(
            organization_id=1,
            query="Find the bug",
            tools=[tool],
        )
        assert len(req.tools) == 1
        assert req.tools[0].name == "search_code"

    def test_request_with_conduit(self):
        req = ExplorerChatRequest(
            organization_id=1,
            query="Help",
            conduit=ConduitParams(channel_id="ch_123", url="wss://conduit.example.com"),
        )
        assert req.conduit.channel_id == "ch_123"


class TestExplorerChatResponse:
    def test_processing_response(self):
        resp = ExplorerChatResponse(status="processing", run_id=123)
        assert resp.status == "processing"
        assert resp.run_id == 123

    def test_error_response(self):
        resp = ExplorerChatResponse(status="error", message="Something went wrong")
        assert resp.status == "error"
        assert resp.message == "Something went wrong"


class TestExplorerStateResponse:
    def test_with_session(self):
        state = SeerRunState(run_id=1)
        resp = ExplorerStateResponse(session=state, status="ok")
        assert resp.session.run_id == 1
        assert resp.status == "ok"

    def test_not_found(self):
        resp = ExplorerStateResponse(session=None, status="not_found", message="Run not found")
        assert resp.session is None
        assert resp.status == "not_found"


class TestToolDefinition:
    def test_basic_tool(self):
        tool = ToolDefinition(name="test_tool", description="A test tool")
        assert tool.name == "test_tool"
        assert tool.module_path is None
        assert tool.param_schema is None

    def test_full_tool(self):
        tool = ToolDefinition(
            name="search",
            module_path="sentry.tools.search.SearchTool",
            description="Search for code",
            param_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        )
        assert tool.module_path == "sentry.tools.search.SearchTool"
        assert "required" in tool.param_schema


class TestArtifact:
    def test_basic_artifact(self):
        artifact = Artifact(key="root_cause")
        assert artifact.key == "root_cause"
        assert artifact.data is None
        assert artifact.reason == ""

    def test_artifact_with_data(self):
        artifact = Artifact(
            key="solution",
            data={"fix": "Add null check", "confidence": 0.9},
            reason="Based on stack trace analysis",
        )
        assert artifact.data["fix"] == "Add null check"
        assert artifact.reason == "Based on stack trace analysis"


class TestSerializationRoundtrip:
    def test_memory_block_roundtrip(self):
        msg = Message(
            role="assistant", content="Test", tool_calls=[ToolCall(function="f", args="{}")]
        )
        artifact = Artifact(key="k", data={"v": 1})
        block = MemoryBlock(message=msg, artifacts=[artifact])

        # Serialize and deserialize
        data = block.model_dump(mode="json")
        restored = MemoryBlock.model_validate(data)

        assert restored.message.content == "Test"
        assert restored.artifacts[0].key == "k"

    def test_seer_run_state_roundtrip(self):
        state = SeerRunState(
            run_id=42,
            status=ExplorerStatus.COMPLETED,
            metadata={"org": 1},
        )

        data = state.model_dump(mode="json")
        restored = SeerRunState.model_validate(data)

        assert restored.run_id == 42
        assert restored.status == ExplorerStatus.COMPLETED
