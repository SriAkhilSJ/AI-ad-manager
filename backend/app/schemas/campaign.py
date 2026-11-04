from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CampaignCreate(BaseModel):
    brand_profile_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    objective: str = ""
    brief: str = ""
    platforms: list[str] = Field(default_factory=list)
    start_date: date | None = None
    end_date: date | None = None


class CampaignUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    objective: str | None = None
    brief: str | None = None
    platforms: list[str] | None = None
    start_date: date | None = None
    end_date: date | None = None
    status: str | None = None


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    brand_profile_id: uuid.UUID
    name: str
    objective: str
    brief: str
    status: str
    platforms: list[str]
    start_date: date | None
    end_date: date | None
    plan: dict[str, Any]
    created_by_agent: str | None
    created_at: datetime
    updated_at: datetime | None = None


class CampaignDetailOut(CampaignOut):
    content_counts: dict[str, int] = Field(default_factory=dict)
    content_item_count: int = 0
