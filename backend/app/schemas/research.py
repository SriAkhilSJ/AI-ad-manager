"""API schemas for the Research Centre."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.research import RESEARCH_CATEGORIES


class ResearchProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand_profile_id: uuid.UUID
    objective: str = Field(min_length=3, max_length=2000)
    categories: list[str] = Field(default_factory=list)
    max_cost_usd: float | None = Field(default=None, ge=0, le=100)
    run_now: bool = Field(
        default=True,
        description="Execute immediately. When false the project is created in 'queued'.",
    )

    @field_validator("categories")
    @classmethod
    def _validate_categories(cls, value: list[str]) -> list[str]:
        unknown = [c for c in value if c not in RESEARCH_CATEGORIES]
        if unknown:
            raise ValueError(
                f"Unknown categor{'y' if len(unknown) == 1 else 'ies'}: "
                f"{', '.join(unknown)}. Allowed: {', '.join(RESEARCH_CATEGORIES)}"
            )
        return value


class ResearchProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    brand_profile_id: uuid.UUID
    objective: str
    categories: list[str]
    status: str
    summary: str
    provider: str
    max_cost_usd: float
    estimated_cost_usd: float
    actual_cost_usd: float
    tokens_used: int
    tool_calls: int
    iterations: int
    finding_count: int
    source_count: int
    gap_count: int
    error_code: str
    error_message: str
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class ResearchSourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    url: str
    title: str
    publisher: str
    credibility: str
    retrieved_at: datetime | None
    provider: str
    excerpt: str


class ResearchFindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    research_project_id: uuid.UUID
    category: str
    finding: str
    evidence_summary: str
    source_url: str
    source_title: str
    retrieved_at: datetime | None
    confidence: float
    verification_status: str
    competitor_name: str
    tags: list[str]
    provider: str
    created_at: datetime


class ResearchFindingPage(BaseModel):
    items: list[ResearchFindingOut]
    total: int
    limit: int
    offset: int


class ResearchProjectDetail(BaseModel):
    project: ResearchProjectOut
    findings: list[ResearchFindingOut]
    sources: list[ResearchSourceOut]
    category_coverage: list[dict[str, Any]]
    missing_categories: list[str]


class ResearchRunResult(BaseModel):
    project: ResearchProjectOut
    findings_created: int
    sources_created: int
    gaps: list[str]
    warnings: list[str]
    provider: str
    provider_status: str
    cost: dict[str, Any]
    limits: dict[str, Any]
    stop_reason: str


class CompetitorSnapshotOut(BaseModel):
    name: str
    website: str | None = None
    positioning: str | None = None
    source: str | None = None
    findings: int = 0


class AuditTrailEntry(BaseModel):
    """A single recorded decision or state change."""

    at: str
    entry: str
    detail: dict[str, Any] = Field(default_factory=dict)
