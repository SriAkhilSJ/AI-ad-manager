from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, status
from fastapi.responses import JSONResponse

from app.api.deps import DB, CurrentUser, EditorUser, FounderUser, SettingsDep
from app.integrations.platforms import CAPABILITIES, capabilities_for
from app.models.social import PublishedPost, ScheduledPost, SocialAccountStatus
from app.publishing.adapters import get_adapter_registry
from app.schemas.social import (
    AnalyticsIngestOut,
    AnalyticsIngestRequest,
    OAuthCallbackIn,
    OAuthConnectionOut,
    OAuthStartOut,
    PlatformStatusOut,
    PublishedPostOut,
    ScheduledPostOut,
    SocialAccountCreate,
    SocialAccountOut,
)
from app.services.analytics_ingest import AnalyticsIngestService
from app.services.oauth import OAuthService
from app.services.social import SocialAccountService
from sqlalchemy import select

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


# --------------------------------------------------------------------- OAuth
@router.get(
    "/capabilities",
    summary="Documented support, implementation state and verification state per platform",
)
async def capability_matrix(user: CurrentUser) -> dict[str, object]:
    """Three separate questions per capability.

    ``documented`` — what the platform's API reference says it can do.
    ``implemented`` — whether this build implements it.
    ``verification`` — whether it has ever been exercised against a real account.
    On a fresh installation every entry is ``unverified``, because it has not.
    """
    return {
        "platforms": CAPABILITIES,
        "note": (
            "Documented support comes from each provider's public API reference. "
            "Verification is only set to 'sandbox' or 'production' after a real call."
        ),
    }


@router.post(
    "/oauth/{platform}/start",
    response_model=OAuthStartOut,
    summary="Begin an OAuth connection (founder only)",
)
async def oauth_start(
    platform: str,
    brand_profile_id: uuid.UUID,
    session: DB,
    settings: SettingsDep,
    user: FounderUser,
) -> OAuthStartOut:
    result = await OAuthService(session, settings).start(
        platform=platform, brand_profile_id=brand_profile_id, actor_id=user.id
    )
    await session.commit()
    return OAuthStartOut(**result)


@router.get(
    "/oauth/callback",
    response_model=OAuthConnectionOut,
    summary="OAuth redirect target (authenticated by signed, single-use state)",
)
async def oauth_callback(
    session: DB,
    settings: SettingsDep,
    state: str = Query(min_length=8),
    code: str = Query(min_length=1),
    brand_profile_id: uuid.UUID = Query(...),
) -> OAuthConnectionOut:
    """The provider redirects here, so there is no bearer token in the request.

    Authorisation comes from the signed state instead: HMAC over a payload that
    names the brand and platform, an expiry, and a single-use row. A replayed or
    forged callback is rejected before any code is exchanged.
    """
    result = await OAuthService(session, settings).callback(
        state=state, code=code, brand_profile_id=brand_profile_id
    )
    await session.commit()
    return OAuthConnectionOut(**result)


@router.post(
    "/accounts/{account_id}/refresh",
    response_model=OAuthConnectionOut,
    summary="Renew an account credential (founder only)",
)
async def refresh_account(
    account_id: uuid.UUID, session: DB, settings: SettingsDep, user: FounderUser
) -> OAuthConnectionOut:
    result = await OAuthService(session, settings).refresh(account_id)
    await session.commit()
    return OAuthConnectionOut(**result)


# ------------------------------------------------------------------ analytics
@router.post(
    "/scheduled/run",
    summary="Publish every due scheduled post now (founder only)",
)
async def run_due_posts(
    session: DB, settings: SettingsDep, user: FounderUser, limit: int = Query(default=50, ge=1, le=200)
) -> dict[str, object]:
    """Executes the scheduler immediately.

    Each publish goes through the same approval and transport gates as a manual
    one, so a schedule whose approval was invalidated by an edit fails here rather
    than shipping an unapproved version.
    """
    from app.services.scheduler import SchedulerService

    result = await SchedulerService(session, settings).run_due(limit=limit)
    await session.commit()
    return result


@router.post(
    "/analytics/ingest",
    response_model=AnalyticsIngestOut,
    summary="Pull measured post metrics from platform APIs",
)
async def ingest_analytics(
    payload: AnalyticsIngestRequest,
    session: DB,
    settings: SettingsDep,
    user: EditorUser,
) -> AnalyticsIngestOut:
    result = await AnalyticsIngestService(session, settings).ingest(
        brand_profile_id=payload.brand_profile_id,
        platforms=payload.platforms or None,
        days=payload.days,
    )
    await session.commit()
    return AnalyticsIngestOut(**result)
