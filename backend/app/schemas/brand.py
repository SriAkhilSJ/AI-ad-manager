from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BrandProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    company: str = Field(min_length=1, max_length=200)
    product: str = Field(min_length=1, max_length=200)
    tagline: str = Field(default="", max_length=300)
    positioning: str = ""
    audience: list[str] = Field(default_factory=list)
    personality: list[str] = Field(default_factory=list)
    tone_of_voice: list[str] = Field(default_factory=list)
    visual_direction: dict[str, Any] = Field(default_factory=dict)
    brand_values: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    do_not_claim: list[str] | None = None
    prohibited_phrases: list[str] = Field(default_factory=list)
    compliance_notes: str = ""


class BrandProfileUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    tagline: str | None = Field(default=None, max_length=300)
    positioning: str | None = None
    audience: list[str] | None = None
    personality: list[str] | None = None
    tone_of_voice: list[str] | None = None
    visual_direction: dict[str, Any] | None = None
    brand_values: list[str] | None = None
    keywords: list[str] | None = None
    do_not_claim: list[str] | None = None
    prohibited_phrases: list[str] | None = None
    compliance_notes: str | None = None
    is_active: bool | None = None


class BrandProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    company: str
    product: str
    tagline: str
    positioning: str
    audience: list[str]
    personality: list[str]
    tone_of_voice: list[str]
    visual_direction: dict[str, Any]
    brand_values: list[str]
    keywords: list[str]
    do_not_claim: list[str]
    prohibited_phrases: list[str]
    compliance_notes: str
    version: int
    is_active: bool
    created_at: datetime
    updated_at: datetime | None = None


class MarketingGoalCreate(BaseModel):
    objective: str = Field(min_length=3)
    kpi: str = ""
    target_value: float | None = None
    horizon: str = Field(default="quarter", pattern="^(week|month|quarter|year)$")
    priority: int = Field(default=1, ge=1, le=5)
    due_on: date | None = None


class MarketingGoalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    brand_profile_id: uuid.UUID
    objective: str
    kpi: str
    target_value: float | None
    current_value: float | None
    horizon: str
    status: str
    priority: int
    due_on: date | None
    created_at: datetime


class CompetitorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    website: str | None = None
    positioning: str = ""
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    source: str = Field(default="manual", max_length=500)


class CompetitorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    brand_profile_id: uuid.UUID
    name: str
    website: str | None
    positioning: str
    strengths: list[str]
    weaknesses: list[str]
    source: str
    created_at: datetime


class PillarCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
    platforms: list[str] = Field(default_factory=list)
    weight: float = Field(default=1.0, ge=0)


class PillarOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    brand_profile_id: uuid.UUID
    name: str
    description: str
    platforms: list[str]
    weight: float
    is_active: bool
    created_at: datetime
