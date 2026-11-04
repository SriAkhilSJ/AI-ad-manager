"""Authentication & authorisation primitives.

- Passwords hashed with bcrypt (cost 12), verified in constant time.
- JWT access tokens (HS256) carrying ``sub``, ``role``, ``exp``, ``jti``.
- Role based access control via :data:`ROLE_RANK`; ``founder`` is the only role
  allowed to approve content.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.core.config import Settings, get_settings
from app.core.errors import AuthenticationError, PermissionDeniedError

ALGORITHM = "HS256"
BCRYPT_ROUNDS = 12


class Role(str, enum.Enum):
    FOUNDER = "founder"          # sole approver + full administration
    MARKETER = "marketer"        # can create/edit content, agents, campaigns
    VIEWER = "viewer"            # read-only


ROLE_RANK = {Role.VIEWER: 1, Role.MARKETER: 2, Role.FOUNDER: 3}
APPROVER_ROLES = {Role.FOUNDER}


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(BCRYPT_ROUNDS)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(
    *,
    subject: str,
    role: Role,
    settings: Settings | None = None,
    expires_delta: timedelta | None = None,
) -> tuple[str, datetime]:
    settings = settings or get_settings()
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(minutes=settings.access_token_expire_minutes))
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role.value,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "jti": uuid.uuid4().hex,
        "iss": "crusor-marketing-director",
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)
    return token, expire


def decode_access_token(token: str, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except JWTError as exc:  # expired, tampered, malformed
        raise AuthenticationError("Invalid or expired access token.") from exc
    if not payload.get("sub"):
        raise AuthenticationError("Access token missing subject.")
    return payload


def role_at_least(role: Role, minimum: Role) -> bool:
    return ROLE_RANK[role] >= ROLE_RANK[minimum]


def require_role(role: Role, minimum: Role) -> None:
    if not role_at_least(role, minimum):
        raise PermissionDeniedError(
            f"Role '{role.value}' is insufficient; '{minimum.value}' or higher required."
        )
