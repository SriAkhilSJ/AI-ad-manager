"""Append-only approval ledger.

A row is *never* updated to mean something different. Revocation is expressed by
setting ``invalidated_at``/``invalidated_reason`` on the specific record, which
leaves the audit history intact and makes "was this ever approved?" answerable.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import GUID, JSONB


class ApprovalAction(str, enum.Enum):
    SUBMIT = "submit"
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"
    REVOKE = "revoke"
    CANCEL = "cancel"


class ApprovalRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "approval_records"

    content_item_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Approval is bound to an exact immutable version — never "the latest".
    content_version_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("content_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Fingerprint of the payload that was approved; compared at publish time.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invalidated_reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    request_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )

    __table_args__ = (
        CheckConstraint(
            "action in ('submit','approve','reject','request_changes','revoke','cancel')",
            name="approval_action_valid",
        ),
        Index("ix_approvals_item_created", "content_item_id", "created_at"),
        Index(
            "ix_approvals_valid_lookup",
            "content_item_id",
            "content_version_id",
            "action",
        ),
    )

    @property
    def is_valid(self) -> bool:
        return self.invalidated_at is None and self.action == ApprovalAction.APPROVE.value
