from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.api.deps import DB, CurrentUser, EditorUser, Pagination
from app.schemas.campaign import CampaignCreate, CampaignDetailOut, CampaignOut, CampaignUpdate
from app.services.campaign import CampaignService

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


@router.post(
    "", response_model=CampaignOut, status_code=status.HTTP_201_CREATED,
    summary="Create a campaign",
)
async def create_campaign(
    payload: CampaignCreate, session: DB, user: EditorUser
) -> CampaignOut:
    campaign = await CampaignService(session).create(
        **payload.model_dump(), actor_id=user.id
    )
    await session.commit()
    await session.refresh(campaign)
    return CampaignOut.model_validate(campaign)


@router.get("", response_model=list[CampaignOut], summary="List campaigns")
async def list_campaigns(
    session: DB,
    user: CurrentUser,
    page: Pagination,
    brand_profile_id: uuid.UUID | None = None,
    status_filter: str | None = None,
) -> list[CampaignOut]:
    rows, _total = await CampaignService(session).list(
        brand_profile_id=brand_profile_id,
        status=status_filter,
        limit=page["limit"],
        offset=page["offset"],
    )
    return [CampaignOut.model_validate(c) for c in rows]


@router.get("/{campaign_id}", response_model=CampaignDetailOut, summary="Campaign detail")
async def get_campaign(
    campaign_id: uuid.UUID, session: DB, user: CurrentUser
) -> CampaignDetailOut:
    service = CampaignService(session)
    campaign = await service.get(campaign_id)
    counts = await service.content_counts(campaign_id)
    detail = CampaignDetailOut.model_validate(campaign)
    detail.content_counts = counts
    detail.content_item_count = sum(counts.values())
    return detail


@router.patch("/{campaign_id}", response_model=CampaignOut, summary="Update a campaign")
async def update_campaign(
    campaign_id: uuid.UUID, payload: CampaignUpdate, session: DB, user: EditorUser
) -> CampaignOut:
    service = CampaignService(session)
    campaign = await service.get(campaign_id)
    data = payload.model_dump(exclude_unset=True)
    status_value = data.pop("status", None)
    for field, value in data.items():
        if value is not None and hasattr(campaign, field):
            setattr(campaign, field, value)
    session.add(campaign)
    await session.flush()
    if status_value:
        campaign = await service.update_status(campaign_id, status_value, actor_id=user.id)
    await session.commit()
    await session.refresh(campaign)
    return CampaignOut.model_validate(campaign)
