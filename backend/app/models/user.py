from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.security import Role
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import GUID

# Phase 1 is single-organisation, but every brand-scoped table carries an
# organisation id so adding tenant isolation later is a data migration, not a
# schema change.
DEFAULT_ORGANIZATION_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        String(32), nullable=False, default=Role.VIEWER.value
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), nullable=False, default=DEFAULT_ORGANIZATION_ID, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (Index("ix_users_org_role", "organization_id", "role"),)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.email} role={self.role}>"
