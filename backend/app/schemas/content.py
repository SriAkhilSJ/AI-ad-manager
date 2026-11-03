from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ContentItemCreate(BaseModel):
    brand_profile_id: uuid.UUID
    title: str = Field(min_length=1, max_length=300)
    platform: str
    format: str = Field(default="post", max_length=48)
    campaign_id: uuid.UUID | None = None
    pillar_id: uuid.UUID | None = None
    headline: str = Field(default="", max_length=300)
    body: str = ""
    hashtags: list[str] = Field(default_factory=list)
    call_to_action: str = Field(default="", max_length=300)
    media_refs: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class ContentVersionCreate(BaseModel):
    """A new immutable version. Unspecified fields are inherited."""

    headline: str | None = Field(default=None, max_length=300)
    body: str | None = None
    hashtags: list[str] | None = None
    call_to_action: str | None = Field(default=None, max_length=300)
    media_refs: list[str] | None = None
    change_note: str = Field(default="", max_length=500)


class ContentVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content_item_id: uuid.UUID
    version_number: int
    headline: str
    body: str
    hashtags: list[str]
    call_to_action: str
    media_refs: list[str]
    content_hash: str
    change_note: str
    created_by: uuid.UUID | None
    created_by_agent: str | None
    generation_metadata: dict[str, Any]
    created_at: datetime


class ContentItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    brand_profile_id: uuid.UUID
    campaign_id: uuid.UUID | None
    pillar_id: uuid.UUID | None
    platform: str
    format: str
    title: str
    status: str
    current_version_id: uuid.UUID | None
    version_count: int
    created_by_agent: str | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime | None = None


class ContentItemDetailOut(ContentItemOut):
    current_version: ContentVersionOut | None = None
    allowed_actions: list[str] = Field(default_factory=list)
    has_valid_approval: bool = False
    approval_count: int = 0


class TransitionRequest(BaseModel):
    action: str = Field(
        pattern="^(submit|approve|reject|request_changes|revoke|cancel)$"
    )
    reason: str = Field(default="", max_length=2000)
    content_version_id: uuid.UUID | None = None


class ApprovalRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content_item_id: uuid.UUID
    content_version_id: uuid.UUID
    action: str
    actor_id: uuid.UUID | None
    actor_role: str
    reason: str
    content_hash: str
    invalidated_at: datetime | None
    invalidated_reason: str
    created_at: datetime


class PublishRequest(BaseModel):
    social_account_id: uuid.UUID | None = None
    scheduled_post_id: uuid.UUID | None = None


class ScheduleRequest(BaseModel):
    social_account_id: uuid.UUID
    scheduled_for: datetime


class PublishOut(BaseModel):
    content_item_id: uuid.UUID
    status: str
    published_post_id: uuid.UUID | None = None
    external_post_id: str | None = None
    approval_record_id: uuid.UUID | None = None
    message: str = ""
