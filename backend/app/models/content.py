"""Content items and immutable content versions.

Design rule that makes the approval system trustworthy: **content is versioned,
versions are immutable.** Editing a content item never mutates an existing
version row; it appends a new one. Approvals bind to a ``content_version_id``,
so an edit can never silently carry an approval forward.
"""

from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
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


class ContentStatus(str, enum.Enum):
    """The approval state machine. See ``app/services/approval.py``."""

    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    SCHEDULED = "SCHEDULED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @classmethod
    def values(cls) -> list[str]:
        return [c.value for c in cls]

    @property
    def is_terminal(self) -> bool:
        return self in {
            ContentStatus.PUBLISHED,
            ContentStatus.CANCELLED,
            ContentStatus.REJECTED,
        }


class ContentItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "content_items"

    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pillar_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("content_pillars.id", ondelete="SET NULL"), nullable=True
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    format: Mapped[str] = mapped_column(String(48), nullable=False, default="post")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ContentStatus.DRAFT.value, index=True
    )
    # The only publishable version. FK is enforced by the DB.
    # NOTE: circular FK (content_items -> content_versions -> content_items).
    # The Alembic migration creates both tables first and then adds this
    # constraint via a batch ALTER, which works on PostgreSQL and SQLite. Plain
    # ``create_all`` cannot order this cycle on PostgreSQL; use migrations there.
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey(
            "content_versions.id",
            ondelete="SET NULL",
            name="fk_content_items_current_version_id",
        ),
        nullable=True,
    )
    version_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by_agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    versions: Mapped[list[ContentVersion]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
        order_by="ContentVersion.version_number",
        foreign_keys="ContentVersion.content_item_id",
    )

    __table_args__ = (
        CheckConstraint(
            "status in ('DRAFT','IN_REVIEW','APPROVED','SCHEDULED','PUBLISHING',"
            "'PUBLISHED','REJECTED','CHANGES_REQUESTED','FAILED','CANCELLED')",
            name="content_status_valid",
        ),
        CheckConstraint(
            f"platform in ({','.join(repr(p.value) for p in Platform)})",
            name="content_platform_valid",
        ),
        CheckConstraint("version_count >= 0", name="content_version_count_non_negative"),
        Index("ix_content_items_brand_status", "brand_profile_id", "status"),
        Index("ix_content_items_platform_status", "platform", "status"),
        Index("ix_content_items_campaign", "campaign_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ContentItem {self.title!r} status={self.status} v={self.version_count}>"


class ContentVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Immutable snapshot of a content item's copy and media references."""

    __tablename__ = "content_versions"

    content_item_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    headline: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    hashtags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    call_to_action: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    media_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    # Deterministic hash of the publishable payload; used for audit + integrity.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    change_note: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by_agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generation_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )

    item: Mapped[ContentItem] = relationship(
        back_populates="versions", foreign_keys=[content_item_id]
    )

    __table_args__ = (
        UniqueConstraint(
            "content_item_id", "version_number", name="uq_content_versions_item_number"
        ),
        CheckConstraint("version_number >= 1", name="content_version_number_positive"),
        Index("ix_content_versions_item", "content_item_id", "version_number"),
    )
