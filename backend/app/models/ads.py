"""Paid advertising: proposals, founder authorisations and an append-only ledger.

THE RULE THIS TABLE SET EXISTS TO ENFORCE
-----------------------------------------
**No money is spent on an AI decision.** Creating, launching, pausing, resuming or
materially changing a paid campaign requires a founder authorisation that names
the exact proposal hash, the platform, the maximum spend and an expiry. The
authorisation is single-use and is consumed by the operation that uses it, so the
same approval cannot fund two launches.

Why the hash matters: an authorisation that merely said "approve this campaign"
would still be valid after the budget or the audience was edited. ``proposal_hash``
is recomputed from the mutable proposal fields at execution time, so any edit
after approval invalidates it — the same exact-version rule the content approval
gate uses.

``AdOperation`` is append-only: every attempted external mutation (including
refused ones) is recorded with its authorisation reference and evidence. A refusal
is evidence too — an audit trail that only records successes cannot prove the gate
held.
"""

from __future__ import annotations

import enum
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
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import GUID, JSONB

AD_PLATFORM_VALUES_SQL = "'meta_ads','google_ads','linkedin_ads'"


class AdPlatform(str, enum.Enum):
    META_ADS = "meta_ads"
    GOOGLE_ADS = "google_ads"
    LINKEDIN_ADS = "linkedin_ads"

    @classmethod
    def values(cls) -> list[str]:
        return [c.value for c in cls]


class AdCampaignStatus(str, enum.Enum):
    PROPOSED = "proposed"        # AI drafted it, nothing external exists
    AUTHORIZED = "authorized"    # founder approved this exact version
    CREATING = "creating"        # an external call is in flight
    ACTIVE = "active"            # verified to exist remotely
    PAUSED = "paused"
    COMPLETED = "completed"
    REJECTED = "rejected"        # founder declined
    FAILED = "failed"            # remote creation failed; nothing is running

    @classmethod
    def values(cls) -> list[str]:
        return [c.value for c in cls]


AD_CAMPAIGN_STATUS_SQL = ",".join(repr(s.value) for s in AdCampaignStatus)


class AdOperationKind(str, enum.Enum):
    CREATE = "create"
    LAUNCH = "launch"
    PAUSE = "pause"
    RESUME = "resume"
    BUDGET_CHANGE = "budget_change"
    READ = "read"

    @classmethod
    def values(cls) -> list[str]:
        return [c.value for c in cls]


class AdAuthorizationStatus(str, enum.Enum):
    ACTIVE = "active"
    CONSUMED = "consumed"
    REVOKED = "revoked"
    EXPIRED = "expired"

    @classmethod
    def values(cls) -> list[str]:
        return [c.value for c in cls]


class AdCampaign(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A paid campaign proposal and, once authorised, its remote mirror."""

    __tablename__ = "ad_campaigns"

    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Optional link to the organic campaign this paid push supports.
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    objective: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AdCampaignStatus.PROPOSED.value
    )
    # Everything a human must review, in structured form.
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    targeting: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    creatives: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    # Money is stored in minor units (cents) so floating point cannot round a
    # budget down between approval and execution.
    daily_budget_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_budget_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    # Recomputed at execution time; any edit to plan/targeting/budget changes it.
    proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    external_campaign_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by_agent: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint(f"platform in ({AD_PLATFORM_VALUES_SQL})", name="ad_campaign_platform_valid"),
        CheckConstraint(
            f"status in ({AD_CAMPAIGN_STATUS_SQL})", name="ad_campaign_status_valid"
        ),
        CheckConstraint(
            "daily_budget_cents >= 0 and total_budget_cents >= 0",
            name="ad_campaign_budget_non_negative",
        ),
        Index("ix_ad_campaigns_brand_status", "brand_profile_id", "status"),
    )


class AdBudgetAuthorization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Founder approval to spend, bound to one exact proposal version."""

    __tablename__ = "ad_budget_authorizations"

    ad_campaign_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("ad_campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    max_spend_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    allowed_operations: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AdAuthorizationStatus.ACTIVE.value
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Only a founder may create one; recorded explicitly for the audit trail.
    authorized_by: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consumed_by_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), nullable=True
    )
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (
        CheckConstraint(
            "status in ('active','consumed','revoked','expired')",
            name="ad_authorization_status_valid",
        ),
        CheckConstraint("max_spend_cents > 0", name="ad_authorization_spend_positive"),
        Index("ix_ad_authorizations_campaign_status", "ad_campaign_id", "status"),
    )


class AdOperation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Append-only record of every attempted external mutation."""

    __tablename__ = "ad_operations"

    ad_campaign_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("ad_campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    authorization_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("ad_budget_authorizations.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # requested | refused | succeeded | failed | unverified
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False, default="system")

    __table_args__ = (
        CheckConstraint(
            "kind in ('create','launch','pause','resume','budget_change','read')",
            name="ad_operation_kind_valid",
        ),
        CheckConstraint(
            "outcome in ('requested','refused','succeeded','failed','unverified')",
            name="ad_operation_outcome_valid",
        ),
        UniqueConstraint(
            "ad_campaign_id",
            "kind",
            "request_fingerprint",
            name="uq_ad_operations_idempotent",
        ),
        Index("ix_ad_operations_campaign_created", "ad_campaign_id", "created_at"),
    )
