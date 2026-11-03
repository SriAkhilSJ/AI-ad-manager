"""Social accounts, scheduled posts and published posts.

SECURITY: ``social_accounts`` deliberately has **no** column capable of holding an
OAuth access/refresh token. It stores only ``token_ref`` — an opaque pointer into
an external token vault — plus non-secret metadata. Response schemas are
allow-lists, so even this table cannot leak credentials to the frontend.
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
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import GUID, JSONB


class Platform(str, enum.Enum):
    INSTAGRAM = "instagram"
    LINKEDIN = "linkedin"
    YOUTUBE = "youtube"
    X = "x"
    FACEBOOK = "facebook"
    REDDIT = "reddit"

    @classmethod
    def values(cls) -> list[str]:
        return [c.value for c in cls]


PLATFORM_VALUES_SQL = ",".join(repr(p.value) for p in Platform)


class SocialAccountStatus(str, enum.Enum):
    NOT_CONNECTED = "not_connected"    # known platform, no account linked
    PENDING = "pending"                # OAuth started, not completed
    CONNECTED = "connected"
    EXPIRED = "expired"
    REVOKED = "revoked"
    ERROR = "error"


class SocialAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "social_accounts"

    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    handle: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    external_account_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SocialAccountStatus.NOT_CONNECTED.value
    )
    # Opaque reference into an external vault. Never a token.
    token_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (
        CheckConstraint(
            f"platform in ({PLATFORM_VALUES_SQL})", name="social_account_platform_valid"
        ),
        CheckConstraint(
            "status in ('not_connected','pending','connected','expired','revoked','error')",
            name="social_account_status_valid",
        ),
        UniqueConstraint(
            "brand_profile_id", "platform", "handle", name="uq_social_accounts_brand_platform_handle"
        ),
        Index("ix_social_accounts_brand_platform", "brand_profile_id", "platform"),
    )


class ScheduledPost(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "scheduled_posts"

    content_item_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content_version_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("content_versions.id", ondelete="CASCADE"), nullable=False
    )
    social_account_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("social_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The approval that authorised this schedule. Restrict on delete: never
    # orphan a scheduled post from its authority.
    approval_record_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("approval_records.id", ondelete="RESTRICT"),
        nullable=False,
    )
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (
        CheckConstraint(
            "status in ('pending','queued','published','failed','cancelled')",
            name="scheduled_post_status_valid",
        ),
        Index("ix_scheduled_posts_due", "status", "scheduled_for"),
    )


class PublishedPost(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "published_posts"

    content_item_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("content_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    content_version_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("content_versions.id", ondelete="RESTRICT"), nullable=False
    )
    social_account_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("social_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    # Publication cannot exist without naming the approval that authorised it.
    approval_record_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("approval_records.id", ondelete="RESTRICT"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    external_post_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    permalink: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="published")
    publish_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )

    __table_args__ = (
        UniqueConstraint(
            "content_version_id", "social_account_id", name="uq_published_version_account"
        ),
        Index("ix_published_posts_platform_date", "platform", "published_at"),
    )
