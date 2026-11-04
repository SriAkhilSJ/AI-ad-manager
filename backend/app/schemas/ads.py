from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

AD_PLATFORM_PATTERN = "^(meta_ads|google_ads|linkedin_ads)$"


class AdProposalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand_profile_id: uuid.UUID
    platform: str = Field(pattern=AD_PLATFORM_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    objective: str = Field(default="", max_length=64)
    plan: dict[str, Any] = Field(default_factory=dict)
    targeting: dict[str, Any] = Field(default_factory=dict)
    creatives: list[Any] = Field(default_factory=list)
    daily_budget_cents: int = Field(default=0, ge=0)
    total_budget_cents: int = Field(default=0, ge=0)
    currency: str = Field(default="USD", max_length=8)
    campaign_id: uuid.UUID | None = None
    created_by_agent: str | None = Field(default=None, max_length=64)
    # A proposal may arrive already approved; in that case the founder's
    # authorisation is created in the same request and names this exact hash.
    authorize: bool = False
    max_spend_cents: int = Field(default=0, ge=0)
    allowed_operations: list[str] = Field(default_factory=list)
    authorization_ttl_hours: int = Field(default=24, ge=1, le=168)


class AdCampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    brand_profile_id: uuid.UUID
    campaign_id: uuid.UUID | None
    platform: str
    name: str
    objective: str
    status: str
    plan: dict[str, Any]
    targeting: dict[str, Any]
    creatives: list[Any]
    daily_budget_cents: int
    total_budget_cents: int
    currency: str
    proposal_hash: str
    external_campaign_id: str | None
    created_by_agent: str | None
    created_at: datetime


class AdAuthorizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ad_campaign_id: uuid.UUID
    proposal_hash: str
    max_spend_cents: int
    allowed_operations: list[str]
    currency: str
    status: str
    expires_at: datetime
    authorized_by: uuid.UUID
    consumed_at: datetime | None
    note: str = ""


class AdAuthorizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_spend_cents: int = Field(gt=0)
    allowed_operations: list[str] = Field(min_length=1)
    ttl_hours: int = Field(default=24, ge=1, le=168)
    note: str = Field(default="", max_length=2000)


class AdOperationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ad_campaign_id: uuid.UUID
    authorization_id: uuid.UUID | None
    kind: str
    outcome: str
    detail: str
    request_fingerprint: str
    evidence: dict[str, Any]
    actor_role: str
    created_at: datetime


class AdExecuteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(pattern="^(create|launch|pause|resume|budget_change)$")
    authorization_id: uuid.UUID
    params: dict[str, Any] = Field(default_factory=dict)


class AdTransportOut(BaseModel):
    platform: str
    implemented: bool
    credentials_configured: bool
    available: bool
    reason: str
    docs_url: str


class AdCampaignDetailOut(BaseModel):
    campaign: AdCampaignOut
    operations: list[AdOperationOut]
    authorizations: list[AdAuthorizationOut]
    transport: AdTransportOut
