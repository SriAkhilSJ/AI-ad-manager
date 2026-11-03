"""Weekly content planning (Phase 3A).

Takes a campaign, the brand knowledge base, an optional strategy and optional
research, and produces a *balanced* week of platform-specific drafts:

    plan → propose slots → generate variants → dedupe → validate → persist

Design notes
------------
* **Distribution is deliberate, not uniform.** Six platforms are never posted to
  every day. Each platform gets a weekly cadence from its spec: broadcast channels
  rotate, community-first channels (Reddit) are capped low, and long-form script
  work (YouTube) is rationed because it costs the reader more attention.
* **Nothing is duplicated.** Every style of "six near-identical LinkedIn posts"
  is caught twice: variant selection inside a slot, and a similarity check against
  every sibling draft already generated in the plan.
* **Budget-bounded.** One :class:`BudgetGuard` is shared by the Content Agent and
  the Brand Guardian for the whole plan, so the per-plan envelope is a real
  ledger rather than a per-call hope. Generation stops cleanly when it is spent.
* **Idempotent at the slot level.** A slot that already produced a content item is
  skipped, so a retried request cannot duplicate content.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import AgentContext, AgentBudgetExceeded
from app.agents.brand_guardian import BrandGuardianAgent, BrandGuardianInput
from app.agents.content import BrandFact, ContentAgent, ContentInput, ResearchReference
from app.agents.limits import ExecutionGuard, ExecutionLimits
from app.agents.registry import get_registry
from app.content.platforms import ContentType, get_spec
from app.core.config import get_settings
from app.core.errors import BudgetExhaustedError, ValidationProblem
from app.core.logging import get_logger
from app.llm.gateway import LLMGateway
from app.models.brand import BrandProfile, ContentPillar
from app.models.campaign import Campaign
from app.models.content_plan import (
    ContentCalendarEntry,
    ContentPlan,
    ContentPlanSlot,
    ContentValidation,
    PlanStatus,
    SlotStatus,
    ValidationVerdict,
)
from app.models.research import ProductFact, ResearchFinding
from app.models.social import Platform
from app.services.content import ContentService

logger = get_logger(__name__)

#: Cadence for one week. These are *maxima*, not quotas: the planner thins them
#: further when the founder asks for a short week.
DEFAULT_WEEKLY_CADENCE: dict[str, int] = {
    "linkedin": 2,
    "x": 2,
    "instagram": 1,
    "youtube": 1,
    "facebook": 1,
    "reddit": 1,
}

#: Preferred content types per platform, in priority order. The planner walks this
#: list so a platform does not publish the same shape twice in a week.
PLATFORM_CONTENT_MIX: dict[str, tuple[str, ...]] = {
    "linkedin": (
        ContentType.SOCIAL_POST.value,
        ContentType.FOUNDER_STORY.value,
        ContentType.EDUCATIONAL_POST.value,
        ContentType.PRODUCT_ANNOUNCEMENT.value,
    ),
    "x": (
        ContentType.SOCIAL_POST.value,
        ContentType.THREAD.value,
        ContentType.EDUCATIONAL_POST.value,
        ContentType.FOUNDER_STORY.value,
    ),
    "instagram": (
        ContentType.CAPTION.value,
        ContentType.SHORT_FORM_VIDEO_SCRIPT.value,
        ContentType.CAROUSEL_OUTLINE.value,
        ContentType.EDUCATIONAL_POST.value,
    ),
    "youtube": (
        ContentType.SHORT_FORM_VIDEO_SCRIPT.value,
        ContentType.HOOK.value,
    ),
    "facebook": (
        ContentType.SOCIAL_POST.value,
        ContentType.EDUCATIONAL_POST.value,
        ContentType.PRODUCT_ANNOUNCEMENT.value,
    ),
    "reddit": (
        ContentType.COMMUNITY_DISCUSSION.value,
        ContentType.EDUCATIONAL_POST.value,
    ),
}

#: Platforms whose copy must lead with community value.
COMMUNITY_FIRST = {"reddit"}

DEFAULT_WINDOWS: dict[str, str] = {
    "linkedin": "09:00",
    "x": "12:30",
    "instagram": "18:00",
    "youtube": "17:00",
    "facebook": "10:30",
    "reddit": "15:00",
}


@dataclass(frozen=True)
class PlannedSlot:
    """One proposed slot before any copy is written."""

    platform: str
    content_type: str
    scheduled_date: date
    scheduled_time: str
    audience: str
    pillar: str
    objective: str
    primary_message: str
    rationale: str
    variant_index: int = 0

    def as_kwargs(self, position: int) -> dict[str, Any]:
        return {
            "position": position,
            "platform": self.platform,
            "content_type": self.content_type,
            "scheduled_date": self.scheduled_date,
            "scheduled_time": self.scheduled_time,
            "audience": self.audience,
            "pillar": self.pillar,
            "objective": self.objective,
            "primary_message": self.primary_message,
            "rationale": self.rationale,
            "variant_index": self.variant_index,
        }


class ContentPlanningService:
    """Builds and materialises weekly content plans."""

    def __init__(self, session: AsyncSession, *, gateway: LLMGateway | None = None) -> None:
        self.session = session
        self.settings = get_settings()
        self.content = ContentService(session)
        self.gateway = gateway or LLMGateway(session=session)

    # ------------------------------------------------------------------ config
    def normalise_config(self, config: dict[str, Any] | None) -> dict[str, Any]:
        """Validate and clamp the founder-configurable knobs.

        Unknown keys are rejected rather than ignored, because a silently dropped
        ``posts_per_week`` would produce a plan the founder did not ask for.
        """
        raw = dict(config or {})
        allowed = {
            "posts_per_week",
            "platforms",
            "content_mix",
            "audience_focus",
            "publishing_windows",
            "variants",
            "campaign_duration_weeks",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ValidationProblem(
                f"Unknown plan option(s): {sorted(unknown)}.",
                details={"allowed": sorted(allowed)},
            )

        posts = raw.get("posts_per_week", len(DEFAULT_WEEKLY_CADENCE))
        try:
            posts = int(posts)
        except (TypeError, ValueError) as exc:
            raise ValidationProblem("posts_per_week must be an integer.") from exc
        if not 1 <= posts <= int(self.settings.content_max_slots):
            raise ValidationProblem(
                f"posts_per_week must be between 1 and {self.settings.content_max_slots}.",
                details={"requested": posts},
            )

        platforms = raw.get("platforms") or list(DEFAULT_WEEKLY_CADENCE)
        if not isinstance(platforms, (list, tuple)) or not platforms:
            raise ValidationProblem("platforms must be a non-empty list.")
        clean_platforms: list[str] = []
        for platform in platforms:
            key = str(platform).lower()
            if key not in Platform.values():
                raise ValidationProblem(
                    f"Unsupported platform '{platform}'.",
                    details={"allowed": Platform.values()},
                )
            if key not in clean_platforms:
                clean_platforms.append(key)

        windows = raw.get("publishing_windows") or {}
        if not isinstance(windows, dict):
            raise ValidationProblem("publishing_windows must be a mapping of platform -> HH:MM.")
        clean_windows: dict[str, str] = {}
        for platform, value in windows.items():
            key = str(platform).lower()
            if key not in Platform.values():
                raise ValidationProblem(f"publishing_windows names unknown platform '{platform}'.")
            clean_windows[key] = _validate_time(str(value), field="publishing_windows")

        variants = raw.get("variants", 2)
        try:
            variants = int(variants)
        except (TypeError, ValueError) as exc:
            raise ValidationProblem("variants must be an integer.") from exc
        if not 1 <= variants <= 3:
            raise ValidationProblem("variants must be between 1 and 3.")

        mix = raw.get("content_mix") or {}
        if not isinstance(mix, dict):
            raise ValidationProblem("content_mix must be a mapping of platform -> list of types.")
        clean_mix: dict[str, list[str]] = {}
        for platform, types in mix.items():
            key = str(platform).lower()
            if key not in Platform.values():
                raise ValidationProblem(f"content_mix names unknown platform '{platform}'.")
            if not isinstance(types, (list, tuple)) or not types:
                raise ValidationProblem(f"content_mix for {key} must be a non-empty list.")
            spec = get_spec(key)
            resolved: list[str] = []
            for content_type in types:
                name = str(content_type)
                if name not in ContentType.values():
                    raise ValidationProblem(f"Unknown content type '{name}'.")
                if not spec.supports(name):
                    raise ValidationProblem(
                        f"{key} does not support content type '{name}'.",
                        details={"supported": list(spec.supported_types)},
                    )
                resolved.append(name)
            clean_mix[key] = resolved

        # Capacity check: the weekly cadence table caps how much content a week can
        # usefully hold. Asking for more is a configuration mistake, not something
        # to silently truncate.
        capacity = sum(DEFAULT_WEEKLY_CADENCE.get(p, 1) for p in clean_platforms)
        if posts > capacity:
            raise ValidationProblem(
                f"posts_per_week={posts} exceeds what the selected platforms support "
                f"in one week ({capacity}).",
                code="over_capacity",
                details={"max_posts_per_week": capacity, "platforms": clean_platforms},
            )

        duration = raw.get("campaign_duration_weeks", 1)
        try:
            duration = int(duration)
        except (TypeError, ValueError) as exc:
            raise ValidationProblem("campaign_duration_weeks must be an integer.") from exc
        if not 1 <= duration <= 52:
            raise ValidationProblem("campaign_duration_weeks must be between 1 and 52.")

        return {
            "posts_per_week": posts,
            "platforms": clean_platforms,
            "content_mix": clean_mix,
            "audience_focus": str(raw.get("audience_focus") or ""),
            "publishing_windows": clean_windows,
            "variants": variants,
            "campaign_duration_weeks": duration,
        }

    # --------------------------------------------------------------- planning
    def propose_slots(
        self,
        *,
        week_start: date,
        config: dict[str, Any],
        campaign: Campaign | None,
        brand: BrandProfile,
        pillars: Sequence[str] = (),
    ) -> list[PlannedSlot]:
        """Distribute a week's posts across the selected platforms.

        Deterministic: the same inputs always produce the same plan, so a plan can
        be reviewed, regenerated and compared.
        """
        platforms: list[str] = list(config["platforms"])
        posts = int(config["posts_per_week"])
        mix = config.get("content_mix") or {}
        windows = config.get("publishing_windows") or {}
        audience = config.get("audience_focus") or ", ".join(
            (brand.audience or ["developers"])[:2]
        )
        objective = (campaign.objective if campaign else "") or "Brand awareness."
        pillars_list = list(pillars)

        # 1. build a cadence order: platforms with a higher weekly cadence get
        #    more turns, community-first platforms are held to one.
        # Give every selected platform one slot first when the week is long
        # enough, then hand the remainder to the highest-cadence channels. This is
        # what keeps a six-platform awareness campaign balanced instead of
        # letting two channels eat the whole week.
        cadence = {p: 0 for p in platforms}
        budget_posts = posts
        if budget_posts >= len(platforms):
            for platform in platforms:
                cadence[platform] = 1
            budget_posts -= len(platforms)

        ordered = sorted(
            platforms, key=lambda p: (-DEFAULT_WEEKLY_CADENCE.get(p, 1), platforms.index(p))
        )
        index = 0
        while budget_posts > 0 and ordered:
            platform = ordered[index % len(ordered)]
            if cadence[platform] < DEFAULT_WEEKLY_CADENCE.get(platform, 1):
                cadence[platform] += 1
                budget_posts -= 1
            index += 1
            if index > len(ordered) * 3:  # every platform is at its cap
                break

        # 2. interleave platforms so two posts never share a platform on one day
        sequence: list[str] = []
        remaining = dict(cadence)
        while any(remaining.values()):
            for platform in platforms:
                if remaining.get(platform, 0) > 0:
                    sequence.append(platform)
                    remaining[platform] -= 1
                    if len(sequence) >= int(config["posts_per_week"]):
                        break
            if len(sequence) >= int(config["posts_per_week"]):
                break
        if not sequence:  # pragma: no cover - posts_per_week >= 1 guarantees one
            sequence = [platforms[0]]

        # 3. spread across the working week, one post per day where possible
        slots: list[PlannedSlot] = []
        used_types: dict[str, int] = {p: 0 for p in platforms}
        for index, platform in enumerate(sequence):
            day_offset = index % 7
            scheduled = week_start + timedelta(days=day_offset)
            type_pool = tuple(mix.get(platform) or PLATFORM_CONTENT_MIX.get(platform, ()))
            content_type = type_pool[used_types[platform] % len(type_pool)]
            used_types[platform] += 1
            pillar = pillars_list[index % len(pillars_list)] if pillars_list else ""
            slots.append(
                PlannedSlot(
                    platform=platform,
                    content_type=content_type,
                    scheduled_date=scheduled,
                    scheduled_time=windows.get(platform) or DEFAULT_WINDOWS.get(platform, "09:00"),
                    audience=audience,
                    pillar=pillar,
                    objective=objective,
                    primary_message=self._primary_message(brand, pillar, objective, platform),
                    rationale=self._rationale(platform, content_type, index),
                )
            )
        return slots

    def _primary_message(
        self, brand: BrandProfile, pillar: str, objective: str, platform: str
    ) -> str:
        core = pillar or (brand.tagline or "Build and Ship with Ease.")
        if platform in COMMUNITY_FIRST:
            return f"A genuine question about {core.lower()}"
        return core

    def _rationale(self, platform: str, content_type: str, index: int) -> str:
        spec = get_spec(platform)
        reason = (
            f"{spec.label} {content_type.replace('_', ' ')} — slot {index + 1} in the week, "
            f"chosen for {spec.tone} tone"
        )
        if platform in COMMUNITY_FIRST:
            reason += "; community-first, promotional density held to one"
        return reason + "."

    # ------------------------------------------------------------ persistence
    async def create_plan(
        self,
        *,
        brand: BrandProfile,
        week_start: date,
        campaign: Campaign | None = None,
        config: dict[str, Any] | None = None,
        actor_id: uuid.UUID | None = None,
        strategy_id: uuid.UUID | None = None,
        research_project_id: uuid.UUID | None = None,
        replace: bool = False,
    ) -> ContentPlan:
        """Create (or replace) the plan shell and its proposed slots."""
        clean = self.normalise_config(config)
        week_end = week_start + timedelta(days=6)

        existing = (
            await self.session.execute(
                select(ContentPlan).where(
                    ContentPlan.brand_profile_id == brand.id,
                    ContentPlan.campaign_id == (campaign.id if campaign else None),
                    ContentPlan.week_start == week_start,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if not replace:
                raise ValidationProblem(
                    "A content plan already exists for that campaign and week.",
                    code="plan_exists",
                    details={"plan_id": str(existing.id)},
                )
            if existing.status == PlanStatus.GENERATED.value and existing.item_count:
                raise ValidationProblem(
                    "That plan already produced content; archive it instead of replacing it.",
                    code="plan_generated",
                    details={"plan_id": str(existing.id)},
                )
            await self.session.delete(existing)
            await self.session.flush()

        pillars = await self._active_pillars(brand)
        slots = self.propose_slots(
            week_start=week_start,
            config=clean,
            campaign=campaign,
            brand=brand,
            pillars=pillars,
        )

        plan = ContentPlan(
            brand_profile_id=brand.id,
            campaign_id=campaign.id if campaign else None,
            strategy_id=strategy_id,
            research_project_id=research_project_id,
            week_start=week_start,
            week_end=week_end,
            status=PlanStatus.DRAFT.value,
            objective=(campaign.objective if campaign else "") or "Brand awareness.",
            platforms=list(clean["platforms"]),
            config=clean,
            slot_count=len(slots),
            item_count=0,
            completeness_score=0.0,
            generation_metadata={},
            generated_by="deterministic",
            created_by=actor_id,
        )
        self.session.add(plan)
        # Assign through the relationship so the in-memory collection is already
        # loaded; a lazy refresh here would raise under an async session.
        plan.slots = [
            ContentPlanSlot(**slot.as_kwargs(position))
            for position, slot in enumerate(slots)
        ]
        await self.session.flush()
        return plan

    # ------------------------------------------------------------- generation
    async def generate(
        self,
        plan: ContentPlan,
        *,
        brand: BrandProfile,
        campaign: Campaign | None = None,
        actor_id: uuid.UUID | None = None,
        force: bool = False,
    ) -> ContentPlan:
        """Write, validate and persist the drafts for every slot in the plan."""
        if self.settings.content_provider == "disabled":
            raise ValidationProblem(
                "Content generation is disabled by configuration (CONTENT_PROVIDER=disabled).",
                code="content_disabled",
            )

        slots = sorted(plan.slots, key=lambda s: s.position)
        if not slots:
            raise ValidationProblem("Plan has no slots to generate.", code="empty_plan")

        config = dict(plan.config or {})
        variant_count = int(config.get("variants", 2))

        guard = ExecutionGuard(limits=ExecutionLimits.for_content(self.settings))
        plan.status = PlanStatus.GENERATING.value
        await self.session.flush()

        brand_facts = await self._brand_facts(brand)
        research_refs = await self._research_refs(plan)
        banned_claims = list(getattr(brand, "do_not_claim", []) or [])

        content_agent = get_registry().get("content")
        guardian = get_registry().get("brand_guardian")

        sibling_bodies: list[str] = []
        sibling_hooks: list[str] = []
        generated = 0
        skipped = 0
        failures: list[str] = []
        stop_reason = ""

        for slot in slots:
            if slot.status == SlotStatus.GENERATED.value and slot.content_item_id and not force:
                # Idempotency: a slot that already produced content is not re-run.
                skipped += 1
                item = await self.content.get_item(slot.content_item_id)
                version = await self.content.current_version(item)
                if version is not None:
                    sibling_bodies.append(version.body)
                    sibling_hooks.append(version.headline)
                continue

            # Phase 2.6 enforcement lives in the shared guard: a paid slot raises
            # AgentBudgetExceeded and generation stops cleanly. The free mock
            # provider is non-billable, so a zero budget does not stop it.
            if guard.iterations >= guard.limits.max_iterations:
                stop_reason = "iteration_limit"
                break

            guard.iterations += 1
            ctx = AgentContext(
                session=self.session,
                brand=brand,
                gateway=self.gateway,
                actor_id=actor_id,
                extra={"budget_guard": guard},
            )

            try:
                variants = await self._generate_variants(
                    plan=plan,
                    ctx=ctx,
                    guard=guard,
                    content_agent=content_agent,
                    guardian=guardian,
                    slot=slot,
                    brand=brand,
                    brand_facts=brand_facts,
                    research_refs=research_refs,
                    banned_claims=banned_claims,
                    variant_count=variant_count,
                    sibling_bodies=sibling_bodies,
                    sibling_hooks=sibling_hooks,
                )
            except (AgentBudgetExceeded, BudgetExhaustedError) as exc:
                stop_reason = "budget_exhausted"
                failures.append(f"slot {slot.position}: {exc}")
                break
            except Exception as exc:  # noqa: BLE001 - one slot must not kill the plan
                logger.warning(
                    "content_slot_failed",
                    slot=slot.position,
                    error=type(exc).__name__,
                    detail=str(exc)[:300],
                )
                slot.status = SlotStatus.FAILED.value
                failures.append(f"slot {slot.position}: {type(exc).__name__}")
                await self.session.flush()
                continue

            if not variants:
                slot.status = SlotStatus.FAILED.value
                failures.append(f"slot {slot.position}: no variant produced")
                await self.session.flush()
                continue

            chosen = max(variants, key=lambda v: v["validation"]["score"])
            item = await self._persist(
                plan=plan,
                slot=slot,
                chosen=chosen,
                variants=variants,
                brand=brand,
                actor_id=actor_id,
            )
            slot.content_item_id = item.id
            slot.status = SlotStatus.GENERATED.value
            slot.generation_metadata = {
                "variants": variants,
                "selected_variant": chosen["index"],
                "provider": chosen["provider"],
                "model": chosen["model"],
                "is_mock": chosen["is_mock"],
                "validation_status": chosen["validation"]["status"],
            }
            sibling_bodies.append(chosen["body"])
            sibling_hooks.append(chosen["hook"])
            generated += 1
            await self.session.flush()

        # Count everything the plan owns, not just what this call created: a
        # second generate() skips existing slots and must not report zero.
        plan.item_count = sum(1 for s in slots if s.content_item_id)
        plan.completeness_score = self.completeness(plan, slots_override=slots)
        plan.generated_by = (
            "mock" if self.settings.content_provider == "mock" else "llm"
        )
        plan.generation_metadata = {
            **(plan.generation_metadata or {}),
            "provider": "mock" if self.settings.content_provider == "mock" else "llm",
            "model": "mock-content-v1" if self.settings.content_provider == "mock" else "",
            "is_mock": self.settings.content_provider == "mock",
            "generation_mode": "mock" if self.settings.content_provider == "mock" else "llm",
            "variants_per_slot": variant_count,
            "slots_generated": generated,
            "slots_skipped": skipped,
            "failures": failures[:10],
            "stop_reason": stop_reason,
            "budget": guard.budget.snapshot(),
            "guard": guard.snapshot(),
        }
        plan.status = (
            PlanStatus.GENERATED.value if generated or skipped else PlanStatus.FAILED.value
        )
        await self.session.flush()
        return plan

    async def _generate_variants(
        self,
        *,
        plan: ContentPlan,
        ctx: AgentContext,
        guard: ExecutionGuard,
        content_agent: Any,
        guardian: Any,
        slot: ContentPlanSlot,
        brand: BrandProfile,
        brand_facts: list[BrandFact],
        research_refs: list[ResearchReference],
        banned_claims: list[str],
        variant_count: int,
        sibling_bodies: list[str],
        sibling_hooks: list[str],
    ) -> list[dict[str, Any]]:
        variants: list[dict[str, Any]] = []
        for index in range(variant_count):
            payload = ContentInput(
                campaign_id=plan.campaign_id,
                brand_profile_id=brand.id,
                platform=slot.platform,
                content_type=slot.content_type,
                angle=slot.primary_message or slot.pillar,
                pillar=slot.pillar,
                objective=slot.objective,
                audience=slot.audience,
                primary_message=slot.primary_message,
                research_refs=research_refs,
                brand_facts=brand_facts,
                prohibited_claims=banned_claims,
                suggested_date=slot.scheduled_date.isoformat(),
                suggested_time=slot.scheduled_time,
                variant_index=index,
                variant_count=variant_count,
                variant_seed=index,
                avoid_hooks=sibling_hooks,
            )
            draft = await content_agent.run(ctx, payload)
            spec = get_spec(slot.platform)
            validation = await guardian.run(
                ctx,
                BrandGuardianInput(
                    platform=slot.platform,
                    content_type=slot.content_type,
                    title=draft.headline,
                    hook=draft.hook,
                    body=draft.body,
                    hashtags=draft.hashtags,
                    call_to_action=draft.call_to_action,
                    outline=draft.outline,
                    audience=slot.audience,
                    objective=slot.objective,
                    pillar=slot.pillar,
                    source_refs=research_refs,
                    sibling_bodies=sibling_bodies,
                    sibling_hooks=sibling_hooks,
                ),
            )
            variants.append(
                {
                    "index": index,
                    "headline": draft.headline[: spec.title_max_chars],
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
                    "source_refs": [
                        ref.model_dump(mode="json") if hasattr(ref, "model_dump") else ref
                        for ref in research_refs
                    ],
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
        return variants

    async def _persist(
        self,
        *,
        plan: ContentPlan,
        slot: ContentPlanSlot,
        chosen: dict[str, Any],
        variants: list[dict[str, Any]],
        brand: BrandProfile,
        actor_id: uuid.UUID | None,
    ) -> Any:
        pillar_id = await self._pillar_id(brand, slot.pillar)
        metadata = {
            "plan_id": str(plan.id),
            "campaign_id": str(plan.campaign_id) if plan.campaign_id else "",
            "objective": slot.objective,
            "platform": slot.platform,
            "content_type": slot.content_type,
            "audience": slot.audience,
            "pillar": slot.pillar,
            "primary_message": slot.primary_message,
            "hook": chosen["hook"],
            "outline": chosen["outline"],
            "source_refs": [
                ref.model_dump(mode="json") if hasattr(ref, "model_dump") else ref
                for ref in (chosen.get("source_refs") or [])
            ],
            "suggested_date": slot.scheduled_date.isoformat(),
            "suggested_time": slot.scheduled_time,
            "variants": variants,
            "selected_variant": chosen["index"],
            "variant_index": chosen["index"],
            "variant_count": len(variants),
            "provider": chosen["provider"],
            "model": chosen["model"],
            "is_mock": chosen["is_mock"],
            "generation_mode": chosen["generation_mode"],
            "validation": chosen["validation"],
        }
        item = await self.content.create_item(
            brand_profile_id=brand.id,
            title=(chosen["hook"] or chosen["headline"] or f"{slot.platform} draft")[:300],
            platform=slot.platform,
            format=slot.content_type,
            campaign_id=plan.campaign_id,
            pillar_id=pillar_id,
            created_by=actor_id,
            created_by_agent="content",
            headline=chosen["headline"],
            body=chosen["body"],
            hashtags=chosen["hashtags"],
            call_to_action=chosen["call_to_action"],
            generation_metadata=metadata,
            tags=[slot.platform, slot.content_type, "phase3a"],
        )
        version = await self.content.current_version(item)
        if version is not None:
            await ContentValidationService(self.session).record(
                item=item,
                version=version,
                validation=chosen["validation"],
                provider=chosen["provider"],
                generation_mode=chosen["generation_mode"],
                created_by_agent="brand_guardian",
            )
        self.session.add(
            ContentCalendarEntry(
                content_item_id=item.id,
                brand_profile_id=brand.id,
                campaign_id=plan.campaign_id,
                plan_id=plan.id,
                planned_date=slot.scheduled_date,
                planned_time=slot.scheduled_time,
                note=f"Planned by weekly plan for {slot.platform}",
                created_by=actor_id,
            )
        )
        await self.session.flush()
        return item

    # ------------------------------------------------------------------ scores
    def completeness(
        self, plan: ContentPlan, *, slots_override: Sequence[ContentPlanSlot] | None = None
    ) -> float:
        """How complete the week is, 0–1.

        Half the score is coverage (did every planned slot produce a draft), half
        is quality (what share of those drafts are not blocked). A plan that
        generated everything but failed validation therefore cannot look complete.
        """
        slots = list(slots_override if slots_override is not None else plan.slots)
        if not slots:
            return 0.0
        generated = [s for s in slots if s.status == SlotStatus.GENERATED.value]
        coverage = len(generated) / len(slots)
        if not generated:
            return round(coverage * 0.5, 3)
        passing = 0
        for slot in generated:
            meta = slot.generation_metadata or {}
            status = (meta.get("validation") or {}).get("status") or meta.get("validation_status")
            if status in (ValidationVerdict.PASS.value, ValidationVerdict.PASS_WITH_WARNINGS.value):
                passing += 1
        quality = passing / len(generated)
        return round(coverage * 0.5 + quality * 0.5, 3)

    # ------------------------------------------------------------------ inputs
    async def _brand_facts(self, brand: BrandProfile) -> list[BrandFact]:
        """Approved facts only. Unverified ones are passed with their status, never as truth."""
        facts: list[BrandFact] = [
            BrandFact(statement=f"Company: {brand.company}", verification_status="verified"),
            BrandFact(statement=f"Product: {brand.product}", verification_status="verified"),
        ]
        if brand.tagline:
            facts.append(
                BrandFact(statement=f"Tagline: {brand.tagline}", verification_status="verified")
            )
        if brand.positioning:
            facts.append(
                BrandFact(statement=f"Positioning: {brand.positioning}", verification_status="verified")
            )
        rows = (
            await self.session.execute(
                select(ProductFact).where(ProductFact.brand_profile_id == brand.id).limit(25)
            )
        ).scalars().all()
        for row in rows:
            facts.append(
                BrandFact(
                    statement=row.statement,
                    verification_status=str(getattr(row, "verification_status", "unverified")),
                    source=getattr(row, "source", "product_fact") or "product_fact",
                )
            )
        return facts

    async def _research_refs(self, plan: ContentPlan) -> list[ResearchReference]:
        project_id = plan.research_project_id
        if not project_id:
            campaign = (
                await self.session.get(Campaign, plan.campaign_id) if plan.campaign_id else None
            )
            project_id = getattr(campaign, "research_project_id", None) if campaign else None
        if not project_id:
            return []
        rows = (
            await self.session.execute(
                select(ResearchFinding)
                .where(ResearchFinding.research_project_id == project_id)
                .limit(10)
            )
        ).scalars().all()
        refs: list[ResearchReference] = []
        for row in rows:
            refs.append(
                ResearchReference(
                    finding_id=str(row.id),
                    claim=row.finding,
                    source=row.source_url or row.source_title,
                    verification_status=str(getattr(row, "verification_status", "unverified")),
                )
            )
        return refs

    async def _active_pillars(self, brand: BrandProfile) -> list[str]:
        rows = (
            await self.session.execute(
                select(ContentPillar)
                .where(ContentPillar.brand_profile_id == brand.id, ContentPillar.is_active.is_(True))
                .order_by(ContentPillar.name)
            )
        ).scalars().all()
        return [row.name for row in rows]

    async def _pillar_id(self, brand: BrandProfile, pillar_name: str) -> uuid.UUID | None:
        if not pillar_name:
            return None
        row = (
            await self.session.execute(
                select(ContentPillar).where(
                    ContentPillar.brand_profile_id == brand.id,
                    ContentPillar.name == pillar_name,
                )
            )
        ).scalar_one_or_none()
        return row.id if row else None


class ContentValidationService:
    """Records Brand Guardian verdicts against immutable versions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(
        self,
        *,
        item: Any,
        version: Any,
        validation: dict[str, Any],
        provider: str = "deterministic",
        generation_mode: str = "deterministic",
        created_by_agent: str | None = "brand_guardian",
    ) -> ContentValidation:
        row = ContentValidation(
            content_item_id=item.id,
            content_version_id=version.id,
            verdict=str(validation.get("status") or ValidationVerdict.PASS.value),
            score=float(validation.get("score") or 0.0),
            issues=list(validation.get("issues") or []),
            checked_rules=list(validation.get("checked_rules") or []),
            blocking_count=int(validation.get("blocking_count") or 0),
            warning_count=int(validation.get("warning_count") or 0),
            requires_founder_confirmation=bool(
                validation.get("requires_founder_confirmation")
            ),
            provider=provider,
            generation_mode=generation_mode,
            created_by_agent=created_by_agent,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def latest_for_item(self, item_id: uuid.UUID) -> ContentValidation | None:
        return (
            await self.session.execute(
                select(ContentValidation)
                .where(ContentValidation.content_item_id == item_id)
                .order_by(ContentValidation.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def latest_for_version(self, version_id: uuid.UUID) -> ContentValidation | None:
        return (
            await self.session.execute(
                select(ContentValidation)
                .where(ContentValidation.content_version_id == version_id)
                .order_by(ContentValidation.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def history(self, item_id: uuid.UUID) -> list[ContentValidation]:
        return list(
            (
                await self.session.execute(
                    select(ContentValidation)
                    .where(ContentValidation.content_item_id == item_id)
                    .order_by(ContentValidation.created_at)
                )
            )
            .scalars()
            .all()
        )


def _validate_time(value: str, *, field: str) -> str:
    try:
        parsed = datetime.strptime(value, "%H:%M")
    except (TypeError, ValueError) as exc:
        raise ValidationProblem(f"{field} value '{value}' must be HH:MM (24h).") from exc
    return parsed.strftime("%H:%M")


def week_bounds(week_start: date) -> tuple[date, date]:
    return week_start, week_start + timedelta(days=6)


def default_week_start(today: date | None = None) -> date:
    """Next Monday, so "prepare next week's content" does the obvious thing."""
    today = today or datetime.now(timezone.utc).date()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)
