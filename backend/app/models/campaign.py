from __future__ import annotations

import enum
import uuid
from datetime import date
from typing import Any

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import GUID, JSONB


class CampaignStatus(str, enum.Enum):
    DRAFT = "draft"
    PLANNED = "planned"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"

    @classmethod
    def values(cls) -> list[str]:
        return [c.value for c in cls]


class Campaign(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "campaigns"

    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False, default="")
    brief: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CampaignStatus.DRAFT.value
    )
    platforms: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Structured strategy produced by the Strategy Agent (audience insight,
    # messaging pillars, channel mix, success metrics). Never free prose only.
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_by_agent: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status in ('draft','planned','active','paused','completed','archived')",
            name="campaign_status_valid",
        ),
        CheckConstraint(
            "end_date is null or start_date is null or end_date >= start_date",
            name="campaign_date_order",
        ),
        Index("ix_campaigns_brand_status", "brand_profile_id", "status"),
        Index("ux_campaigns_brand_name", "brand_profile_id", "name", unique=True),
    )
