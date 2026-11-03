"""Creative brief creation and versioning (Phase 3B).

The Creative Director agent produces the content; this service persists it, binds
it to one exact content version and hands out immutable versions. Generating a
brief is strictly read-only with respect to the content it describes — the content
version id *and* its hash are recorded so that claim is checkable after the fact.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import AgentContext
from app.agents.creative import CreativeAgent, CreativeInput
from app.core.audit import record_audit
from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError, ValidationProblem
from app.models.brand import BrandProfile
from app.models.campaign import Campaign
from app.models.content import ContentItem, ContentVersion
from app.models.creative_brief import CreativeBrief
from app.services.brand import BrandService
from app.services.content import ContentService


def _hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CreativeBriefService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.content = ContentService(session)

    # ------------------------------------------------------------------ reads
    async def get(self, brief_id: uuid.UUID) -> CreativeBrief:
        brief = await self.session.get(CreativeBrief, brief_id)
        if brief is None:
            raise NotFoundError(f"Creative brief {brief_id} not found.")
        return brief

    async def list(
        self,
        *,
        brand_profile_id: uuid.UUID | None = None,
        campaign_id: uuid.UUID | None = None,
        content_item_id: uuid.UUID | None = None,
        limit: int = 100,
    ) -> list[CreativeBrief]:
        stmt = select(CreativeBrief)
        if brand_profile_id:
            stmt = stmt.where(CreativeBrief.brand_profile_id == brand_profile_id)
        if campaign_id:
            stmt = stmt.where(CreativeBrief.campaign_id == campaign_id)
        if content_item_id:
            stmt = stmt.where(CreativeBrief.content_item_id == content_item_id)
        stmt = stmt.order_by(CreativeBrief.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def latest_for_version(
        self, version_id: uuid.UUID, asset_type: str | None = None
    ) -> CreativeBrief | None:
        stmt = select(CreativeBrief).where(CreativeBrief.content_version_id == version_id)
        if asset_type:
            stmt = stmt.where(CreativeBrief.asset_type == asset_type)
        stmt = stmt.order_by(CreativeBrief.version_number.desc()).limit(1)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # ----------------------------------------------------------------- create
    async def create(
        self,
        *,
        content_version_id: uuid.UUID,
        asset_type: str = "image",
        actor_id: uuid.UUID | None = None,
        creative_direction: str = "",
        preferred_aspect_ratio: str = "",
        duration_seconds: int = 0,
        strategy_reference: str = "",
        force_new_version: bool = False,
    ) -> tuple[CreativeBrief, bool]:
        """Create (or reuse) a brief for an exact content version.

        Returns ``(brief, created)``. An identical request for the same version and
        asset type returns the existing brief rather than piling up duplicates.
        """
        if asset_type not in ("image", "video"):
            raise ValidationProblem(
                "Phase 3B generates images and video only.", code="unsupported_asset_type"
            )
        version = await self.session.get(ContentVersion, content_version_id)
        if version is None:
            raise NotFoundError(f"Content version {content_version_id} not found.")
        item = await self.session.get(ContentItem, version.content_item_id)
        if item is None:
            raise NotFoundError("The content item for that version no longer exists.")

        brand = await self._brand_for(item)
        campaign = (
            await self.session.get(Campaign, item.campaign_id) if item.campaign_id else None
        )

        request = {
            "content_version_id": str(version.id),
            "content_hash": version.content_hash,
            "asset_type": asset_type,
            "creative_direction": creative_direction,
            "preferred_aspect_ratio": preferred_aspect_ratio,
            "duration_seconds": duration_seconds,
            "strategy_reference": strategy_reference,
            "platform": item.platform,
        }
        request_hash = _hash(request)

        if not force_new_version:
            existing = await self._find_identical(
                version.id, asset_type, request_hash
            )
            if existing is not None:
                return existing, False

        agent = CreativeAgent(self.settings)
        ctx = AgentContext(
            session=self.session,
            brand=brand,
            gateway=None,
            actor_id=actor_id,
            extra={"surface": "creative_director"},
        )
        # Read-only in both directions: the agent receives the content text, and the
        # service writes nothing to the content item or version.
        payload = CreativeInput(
            platform=item.platform,
            format=item.format or asset_type,
            concept=item.title,
            headline=version.headline,
            aspect_ratio=version_headline_aspect(item.platform),
            request_director_brief=True,
            campaign_id=item.campaign_id,
            campaign_objective=(campaign.objective if campaign else ""),
            content_item_id=item.id,
            content_version_id=version.id,
            content_hash=version.content_hash,
            content_body=version.body,
            content_cta=version.call_to_action,
            content_hashtags=list(version.hashtags or []),
            audience=", ".join((brand.audience or [])[:3]) if brand else "",
            asset_type=asset_type,
            preferred_aspect_ratio=preferred_aspect_ratio,
            creative_direction=creative_direction,
            strategy_reference=strategy_reference,
            duration_seconds=duration_seconds,
            brand_facts=_brand_facts(brand),
            prohibited_claims=list(getattr(brand, "prohibited_phrases", []) or [])
            if brand
            else [],
        )
        output = await agent.execute(ctx, payload)
        brief_data = output.director_brief
        if brief_data is None:  # pragma: no cover - defensive
            raise ValidationProblem(
                "The Creative Director did not return a brief.", code="brief_generation_failed"
            )

        next_version = await self._next_version_number(version.id, asset_type)
        brief_hash = _hash(
            {
                "request": request,
                "prompt": brief_data.image_prompt,
                "concept": brief_data.visual_concept,
                "negative": brief_data.negative_prompt,
                "aspect_ratio": brief_data.aspect_ratio,
                "asset_type": asset_type,
            }
        )
        brief = CreativeBrief(
            brand_profile_id=brand.id if brand else _require_brand(item),
            campaign_id=item.campaign_id,
            content_item_id=item.id,
            content_version_id=version.id,
            content_hash=version.content_hash,
            strategy_version_id=None,
            version_number=next_version,
            brief_hash=brief_hash,
            platform=item.platform,
            asset_type=asset_type,
            creative_objective=brief_data.creative_objective,
            visual_concept=brief_data.visual_concept,
            scenes=list(brief_data.scenes),
            subjects=list(brief_data.subjects),
            background=brief_data.background,
            lighting=brief_data.lighting,
            composition=list(brief_data.composition),
            colour_palette=dict(brief_data.colour_palette),
            typography=list(brief_data.typography),
            brand_asset_requirements=list(brief_data.brand_asset_requirements),
            image_prompt=brief_data.image_prompt,
            negative_prompt=list(brief_data.negative_prompt),
            aspect_ratio=brief_data.aspect_ratio,
            resolution=brief_data.resolution,
            duration_seconds=(
                float(brief_data.duration_seconds) if brief_data.duration_seconds else None
            ),
            reference_assets=list(brief_data.reference_assets),
            validation_constraints=dict(brief_data.validation_constraints),
            generation_mode=brief_data.generation_mode,
            created_by_agent="creative",
            created_by=actor_id,
            generation_metadata={
                "request_hash": request_hash,
                "provider": (getattr(self.settings, "creative_provider", "mock") or "mock"),
                "agent_summary": output.summary,
                "content_modified": False,
            },
        )
        self.session.add(brief)
        await self.session.flush()

        await record_audit(
            self.session,
            action="creative.brief_created",
            entity_type="creative_brief",
            entity_id=brief.id,
            actor_id=actor_id,
            after={
                "version_number": brief.version_number,
                "asset_type": brief.asset_type,
                "content_version_id": str(version.id),
                "content_hash": version.content_hash,
            },
        )
        return brief, True

    # -------------------------------------------------------------- internals
    async def _find_identical(
        self, version_id: uuid.UUID, asset_type: str, request_hash: str
    ) -> CreativeBrief | None:
        stmt = (
            select(CreativeBrief)
            .where(
                CreativeBrief.content_version_id == version_id,
                CreativeBrief.asset_type == asset_type,
            )
            .order_by(CreativeBrief.version_number.desc())
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        for row in rows:
            if (row.generation_metadata or {}).get("request_hash") == request_hash:
                return row
        return None

    async def _next_version_number(self, version_id: uuid.UUID, asset_type: str) -> int:
        current = (
            await self.session.execute(
                select(func.max(CreativeBrief.version_number)).where(
                    CreativeBrief.content_version_id == version_id,
                    CreativeBrief.asset_type == asset_type,
                )
            )
        ).scalar()
        return int(current or 0) + 1

    async def _brand_for(self, item: ContentItem) -> BrandProfile | None:
        if item.brand_profile_id:
            brand = await self.session.get(BrandProfile, item.brand_profile_id)
            if brand is not None:
                return brand
        return await BrandService(self.session).active()


def _require_brand(item: ContentItem) -> uuid.UUID:
    if item.brand_profile_id is None:
        raise ValidationProblem(
            "The content item has no brand profile, so no visual direction exists.",
            code="brand_required",
        )
    return item.brand_profile_id


def version_headline_aspect(platform: str) -> str:
    from app.agents.creative import PLATFORM_ASPECT

    return PLATFORM_ASPECT.get((platform or "").lower(), "4:5")


def _brand_facts(brand: BrandProfile | None) -> list[str]:
    if brand is None:
        return []
    facts = []
    if brand.product:
        facts.append(f"Product: {brand.product}")
    if brand.company:
        facts.append(f"Company: {brand.company}")
    if brand.tagline:
        facts.append(f"Tagline: {brand.tagline}")
    return facts
