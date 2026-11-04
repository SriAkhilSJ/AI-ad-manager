"""Phase 3A models: weekly content plans, plan slots, internal calendar, validation.

Design rules
------------
* **Reuse, don't duplicate.** Cards of publishable copy live in the existing
  ``content_items`` / ``content_versions`` tables, and approvals stay in
  ``approval_records``. Phase 3A adds only the *planning* layer: which slots a
  week contains, when a draft is planned internally, and what the Brand Guardian
  said about a specific immutable version.
* **The calendar is internal.** Nothing here schedules anything on a real
  platform. ``content_calendar_entries`` is a planning affordance; external
  scheduling still goes through the Phase 1 ``scheduled_posts`` gate, which
  requires a live social account and is untouched by this phase.
* **Validation binds to a version, not an item.** A verdict recorded against
  version 3 says nothing about version 4, mirroring how approvals bind.
"""

from __future__ import annotations

import enum
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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import GUID, JSONB
from app.models.social import Platform


class PlanStatus(str, enum.Enum):
    DRAFT = "draft"
    GENERATING = "generating"
    GENERATED = "generated"
    FAILED = "failed"
    ARCHIVED = "archived"

    @classmethod
    def values(cls) -> list[str]:
        return [c.value for c in cls]


class SlotStatus(str, enum.Enum):
    PROPOSED = "proposed"
    GENERATED = "generated"
    SKIPPED = "skipped"
    FAILED = "failed"

    @classmethod
    def values(cls) -> list[str]:
        return [c.value for c in cls]


class ValidationVerdict(str, enum.Enum):
    """Phase 3A verdicts, in increasing order of severity."""

    PASS = "PASS"
    PASS_WITH_WARNINGS = "PASS_WITH_WARNINGS"
    NEEDS_REVISION = "NEEDS_REVISION"
    BLOCKED = "BLOCKED"

    @classmethod
    def values(cls) -> list[str]:
        return [c.value for c in cls]

    @property
    def blocks_approval(self) -> bool:
        """Verdicts that may not enter the review/approval state machine."""
        return self in {ValidationVerdict.BLOCKED, ValidationVerdict.NEEDS_REVISION}


_PLATFORM_SQL = ",".join(repr(p.value) for p in Platform)


class ContentPlan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One week of planned content for a campaign."""

    __tablename__ = "content_plans"

    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    strategy_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("marketing_strategies.id", ondelete="SET NULL"), nullable=True
    )
    research_project_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("research_projects.id", ondelete="SET NULL"), nullable=True
    )
    week_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    week_end: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PlanStatus.DRAFT.value, index=True
    )
    objective: Mapped[str] = mapped_column(Text, nullable=False, default="")
    platforms: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    # Founder-configurable knobs: posts_per_week, content_mix, audience_focus,
    # publishing_windows. Stored verbatim so a plan is reproducible.
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    slot_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completeness_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # Provider attribution: which model (or the Phase 3A mock) produced the drafts,
    # plus the budget snapshot at generation time.
    generation_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    #: ``mock`` | ``deterministic`` | ``llm`` — surfaced in the UI so nobody
    #: mistakes mock output for real model output.
    generated_by: Mapped[str] = mapped_column(String(32), nullable=False, default="deterministic")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    slots: Mapped[list[ContentPlanSlot]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="ContentPlanSlot.position",
        # Eager by default: async sessions cannot lazy-load from inside a
        # service method, and a plan is useless without its slots.
        lazy="selectin",
    )

    __table_args__ = (
        CheckConstraint(
            f"status in ({','.join(repr(s.value) for s in PlanStatus)})",
            name="content_plan_status_valid",
        ),
        CheckConstraint("week_end >= week_start", name="content_plan_week_order"),
        CheckConstraint("slot_count >= 0", name="content_plan_slot_count_non_negative"),
        Index("ix_content_plans_brand_week", "brand_profile_id", "week_start"),
        UniqueConstraint(
            "brand_profile_id", "campaign_id", "week_start", name="uq_content_plans_brand_campaign_week"
        ),
    )


class ContentPlanSlot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A proposed slot in a weekly plan; becomes a ContentItem once generated."""

    __tablename__ = "content_plan_slots"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("content_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content_item_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("content_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    content_type: Mapped[str] = mapped_column(String(48), nullable=False)
    scheduled_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    scheduled_time: Mapped[str] = mapped_column(String(5), nullable=False, default="09:00")
    audience: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    pillar: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    objective: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    primary_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SlotStatus.PROPOSED.value
    )
    variant_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    generation_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )

    plan: Mapped[ContentPlan] = relationship(back_populates="slots")

    __table_args__ = (
        CheckConstraint(f"platform in ({_PLATFORM_SQL})", name="content_plan_slot_platform_valid"),
        CheckConstraint(
            f"status in ({','.join(repr(s.value) for s in SlotStatus)})",
            name="content_plan_slot_status_valid",
        ),
        Index("ix_content_plan_slots_plan_platform", "plan_id", "platform"),
    )


class ContentCalendarEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An internal planning date for a content item. Never an external schedule."""

    __tablename__ = "content_calendar_entries"

    content_item_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("content_items.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("content_plans.id", ondelete="SET NULL"), nullable=True
    )
    planned_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    planned_time: Mapped[str] = mapped_column(String(5), nullable=False, default="09:00")
    note: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("ix_content_calendar_brand_date", "brand_profile_id", "planned_date"),
        Index("ix_content_calendar_campaign_date", "campaign_id", "planned_date"),
    )


class ContentValidation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A Brand Guardian verdict bound to one immutable content version."""

    __tablename__ = "content_validations"

    content_item_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content_version_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("content_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    issues: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    checked_rules: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    blocking_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requires_founder_confirmation: Mapped[bool] = mapped_column(
        default=False, nullable=False
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="deterministic")
    generation_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="deterministic"
    )
    created_by_agent: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"verdict in ({','.join(repr(v.value) for v in ValidationVerdict)})",
            name="content_validation_verdict_valid",
        ),
        Index("ix_content_validations_version", "content_version_id", "created_at"),
    )
