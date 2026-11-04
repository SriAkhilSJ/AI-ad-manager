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
    """Honest per-platform readiness.

    ``credentials_configured`` — the platform's own app id/secret are present.
    ``vault_available``       — a token vault is configured, so a credential has
                                somewhere encrypted to live.
    ``publish_enabled``       — both of the above. Only then does a live transport
                                exist.
    ``capabilities``          — per-operation documented support, whether this
                                build implements it, and whether it has ever been
                                verified against a real account.
    """

    platform: str
    credentials_configured: bool
    vault_available: bool = False
    transport_available: bool
    publish_enabled: bool
    capabilities: dict[str, Any] = Field(default_factory=dict)
    reason: str


class OAuthStartOut(BaseModel):
    platform: str
    authorize_url: str
    state: str
    redirect_uri: str
    scopes: list[str]
    pkce: bool
    expires_in: int


class OAuthCallbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str = Field(min_length=8, max_length=4096)
    code: str = Field(min_length=1, max_length=4096)
    brand_profile_id: uuid.UUID


class OAuthConnectionOut(BaseModel):
    platform: str
    status: str
    handle: str
    external_account_id: str | None
    scopes: list[str]
    expires_at: datetime | None
    refreshed: bool
    #: True only when the provider's credential was exercised with a real read.
    verified: bool
    detail: str


class AnalyticsIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand_profile_id: uuid.UUID
    platforms: list[str] = Field(default_factory=list)
    days: int = Field(default=30, ge=1, le=365)


class AnalyticsIngestOut(BaseModel):
    requested: int
    ingested: int
    skipped: int
    unavailable: list[dict[str, str]] = Field(default_factory=list)
    detail: str


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
