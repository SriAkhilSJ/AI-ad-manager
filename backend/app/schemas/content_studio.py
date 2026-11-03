"""Phase 3A request/response schemas for the Content Studio."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.content.platforms import CONTENT_TYPES


class PlatformSpecOut(BaseModel):
    platform: str
    label: str
    supported_types: list[str]
    body_max_chars: int
    title_max_chars: int
    hook_max_chars: int
    cta_max_chars: int
    hashtag_min: int
    hashtag_max: int
    hashtag_guidance: str
    formatting_rules: list[str]
    cta_guidance: str
    validation_rules: list[str]
    tone: str
    community_first: bool


# --------------------------------------------------------------------------- #
# Plan configuration
# --------------------------------------------------------------------------- #
class PlanConfigIn(BaseModel):
    """Founder-configurable planning knobs. Unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    posts_per_week: int = Field(default=6, ge=1, le=12)
    platforms: list[str] | None = None
    content_mix: dict[str, list[str]] | None = None
    audience_focus: str = Field(default="", max_length=200)
    publishing_windows: dict[str, str] | None = None
    variants: int = Field(default=2, ge=1, le=3)
    campaign_duration_weeks: int = Field(default=1, ge=1, le=52)


class PlanCreateIn(BaseModel):
    campaign_id: uuid.UUID | None = None
    week_start: date | None = None
    config: PlanConfigIn = Field(default_factory=PlanConfigIn)
    strategy_id: uuid.UUID | None = None
    research_project_id: uuid.UUID | None = None
    generate: bool = False
    replace: bool = False


class PlanSlotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int
    platform: str
    content_type: str
    scheduled_date: date
    scheduled_time: str
    audience: str
    pillar: str
    objective: str
    primary_message: str
    rationale: str
    status: str
    content_item_id: uuid.UUID | None = None
    variant_count: int = 0
    selected_variant: int | None = None
    validation_status: str | None = None
    is_mock: bool | None = None


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    brand_profile_id: uuid.UUID
    campaign_id: uuid.UUID | None
    week_start: date
    week_end: date
    status: str
    objective: str
    platforms: list[str]
    config: dict[str, Any]
    slot_count: int
    item_count: int
    completeness_score: float
    generated_by: str
    generation_metadata: dict[str, Any]


class PlanDetailOut(PlanOut):
    slots: list[PlanSlotOut] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #
class SingleGenerateIn(BaseModel):
    campaign_id: uuid.UUID | None = None
    platform: str
    content_type: str = "social_post"
    audience: str = Field(default="", max_length=200)
    pillar: str = Field(default="", max_length=160)
    objective: str = Field(default="", max_length=200)
    primary_message: str = Field(default="", max_length=500)
    variants: int = Field(default=1, ge=1, le=3)
    planned_date: date | None = None
    planned_time: str = Field(default="09:00", pattern=r"^\d{2}:\d{2}$")

    @field_validator("content_type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        if value not in CONTENT_TYPES:
            raise ValueError(f"unknown content type '{value}'")
        return value


class VariantOut(BaseModel):
    index: int
    headline: str
    hook: str
    body: str
    hashtags: list[str]
    call_to_action: str
    outline: list[str] = Field(default_factory=list)
    character_count: int
    provider: str
    model: str
    is_mock: bool
    generation_mode: str
    score: float
    validation: dict[str, Any]


class GeneratedItemOut(BaseModel):
    content_item_id: uuid.UUID
    version_id: uuid.UUID
    version_number: int
    platform: str
    content_type: str
    title: str
    status: str
    validation_status: str
    selected_variant: int
    variants: list[VariantOut]
    is_mock: bool
    provider: str
    generation_metadata: dict[str, Any]


class ValidationIssueOut(BaseModel):
    rule: str
    severity: str
    category: str = "general"
    message: str
    explanation: str = ""
    evidence: str = ""
    affected_text: str = ""
    suggestion: str = ""
    requires_founder_confirmation: bool = False


class ValidationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    verdict: str
    score: float
    issues: list[dict[str, Any]]
    checked_rules: list[str]
    blocking_count: int
    warning_count: int
    requires_founder_confirmation: bool
    provider: str
    generation_mode: str
    content_version_id: uuid.UUID


class VariantRegenerateIn(BaseModel):
    """Ask for a fresh variant set on an existing item.

    Live model execution is disabled in Phase 3A, so the endpoint refuses to run
    unless the mock provider is active.
    """

    model_config = ConfigDict(extra="forbid")

    variants: int = Field(default=2, ge=1, le=3)
    change_note: str = Field(default="", max_length=500)


class VariantSelectIn(BaseModel):
    variant_index: int = Field(ge=0, le=2)
    change_note: str = Field(default="", max_length=500)


# --------------------------------------------------------------------------- #
# Calendar
# --------------------------------------------------------------------------- #
class CalendarEntryOut(BaseModel):
    entry_id: str
    content_item_id: str
    title: str
    platform: str
    content_type: str
    campaign_id: str | None = None
    plan_id: str | None = None
    status: str
    label: str
    planned_date: str
    planned_time: str
    note: str = ""
    approval_required: bool = True
    external_scheduling: Literal[False] = False
    publishing_available: Literal[False] = False


class CalendarMoveIn(BaseModel):
    planned_date: date | None = None
    planned_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    note: str | None = Field(default=None, max_length=500)


class CalendarViewOut(BaseModel):
    view: Literal["month", "week", "list"]
    start: date
    end: date
    entries: list[CalendarEntryOut]
    #: Repeated in every payload so a client cannot mistake this for a scheduler.
    external_scheduling: Literal[False] = False
    publishing_available: Literal[False] = False


class StudioOverviewOut(BaseModel):
    total_drafts: int
    pending_review: int
    approved: int
    scheduled_internally: int
    blocked: int
    by_platform: dict[str, int]
    by_campaign: dict[str, int]
    by_validation_status: dict[str, int]
    content_provider: str
    is_mock_provider: bool
    live_model_calls_enabled: bool
