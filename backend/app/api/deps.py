"""Shared FastAPI dependencies: DB session, current user, RBAC, rate limiting."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AuthenticationError, PermissionDeniedError, RateLimitedError
from app.core.rate_limit import get_rate_limiter
from app.core.security import Role, decode_access_token, role_at_least
from app.db.session import get_db
from app.models.user import User

bearer_scheme = HTTPBearer(auto_error=False)

DB = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def _resolve_user(
    credentials: HTTPAuthorizationCredentials | None, session: AsyncSession
) -> User:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Missing bearer token.")
    payload = decode_access_token(credentials.credentials)
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("Malformed token subject.") from exc
    user = (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthenticationError("User not found or inactive.")
    token_role = payload.get("role")
    if token_role and token_role != user.role:
        # Role changed since the token was issued: force re-authentication.
        raise AuthenticationError("Token role is stale; sign in again.")
    return user


async def get_current_user(
    session: DB,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ] = None,
) -> User:
    return await _resolve_user(credentials, session)


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_min_role(minimum: Role):  # type: ignore[no-untyped-def]
    async def _dep(user: CurrentUser) -> User:
        if not role_at_least(Role(user.role), minimum):
            raise PermissionDeniedError(
                f"'{minimum.value}' role or higher required.",
                details={"your_role": user.role},
            )
        return user

    return _dep


async def require_founder(user: CurrentUser) -> User:
    """Approval is founder-only. There is no configuration that widens this."""
    if user.role != Role.FOUNDER.value:
        raise PermissionDeniedError(
            "Only the founder can approve or reject content.",
            details={"your_role": user.role},
        )
    return user


FounderUser = Annotated[User, Depends(require_founder)]
EditorUser = Annotated[User, Depends(require_min_role(Role.MARKETER))]
AnyUser = CurrentUser


async def rate_limit(request: Request, settings: SettingsDep) -> None:
    """Per-identity HTTP rate limit. Identity = user id or client host."""
    limiter = get_rate_limiter()
    identity = getattr(request.state, "user_id", None) or (
        request.client.host if request.client else "anonymous"
    )
    allowed, retry_after = limiter.hit(
        f"{request.url.path}:{identity}",
        limit=settings.rate_limit_per_minute,
        window_seconds=60,
    )
    if not allowed:
        raise RateLimitedError(
            "Too many requests.",
            details={"retry_after_seconds": retry_after, "driver": limiter.driver},
        )


def idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str | None:
    return idempotency_key


IdempotencyKeyDep = Annotated[str | None, Depends(idempotency_key)]


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def pagination(limit: int = 50, offset: int = 0) -> dict[str, int]:
    return {"limit": max(1, min(limit, 200)), "offset": max(0, offset)}


Pagination = Annotated[dict[str, int], Depends(pagination)]
