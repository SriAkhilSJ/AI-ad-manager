from __future__ import annotations

import enum
import uuid
from typing import Any

from datetime import datetime

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
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import GUID, JSONB


class CreativeKind(str, enum.Enum):
    IMAGE = "image"
    VIDEO = "video"
    CAROUSEL = "carousel"
    THUMBNAIL = "thumbnail"
    DOCUMENT = "document"


class CreativeAssetStatus(str, enum.Enum):
    PENDING = "pending"          # registered, bytes not yet stored
    READY = "ready"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class CreativeValidationStatus(str, enum.Enum):
    """Outcome of the automated Creative QA pass (never a human visual verdict)."""

    UNVERIFIED = "unverified"
    PASS = "pass"
    PASS_WITH_WARNINGS = "pass_with_warnings"
    NEEDS_REVISION = "needs_revision"
    BLOCKED = "blocked"

    @classmethod
    def values(cls) -> list[str]:
        return [status.value for status in cls]


class CreativeAsset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "creative_assets"

    brand_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Phase 3B: the creative brief (and its immutable version) this asset serves.
    brief_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("creative_briefs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    brief_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Deliberately not a foreign key: the job row points at the asset, and a
    #: mutual FK pair would make table creation order ambiguous. The id is still
    #: indexed and queryable in both directions.
    generation_job_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    #: True for the asset currently bound to its content version. Selecting another
    #: asset clears this flag on the previous one, which is what invalidates an
    #: approval that referenced it.
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Asset versions: a replacement creates version n+1 rather than overwriting
    #: version n, so an approved original can never be silently destroyed.
    asset_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    supersedes_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("creative_assets.id", ondelete="SET NULL"), nullable=True
    )
    root_asset_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)

    content_item_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("content_items.id", ondelete="CASCADE"), nullable=True, index=True
    )
    content_version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("content_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default=CreativeKind.IMAGE.value)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CreativeAssetStatus.PENDING.value
    )
    filename: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Object-storage key; bytes never live in the database.
    storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    public_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # SHA-256 of the bytes. Replacing an asset changes the checksum, which is the
    # signal used to invalidate any approval that referenced the old asset.
    checksum: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    aspect_ratio: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    #: True when the bytes came from the deterministic mock provider. Every
    #: surface that renders an asset must show this, so a synthetic frame is never
    #: mistaken for a real generated asset.
    is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: MIME derived from the bytes, not from the client's Content-Type header.
    detected_mime: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    metadata_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    validation_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CreativeValidationStatus.UNVERIFIED.value
    )
    validation_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    qa_report: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    generated_by_agent: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "kind in ('image','video','carousel','thumbnail','document')",
            name="creative_kind_valid",
        ),
        CheckConstraint(
            "status in ('pending','ready','failed','superseded')",
            name="creative_asset_status_valid",
        ),
        CheckConstraint("size_bytes >= 0", name="creative_asset_size_non_negative"),
        CheckConstraint(
            "validation_status in ('unverified','pass','pass_with_warnings',"
            "'needs_revision','blocked')",
            name="creative_asset_validation_valid",
        ),
        Index("ix_creative_assets_item_kind", "content_item_id", "kind"),
        Index("ix_creative_assets_checksum", "checksum"),
    )
