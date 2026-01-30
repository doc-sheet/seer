"""
Stub models for Seer Explorer endpoints.

These are minimal models to avoid 404 errors on self-hosted installations.
The full Explorer feature requires Sentry SaaS infrastructure.
"""

from typing import Any, Literal

from pydantic import BaseModel


class ExplorerRunsRequest(BaseModel):
    """Request to list explorer runs."""

    organization_id: int | None = None


class ExplorerRunsResponse(BaseModel):
    """Response for listing explorer runs."""

    runs: list[dict[str, Any]] = []
    message: str = "Explorer runs not available in self-hosted mode"


class ExplorerChatRequest(BaseModel):
    """Request for explorer chat."""

    run_id: str | None = None
    message: str | None = None
    organization_id: int | None = None

    class Config:
        extra = "allow"


class ExplorerChatResponse(BaseModel):
    """Response for explorer chat."""

    status: Literal["error", "not_available"] = "not_available"
    message: str = "Explorer chat not available in self-hosted mode"
    run_id: str | None = None


class ExplorerStateRequest(BaseModel):
    """Request for explorer run state."""

    run_id: str

    class Config:
        extra = "allow"


class ExplorerStateResponse(BaseModel):
    """Response for explorer run state."""

    status: Literal["not_found", "not_available"] = "not_available"
    message: str = "Explorer not available in self-hosted mode"


class ExplorerUpdateRequest(BaseModel):
    """Request to update explorer run."""

    run_id: str
    update_type: str | None = None

    class Config:
        extra = "allow"


class ExplorerUpdateResponse(BaseModel):
    """Response for explorer update."""

    status: Literal["error", "not_available"] = "not_available"
    message: str = "Explorer updates not available in self-hosted mode"


# ============================================
# Additional stub models for missing endpoints
# ============================================


class CodingAgentStateSetRequest(BaseModel):
    """Request to set coding agent state."""

    run_id: str | None = None

    class Config:
        extra = "allow"


class CodingAgentStateSetResponse(BaseModel):
    """Response for setting coding agent state."""

    status: Literal["ok", "not_available"] = "not_available"
    message: str = "Coding agent not available in self-hosted mode"


class CodingAgentStateUpdateRequest(BaseModel):
    """Request to update coding agent state."""

    run_id: str | None = None

    class Config:
        extra = "allow"


class CodingAgentStateUpdateResponse(BaseModel):
    """Response for updating coding agent state."""

    status: Literal["ok", "not_available"] = "not_available"
    message: str = "Coding agent not available in self-hosted mode"


class AutofixPromptRequest(BaseModel):
    """Request for autofix prompt."""

    run_id: str | None = None

    class Config:
        extra = "allow"


class AutofixPromptResponse(BaseModel):
    """Response for autofix prompt."""

    prompt: str | None = None
    message: str = "Autofix prompt not available in self-hosted mode"


class CodegenPrReviewRerunRequest(BaseModel):
    """Request to rerun PR review."""

    run_id: str | None = None

    class Config:
        extra = "allow"


class CodegenPrReviewRerunResponse(BaseModel):
    """Response for PR review rerun."""

    status: Literal["ok", "not_available"] = "not_available"
    message: str = "PR review rerun not available in self-hosted mode"


class ProjectPreferenceBulkRequest(BaseModel):
    """Request for bulk project preferences."""

    project_ids: list[int] = []

    class Config:
        extra = "allow"


class ProjectPreferenceBulkResponse(BaseModel):
    """Response for bulk project preferences."""

    preferences: dict[str, Any] = {}
    message: str = "Bulk preferences retrieved"


class ProjectPreferenceBulkSetRequest(BaseModel):
    """Request to bulk set project preferences."""

    preferences: dict[str, Any] = {}

    class Config:
        extra = "allow"


class ProjectPreferenceBulkSetResponse(BaseModel):
    """Response for bulk setting project preferences."""

    status: Literal["ok", "not_available"] = "ok"
    message: str = "Bulk preferences set"
