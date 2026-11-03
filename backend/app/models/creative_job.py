"""Creative generation jobs (Phase 3B).

One row per generation attempt. The lifecycle is explicit
(``queued`` -> ``submitted`` -> ``running`` -> ``succeeded`` / ``failed`` /
``cancelled`` / ``timed_out``) and every transition is appended to
``audit_trail``, so a job's history is readable without joining an audit table.

Two safety properties are represented in the schema:

* ``idempotency_key`` is unique — a retried request cannot create a second job;
* ``ambiguous`` survives on the row, so a timeout whose outcome is unknown can be
  surfaced and refused for automatic resubmission instead of quietly spending
  money twice.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
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


class CreativeGenerationJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "creative_generation_jobs"

    brief_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("creative_briefs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    brief_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    brand_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    content_item_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("content_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    content_version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("content_versions.id", ondelete="SET NULL"), nullable=True
    )

    job_type: Mapped[str] = mapped_column(String(16), nullable=False, default="image")
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="mock")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued", index=True)
    external_job_id: Mapped[str] = mapped_column(Text, nullable=False, default="")

    submitted_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    negative_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)

    error_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error_retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Outcome unknown (timeout / 5xx / reset). Blocks automatic resubmission.
    ambiguous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    cost_estimate_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_actual_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("creative_assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    audit_trail: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_creative_job_idempotency"),
        CheckConstraint(
            "status in ('queued','submitted','running','succeeded','failed',"
            "'cancelled','timed_out')",
            name="creative_job_status_valid",
        ),
        CheckConstraint("job_type in ('image','video')", name="creative_job_type_valid"),
        CheckConstraint("attempts >= 0", name="creative_job_attempts_non_negative"),
        Index("ix_creative_jobs_brief_status", "brief_id", "status"),
        Index("ix_creative_jobs_created", "created_at"),
    )
