from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import DB, CurrentUser, FounderUser
from app.core.errors import AuthenticationError, ConflictError
from app.core.security import Role, create_access_token, hash_password, verify_password
from app.models.user import User
from app.schemas.auth import TokenRequest, TokenResponse, UserCreate, UserOut
from sqlalchemy import select

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/token", response_model=TokenResponse, summary="Obtain an access token")
async def login(payload: TokenRequest, session: DB) -> TokenResponse:
    user = (
        await session.execute(select(User).where(User.email == payload.email.lower()))
    ).scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.hashed_password):
        # Same error for unknown user and bad password (no account enumeration).
        raise AuthenticationError("Invalid email or password.")
    if not user.is_active:
        raise AuthenticationError("This account is disabled.")
    token, expires_at = create_access_token(subject=str(user.id), role=Role(user.role))
    return TokenResponse(
        access_token=token, expires_at=expires_at, user=UserOut.model_validate(user)
    )


@router.get("/me", response_model=UserOut, summary="Current authenticated user")
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.post(
    "/users",
    response_model=UserOut,
    status_code=201,
    summary="Create a user (founder only)",
)
async def create_user(payload: UserCreate, founder: FounderUser, session: DB) -> UserOut:
    exists = (
        await session.execute(select(User).where(User.email == payload.email.lower()))
    ).scalar_one_or_none()
    if exists is not None:
        raise ConflictError("A user with this email already exists.")
    user = User(
        email=payload.email.lower(),
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role=payload.role,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return UserOut.model_validate(user)
