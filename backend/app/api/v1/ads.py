"""Paid advertising API.

Every mutating route is founder-only except ``POST /ads/proposals``, which only
creates a local draft and cannot spend anything. The AI may propose; only a human
may authorise; and the execution route refuses without a matching, unexpired,
single-use authorisation.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status
from sqlalchemy import select

from app.api.deps import DB, CurrentUser, EditorUser, FounderUser, SettingsDep
from app.integrations.ads import ads_transport_report, get_ads_transport
from app.models.ads import AdBudgetAuthorization
from app.schemas.ads import (
    AdAccountValidationOut,
    AdAuthorizeIn,
    AdPreviewOut,
    AdReportOut,
    AdAuthorizationOut,
    AdCampaignDetailOut,
    AdCampaignOut,
    AdExecuteIn,
    AdOperationOut,
    AdProposalIn,
    AdTransportOut,
)
from app.services.ads import AdsService

router = APIRouter(prefix="/ads", tags=["ads"])


@router.get(
    "/transports",
    response_model=list[AdTransportOut],
    summary="Which paid-advertising transports exist in this build",
)
async def transports(user: CurrentUser, settings: SettingsDep) -> list[AdTransportOut]:
    return [AdTransportOut(**row) for row in ads_transport_report(settings)]


@router.post(
    "/proposals",
    response_model=AdCampaignOut,
    status_code=status.HTTP_201_CREATED,
    summary="Draft a paid campaign proposal (cannot spend)",
)
async def create_proposal(
    payload: AdProposalIn, session: DB, settings: SettingsDep, user: EditorUser
) -> AdCampaignOut:
    service = AdsService(session, settings)
    campaign = await service.propose(
        brand_profile_id=payload.brand_profile_id,
        platform=payload.platform,
        name=payload.name,
        objective=payload.objective,
        plan=payload.plan,
        targeting=payload.targeting,
        creatives=payload.creatives,
        daily_budget_cents=payload.daily_budget_cents,
        total_budget_cents=payload.total_budget_cents,
        currency=payload.currency,
        campaign_id=payload.campaign_id,
        created_by_agent=payload.created_by_agent,
    )
    if payload.authorize:
        if user.role != "founder":
            # A non-founder asking for an authorised proposal gets the draft and
            # an explicit state of 'proposed'; nothing is approved implicitly.
            await session.commit()
            await session.refresh(campaign)
            return AdCampaignOut.model_validate(campaign)
        await service.authorize(
            campaign.id,
            actor_id=user.id,
            actor_role=user.role,
            max_spend_cents=payload.max_spend_cents or campaign.total_budget_cents,
            allowed_operations=payload.allowed_operations
            or ["create", "launch", "pause", "resume"],
            ttl_hours=payload.authorization_ttl_hours,
        )
    await session.commit()
    await session.refresh(campaign)
    return AdCampaignOut.model_validate(campaign)


@router.get("/campaigns", response_model=list[AdCampaignOut], summary="List ad proposals")
async def list_campaigns(
    brand_profile_id: uuid.UUID, session: DB, settings: SettingsDep, user: CurrentUser
) -> list[AdCampaignOut]:
    rows = await AdsService(session, settings).list_campaigns(brand_profile_id)
    return [AdCampaignOut.model_validate(row) for row in rows]


@router.get(
    "/campaigns/{campaign_id}",
    response_model=AdCampaignDetailOut,
    summary="Proposal, authorisations, ledger and transport status",
)
async def campaign_detail(
    campaign_id: uuid.UUID, session: DB, settings: SettingsDep, user: CurrentUser
) -> AdCampaignDetailOut:
    service = AdsService(session, settings)
    campaign = await service.get(campaign_id)
    operations = await service.operations(campaign_id)
    authorizations = list(
        (
            await session.execute(
                select(AdBudgetAuthorization)
                .where(AdBudgetAuthorization.ad_campaign_id == campaign_id)
                .order_by(AdBudgetAuthorization.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return AdCampaignDetailOut(
        campaign=AdCampaignOut.model_validate(campaign),
        operations=[AdOperationOut.model_validate(o) for o in operations],
        authorizations=[AdAuthorizationOut.model_validate(a) for a in authorizations],
        transport=AdTransportOut(
            **get_ads_transport(campaign.platform, settings).describe()
        ),
    )


@router.post(
    "/campaigns/{campaign_id}/preview",
    response_model=AdPreviewOut,
    summary="Dry run: the exact requests that would be sent, without sending them",
)
async def preview(
    campaign_id: uuid.UUID,
    session: DB,
    settings: SettingsDep,
    user: CurrentUser,
    kind: str = "create",
) -> AdPreviewOut:
    result = await AdsService(session, settings).preview(campaign_id, kind=kind)
    return AdPreviewOut(**result)


@router.post(
    "/campaigns/{campaign_id}/validate-account",
    response_model=AdAccountValidationOut,
    summary="Read-only check that the credential reaches a usable ad account",
)
async def validate_account(
    campaign_id: uuid.UUID, session: DB, settings: SettingsDep, user: FounderUser
) -> AdAccountValidationOut:
    result = await AdsService(session, settings).validate_account(campaign_id)
    return AdAccountValidationOut(**result)


@router.get(
    "/campaigns/{campaign_id}/report",
    response_model=AdReportOut,
    summary="Campaign performance as measured by the platform",
)
async def report(
    campaign_id: uuid.UUID, session: DB, settings: SettingsDep, user: CurrentUser
) -> AdReportOut:
    result = await AdsService(session, settings).report(campaign_id)
    await session.commit()
    return AdReportOut(**result)


@router.post(
    "/campaigns/{campaign_id}/authorize",
    response_model=AdAuthorizationOut,
    summary="Authorise a spend ceiling for this exact proposal (founder only)",
)
async def authorize(
    campaign_id: uuid.UUID,
    payload: AdAuthorizeIn,
    session: DB,
    settings: SettingsDep,
    user: FounderUser,
) -> AdAuthorizationOut:
    authorization = await AdsService(session, settings).authorize(
        campaign_id,
        actor_id=user.id,
        actor_role=user.role,
        max_spend_cents=payload.max_spend_cents,
        allowed_operations=payload.allowed_operations,
        ttl_hours=payload.ttl_hours,
        note=payload.note,
    )
    await session.commit()
    await session.refresh(authorization)
    return AdAuthorizationOut.model_validate(authorization)


@router.post(
    "/campaigns/{campaign_id}/reject",
    response_model=AdCampaignOut,
    summary="Reject a proposal (founder only)",
)
async def reject(
    campaign_id: uuid.UUID, session: DB, settings: SettingsDep, user: FounderUser
) -> AdCampaignOut:
    campaign = await AdsService(session, settings).reject(
        campaign_id, actor_id=user.id, actor_role=user.role
    )
    await session.commit()
    await session.refresh(campaign)
    return AdCampaignOut.model_validate(campaign)


@router.post(
    "/campaigns/{campaign_id}/operations",
    response_model=AdOperationOut,
    summary="Attempt an external ad mutation under a founder authorisation",
)
async def execute(
    campaign_id: uuid.UUID,
    payload: AdExecuteIn,
    session: DB,
    settings: SettingsDep,
    user: FounderUser,
) -> AdOperationOut:
    operation = await AdsService(session, settings).execute(
        campaign_id,
        kind=payload.kind,
        actor_id=user.id,
        actor_role=user.role,
        authorization_id=payload.authorization_id,
        params=payload.params,
    )
    await session.commit()
    await session.refresh(operation)
    return AdOperationOut.model_validate(operation)
