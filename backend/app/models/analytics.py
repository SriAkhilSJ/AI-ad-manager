from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import GUID, JSONB
from app.models.social import PLATFORM_VALUES_SQL


class AnalyticsSnapshot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A measured observation. Rows exist only when a real data source produced
    them — the API never synthesises metrics to fill a chart."""

    __tablename__ = "analytics_snapshots"

    published_post_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("published_posts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    window: Mapped[str] = mapped_column(String(32), nullable=False, default="lifetime")
    impressions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reach: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    engagements: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    likes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    comments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shares: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    saves: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    followers_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Provenance: which connector produced this row.
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")

    __table_args__ = (
        CheckConstraint(
            f"platform in ({PLATFORM_VALUES_SQL})", name="analytics_platform_valid"
        ),
        CheckConstraint(
            "impressions >= 0 and reach >= 0 and engagements >= 0 and clicks >= 0",
            name="analytics_metrics_non_negative",
        ),
        UniqueConstraint(
            "published_post_id", "platform", "captured_at", "window",
            name="uq_analytics_post_platform_capture",
        ),
        Index("ix_analytics_brand_captured", "brand_profile_id", "captured_at"),
    )
