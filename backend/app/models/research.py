"""Research, audience and brand-fact persistence.

Design rules that the schema enforces structurally, not by convention:

* **A finding cannot be ``verified`` without a source.** Enforced by
  ``ck_research_findings_verified_requires_source``; a service bug or a careless
  migration cannot produce an uncited "verified" claim.
* **Findings are deduplicated per project** by a stable ``dedupe_key``
  (category + normalised source + normalised finding), so re-running research
  cannot inflate the corpus.
* **Unknown brand facts are stored as unverified**, never silently omitted and
  never asserted.

Provenance columns mirror the required finding envelope exactly: finding, source
URL, source title, retrieval date, confidence, evidence summary, verification
status.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import GUID, JSONB


class VerificationStatus(str, enum.Enum):
    """Whether a statement is backed by a checkable source."""

    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    NEEDS_CONFIRMATION = "needs_confirmation"
    REJECTED = "rejected"

    @classmethod
    def values(cls) -> list[str]:
        return [c.value for c in cls]


VERIFICATION_SQL = "('verified','unverified','needs_confirmation','rejected')"


class ResearchStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"

    @classmethod
    def values(cls) -> list[str]:
        return [c.value for c in cls]


RESEARCH_STATUS_SQL = (
    "('queued','running','succeeded','partial','failed','budget_exceeded','cancelled',"
    "'unavailable')"
)

# The nine research categories the brief requires the agent to cover.
RESEARCH_CATEGORIES = (
    "competitor_identification",
    "competitor_positioning",
    "feature_comparison",
    "competitor_content_strategy",
    "developer_pain_points",
    "market_trends",
    "developer_communities",
    "content_opportunities",
    "audience_questions_objections",
)

RESEARCH_CATEGORY_SQL = "(" + ",".join(f"'{c}'" for c in RESEARCH_CATEGORIES) + ")"

# Business objectives. Strategies must map onto at least one of these.
BUSINESS_OBJECTIVES = (
    "brand_awareness",
    "developer_acquisition",
    "waitlist_registrations",
    "product_adoption",
    "community_growth",
)

BUSINESS_OBJECTIVE_SQL = "(" + ",".join(f"'{o}'" for o in BUSINESS_OBJECTIVES) + ")"


class ResearchSource(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A citable source. One row per (project, url) observation."""

    __tablename__ = "research_sources"

    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    research_project_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("research_projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    url: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    publisher: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    retrieved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Stable hash of the source content, used for dedup and change detection.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    credibility: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    project: Mapped[ResearchProject | None] = relationship(
        "ResearchProject", back_populates="sources"
    )

    __table_args__ = (
        CheckConstraint("length(url) > 0", name="research_source_requires_url"),
        UniqueConstraint(
            "research_project_id", "url", name="uq_research_sources_project_url"
        ),
        Index("ix_research_sources_brand_retrieved", "brand_profile_id", "retrieved_at"),
    )


class ResearchProject(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One research run: an objective, a scope, and its cost envelope."""

    __tablename__ = "research_projects"

    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workflow_thread_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ResearchStatus.QUEUED.value, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    categories: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    # Budget envelope, reported before and after execution.
    max_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    actual_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    finding_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gap_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    sources: Mapped[list[ResearchSource]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint(f"status in {RESEARCH_STATUS_SQL}", name="research_project_status_valid"),
        CheckConstraint(
            "max_cost_usd >= 0 and estimated_cost_usd >= 0 and actual_cost_usd >= 0",
            name="research_project_costs_non_negative",
        ),
        Index("ix_research_projects_brand_status", "brand_profile_id", "status"),
    )


class ResearchFinding(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single sourced claim.

    ``source_url`` is denormalised from the source row so the finding remains
    self-describing even if the source is later pruned, and so the
    "verified requires a source" constraint can be expressed in SQL.
    """

    __tablename__ = "research_findings"

    research_project_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("research_projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("research_sources.id", ondelete="SET NULL"), nullable=True
    )

    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    finding: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # --- provenance envelope (all required by the brief) -------------------
    source_url: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    source_title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    retrieved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    verification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=VerificationStatus.UNVERIFIED.value, index=True
    )

    competitor_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint(
            f"category in {RESEARCH_CATEGORY_SQL}", name="research_finding_category_valid"
        ),
        CheckConstraint(
            f"verification_status in {VERIFICATION_SQL}",
            name="research_finding_verification_valid",
        ),
        # The core anti-fabrication guarantee, expressed in the database.
        CheckConstraint(
            "verification_status != 'verified' OR length(source_url) > 0",
            name="research_findings_verified_requires_source",
        ),
        CheckConstraint(
            "confidence >= 0 and confidence <= 1", name="research_finding_confidence_range"
        ),
        UniqueConstraint(
            "research_project_id", "dedupe_key", name="uq_research_findings_project_dedupe"
        ),
        Index("ix_research_findings_project_category", "research_project_id", "category"),
        Index(
            "ix_research_findings_brand_verification",
            "brand_profile_id",
            "verification_status",
        ),
    )


class AudienceSegment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A named, editable audience definition (first-class, not a string list)."""

    __tablename__ = "audience_segments"

    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    motivations: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    channels: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    source: Mapped[str] = mapped_column(String(500), nullable=False, default="founder")
    verification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=VerificationStatus.VERIFIED.value
    )

    __table_args__ = (
        CheckConstraint("priority between 1 and 5", name="audience_segment_priority_range"),
        CheckConstraint(
            f"verification_status in {VERIFICATION_SQL}",
            name="audience_segment_verification_valid",
        ),
        UniqueConstraint("brand_profile_id", "name", name="uq_audience_segments_brand_name"),
        Index("ix_audience_segments_brand_active", "brand_profile_id", "is_active"),
    )


class AudiencePainPoint(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A stated audience problem, optionally linked to a segment and a source."""

    __tablename__ = "audience_pain_points"

    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    audience_segment_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("audience_segments.id", ondelete="SET NULL"), nullable=True
    )
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False, default="medium")
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("research_sources.id", ondelete="SET NULL"), nullable=True
    )
    source_url: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    verification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=VerificationStatus.UNVERIFIED.value
    )
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "severity in ('low','medium','high','critical')", name="pain_point_severity_valid"
        ),
        CheckConstraint(
            f"verification_status in {VERIFICATION_SQL}",
            name="pain_point_verification_valid",
        ),
        CheckConstraint(
            "verification_status != 'verified' OR length(source_url) > 0",
            name="pain_points_verified_requires_source",
        ),
        UniqueConstraint(
            "brand_profile_id", "dedupe_key", name="uq_pain_points_brand_dedupe"
        ),
    )


class ProductFact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A product claim with an explicit verification state.

    Unverifiable facts are stored as ``unverified``/``needs_confirmation`` and are
    surfaced to the founder for confirmation. They are never presented as known.
    """

    __tablename__ = "product_facts"

    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="capability")
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_url: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    verification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=VerificationStatus.NEEDS_CONFIRMATION.value, index=True
    )
    verified_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_public: Mapped[bool] = mapped_column(default=False, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        CheckConstraint(
            f"verification_status in {VERIFICATION_SQL}",
            name="product_fact_verification_valid",
        ),
        CheckConstraint(
            "verification_status != 'verified' OR length(source_url) > 0",
            name="product_facts_verified_requires_source",
        ),
        UniqueConstraint("brand_profile_id", "dedupe_key", name="uq_product_facts_brand_dedupe"),
    )


class MarketingObjective(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A business objective a strategy must connect to.

    ``objective_type`` is constrained to the five approved business outcomes so a
    strategy cannot claim to serve a goal that does not exist.
    """

    __tablename__ = "marketing_objectives"

    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    objective_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    primary_metric: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    target_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    horizon: Mapped[str] = mapped_column(String(50), nullable=False, default="quarter")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    __table_args__ = (
        CheckConstraint(
            f"objective_type in {BUSINESS_OBJECTIVE_SQL}", name="marketing_objective_type_valid"
        ),
        CheckConstraint("priority between 1 and 5", name="marketing_objective_priority_range"),
        Index("ix_marketing_objectives_brand_active", "brand_profile_id", "is_active"),
    )
