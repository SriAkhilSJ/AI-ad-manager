"""API schemas for Strategy Studio."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StrategyVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    strategy_id: uuid.UUID
    version_number: int
    positioning_recommendations: list[dict[str, Any]]
    audience_segmentation: list[dict[str, Any]]
    competitor_differentiation: list[dict[str, Any]]
    content_pillars: list[dict[str, Any]]
    campaign_concepts: list[dict[str, Any]]
    platform_strategies: list[dict[str, Any]]
    weekly_objectives: list[dict[str, Any]]
    publishing_frequency: dict[str, Any]
    lead_generation: list[dict[str, Any]]
    growth_experiments: list[dict[str, Any]]
    kpis: list[dict[str, Any]]
    measurement_plan: dict[str, Any]
    business_objectives: list[str]
    objective_alignment: dict[str, Any]
    source_finding_ids: list[str]
    research_summary: str
    rationale: str
    brand_check: dict[str, Any]
    missing_information: list[dict[str, Any]]
    warnings: list[str]
    generation_mode: str
    confidence: float
    cost_usd: float
    tokens_used: int
    content_hash: str
    change_note: str
    created_by_agent: str | None
    created_at: datetime


class StrategyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    brand_profile_id: uuid.UUID
    research_project_id: uuid.UUID | None
    campaign_id: uuid.UUID | None
    workflow_thread_id: str | None
    name: str
    objective: str
    status: str
    primary_objective_type: str
    version_count: int
    current_version_id: uuid.UUID | None
    created_by_agent: str | None
    created_at: datetime
    updated_at: datetime


class StrategySummary(StrategyOut):
    """List-view row: the strategy plus its live approval state."""

    has_valid_approval: bool = False
    approved_version_id: uuid.UUID | None = None
    approved_at: datetime | None = None
    latest_version_number: int = 0
    confidence: float = 0.0
    business_objectives: list[str] = Field(default_factory=list)


class StrategyDetail(BaseModel):
    strategy: StrategyOut
    current_version: StrategyVersionOut | None
    versions: list[dict[str, Any]]
    approval: dict[str, Any] | None
    approval_history: list[dict[str, Any]]
    can_execute: bool
    execution_blocked_reason: str
    experiments: list[dict[str, Any]]


class StrategyGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand_profile_id: uuid.UUID
    objective: str = Field(min_length=3, max_length=2000)
    business_objectives: list[str] = Field(default_factory=lambda: ["developer_acquisition"])
    primary_objective_type: str = "developer_acquisition"
    research_project_id: uuid.UUID | None = None
    platforms: list[str] = Field(default_factory=list)
    name: str = ""
    max_cost_usd: float | None = Field(default=None, ge=0, le=100)


class StrategyDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(description="approve | request_changes | reject | submit | revoke")
    reason: str = Field(default="", max_length=4000)
    version_id: uuid.UUID | None = Field(
        default=None,
        description="Omit to act on the current version. Supply to pin a decision.",
    )


class StrategyVersionCreate(BaseModel):
    """Founder edit: always produces a new version and invalidates approval."""

    model_config = ConfigDict(extra="forbid")

    positioning_recommendations: list[dict[str, Any]] | None = None
    audience_segmentation: list[dict[str, Any]] | None = None
    competitor_differentiation: list[dict[str, Any]] | None = None
    content_pillars: list[dict[str, Any]] | None = None
    campaign_concepts: list[dict[str, Any]] | None = None
    platform_strategies: list[dict[str, Any]] | None = None
    weekly_objectives: list[dict[str, Any]] | None = None
    publishing_frequency: dict[str, Any] | None = None
    lead_generation: list[dict[str, Any]] | None = None
    growth_experiments: list[dict[str, Any]] | None = None
    kpis: list[dict[str, Any]] | None = None
    measurement_plan: dict[str, Any] | None = None
    business_objectives: list[str] | None = None
    research_summary: str | None = None
    rationale: str | None = None
    change_note: str = Field(default="", max_length=2000)


class VersionDiffOut(BaseModel):
    left: dict[str, Any]
    right: dict[str, Any]
    changed_fields: list[str]
    changes: dict[str, Any]
    identical: bool


class StrategyDirectorStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand_profile_id: uuid.UUID
    objective: str = Field(min_length=3, max_length=2000)
    business_objectives: list[str] = Field(default_factory=lambda: ["developer_acquisition"])
    primary_objective_type: str = "developer_acquisition"
    platforms: list[str] = Field(default_factory=list)
    research_project_id: uuid.UUID | None = None
    budget_usd: float | None = Field(default=None, ge=0, le=100)
    max_iterations: int | None = Field(default=None, ge=1, le=20)


class StrategyDirectorDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(description="approve | request_changes | reject")
    reason: str = Field(default="", max_length=4000)
