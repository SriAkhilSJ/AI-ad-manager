from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text
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


class CreativeAsset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "creative_assets"

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
        Index("ix_creative_assets_item_kind", "content_item_id", "kind"),
        Index("ix_creative_assets_checksum", "checksum"),
    )
