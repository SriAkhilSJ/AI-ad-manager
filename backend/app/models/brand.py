"""Brand knowledge base, goals, competitors and content pillars.

Brand data is *data*, not code: agents read it from the database at run time so
marketing can be re-positioned without a deployment.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import GUID, JSONB
from app.models.user import DEFAULT_ORGANIZATION_ID


class BrandProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "brand_profiles"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), nullable=False, default=DEFAULT_ORGANIZATION_ID, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    company: Mapped[str] = mapped_column(String(200), nullable=False)
    product: Mapped[str] = mapped_column(String(200), nullable=False)
    tagline: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    positioning: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Structured marketing context (audience, personality, tone, visual direction…)
    audience: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    personality: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    tone_of_voice: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    visual_direction: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    brand_values: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    keywords: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    # Claims the agents must never make (unverified numbers, pricing, etc.)
    do_not_claim: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    prohibited_phrases: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    compliance_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    goals: Mapped[list[MarketingGoal]] = relationship(
        back_populates="brand", cascade="all, delete-orphan", lazy="selectin"
    )
    pillars: Mapped[list[ContentPillar]] = relationship(
        back_populates="brand", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        Index("ix_brand_profiles_org_active", "organization_id", "is_active"),
        CheckConstraint("version >= 1", name="brand_version_positive"),
    )


class MarketingGoal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "marketing_goals"

    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    kpi: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    target_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    horizon: Mapped[str] = mapped_column(String(50), nullable=False, default="quarter")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    due_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    brand: Mapped[BrandProfile] = relationship(back_populates="goals")

    __table_args__ = (
        CheckConstraint(
            "status in ('active','paused','achieved','missed','archived')",
            name="marketing_goal_status_valid",
        ),
        CheckConstraint("priority between 1 and 5", name="marketing_goal_priority_range"),
        Index("ix_marketing_goals_brand_status", "brand_profile_id", "status"),
    )


class CompetitorProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "competitor_profiles"

    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    positioning: Mapped[str] = mapped_column(Text, nullable=False, default="")
    strengths: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    weaknesses: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    # Provenance is mandatory: agents may only cite observed facts.
    source: Mapped[str] = mapped_column(String(500), nullable=False, default="manual")
    observed_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    __table_args__ = (Index("ix_competitors_brand_name", "brand_profile_id", "name"),)


class ContentPillar(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "content_pillars"

    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    platforms: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    brand: Mapped[BrandProfile] = relationship(back_populates="pillars")

    __table_args__ = (
        CheckConstraint("weight >= 0", name="content_pillar_weight_non_negative"),
        Index("ix_content_pillars_brand_active", "brand_profile_id", "is_active"),
    )
