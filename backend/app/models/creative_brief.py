"""Creative briefs (Phase 3B).

A brief is the Creative Director's structured output: everything a generation
provider or a human designer needs, bound to one exact content version. Briefs are
**immutable and versioned** — a change produces version n+1, so an approval that
referenced version n keeps referring to text that still exists.

The brief never mutates the content it describes: it stores the content version id
and the content hash it was derived from, which is what makes "the brief was
generated from exactly this copy" checkable later.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import GUID, JSONB


class CreativeBrief(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "creative_briefs"

    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    content_item_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("content_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    content_version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("content_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Hash of the content version at brief time — proof the brief was not built
    #: from a version that has since changed.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: Optional pointer to the approved strategy version this campaign follows.
    strategy_version_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)

    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    brief_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)

    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False, default="image")
    creative_objective: Mapped[str] = mapped_column(Text, nullable=False, default="")
    visual_concept: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scenes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    subjects: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    background: Mapped[str] = mapped_column(Text, nullable=False, default="")
    lighting: Mapped[str] = mapped_column(Text, nullable=False, default="")
    composition: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    colour_palette: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    typography: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    brand_asset_requirements: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    image_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    negative_prompt: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    aspect_ratio: Mapped[str] = mapped_column(String(16), nullable=False, default="4:5")
    resolution: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    reference_assets: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    validation_constraints: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )

    generation_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="deterministic")
    created_by_agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    generation_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint(
            "content_version_id",
            "asset_type",
            "version_number",
            name="uq_creative_brief_version",
        ),
        CheckConstraint("version_number >= 1", name="creative_brief_version_positive"),
        CheckConstraint(
            "asset_type in ('image','video','carousel','thumbnail','document')",
            name="creative_brief_asset_type_valid",
        ),
        Index("ix_creative_briefs_campaign_created", "campaign_id", "created_at"),
    )
