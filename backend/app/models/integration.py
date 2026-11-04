"""OAuth credentials, pending authorisation flows and platform connections.

SECURITY MODEL
--------------
``social_accounts`` deliberately has no column that can hold a token. This module
adds two *separate* tables:

``oauth_tokens``
    Holds **ciphertext only** (AES-256-GCM, associated data = brand|platform|
    account). The plaintext token exists in memory for the duration of one API
    call. No response schema maps to this table, so it cannot leak through the
    API by accident, and a database dump alone does not yield usable credentials.

``oauth_flows``
    One row per authorisation in progress. The PKCE ``code_verifier`` is stored
    sealed with the same cipher, and the row is **single use**: the ``state``
    nonce has a unique constraint and ``consumed_at`` is set on the first
    successful exchange, so a replayed callback is rejected rather than
    re-exchanging a code.

Both tables are excluded from every API schema. ``token_ref`` on
``social_accounts`` is the only pointer the rest of the application ever sees.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
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
from app.models.social import PLATFORM_VALUES_SQL


class OAuthToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Encrypted credential material. Never serialised."""

    __tablename__ = "oauth_tokens"

    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    # Stable identity of the remote account (page id, org urn, user id…).
    account_key: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # ``v1:<nonce>:<ciphertext>`` — the only place a token lives at rest.
    sealed_access: Mapped[str] = mapped_column(Text, nullable=False)
    sealed_refresh: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_type: Mapped[str] = mapped_column(String(32), nullable=False, default="Bearer")
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Counts successful refreshes so an operator can spot a loop.
    refresh_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            f"platform in ({PLATFORM_VALUES_SQL})", name="oauth_token_platform_valid"
        ),
        UniqueConstraint(
            "brand_profile_id",
            "platform",
            "account_key",
            name="uq_oauth_tokens_owner",
        ),
        Index("ix_oauth_tokens_brand_platform", "brand_profile_id", "platform"),
    )


class OAuthFlow(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single-use authorisation attempt."""

    __tablename__ = "oauth_flows"

    brand_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(255), nullable=False)
    # Sealed PKCE verifier: useless without TOKEN_VAULT_KEY.
    sealed_code_verifier: Mapped[str | None] = mapped_column(Text, nullable=True)
    redirect_uri: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    requested_scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    pkce_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        CheckConstraint(
            f"platform in ({PLATFORM_VALUES_SQL})", name="oauth_flow_platform_valid"
        ),
        UniqueConstraint("state", name="uq_oauth_flows_state"),
        Index("ix_oauth_flows_brand_platform", "brand_profile_id", "platform"),
    )
