from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_audit
from app.core.errors import NotFoundError, ValidationProblem
from app.models.campaign import Campaign, CampaignStatus
from app.models.content import ContentItem
from app.models.social import Platform


class CampaignService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, campaign_id: uuid.UUID) -> Campaign:
        campaign = await self.session.get(Campaign, campaign_id)
        if campaign is None:
            raise NotFoundError(f"Campaign {campaign_id} not found.")
        return campaign

    async def find_by_name(
        self, *, brand_profile_id: uuid.UUID, name: str
    ) -> Campaign | None:
        """Look up a campaign by its unique (brand, name) pair."""
        result = await self.session.execute(
            select(Campaign).where(
                Campaign.brand_profile_id == brand_profile_id, Campaign.name == name
            )
        )
        return result.scalars().first()

    async def list(
        self,
        *,
        brand_profile_id: uuid.UUID | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Campaign], int]:
        conds = []
        if brand_profile_id:
            conds.append(Campaign.brand_profile_id == brand_profile_id)
        if status:
            conds.append(Campaign.status == status)
        stmt = select(Campaign)
        count_stmt = select(func.count()).select_from(Campaign)
        for c in conds:
            stmt = stmt.where(c)
            count_stmt = count_stmt.where(c)
        stmt = stmt.order_by(Campaign.created_at.desc()).limit(limit).offset(offset)
        rows = list((await self.session.execute(stmt)).scalars().all())
        total = int((await self.session.execute(count_stmt)).scalar_one())
        return rows, total

    async def create(
        self,
        *,
        brand_profile_id: uuid.UUID,
        name: str,
        objective: str = "",
        brief: str = "",
        platforms: list[str] | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        plan: dict[str, Any] | None = None,
        created_by_agent: str | None = None,
        actor_id: uuid.UUID | None = None,
        status: str = CampaignStatus.DRAFT.value,
    ) -> Campaign:
        for platform in platforms or []:
            if platform not in Platform.values():
                raise ValidationProblem(f"Unsupported platform '{platform}'.")
        existing = await self.session.execute(
            select(Campaign.id).where(
                Campaign.brand_profile_id == brand_profile_id, Campaign.name == name
            )
        )
        if existing.first() is not None:
            raise ValidationProblem(
                "A campaign with this name already exists for the brand.",
                code="duplicate_campaign",
            )
        campaign = Campaign(
            brand_profile_id=brand_profile_id,
            name=name,
            objective=objective,
            brief=brief,
            platforms=platforms or [],
            start_date=start_date,
            end_date=end_date,
            plan=plan or {},
            created_by_agent=created_by_agent,
            status=status,
        )
        self.session.add(campaign)
        await self.session.flush()
        await record_audit(
            self.session,
            action="campaign.created",
            entity_type="campaign",
            entity_id=campaign.id,
            actor_id=actor_id,
            after={"name": name, "status": campaign.status},
        )
        return campaign

    async def update_status(
        self, campaign_id: uuid.UUID, status: str, *, actor_id: uuid.UUID | None
    ) -> Campaign:
        if status not in CampaignStatus.values():
            raise ValidationProblem(f"Invalid campaign status '{status}'.")
        campaign = await self.get(campaign_id)
        before = campaign.status
        campaign.status = status
        self.session.add(campaign)
        await self.session.flush()
        await record_audit(
            self.session,
            action="campaign.status_changed",
            entity_type="campaign",
            entity_id=campaign.id,
            actor_id=actor_id,
            before={"status": before},
            after={"status": status},
        )
        return campaign

    async def content_counts(self, campaign_id: uuid.UUID) -> dict[str, int]:
        stmt = (
            select(ContentItem.status, func.count())
            .where(ContentItem.campaign_id == campaign_id)
            .group_by(ContentItem.status)
        )
        rows = (await self.session.execute(stmt)).all()
        return {status: int(count) for status, count in rows}
