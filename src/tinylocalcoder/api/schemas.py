"""Pydantic schemas for the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class PromptRequest(BaseModel):
    prompt: str = ""
    thinking_enabled: bool | None = None


class ApproveRequest(BaseModel):
    command_id: str
    decision: Literal["allow", "deny", "allow_all"]


class PendingApproval(BaseModel):
    command_id: str
    command: str
    cwd: str
    reason: str = ""


class RunResponse(BaseModel):
    status: str
    mode: str
    output: str = ""
    phase: str = ""
    last_file: str = ""
    last_command: str = ""
    progress: dict[str, int] = Field(default_factory=dict)
    pending_approval: PendingApproval | None = None
    critic: str = ""
    error: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)
