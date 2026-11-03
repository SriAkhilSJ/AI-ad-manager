from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.api.deps import DB, CurrentUser, EditorUser
from app.models.social import SocialAccountStatus
from app.publishing.adapters import get_adapter_registry
from app.schemas.social import (
    PlatformStatusOut,
    PublishedPostOut,
    ScheduledPostOut,
    SocialAccountCreate,
    SocialAccountOut,
)
from app.services.social import SocialAccountService
from sqlalchemy import select
from app.models.social import PublishedPost, ScheduledPost

router = APIRouter(prefix="/social", tags=["social"])


@router.get(
    "/platforms",
    response_model=list[PlatformStatusOut],
    summary="Which platforms are configured and publishable",
)
async def platform_status(user: CurrentUser) -> list[PlatformStatusOut]:
    return [PlatformStatusOut(**row) for row in get_adapter_registry().availability()]


@router.get("/accounts", response_model=list[SocialAccountOut], summary="List social accounts")
async def list_accounts(
    session: DB, user: CurrentUser, brand_profile_id: uuid.UUID | None = None
) -> list[SocialAccountOut]:
    rows = await SocialAccountService(session).list(brand_profile_id=brand_profile_id)
    return [SocialAccountOut.model_validate(r) for r in rows]


@router.post(
    "/accounts",
    response_model=SocialAccountOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a social account (metadata only; no tokens are accepted here)",
)
async def create_account(
    payload: SocialAccountCreate, session: DB, user: EditorUser
) -> SocialAccountOut:
    account = await SocialAccountService(session).upsert_metadata(
        brand_profile_id=payload.brand_profile_id,
        platform=payload.platform,
        handle=payload.handle,
        display_name=payload.display_name,
        status=SocialAccountStatus.PENDING.value,
    )
    await session.commit()
    await session.refresh(account)
    return SocialAccountOut.model_validate(account)


@router.post(
    "/accounts/{account_id}/revoke",
    response_model=SocialAccountOut,
    summary="Revoke an account link (clears any token reference)",
)
async def revoke_account(
    account_id: uuid.UUID, session: DB, user: EditorUser
) -> SocialAccountOut:
    account = await SocialAccountService(session).revoke(account_id)
    await session.commit()
    await session.refresh(account)
    return SocialAccountOut.model_validate(account)


@router.get(
    "/scheduled", response_model=list[ScheduledPostOut], summary="Scheduled posts"
)
async def list_scheduled(session: DB, user: CurrentUser) -> list[ScheduledPostOut]:
    rows = (
        await session.execute(
            select(ScheduledPost).order_by(ScheduledPost.scheduled_for.asc())
        )
    ).scalars().all()
    return [ScheduledPostOut.model_validate(r) for r in rows]


@router.get(
    "/published", response_model=list[PublishedPostOut], summary="Published posts"
)
async def list_published(session: DB, user: CurrentUser) -> list[PublishedPostOut]:
    rows = (
        await session.execute(
            select(PublishedPost).order_by(PublishedPost.published_at.desc().nullslast())
        )
    ).scalars().all()
    return [PublishedPostOut.model_validate(r) for r in rows]
