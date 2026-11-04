"""Brand knowledge base service.

Brand facts live in the database so marketing can be re-positioned without a code
change. The service refuses to silently drop history: each update bumps
``version`` and writes an audit row.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_audit
from app.core.errors import NotFoundError, ValidationProblem
from app.models.brand import BrandProfile, CompetitorProfile, ContentPillar, MarketingGoal
from app.models.social import Platform

PROHIBITED_DEFAULT = [
    "customer counts",
    "funding amounts",
    "revenue figures",
    "performance benchmarks",
    "testimonials",
    "pricing",
]


class BrandService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, brand_id: uuid.UUID) -> BrandProfile:
        brand = await self.session.get(BrandProfile, brand_id)
        if brand is None:
            raise NotFoundError(f"Brand profile {brand_id} not found.")
        return brand

    async def get_active(self) -> BrandProfile | None:
        stmt = (
            select(BrandProfile)
            .where(BrandProfile.is_active.is_(True))
            .order_by(BrandProfile.created_at.asc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def require_active(self) -> BrandProfile:
        brand = await self.get_active()
        if brand is None:
            raise NotFoundError(
                "No active brand profile. Create one or run `python -m app.seed.brand_crusor`."
            )
        return brand

    async def list(self) -> list[BrandProfile]:
        stmt = select(BrandProfile).order_by(BrandProfile.created_at.asc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def create(
        self,
        *,
        name: str,
        company: str,
        product: str,
        tagline: str = "",
        positioning: str = "",
        audience: list[str] | None = None,
        personality: list[str] | None = None,
        tone_of_voice: list[str] | None = None,
        visual_direction: dict[str, Any] | None = None,
        brand_values: list[str] | None = None,
        keywords: list[str] | None = None,
        do_not_claim: list[str] | None = None,
        prohibited_phrases: list[str] | None = None,
        compliance_notes: str = "",
        actor_id: uuid.UUID | None = None,
    ) -> BrandProfile:
        if not name.strip() or not company.strip() or not product.strip():
            raise ValidationProblem("name, company and product are required.")
        brand = BrandProfile(
            name=name,
            company=company,
            product=product,
            tagline=tagline,
            positioning=positioning,
            audience=audience or [],
            personality=personality or [],
            tone_of_voice=tone_of_voice or [],
            visual_direction=visual_direction or {},
            brand_values=brand_values or [],
            keywords=keywords or [],
            do_not_claim=do_not_claim if do_not_claim is not None else list(PROHIBITED_DEFAULT),
            prohibited_phrases=prohibited_phrases or [],
            compliance_notes=compliance_notes,
        )
        self.session.add(brand)
        await self.session.flush()
        await record_audit(
            self.session,
            action="brand.created",
            entity_type="brand_profile",
            entity_id=brand.id,
            actor_id=actor_id,
            after={"name": brand.name, "version": brand.version},
        )
        return brand

    async def update(
        self,
        brand_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None,
        changes: dict[str, Any],
    ) -> BrandProfile:
        brand = await self.get(brand_id)
        before = {k: getattr(brand, k) for k in changes if hasattr(brand, k)}
        immutable = {"id", "created_at", "updated_at", "version", "organization_id"}
        applied: dict[str, Any] = {}
        for field, value in changes.items():
            if field in immutable or not hasattr(brand, field):
                continue
            if value is None:
                continue
            setattr(brand, field, value)
            applied[field] = value
        if not applied:
            raise ValidationProblem("No updatable fields were supplied.")
        brand.version += 1
        self.session.add(brand)
        await self.session.flush()
        await record_audit(
            self.session,
            action="brand.updated",
            entity_type="brand_profile",
            entity_id=brand.id,
            actor_id=actor_id,
            before=before,
            after={**applied, "version": brand.version},
        )
        return brand

    # -------------------------------------------------------------- goals
    async def create_goal(
        self,
        brand_id: uuid.UUID,
        *,
        objective: str,
        kpi: str = "",
        target_value: float | None = None,
        horizon: str = "quarter",
        priority: int = 1,
        due_on=None,  # type: ignore[no-untyped-def]
        actor_id: uuid.UUID | None = None,
    ) -> MarketingGoal:
        goal = MarketingGoal(
            brand_profile_id=brand_id,
            objective=objective,
            kpi=kpi,
            target_value=target_value,
            horizon=horizon,
            priority=priority,
            due_on=due_on,
        )
        self.session.add(goal)
        await self.session.flush()
        await record_audit(
            self.session,
            action="marketing_goal.created",
            entity_type="marketing_goal",
            entity_id=goal.id,
            actor_id=actor_id,
            after={"objective": objective, "brand_profile_id": str(brand_id)},
        )
        return goal

    async def list_goals(self, brand_id: uuid.UUID) -> list[MarketingGoal]:
        stmt = (
            select(MarketingGoal)
            .where(MarketingGoal.brand_profile_id == brand_id)
            .order_by(MarketingGoal.priority.asc(), MarketingGoal.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    # -------------------------------------------------------- competitors
    async def create_competitor(
        self,
        brand_id: uuid.UUID,
        *,
        name: str,
        website: str | None = None,
        positioning: str = "",
        strengths: list[str] | None = None,
        weaknesses: list[str] | None = None,
        source: str = "manual",
        actor_id: uuid.UUID | None = None,
    ) -> CompetitorProfile:
        competitor = CompetitorProfile(
            brand_profile_id=brand_id,
            name=name,
            website=website,
            positioning=positioning,
            strengths=strengths or [],
            weaknesses=weaknesses or [],
            source=source,
        )
        self.session.add(competitor)
        await self.session.flush()
        return competitor

    async def list_competitors(self, brand_id: uuid.UUID) -> list[CompetitorProfile]:
        stmt = select(CompetitorProfile).where(CompetitorProfile.brand_profile_id == brand_id)
        return list((await self.session.execute(stmt)).scalars().all())

    # ------------------------------------------------------------ pillars
    async def create_pillar(
        self,
        brand_id: uuid.UUID,
        *,
        name: str,
        description: str = "",
        platforms: list[str] | None = None,
        weight: float = 1.0,
        actor_id: uuid.UUID | None = None,
    ) -> ContentPillar:
        for platform in platforms or []:
            if platform not in Platform.values():
                raise ValidationProblem(f"Unsupported platform '{platform}'.")
        pillar = ContentPillar(
            brand_profile_id=brand_id,
            name=name,
            description=description,
            platforms=platforms or [],
            weight=weight,
        )
        self.session.add(pillar)
        await self.session.flush()
        return pillar

    async def list_pillars(self, brand_id: uuid.UUID) -> list[ContentPillar]:
        stmt = (
            select(ContentPillar)
            .where(ContentPillar.brand_profile_id == brand_id)
            .order_by(ContentPillar.weight.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())
