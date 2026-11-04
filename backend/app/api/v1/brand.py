from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import DB, CurrentUser, EditorUser
from app.core.errors import NotFoundError
from app.schemas.brand import (
    BrandProfileCreate,
    BrandProfileOut,
    BrandProfileUpdate,
    CompetitorCreate,
    CompetitorOut,
    MarketingGoalCreate,
    MarketingGoalOut,
    PillarCreate,
    PillarOut,
)
from app.services.brand import BrandService

router = APIRouter(prefix="/brand", tags=["brand"])


@router.post(
    "",
    response_model=BrandProfileOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a brand profile",
)
async def create_brand(
    payload: BrandProfileCreate, session: DB, user: EditorUser
) -> BrandProfileOut:
    brand = await BrandService(session).create(
        **payload.model_dump(), actor_id=user.id
    )
    await session.commit()
    await session.refresh(brand)
    return BrandProfileOut.model_validate(brand)


@router.get("", response_model=list[BrandProfileOut], summary="List brand profiles")
async def list_brands(session: DB, user: CurrentUser) -> list[BrandProfileOut]:
    brands = await BrandService(session).list()
    return [BrandProfileOut.model_validate(b) for b in brands]


@router.get(
    "/active",
    response_model=BrandProfileOut,
    summary="The active brand profile",
    responses={404: {"description": "No active brand profile"}},
)
async def get_active_brand(session: DB, user: CurrentUser) -> BrandProfileOut:
    brand = await BrandService(session).get_active()
    if brand is None:
        raise NotFoundError(
            "No active brand profile yet.",
            details={"next_step": "POST /api/v1/brand or seed the Crusor profile"},
        )
    return BrandProfileOut.model_validate(brand)


@router.get("/{brand_id}", response_model=BrandProfileOut, summary="Get a brand profile")
async def get_brand(brand_id: uuid.UUID, session: DB, user: CurrentUser) -> BrandProfileOut:
    brand = await BrandService(session).get(brand_id)
    return BrandProfileOut.model_validate(brand)


@router.patch(
    "/{brand_id}",
    response_model=BrandProfileOut,
    summary="Update a brand profile (bumps version, writes audit)",
)
async def update_brand(
    brand_id: uuid.UUID,
    payload: BrandProfileUpdate,
    session: DB,
    user: EditorUser,
) -> BrandProfileOut:
    brand = await BrandService(session).update(
        brand_id, actor_id=user.id, changes=payload.model_dump(exclude_unset=True)
    )
    await session.commit()
    await session.refresh(brand)
    return BrandProfileOut.model_validate(brand)


# --------------------------------------------------------------------- goals
@router.post(
    "/{brand_id}/goals",
    response_model=MarketingGoalOut,
    status_code=201,
    summary="Create a marketing goal",
)
async def create_goal(
    brand_id: uuid.UUID, payload: MarketingGoalCreate, session: DB, user: EditorUser
) -> MarketingGoalOut:
    await BrandService(session).get(brand_id)
    goal = await BrandService(session).create_goal(
        brand_id, **payload.model_dump(), actor_id=user.id
    )
    await session.commit()
    await session.refresh(goal)
    return MarketingGoalOut.model_validate(goal)


@router.get(
    "/{brand_id}/goals",
    response_model=list[MarketingGoalOut],
    summary="List marketing goals",
)
async def list_goals(
    brand_id: uuid.UUID, session: DB, user: CurrentUser
) -> list[MarketingGoalOut]:
    goals = await BrandService(session).list_goals(brand_id)
    return [MarketingGoalOut.model_validate(g) for g in goals]


# --------------------------------------------------------------- competitors
@router.post(
    "/{brand_id}/competitors",
    response_model=CompetitorOut,
    status_code=201,
    summary="Create a competitor profile",
)
async def create_competitor(
    brand_id: uuid.UUID, payload: CompetitorCreate, session: DB, user: EditorUser
) -> CompetitorOut:
    competitor = await BrandService(session).create_competitor(
        brand_id, **payload.model_dump(), actor_id=user.id
    )
    await session.commit()
    await session.refresh(competitor)
    return CompetitorOut.model_validate(competitor)


@router.get(
    "/{brand_id}/competitors",
    response_model=list[CompetitorOut],
    summary="List competitor profiles",
)
async def list_competitors(
    brand_id: uuid.UUID, session: DB, user: CurrentUser
) -> list[CompetitorOut]:
    rows = await BrandService(session).list_competitors(brand_id)
    return [CompetitorOut.model_validate(c) for c in rows]


# ------------------------------------------------------------------- pillars
@router.post(
    "/{brand_id}/pillars",
    response_model=PillarOut,
    status_code=201,
    summary="Create a content pillar",
)
async def create_pillar(
    brand_id: uuid.UUID, payload: PillarCreate, session: DB, user: EditorUser
) -> PillarOut:
    pillar = await BrandService(session).create_pillar(
        brand_id, **payload.model_dump(), actor_id=user.id
    )
    await session.commit()
    await session.refresh(pillar)
    return PillarOut.model_validate(pillar)


@router.get(
    "/{brand_id}/pillars",
    response_model=list[PillarOut],
    summary="List content pillars",
)
async def list_pillars(
    brand_id: uuid.UUID, session: DB, user: CurrentUser
) -> list[PillarOut]:
    rows = await BrandService(session).list_pillars(brand_id)
    return [PillarOut.model_validate(p) for p in rows]
