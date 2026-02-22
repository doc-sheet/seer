"""Tests for Explorer prompts."""

from seer.automation.explorer.prompts import (
    ARTIFACT_INSTRUCTIONS_DEFAULT,
    ARTIFACT_INSTRUCTIONS_WITH_SCHEMA,
    EXPLORER_SYSTEM_PROMPT,
    get_explorer_system_prompt,
)


class TestExplorerSystemPrompt:
    def test_base_prompt_structure(self):
        """Test that base prompt has required sections."""
        assert "Seer" in EXPLORER_SYSTEM_PROMPT
        assert "Capabilities" in EXPLORER_SYSTEM_PROMPT
        assert "Goals" in EXPLORER_SYSTEM_PROMPT
        assert "{artifact_instructions}" in EXPLORER_SYSTEM_PROMPT

    def test_get_prompt_default(self):
        """Test getting prompt without artifact parameters."""
        prompt = get_explorer_system_prompt()

        assert "Seer" in prompt
        assert ARTIFACT_INSTRUCTIONS_DEFAULT in prompt
        assert "{artifact_instructions}" not in prompt  # Should be replaced

    def test_get_prompt_with_artifact(self):
        """Test getting prompt with artifact schema."""
        artifact_schema = {
            "type": "object",
            "properties": {"cause": {"type": "string"}},
            "required": ["cause"],
        }

        prompt = get_explorer_system_prompt(
            artifact_key="root_cause",
            artifact_schema=artifact_schema,
        )

        assert "root_cause" in prompt
        assert "cause" in prompt
        assert ARTIFACT_INSTRUCTIONS_DEFAULT not in prompt

    def test_artifact_instructions_default(self):
        """Test default artifact instructions."""
        assert "structured artifact" in ARTIFACT_INSTRUCTIONS_DEFAULT.lower()

    def test_artifact_instructions_with_schema(self):
        """Test artifact instructions template has placeholders."""
        assert "{artifact_key}" in ARTIFACT_INSTRUCTIONS_WITH_SCHEMA
        assert "{artifact_schema}" in ARTIFACT_INSTRUCTIONS_WITH_SCHEMA
