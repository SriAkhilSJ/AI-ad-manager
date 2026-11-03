from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SocialAccountOut(BaseModel):
    """Allow-list response model.

    ``token_ref`` and any credential material are intentionally absent. There is
    no field in this schema that can carry a secret, so a careless serializer
    cannot leak one.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    brand_profile_id: uuid.UUID
    platform: str
    handle: str
    display_name: str
    external_account_id: str | None
    status: str
    scopes: list[str]
    capabilities: dict[str, Any]
    token_expires_at: datetime | None
    last_synced_at: datetime | None
    notes: str
    created_at: datetime


class SocialAccountCreate(BaseModel):
    brand_profile_id: uuid.UUID
    platform: str = Field(pattern="^(instagram|linkedin|youtube|x|facebook|reddit)$")
    handle: str = Field(min_length=1, max_length=200)
    display_name: str = Field(default="", max_length=200)


class PlatformStatusOut(BaseModel):
    platform: str
    credentials_configured: bool
    transport_available: bool
    publish_enabled: bool
    reason: str


class ScheduledPostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content_item_id: uuid.UUID
    content_version_id: uuid.UUID
    social_account_id: uuid.UUID
    approval_record_id: uuid.UUID
    scheduled_for: datetime
    status: str
    attempts: int
    last_error: str
    created_at: datetime


class PublishedPostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content_item_id: uuid.UUID
    content_version_id: uuid.UUID
    social_account_id: uuid.UUID
    approval_record_id: uuid.UUID
    platform: str
    external_post_id: str
    permalink: str | None
    published_at: datetime | None
    status: str
    created_at: datetime
