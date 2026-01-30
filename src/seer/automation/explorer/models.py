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
