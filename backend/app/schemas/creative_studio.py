"""Phase 3B schemas: creative briefs, generation jobs, assets.

Input models forbid extra keys so a typo cannot silently become a default. Output
models carry the honesty flags (``is_mock``, ``visual_verification``,
``human_visual_review_required``) rather than leaving a caller to infer them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field


# --------------------------------------------------------------------- inputs
class BriefCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_version_id: uuid.UUID | None = None
    content_item_id: uuid.UUID | None = None
    asset_type: Literal["image", "video"] = "image"
    creative_direction: str = Field(default="", max_length=2000)
    aspect_ratio: str = Field(default="", max_length=16)
    duration_seconds: int = Field(default=0, ge=0, le=120)
    strategy_reference: str = Field(default="", max_length=500)
    force_new_version: bool = False


class JobCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief_id: uuid.UUID
    job_type: Literal["image", "video"] | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    auto_advance: bool = True


class JobRetryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authorize_ambiguous: bool = False


class AssetSelectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_version_id: uuid.UUID | None = None


# -------------------------------------------------------------------- outputs
class BriefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    brand_profile_id: uuid.UUID
    campaign_id: uuid.UUID | None
    content_item_id: uuid.UUID | None
    content_version_id: uuid.UUID | None
    content_hash: str
    platform: str
    asset_type: str
    version_number: int
    brief_hash: str
    creative_objective: str
    visual_concept: str
    scenes: list[str]
    subjects: list[str]
    background: str
    lighting: str
    composition: list[str]
    colour_palette: dict[str, Any]
    typography: list[str]
    brand_asset_requirements: list[str]
    image_prompt: str
    negative_prompt: list[str]
    aspect_ratio: str
    resolution: str
    duration_seconds: float | None
    reference_assets: list[dict[str, Any]]
    validation_constraints: dict[str, Any]
    generation_mode: str
    created_by_agent: str | None
    generation_metadata: dict[str, Any]
    created_at: datetime


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    brief_id: uuid.UUID
    brief_version: int
    campaign_id: uuid.UUID | None
    content_item_id: uuid.UUID | None
    content_version_id: uuid.UUID | None
    job_type: str
    provider: str
    status: str
    external_job_id: str
    attempts: int
    max_attempts: int
    timeout_seconds: int
    error_code: str
    error_message: str
    error_retryable: bool
    ambiguous: bool
    cancel_requested: bool
    is_mock: bool
    cost_estimate_usd: float | None
    cost_actual_usd: float | None
    price_verified: bool
    asset_id: uuid.UUID | None
    submitted_prompt: str
    parameters: dict[str, Any]
    audit_trail: list[dict[str, Any]]
    submitted_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class AssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    brand_profile_id: uuid.UUID | None
    campaign_id: uuid.UUID | None
    content_item_id: uuid.UUID | None
    content_version_id: uuid.UUID | None
    brief_id: uuid.UUID | None
    brief_version: int
    generation_job_id: uuid.UUID | None
    provider: str
    kind: str
    status: str
    filename: str
    mime_type: str
    detected_mime: str
    size_bytes: int
    width: int | None
    height: int | None
    duration_seconds: float | None
    aspect_ratio: str
    checksum: str
    storage_key: str | None
    public_url: str | None
    is_mock: bool
    selected: bool
    asset_version: int
    supersedes_asset_id: uuid.UUID | None
    root_asset_id: uuid.UUID | None
    prompt: str
    validation_status: str
    qa_report: dict[str, Any]
    validation_checked_at: datetime | None
    metadata_json: dict[str, Any]
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mock_label(self) -> str | None:
        """UI-facing honesty label. Present only for synthetic assets."""
        return (
            "Mock-generated preview: a synthetic abstract frame, not model-generated "
            "artwork and not a screenshot of Crusor software."
            if self.is_mock
            else None
        )


class AssetDetailOut(AssetOut):
    approval_status: str = "unapproved"
    approval: dict[str, Any] | None = None
    brief: BriefOut | None = None
    job: JobOut | None = None
    versions: list[dict[str, Any]] = Field(default_factory=list)


class AssetSelectOut(BaseModel):
    asset: AssetOut
    invalidated_approvals: int
    message: str


class CreativeOverviewOut(BaseModel):
    total_assets: int
    mock_assets: int
    pending_jobs: int
    failed_jobs: int
    succeeded_jobs: int
    approved_assets: int
    needs_review_assets: int
    by_campaign: dict[str, int]
    by_platform: dict[str, int]
    by_validation: dict[str, int]
    provider: str
    is_mock_provider: bool
    paid_generation_enabled: bool
    flow_connector: dict[str, Any]
    storage: dict[str, Any]
