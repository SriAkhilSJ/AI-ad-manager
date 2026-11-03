"""Marketing strategies, their immutable versions, and strategy approval.

Strategy approval deliberately mirrors the Phase 1 content guarantee rather than
reusing it: ``ApprovalRecord.content_item_id`` is ``NOT NULL`` and is load-bearing
for the publish gate, so widening it would put Phase 1 enforcement at risk.

Same semantics, separate table:

* approval binds to an **exact strategy version**, not "the latest";
* the ledger is append-only; revocation sets ``invalidated_at``;
* editing an approved strategy creates a new version and revokes the approval;
* only the founder may approve.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
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
from app.models.research import BUSINESS_OBJECTIVE_SQL, VERIFICATION_SQL


class StrategyStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    SUPERSEDED = "SUPERSEDED"
    ARCHIVED = "ARCHIVED"

    @classmethod
    def values(cls) -> list[str]:
        return [c.value for c in cls]


STRATEGY_STATUS_SQL = "(" + ",".join(f"'{s.value}'" for s in StrategyStatus) + ")"


class MarketingStrategy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "marketing_strategies"

    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    research_project_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("research_projects.id", ondelete="SET NULL"), nullable=True
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True
    )
    workflow_thread_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )

    name: Mapped[str] = mapped_column(String(300), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=StrategyStatus.DRAFT.value, index=True
    )
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    version_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    primary_objective_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="developer_acquisition"
    )
    created_by_agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(f"status in {STRATEGY_STATUS_SQL}", name="strategy_status_valid"),
        CheckConstraint(
            f"primary_objective_type in {BUSINESS_OBJECTIVE_SQL}",
            name="strategy_objective_type_valid",
        ),
        CheckConstraint("version_count >= 0", name="strategy_version_count_non_negative"),
        Index("ix_strategies_brand_status", "brand_profile_id", "status"),
    )


class StrategyVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Immutable snapshot of a strategy. Never updated in place."""

    __tablename__ = "strategy_versions"

    strategy_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("marketing_strategies.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- the strategy payload -------------------------------------------
    positioning_recommendations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    audience_segmentation: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    competitor_differentiation: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    content_pillars: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    campaign_concepts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    platform_strategies: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    weekly_objectives: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    publishing_frequency: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    lead_generation: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    growth_experiments: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    kpis: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    measurement_plan: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )

    # --- linkage + provenance -------------------------------------------
    business_objectives: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    objective_alignment: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    source_finding_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    research_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # --- self-assessment -------------------------------------------------
    brand_check: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    missing_information: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    warnings: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    generation_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="deterministic"
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Fingerprint of the payload; approval binds to it and it is re-checked.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    change_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by_agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint("version_number >= 1", name="strategy_version_number_positive"),
        CheckConstraint(
            "confidence >= 0 and confidence <= 1", name="strategy_version_confidence_range"
        ),
        CheckConstraint("cost_usd >= 0", name="strategy_version_cost_non_negative"),
        UniqueConstraint(
            "strategy_id", "version_number", name="uq_strategy_versions_strategy_version"
        ),
        Index("ix_strategy_versions_strategy_created", "strategy_id", "created_at"),
    )


class StrategyApproval(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Append-only approval ledger bound to an exact strategy version."""

    __tablename__ = "strategy_approvals"

    strategy_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("marketing_strategies.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("strategy_versions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invalidated_reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "action in ('submit','approve','reject','request_changes','revoke')",
            name="strategy_approval_action_valid",
        ),
        Index("ix_strategy_approvals_strategy_created", "strategy_id", "created_at"),
        Index(
            "ix_strategy_approvals_valid",
            "strategy_id",
            "strategy_version_id",
            "action",
        ),
    )

    @property
    def is_valid(self) -> bool:
        return self.invalidated_at is None and self.action == "approve"


class GrowthExperiment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A proposed growth experiment extracted from an approved strategy."""

    __tablename__ = "growth_experiments"

    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    strategy_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("marketing_strategies.id", ondelete="SET NULL"), nullable=True
    )
    strategy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("strategy_versions.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False, default="")
    channel: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    objective_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="developer_acquisition"
    )
    primary_metric: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    expected_effort: Mapped[str] = mapped_column(String(32), nullable=False, default="medium")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="proposed")
    planned_for: Mapped[date | None] = mapped_column(Date, nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"objective_type in {BUSINESS_OBJECTIVE_SQL}", name="growth_experiment_objective_valid"
        ),
        CheckConstraint(
            "status in ('proposed','approved','running','completed','abandoned')",
            name="growth_experiment_status_valid",
        ),
        CheckConstraint(
            "expected_effort in ('low','medium','high')", name="growth_experiment_effort_valid"
        ),
    )
