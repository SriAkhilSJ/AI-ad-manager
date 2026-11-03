"""Marketing memory with semantic recall.

On PostgreSQL the embedding is a real ``pgvector`` column (created by the
migration after ``CREATE EXTENSION IF NOT EXISTS vector``). On SQLite the same
model falls back to a JSON column and similarity is computed in Python, so the
feature is exercised in tests rather than silently absent.
"""

from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import GUID, JSONB


class MemoryKind(str, enum.Enum):
    INSIGHT = "insight"
    DECISION = "decision"
    RESULT = "result"
    AUDIENCE = "audience"
    COMPETITOR = "competitor"
    BRAND_RULE = "brand_rule"
    POST_MORTEM = "post_mortem"

    @classmethod
    def values(cls) -> list[str]:
        return [c.value for c in cls]


class MarketingMemory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "marketing_memory"

    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default=MemoryKind.INSIGHT.value)
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON list[float] portability path. The PostgreSQL migration replaces this
    # with a pgvector column of the configured dimensionality.
    embedding: Mapped[list[float] | None] = mapped_column(JSONB, nullable=True)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False, default="agent")
    source_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    importance: Mapped[float] = mapped_column(default=0.5, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint(
            "kind in ('insight','decision','result','audience','competitor',"
            "'brand_rule','post_mortem')",
            name="marketing_memory_kind_valid",
        ),
        CheckConstraint("importance >= 0 and importance <= 1", name="memory_importance_range"),
        Index("ix_marketing_memory_brand_kind", "brand_profile_id", "kind"),
    )
