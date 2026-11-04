from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


class WorkflowCancelRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


class WorkflowStartRequest(BaseModel):
    brand_profile_id: uuid.UUID | None = None
    objective: str = Field(min_length=3, max_length=2000)
    campaign_id: uuid.UUID | None = None
    platforms: list[str] = Field(
        default_factory=lambda: ["linkedin"],
        description="Any of instagram|linkedin|youtube|x|facebook|reddit",
    )


class WorkflowDecisionRequest(BaseModel):
    action: str = Field(pattern="^(approve|request_changes|reject)$")
    reason: str = Field(default="", max_length=2000)
    #: Approving the copy is not the same as authorising CRUSOR to place it on a
    #: calendar. Scheduling is opt-out, publishing-now is opt-in, and neither
    #: overrides the approval gate the content itself must still pass.
    schedule: bool = Field(
        default=True,
        description="Schedule the approved content on the connected accounts.",
    )
    publish_now: bool = Field(
        default=False,
        description="Publish immediately instead of waiting for the scheduled slot.",
    )
    horizon_days: int = Field(default=7, ge=1, le=30)


class WorkflowStartResponse(BaseModel):
    workflow_id: str
    state: dict[str, Any]
    reused: bool = False


class WorkflowStatusResponse(BaseModel):
    workflow_id: str
    status: str | None = None
    brand_profile_id: str | None = None
    phase: str | None = None
    next: list[str] = Field(default_factory=list)
    awaiting_human: bool = False
    interrupts: list[dict[str, Any]] = Field(default_factory=list)
    iteration: int | None = None
    plan: list[dict[str, Any]] | None = None
    qa_verdict: str | None = None
    qa_findings: list[dict[str, Any]] | None = None
    approval_request: dict[str, Any] | None = None
    approval_decision: dict[str, Any] | None = None
    content_item_ids: list[str] | None = None
    created_campaign_id: str | None = None
    summary: str | None = None
    lifecycle_stage: str | None = None
    schedule_plan: list[dict[str, Any]] | None = None
    schedule_blocked: list[dict[str, Any]] | None = None
    scheduled_post_ids: list[str] | None = None
    publish_results: dict[str, Any] | None = None
    analytics: dict[str, Any] | None = None
    optimizations: list[dict[str, Any]] | None = None
    optimization_note: str | None = None
    trace: list[dict[str, Any]] | None = None
    errors: list[dict[str, Any]] | None = None
    results: dict[str, Any] | None = None
    checkpoint_count: int = 0


class WorkflowSummary(BaseModel):
    workflow_id: str
    status: str | None = None
    node: str | None = None
    step: int | None = None
    updated_at: str | None = None


class AgentRunRequest(BaseModel):
    """Direct agent invocation for operators/tests (bypasses the Director graph
    but keeps every limit, ledger and usage behaviour)."""

    payload: dict[str, Any] = Field(default_factory=dict)
    brand_profile_id: uuid.UUID | None = None


class AgentSpecOut(BaseModel):
    name: str
    description: str
    responsibilities: list[str]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    limits: dict[str, Any]


class AgentRunOut(BaseModel):
    agent: str
    status: str
    output: dict[str, Any] | None = None
    error: str | None = None
    run_id: uuid.UUID | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)
