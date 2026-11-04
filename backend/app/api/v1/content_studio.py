"""Phase 3A — Content Studio API.

Route groups
------------
``/content-studio/platforms``   platform specifications (read-only).
``/content-studio/overview``    the Studio dashboard counts.
``/content-studio/generate``    one-off draft generation (idempotent).
``/content-studio/plans``       weekly plans: create, list, detail, generate.
``/content-studio/items``       per-item variants and validation.
``/content-studio/calendar``    the *internal* planning calendar.

Everything here is additive. Content creation, editing, version history and
approval stay on the Phase 1 routes in ``app/api/v1/content.py``; this module links
to them rather than duplicating them. There is deliberately no publish or external
schedule endpoint: Phase 3A cannot publish, and the existing publish route keeps its
own approval + social-account gates.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import func, select

from app.agents.base import AgentContext
from app.agents.brand_guardian import BrandGuardianAgent, BrandGuardianInput
from app.agents.content import ContentAgent, ContentInput
from app.agents.limits import ExecutionGuard, ExecutionLimits
from app.agents.registry import get_registry
from app.api.deps import DB, CurrentUser, EditorUser, FounderUser, IdempotencyKeyDep
from app.content.platforms import CONTENT_TYPES, get_spec, spec_dict
from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationProblem
from app.core.idempotency import with_idempotency
from app.llm.gateway import LLMGateway
from app.models.brand import BrandProfile
from app.models.campaign import Campaign
from app.models.content import ContentItem, ContentStatus, ContentVersion
from app.models.content_plan import (
    ContentPlan,
    ContentPlanSlot,
    ContentValidation,
    SlotStatus,
    ValidationVerdict,
)
from app.models.social import Platform
from app.schemas.content_studio import (
    CalendarEntryOut,
    CalendarMoveIn,
    CalendarViewOut,
    GeneratedItemOut,
    PlanConfigIn,
    PlanCreateIn,
    PlanDetailOut,
    PlanOut,
    PlanSlotOut,
    PlatformSpecOut,
    SingleGenerateIn,
    StudioOverviewOut,
    ValidationOut,
    VariantRegenerateIn,
    VariantSelectIn,
)
from app.services.approval import ApprovalService
from app.services.brand import BrandService
from app.services.content import ContentService
from app.services.content_calendar import ContentCalendarService, week_bounds
from app.services.content_planning import (
    ContentPlanningService,
    ContentValidationService,
    default_week_start,
)
from app.services.editor import ContentEditor

router = APIRouter(prefix="/content-studio", tags=["content-studio"])


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
async def _brand(session, user) -> BrandProfile:  # type: ignore[no-untyped-def]
    brand = await BrandService(session).get_active()
    if brand is None:
        raise NotFoundError("No active brand profile.")
    return brand


async def _campaign(session, campaign_id: uuid.UUID | None, brand: BrandProfile) -> Campaign | None:  # type: ignore[no-untyped-def]
    if campaign_id is None:
        return None
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None or campaign.brand_profile_id != brand.id:
        raise NotFoundError("Campaign not found.")
    return campaign


def _slot_out(slot: ContentPlanSlot) -> PlanSlotOut:
    meta = slot.generation_metadata or {}
    return PlanSlotOut(
        id=slot.id,
        position=slot.position,
        platform=slot.platform,
        content_type=slot.content_type,
        scheduled_date=slot.scheduled_date,
        scheduled_time=slot.scheduled_time,
        audience=slot.audience,
        pillar=slot.pillar,
        objective=slot.objective,
        primary_message=slot.primary_message,
        rationale=slot.rationale,
        status=slot.status,
        content_item_id=slot.content_item_id,
        variant_count=len(meta.get("variants") or []),
        selected_variant=meta.get("selected_variant"),
        validation_status=(meta.get("validation") or {}).get("status")
        or meta.get("validation_status"),
        is_mock=meta.get("is_mock"),
    )


def _plan_detail(plan: ContentPlan) -> PlanDetailOut:
    base = PlanOut.model_validate(plan)
    return PlanDetailOut(
        **base.model_dump(),
        slots=[_slot_out(slot) for slot in sorted(plan.slots, key=lambda s: s.position)],
    )


# --------------------------------------------------------------------------- #
# platform specs
# --------------------------------------------------------------------------- #
@router.get(
    "/platforms",
    response_model=list[PlatformSpecOut],
    summary="Platform content specifications (lengths, formats, guidance)",
)
async def list_platforms(user: CurrentUser) -> list[PlatformSpecOut]:
    return [PlatformSpecOut(**spec) for spec in spec_dict()]


@router.get(
    "/capabilities",
    summary="What the content system can and cannot do right now",
)
async def capabilities(user: CurrentUser) -> dict[str, Any]:
    settings = get_settings()
    return {
        "content_types": list(CONTENT_TYPES),
        "platforms": Platform.values(),
        "content_provider": settings.content_provider,
        "is_mock_provider": settings.content_provider == "mock",
        "live_model_calls_enabled": settings.content_provider != "mock",
        "external_publishing": False,
        "external_scheduling": False,
        "video_rendering": False,
        "notes": [
            "Phase 3A produces text drafts only; there is no rendering or publishing.",
            "The internal calendar plans dates; it does not schedule on any platform.",
        ],
    }


# --------------------------------------------------------------------------- #
# overview
# --------------------------------------------------------------------------- #
@router.get("/overview", response_model=StudioOverviewOut, summary="Studio dashboard")
async def overview(session: DB, user: CurrentUser) -> StudioOverviewOut:
    brand = await _brand(session, user)
    settings = get_settings()

    items = list(
        (
            await session.execute(
                select(ContentItem).where(ContentItem.brand_profile_id == brand.id)
            )
        )
        .scalars()
        .all()
    )
    by_platform: dict[str, int] = {}
    by_campaign: dict[str, int] = {}
    for item in items:
        by_platform[item.platform] = by_platform.get(item.platform, 0) + 1
        if item.campaign_id:
            by_campaign[str(item.campaign_id)] = by_campaign.get(str(item.campaign_id), 0) + 1

    # The latest verdict per item, computed with one grouped query.
    verdicts = dict(
        (
            await session.execute(
                select(ContentValidation.content_item_id, ContentValidation.verdict)
                .where(ContentValidation.content_item_id.in_([i.id for i in items] or [None]))
                .order_by(ContentValidation.created_at)
            )
        ).all()
    )
    by_validation: dict[str, int] = {}
    for verdict in verdicts.values():
        by_validation[verdict] = by_validation.get(verdict, 0) + 1

    return StudioOverviewOut(
        total_drafts=len(items),
        pending_review=sum(1 for i in items if i.status == ContentStatus.IN_REVIEW.value),
        approved=sum(1 for i in items if i.status == ContentStatus.APPROVED.value),
        scheduled_internally=sum(1 for i in items if i.status == ContentStatus.SCHEDULED.value),
        blocked=by_validation.get(ValidationVerdict.BLOCKED.value, 0)
        + by_validation.get(ValidationVerdict.NEEDS_REVISION.value, 0),
        by_platform=by_platform,
        by_campaign=by_campaign,
        by_validation_status=by_validation,
        content_provider=settings.content_provider,
        is_mock_provider=settings.content_provider == "mock",
        live_model_calls_enabled=settings.content_provider != "mock",
    )


# --------------------------------------------------------------------------- #
# single-slot generation
# --------------------------------------------------------------------------- #
@router.post(
    "/generate",
    response_model=GeneratedItemOut,
    status_code=status.HTTP_201_CREATED,
    summary="Generate one draft (idempotent)",
)
async def generate_one(
    payload: SingleGenerateIn,
    session: DB,
    user: EditorUser,
    response: Response,
    key: IdempotencyKeyDep,
) -> GeneratedItemOut:
    brand = await _brand(session, user)
    campaign = await _campaign(session, payload.campaign_id, brand)
    settings = get_settings()
    if settings.content_provider == "disabled":
        raise ValidationProblem("Content generation is disabled.", code="content_disabled")
    try:
        spec = get_spec(payload.platform)
    except Exception as exc:  # noqa: BLE001 - translated to a 422
        raise ValidationProblem(str(exc)) from exc
    if not spec.supports(payload.content_type):
        raise ValidationProblem(
            f"{spec.label} does not support content type '{payload.content_type}'.",
            details={"supported": list(spec.supported_types)},
        )

    async def operation() -> dict[str, Any]:
        gateway = LLMGateway(session=session)
        guard = ExecutionGuard(limits=ExecutionLimits.for_content(settings))
        ctx = AgentContext(
            session=session,
            brand=brand,
            gateway=gateway,
            actor_id=user.id,
            extra={"budget_guard": guard},
        )
        content_agent = get_registry().get("content")
        guardian = get_registry().get("brand_guardian")

        payloads = {
            "platform": payload.platform,
            "content_type": payload.content_type,
            "campaign_id": campaign.id if campaign else None,
            "brand_profile_id": brand.id,
            "audience": payload.audience,
            "pillar": payload.pillar,
            "objective": payload.objective or (campaign.objective if campaign else ""),
            "primary_message": payload.primary_message,
            "suggested_date": (payload.planned_date or date.today()).isoformat(),
            "suggested_time": payload.planned_time,
            "variant_count": payload.variants,
        }

        variants: list[dict[str, Any]] = []
        for index in range(payload.variants):
            draft = await content_agent.run(
                ctx, ContentInput(**payloads, variant_index=index, variant_seed=index)
            )
            validation = await guardian.run(
                ctx,
                BrandGuardianInput(
                    platform=payload.platform,
                    content_type=payload.content_type,
                    title=draft.headline,
                    hook=draft.hook,
                    body=draft.body,
                    hashtags=draft.hashtags,
                    call_to_action=draft.call_to_action,
                    outline=draft.outline,
                    audience=payload.audience,
                    objective=payloads["objective"],
                    pillar=payload.pillar,
                ),
            )
            variants.append(
                {
                    "index": index,
                    "headline": draft.headline,
                    "hook": draft.hook,
                    "body": draft.body,
                    "hashtags": list(draft.hashtags),
                    "call_to_action": draft.call_to_action,
                    "outline": list(draft.outline),
                    "character_count": len(draft.body),
                    "provider": draft.provider,
                    "model": draft.model,
                    "is_mock": draft.is_mock,
                    "generation_mode": draft.generation_mode,
                    "score": validation.score,
                    "validation": {
                        "status": validation.status,
                        "verdict": validation.verdict,
                        "score": validation.score,
                        "blocking_count": validation.blocking_count,
                        "warning_count": validation.warning_count,
                        "requires_founder_confirmation": validation.requires_founder_confirmation,
                        "issues": validation.issues_as_dicts(),
                        "checked_rules": list(validation.checked_rules),
                    },
                }
            )

        chosen = max(variants, key=lambda v: v["score"])
        content_service = ContentService(session)
        item = await content_service.create_item(
            brand_profile_id=brand.id,
            title=(chosen["hook"] or f"{payload.platform} draft")[:300],
            platform=payload.platform,
            format=payload.content_type,
            campaign_id=campaign.id if campaign else None,
            created_by=user.id,
            created_by_agent="content",
            headline=chosen["headline"],
            body=chosen["body"],
            hashtags=chosen["hashtags"],
            call_to_action=chosen["call_to_action"],
            tags=[payload.platform, payload.content_type, "phase3a"],
            generation_metadata={
                "campaign_id": str(campaign.id) if campaign else "",
                "objective": payloads["objective"],
                "platform": payload.platform,
                "content_type": payload.content_type,
                "audience": payload.audience,
                "pillar": payload.pillar,
                "primary_message": payload.primary_message,
                "hook": chosen["hook"],
                "outline": chosen["outline"],
                "suggested_date": payloads["suggested_date"],
                "suggested_time": payload.planned_time,
                "variant_index": chosen["index"],
                "selected_variant": chosen["index"],
                "variant_count": len(variants),
                "variants": variants,
                "provider": chosen["provider"],
                "model": chosen["model"],
                "is_mock": chosen["is_mock"],
                "generation_mode": chosen["generation_mode"],
                "budget": guard.budget.snapshot(),
            },
        )
        version = await content_service.current_version(item)
        await ContentValidationService(session).record(
            item=item,
            version=version,
            validation=chosen["validation"],
            provider=chosen["provider"],
            generation_mode=chosen["generation_mode"],
        )
        await ContentCalendarService(session).upsert(
            item=item,
            planned_date=payload.planned_date or date.today(),
            planned_time=payload.planned_time,
            actor_id=user.id,
        )
        await session.flush()
        return {
            **GeneratedItemOut(
                content_item_id=item.id,
                version_id=version.id,
                version_number=version.version_number,
                platform=item.platform,
                content_type=item.format,
                title=item.title,
                status=item.status,
                validation_status=chosen["validation"]["status"],
                selected_variant=chosen["index"],
                variants=variants,
                is_mock=chosen["is_mock"],
                provider=chosen["provider"],
                generation_metadata=version.generation_metadata or {},
            ).model_dump(mode="json")
        }

    result, replayed = await with_idempotency(
        session,
        key=key,
        user_id=user.id,
        endpoint="content.generate",
        payload=payload.model_dump(mode="json"),
        operation=operation,
    )
    await session.commit()
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return GeneratedItemOut.model_validate(result)


# --------------------------------------------------------------------------- #
# weekly plans
# --------------------------------------------------------------------------- #
@router.post(
    "/plans",
    response_model=PlanDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create (and optionally generate) a weekly content plan",
)
async def create_plan(
    payload: PlanCreateIn,
    session: DB,
    user: EditorUser,
    response: Response,
    key: IdempotencyKeyDep,
) -> PlanDetailOut:
    brand = await _brand(session, user)
    campaign = await _campaign(session, payload.campaign_id, brand)
    week_start = payload.week_start or default_week_start()

    async def operation() -> dict[str, Any]:
        service = ContentPlanningService(session)
        plan = await service.create_plan(
            brand=brand,
            week_start=week_start,
            campaign=campaign,
            config=payload.config.model_dump(exclude_none=True),
            actor_id=user.id,
            strategy_id=payload.strategy_id,
            research_project_id=payload.research_project_id,
            replace=payload.replace,
        )
        if payload.generate:
            plan = await service.generate(
                plan, brand=brand, campaign=campaign, actor_id=user.id
            )
        await session.flush()
        return _plan_detail(plan).model_dump(mode="json")

    result, replayed = await with_idempotency(
        session,
        key=key,
        user_id=user.id,
        endpoint="content.plans.create",
        payload=payload.model_dump(mode="json"),
        operation=operation,
    )
    await session.commit()
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return PlanDetailOut.model_validate(result)


@router.get("/plans", response_model=list[PlanOut], summary="List weekly plans")
async def list_plans(
    session: DB,
    user: CurrentUser,
    campaign_id: uuid.UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[PlanOut]:
    brand = await _brand(session, user)
    stmt = select(ContentPlan).where(ContentPlan.brand_profile_id == brand.id)
    if campaign_id is not None:
        stmt = stmt.where(ContentPlan.campaign_id == campaign_id)
    stmt = stmt.order_by(ContentPlan.week_start.desc()).limit(limit)
    plans = (await session.execute(stmt)).scalars().all()
    return [PlanOut.model_validate(p) for p in plans]


@router.get("/plans/{plan_id}", response_model=PlanDetailOut, summary="Plan detail")
async def get_plan(plan_id: uuid.UUID, session: DB, user: CurrentUser) -> PlanDetailOut:
    brand = await _brand(session, user)
    plan = await session.get(ContentPlan, plan_id)
    if plan is None or plan.brand_profile_id != brand.id:
        raise NotFoundError("Content plan not found.")
    return _plan_detail(plan)


@router.post(
    "/plans/{plan_id}/generate",
    response_model=PlanDetailOut,
    summary="Generate the drafts for a weekly plan (idempotent per slot)",
)
async def generate_plan(
    plan_id: uuid.UUID,
    session: DB,
    user: EditorUser,
    response: Response,
    key: IdempotencyKeyDep,
    force: bool = Query(default=False, description="Regenerate even already-generated slots"),
) -> PlanDetailOut:
    brand = await _brand(session, user)
    plan = await session.get(ContentPlan, plan_id)
    if plan is None or plan.brand_profile_id != brand.id:
        raise NotFoundError("Content plan not found.")
    campaign = await _campaign(session, plan.campaign_id, brand)

    async def operation() -> dict[str, Any]:
        service = ContentPlanningService(session)
        generated = await service.generate(
            plan, brand=brand, campaign=campaign, actor_id=user.id, force=force
        )
        await session.flush()
        return _plan_detail(generated).model_dump(mode="json")

    result, replayed = await with_idempotency(
        session,
        key=key,
        user_id=user.id,
        endpoint="content.plans.generate",
        payload={"plan_id": str(plan_id), "force": force},
        operation=operation,
    )
    await session.commit()
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return PlanDetailOut.model_validate(result)


# --------------------------------------------------------------------------- #
# variants + validation
# --------------------------------------------------------------------------- #
async def _item(session, item_id: uuid.UUID, brand: BrandProfile) -> ContentItem:  # type: ignore[no-untyped-def]
    item = await session.get(ContentItem, item_id)
    if item is None or item.brand_profile_id != brand.id:
        raise NotFoundError("Content item not found.")
    return item


@router.get(
    "/items/{item_id}/variants",
    summary="Compare the generated variants for an item",
)
async def list_variants(item_id: uuid.UUID, session: DB, user: CurrentUser) -> dict[str, Any]:
    brand = await _brand(session, user)
    item = await _item(session, item_id, brand)
    version = await ContentService(session).current_version(item)
    meta = (version.generation_metadata if version else {}) or {}
    variants = meta.get("variants") or []
    return {
        "content_item_id": str(item.id),
        "current_version_id": str(item.current_version_id) if item.current_version_id else None,
        "version_number": version.version_number if version else None,
        "selected_variant": meta.get("selected_variant"),
        "variants": variants,
        "is_mock": meta.get("is_mock", False),
        "provider": meta.get("provider", ""),
        "note": (
            "Variants are preserved with the version that produced them. Selecting a "
            "different variant creates a new version and invalidates any approval."
        ),
    }


@router.post(
    "/items/{item_id}/variants/regenerate",
    summary="Generate a fresh variant set for an item (creates a new version)",
)
async def regenerate_variants(
    item_id: uuid.UUID,
    session: DB,
    user: EditorUser,
    payload: VariantRegenerateIn | None = None,
) -> dict[str, Any]:
    """Produce new variants for an existing draft.

    A version's stored variants never change once written, so regenerating means
    writing a *new* version: the best-scoring fresh variant becomes the current
    text and the whole set is stored on that version. Any approval on the previous
    version is invalidated by ``ContentEditor.edit`` and its history is retained.
    """
    payload = payload or VariantRegenerateIn()
    settings = get_settings()
    if settings.content_provider != "mock":
        raise ValidationProblem(
            "Live model execution is disabled in Phase 3A; variant regeneration runs on "
            "the mock provider only.",
            code="live_calls_disabled",
        )
    brand = await _brand(session, user)
    item = await _item(session, item_id, brand)
    if item.status in (
        ContentStatus.PUBLISHED.value,
        ContentStatus.PUBLISHING.value,
    ):
        raise ValidationProblem(
            "Published content cannot be regenerated.", code="immutable_item"
        )
    campaign = (
        await session.get(Campaign, item.campaign_id) if item.campaign_id else None
    )
    version = await ContentService(session).current_version(item)
    previous_meta = dict((version.generation_metadata if version else {}) or {})

    gateway = LLMGateway(session=session)
    guard = ExecutionGuard(limits=ExecutionLimits.for_content(settings))
    ctx = AgentContext(
        session=session,
        brand=brand,
        gateway=gateway,
        actor_id=user.id,
        extra={"budget_guard": guard},
    )
    content_agent = get_registry().get("content")
    guardian = get_registry().get("brand_guardian")
    count = max(1, min(payload.variants, 3))

    base = {
        "platform": item.platform,
        "content_type": item.format,
        "campaign_id": item.campaign_id,
        "brand_profile_id": brand.id,
        "objective": (campaign.objective if campaign else "")
        or previous_meta.get("objective", ""),
        "audience": previous_meta.get("audience", ""),
        "pillar": previous_meta.get("pillar", ""),
        "primary_message": previous_meta.get("primary_message", ""),
        "suggested_date": previous_meta.get("suggested_date", date.today().isoformat()),
        "suggested_time": previous_meta.get("suggested_time", "09:00"),
        "variant_count": count,
    }

    # Start from an offset that depends on the version being replaced and forbid
    # the current opening, otherwise a deterministic provider would hand back the
    # text that is already on screen.
    offset = max(1, (version.version_number if version else 0))
    current_body = (version.body if version else "") or ""
    current_headline = (version.headline if version else "") or ""
    variants: list[dict[str, Any]] = []
    hooks: list[str] = [h for h in [current_headline] if h]
    for index in range(count):
        draft = await content_agent.run(
            ctx,
            ContentInput(
                **base,
                variant_index=offset + index,
                variant_seed=offset + index,
                avoid_hooks=list(hooks),
            ),
        )
        validation = await guardian.run(
            ctx,
            BrandGuardianInput(
                platform=item.platform,
                content_type=item.format,
                title=draft.headline,
                hook=draft.hook,
                body=draft.body,
                hashtags=draft.hashtags,
                call_to_action=draft.call_to_action,
                outline=draft.outline,
                audience=base["audience"],
                objective=base["objective"],
                pillar=base["pillar"],
                sibling_hooks=list(hooks),
            ),
        )
        hooks.append(draft.hook)
        variants.append(
            {
                "index": index,
                "headline": draft.headline,
                "hook": draft.hook,
                "body": draft.body,
                "hashtags": list(draft.hashtags),
                "call_to_action": draft.call_to_action,
                "outline": list(draft.outline),
                "character_count": len(draft.body),
                "provider": draft.provider,
                "model": draft.model,
                "is_mock": draft.is_mock,
                "generation_mode": draft.generation_mode,
                "score": validation.score,
                "validation": {
                    "status": validation.status,
                    "verdict": validation.verdict,
                    "score": validation.score,
                    "blocking_count": validation.blocking_count,
                    "warning_count": validation.warning_count,
                    "requires_founder_confirmation": validation.requires_founder_confirmation,
                    "issues": validation.issues_as_dicts(),
                    "checked_rules": list(validation.checked_rules),
                },
            }
        )

    chosen = max(variants, key=lambda v: (v["score"], -v["index"]))
    if chosen["body"] == current_body:
        differing = [v for v in variants if v["body"] != current_body]
        if not differing:
            raise ValidationProblem(
                "Regeneration produced the same text as the current version. Edit the "
                "draft or change its brief before regenerating.",
                code="no_change",
            )
        chosen = max(differing, key=lambda v: (v["score"], -v["index"]))
    metadata = {
        **previous_meta,
        "variants": variants,
        "selected_variant": chosen["index"],
        "variant_index": chosen["index"],
        "variant_count": len(variants),
        "provider": chosen["provider"],
        "model": chosen["model"],
        "is_mock": chosen["is_mock"],
        "generation_mode": chosen["generation_mode"],
        "validation": chosen["validation"],
        "regenerated": True,
    }
    item, new_version, invalidated = await ContentEditor(session).edit(
        item.id,
        actor_id=user.id,
        headline=chosen["headline"],
        body=chosen["body"],
        hashtags=chosen["hashtags"],
        call_to_action=chosen["call_to_action"],
        change_note=payload.change_note
        or f"Regenerated {len(variants)} variant(s) with the content agent",
        created_by_agent="content",
        generation_metadata=metadata,
    )
    await ContentValidationService(session).record(
        item=item,
        version=new_version,
        validation=chosen["validation"],
        provider=chosen["provider"],
        generation_mode=chosen["generation_mode"],
        created_by_agent="brand_guardian",
    )
    await session.commit()
    return {
        "content_item_id": str(item.id),
        "current_version_id": str(new_version.id),
        "version_number": new_version.version_number,
        "selected_variant": chosen["index"],
        "variants": variants,
        "is_mock": chosen["is_mock"],
        "provider": chosen["provider"],
        "approvals_invalidated": invalidated,
        "note": (
            "Variants were written to a new version. The previous version, and any "
            "approval held against it, is preserved unchanged."
        ),
    }


@router.post(
    "/items/{item_id}/variants/select",
    summary="Select a different stored variant (creates a new version)",
)
async def select_variant(
    item_id: uuid.UUID,
    payload: VariantSelectIn,
    session: DB,
    user: EditorUser,
) -> dict[str, Any]:
    brand = await _brand(session, user)
    item = await _item(session, item_id, brand)
    service = ContentService(session)
    version = await service.current_version(item)
    meta = (version.generation_metadata if version else {}) or {}
    variants = meta.get("variants") or []
    chosen = next((v for v in variants if v.get("index") == payload.variant_index), None)
    if chosen is None:
        raise ValidationProblem(
            "That variant is not stored for this item.",
            details={"available": [v.get("index") for v in variants]},
        )
    if chosen.get("body") == (version.body if version else None):
        raise ValidationProblem(
            "That variant is already the current version.", code="no_change"
        )

    item, new_version, invalidated = await ContentEditor(session).edit(
        item.id,
        actor_id=user.id,
        headline=chosen.get("headline", ""),
        body=chosen.get("body", ""),
        hashtags=chosen.get("hashtags", []),
        call_to_action=chosen.get("call_to_action", ""),
        change_note=payload.change_note or f"Selected variant {payload.variant_index}",
        created_by_agent="content",
        generation_metadata={
            **meta,
            "selected_variant": payload.variant_index,
            "selected_from_variant": True,
        },
    )
    await session.commit()
    return {
        "content_item_id": str(item.id),
        "version_id": str(new_version.id),
        "version_number": new_version.version_number,
        "selected_variant": payload.variant_index,
        "status": item.status,
        "approvals_invalidated": invalidated,
        "approval_invalidated": invalidated > 0,
    }


@router.post(
    "/items/{item_id}/validate",
    response_model=ValidationOut,
    summary="Re-run the Brand Guardian against the current version",
)
async def validate_item(item_id: uuid.UUID, session: DB, user: EditorUser) -> ValidationOut:
    brand = await _brand(session, user)
    item = await _item(session, item_id, brand)
    version = await ContentService(session).current_version(item)
    if version is None:
        raise NotFoundError("Item has no version to validate.")

    gateway = LLMGateway(session=session)
    ctx = AgentContext(session=session, brand=brand, gateway=gateway, actor_id=user.id)
    guardian = get_registry().get("brand_guardian")
    result = await guardian.run(
        ctx,
        BrandGuardianInput(
            platform=item.platform,
            content_type=item.format,
            title=version.headline,
            hook=(version.generation_metadata or {}).get("hook", ""),
            body=version.body,
            hashtags=list(version.hashtags),
            call_to_action=version.call_to_action,
            outline=(version.generation_metadata or {}).get("outline", []),
        ),
    )
    row = await ContentValidationService(session).record(
        item=item,
        version=version,
        validation={
            "status": result.status,
            "score": result.score,
            "blocking_count": result.blocking_count,
            "warning_count": result.warning_count,
            "requires_founder_confirmation": result.requires_founder_confirmation,
            "issues": result.issues_as_dicts(),
            "checked_rules": list(result.checked_rules),
        },
        provider="deterministic",
        generation_mode=result.generation_mode,
    )
    await session.commit()
    return ValidationOut(
        verdict=row.verdict,
        score=row.score,
        issues=row.issues,
        checked_rules=row.checked_rules,
        blocking_count=row.blocking_count,
        warning_count=row.warning_count,
        requires_founder_confirmation=row.requires_founder_confirmation,
        provider=row.provider,
        generation_mode=row.generation_mode,
        content_version_id=row.content_version_id,
    )


@router.get(
    "/items/{item_id}/validations",
    response_model=list[ValidationOut],
    summary="Validation history for an item",
)
async def validation_history(
    item_id: uuid.UUID, session: DB, user: CurrentUser
) -> list[ValidationOut]:
    brand = await _brand(session, user)
    item = await _item(session, item_id, brand)
    rows = await ContentValidationService(session).history(item.id)
    return [ValidationOut.model_validate(row) for row in rows]


# --------------------------------------------------------------------------- #
# internal calendar
# --------------------------------------------------------------------------- #
def _window(view: str, anchor: date) -> tuple[date, date]:
    if view == "week":
        return week_bounds(anchor)
    if view == "month":
        first = anchor.replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        return first, nxt - timedelta(days=1)
    # list: 90 days forward from the anchor
    return anchor, anchor + timedelta(days=90)


@router.get("/calendar", response_model=CalendarViewOut, summary="Internal content calendar")
async def calendar(
    session: DB,
    user: CurrentUser,
    view: str = Query(default="month", pattern="^(month|week|list)$"),
    anchor: date | None = None,
    platform: str | None = None,
    campaign_id: uuid.UUID | None = None,
) -> CalendarViewOut:
    brand = await _brand(session, user)
    start, end = _window(view, anchor or datetime.now(timezone.utc).date())
    service = ContentCalendarService(session)
    rows = await service.list_entries(
        brand_profile_id=brand.id,
        start=start,
        end=end,
        platform=platform,
        campaign_id=campaign_id,
    )
    return CalendarViewOut(
        view=view,  # type: ignore[arg-type]
        start=start,
        end=end,
        entries=[CalendarEntryOut(**service.as_dict(entry, item)) for entry, item in rows],
    )


@router.patch(
    "/calendar/{item_id}",
    response_model=CalendarEntryOut,
    summary="Move a draft to another internal calendar date",
)
async def move_calendar_entry(
    item_id: uuid.UUID,
    payload: CalendarMoveIn,
    session: DB,
    user: EditorUser,
) -> CalendarEntryOut:
    brand = await _brand(session, user)
    item = await _item(session, item_id, brand)
    service = ContentCalendarService(session)
    entry = await service.move(
        item_id=item.id,
        planned_date=payload.planned_date,
        planned_time=payload.planned_time,
        note=payload.note,
        brand_profile_id=brand.id,
        actor_id=user.id,
    )
    await session.commit()
    return CalendarEntryOut(**service.as_dict(entry, item))
