"""Flow companion sessions and asset export tickets (Phase 3C.1).

Two small tables, both additive:

``flow_companion_sessions``
    One row per local companion that has authenticated to Crusor. It records when
    the companion was last seen and what it reported about the extension, so the UI
    can say "companion online, no Flow tab" instead of guessing. No credential is
    stored — only a truncated SHA-256 fingerprint used for display.

``flow_asset_exports``
    A one-time, short-lived upload ticket. The companion asks for a ticket, uploads
    the verified media bytes to it, and the ticket is consumed exactly once. The row
    keeps the expected checksum so an upload that does not match is rejected
    *before* an asset exists. The asset id is only filled in after the bytes were
    stored and read back.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import GUID, JSONB


class FlowCompanionSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "flow_companion_sessions"

    companion_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    protocol: Mapped[str] = mapped_column(String(32), nullable=False, default="flow.companion.v1")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="connected", index=True)

    extension_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    extension_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    flow_tab_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    bridge_paired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    live_generation_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    client_host: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: First 16 hex chars of SHA-256(secret). A label, never a credential.
    secret_fingerprint: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status in ('connected','stale','disconnected')",
            name="ck_flow_session_status",
        ),
        Index("ix_flow_sessions_companion_seen", "companion_id", "last_seen_at"),
    )


class FlowAssetExport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "flow_asset_exports"

    #: SHA-256 of the one-time token. The token itself is returned exactly once, in
    #: the response that creates the ticket, and is never recoverable afterwards.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)

    companion_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    generation_job_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("creative_generation_jobs.id", ondelete="SET NULL"), nullable=True
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True
    )
    content_item_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("content_items.id", ondelete="SET NULL"), nullable=True
    )
    content_version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("content_versions.id", ondelete="SET NULL"), nullable=True
    )
    brief_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("creative_briefs.id", ondelete="SET NULL"), nullable=True
    )
    brief_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    declared_content_type: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    filename: Mapped[str] = mapped_column(String(255), nullable=False, default="flow-asset")
    max_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Phase 3C.2 chunked transfer -------------------------------------
    #: ``single`` for the one-shot PUT, ``chunked`` once a chunk arrives. A ticket
    #: never mixes the two, so a partial chunked upload cannot be finished by a
    #: single-shot body (or vice versa).
    upload_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="single")
    chunk_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunks_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: ``[{"index": 0, "sha256": "...", "size": 1234}, ...]`` — one entry per
    #: accepted chunk, in arrival order (which must be index order).
    chunk_manifest: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)

    received_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    storage_key: Mapped[str] = mapped_column(Text, nullable=False, default="")
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("creative_assets.id", ondelete="SET NULL"), nullable=True
    )
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status in ('pending','consumed','expired','failed','cancelled')",
            name="ck_flow_export_status",
        ),
        Index("ix_flow_exports_status_expires", "status", "expires_at"),
    )
