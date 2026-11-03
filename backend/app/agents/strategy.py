"""Strategy Agent — turns evidence into a founder-reviewable marketing strategy.

Phase 1 behaviour (a campaign plan: pillars, channel mix, content slots, metrics)
is preserved as ``mode="campaign"``. Phase 2 adds ``mode="strategy"``, which
produces the full strategic package the brief requires and persists it as an
immutable, approvable version:

    positioning recommendations · audience segmentation · differentiation ·
    content pillars · campaign concepts · per-platform strategies ·
    weekly objectives · publishing frequency · lead generation ·
    growth experiments · KPIs and a measurement plan

Two rules are structural rather than advisory:

* **Every strategy maps to at least one business objective.** The output cannot
  be constructed without a non-empty ``business_objectives`` list, and the
  service refuses to persist a version without one.
* **Developer acquisition beats vanity.** KPIs are validated against a
  blocklist of follower/like/impression-only metrics; a strategy whose KPIs are
  pure reach is reported as misaligned rather than quietly accepted.

The agent proposes. It never publishes, never spends, never edits a live
account. A founder must approve the exact version before anything is executed.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agents.base import AgentContext, AgentPayload, AgentResult, BaseAgent
from app.agents.limits import ExecutionGuard, ExecutionLimits, classify_failure
from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.brand import MarketingGoal
from app.models.research import BUSINESS_OBJECTIVES, ResearchFinding
from app.models.social import Platform
from app.models.strategy import StrategyStatus
from app.services.brand import BrandService
from app.services.strategy import StrategyService

logger = get_logger("crusor.agents.strategy")


# --------------------------------------------------------------------------- #
# Phase 1 schemas (unchanged)
# --------------------------------------------------------------------------- #
class PlannedContent(BaseModel):
    platform: str
    format: str = "post"
    pillar: str = ""
    angle: str
    hook: str = ""


class StrategyInput(AgentPayload):
    research_summary: str = ""
    audience_segments: list[str] = Field(default_factory=list)
    positioning_gaps: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    duration_days: int = 14

    # ---- Phase 2 -------------------------------------------------------
    mode: str = Field(default="campaign", description="campaign | strategy")
    research_project_id: uuid.UUID | None = None
    business_objectives: list[str] = Field(default_factory=list)
    primary_objective_type: str = "developer_acquisition"
    name: str = ""
    max_cost_usd: float | None = None


class StrategyOutput(AgentResult):
    generation_mode: str = "deterministic"
    data_source: str = "brand_profile"
    campaign_name: str = ""
    objective: str = ""
    messaging_pillars: list[str] = Field(default_factory=list)
    channel_mix: list[str] = Field(default_factory=list)
    content_plan: list[PlannedContent] = Field(default_factory=list)
    success_metrics: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class _LLMStrategy(BaseModel):
    campaign_name: str = ""
    objective: str = ""
    messaging_pillars: list[str] = Field(default_factory=list)
    content_plan: list[PlannedContent] = Field(default_factory=list)
    success_metrics: list[str] = Field(default_factory=list)
    summary: str = ""


# --------------------------------------------------------------------------- #
# Phase 2 schemas
# --------------------------------------------------------------------------- #
class StrategyPlanOutput(StrategyOutput):
    """Full strategic package plus the references needed for approval."""

    strategy_id: uuid.UUID | None = None
    strategy_version_id: uuid.UUID | None = None
    version_number: int = 0
    strategy_status: str = ""
    business_objectives: list[str] = Field(default_factory=list)
    objective_alignment: dict[str, Any] = Field(default_factory=dict)
    positioning_recommendations: list[dict[str, Any]] = Field(default_factory=list)
    audience_segmentation: list[dict[str, Any]] = Field(default_factory=list)
    competitor_differentiation: list[dict[str, Any]] = Field(default_factory=list)
    content_pillars: list[dict[str, Any]] = Field(default_factory=list)
    campaign_concepts: list[dict[str, Any]] = Field(default_factory=list)
    platform_strategies: list[dict[str, Any]] = Field(default_factory=list)
    weekly_objectives: list[dict[str, Any]] = Field(default_factory=list)
    publishing_frequency: dict[str, Any] = Field(default_factory=dict)
    lead_generation: list[dict[str, Any]] = Field(default_factory=list)
    growth_experiments: list[dict[str, Any]] = Field(default_factory=list)
    kpis: list[dict[str, Any]] = Field(default_factory=list)
    measurement_plan: dict[str, Any] = Field(default_factory=dict)
    brand_check: dict[str, Any] = Field(default_factory=dict)
    missing_information: list[dict[str, Any]] = Field(default_factory=list)
    evidence_used: list[dict[str, Any]] = Field(default_factory=list)
    unverified_claims_avoided: list[str] = Field(default_factory=list)
    cost: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    stop_reason: str = ""


class _LLMPlan(BaseModel):
    """Model output for a strategy. Every field is optional; code fills gaps.

    A missing field degrades to the deterministic baseline rather than producing
    an empty strategy, and the model cannot introduce a KPI or objective outside
    the approved vocabulary because validation runs afterwards.
    """

    name: str = ""
    objective: str = ""
    positioning_recommendations: list[dict[str, Any]] = Field(default_factory=list)
    audience_segmentation: list[dict[str, Any]] = Field(default_factory=list)
    competitor_differentiation: list[dict[str, Any]] = Field(default_factory=list)
    content_pillars: list[dict[str, Any]] = Field(default_factory=list)
    campaign_concepts: list[dict[str, Any]] = Field(default_factory=list)
    platform_strategies: list[dict[str, Any]] = Field(default_factory=list)
    weekly_objectives: list[dict[str, Any]] = Field(default_factory=list)
    lead_generation: list[dict[str, Any]] = Field(default_factory=list)
    growth_experiments: list[dict[str, Any]] = Field(default_factory=list)
    kpis: list[dict[str, Any]] = Field(default_factory=list)
    business_objectives: list[str] = Field(default_factory=list)
    rationale: str = ""
    summary: str = ""


DEFAULT_FORMAT = {
    Platform.LINKEDIN: "text_post",
    Platform.X: "thread",
    Platform.INSTAGRAM: "carousel",
    Platform.REDDIT: "discussion_post",
    Platform.YOUTUBE: "short",
    Platform.FACEBOOK: "text_post",
}

# KPIs that measure attention rather than acquisition. A strategy may still track
# them, but not as its only measurement.
VANITY_METRIC_TOKENS = (
    "follower",
    "followers",
    "likes",
    "impressions",
    "reach",
    "views",
    "engagement rate",
    "virality",
    "shares",
)

ACQUISITION_METRIC_TOKENS = (
    "signup",
    "sign-up",
    "registration",
    "waitlist",
    "install",
    "activation",
    "trial",
    "conversion",
    "qualified",
    "demo",
    "trial start",
    "repository",
    "download",
    "lead",
)

# Placeholder analytics are impossible when a connector is absent, so the plan
# says what will be measured and with what instrumentation — not fake numbers.
MEASUREMENT_DISCLAIMER = (
    "No analytics or social connector is configured in this deployment. Every KPI "
    "below is a metric definition and a proposed instrumentation source, not a "
    "measured value. Nothing here is a baseline or a target derived from real data."
)


def _is_vanity(metric: str) -> bool:
    lowered = metric.lower()
    return any(token in lowered for token in VANITY_METRIC_TOKENS)


def _is_acquisition(metric: str) -> bool:
    lowered = metric.lower()
    return any(token in lowered for token in ACQUISITION_METRIC_TOKENS)


class StrategyAgent(BaseAgent[StrategyInput, StrategyOutput]):
    name = "strategy"
    description = "Builds the campaign plan and the full strategic package."
    responsibilities = [
        "Translate research into a campaign objective and messaging pillars",
        "Choose a channel mix constrained to supported platforms",
        "Map every recommendation to a business objective",
        "Define measurable developer-acquisition KPIs, not vanity metrics",
        "Surface missing information instead of inventing it",
    ]
    input_schema = StrategyInput
    output_schema = StrategyOutput
    max_tokens = 1600
    max_attempts = 2

    async def execute(self, ctx: AgentContext, payload: StrategyInput) -> StrategyOutput:
        if payload.mode == "strategy":
            return await self._strategy_run(ctx, payload)
        return await self._campaign_run(ctx, payload)

    # ------------------------------------------------ Phase 1: campaign path
    async def _campaign_run(self, ctx: AgentContext, payload: StrategyInput) -> StrategyOutput:
        if ctx.brand is None:
            return StrategyOutput(
                summary="No brand profile; strategy cannot be grounded.",
                data_source="none",
                confidence=0.0,
                warnings=["No active brand profile."],
            )

        brand = ctx.brand
        pillars = await BrandService(ctx.session).list_pillars(brand.id)
        pillar_names = [p.name for p in pillars] or ["Product", "Developer stories", "Education"]

        requested = [p for p in payload.platforms if p in Platform.values()]
        channel_mix = requested or [p.value for p in Platform][:3]

        slots: list[PlannedContent] = []
        per_channel = max(1, payload.duration_days // max(len(channel_mix), 1))
        for channel in channel_mix:
            platform = Platform(channel)
            for index in range(min(per_channel, 4)):
                pillar = pillar_names[index % len(pillar_names)]
                slots.append(
                    PlannedContent(
                        platform=channel,
                        format=DEFAULT_FORMAT.get(platform, "post"),
                        pillar=pillar,
                        angle=f"{pillar}: {brand.tagline}" if index == 0 else f"{pillar} deep-dive",
                        hook=f"{brand.product} for {brand.audience[0] if brand.audience else 'developers'}",
                    )
                )

        output = StrategyOutput(
            summary=(
                f"Planned {len(slots)} content slot(s) across {len(channel_mix)} channel(s) "
                f"over {payload.duration_days} day(s)."
            ),
            campaign_name=f"{brand.company} — {pillar_names[0]} launch",
            objective=(
                payload.research_summary
                or f"Build awareness of {brand.product} among {', '.join(brand.audience[:2])}."
            ),
            messaging_pillars=pillar_names,
            channel_mix=channel_mix,
            content_plan=slots,
            success_metrics=[
                "Content approval turnaround (hours)",
                "Publishing cadence adherence (%)",
                "Engagement rate (measured, once connectors are live)",
            ],
            risks=[
                "No analytics connector configured: performance cannot be measured yet.",
                "All claims must stay within the brand's approved factual envelope.",
            ],
            confidence=0.6,
            data_source="brand_profile+pillars",
        )

        llm_out, used_llm = await self.try_llm(
            ctx,
            system_prompt=(
                "You are the Strategy Agent for a premium developer-tool brand. Build a "
                "focused campaign plan using only supplied brand facts. No invented "
                "metrics, pricing or customer claims. Return JSON per the schema."
            ),
            user_prompt=(
                f"Brand: {brand.company}/{brand.product}; tagline '{brand.tagline}'; "
                f"personality {brand.personality}; audience {brand.audience}; "
                f"pillars {pillar_names}; allowed platforms {[p.value for p in Platform]}; "
                f"research: {payload.research_summary or payload.request}. "
                f"Duration: {payload.duration_days} days."
            ),
            schema=_LLMStrategy,
            schema_name="strategy_output",
            purpose="strategy",
            task_class="strategy",
        )
        if used_llm and llm_out.content_plan:
            output.campaign_name = llm_out.campaign_name or output.campaign_name
            output.objective = llm_out.objective or output.objective
            output.messaging_pillars = llm_out.messaging_pillars or output.messaging_pillars
            output.content_plan = [
                s for s in llm_out.content_plan if s.platform in Platform.values()
            ] or output.content_plan
            output.success_metrics = llm_out.success_metrics or output.success_metrics
            output.summary = llm_out.summary or output.summary
            output.generation_mode = "llm"
            output.confidence = 0.76
        return output

    # ------------------------------------------------ Phase 2: strategy path
    async def _strategy_run(self, ctx: AgentContext, payload: StrategyInput) -> StrategyOutput:
        if ctx.brand is None:
            return StrategyPlanOutput(
                summary="No brand profile; strategy cannot be grounded.",
                data_source="none",
                confidence=0.0,
                warnings=["No active brand profile."],
            )

        settings = get_settings()
        brand = ctx.brand
        brand_service = BrandService(ctx.session)
        strategy_service = StrategyService(ctx.session)

        limits = ExecutionLimits.for_strategy(settings)
        if payload.max_cost_usd is not None:
            # Phase 2.6: a provided envelope is authoritative, including 0.0,
            # which now blocks all paid work instead of meaning "unlimited".
            limits.max_cost_usd = max(0.0, float(payload.max_cost_usd))
        # The Director may hand this run a budget it already owns, so that
        # research and strategy draw down one ledger rather than two.
        shared_budget = (getattr(ctx, "extra", {}) or {}).get("budget_guard")
        guard = ExecutionGuard(limits=limits, shared_budget=shared_budget)

        objectives = payload.business_objectives or ["developer_acquisition"]
        invalid_objectives = [o for o in objectives if o not in BUSINESS_OBJECTIVES]
        if invalid_objectives:
            return StrategyPlanOutput(
                summary="Strategy request rejected: unknown business objective.",
                data_source="none",
                confidence=0.0,
                warnings=[
                    f"Unknown business objective(s): {', '.join(invalid_objectives)}. "
                    f"Allowed: {', '.join(BUSINESS_OBJECTIVES)}"
                ],
            )

        # ---- evidence ---------------------------------------------------
        evidence = await self._gather_evidence(ctx, payload)
        findings: list[ResearchFinding] = evidence["findings"]
        warnings: list[str] = list(evidence["warnings"])
        missing_information: list[dict[str, Any]] = list(evidence["missing_information"])

        goals: list[MarketingGoal] = await brand_service.list_goals(brand.id)
        pillars = await brand_service.list_pillars(brand.id)
        competitors = await brand_service.list_competitors(brand.id)

        # ---- deterministic baseline --------------------------------------
        guard.begin_iteration()
        guard.note_tool_call("strategy:baseline")

        baseline = self._baseline_plan(
            brand=brand,
            goals=goals,
            pillars=pillars,
            competitors=competitors,
            findings=findings,
            objectives=objectives,
            payload=payload,
        )

        # ---- model refinement --------------------------------------------
        plan = baseline
        used_llm = False
        synthesis_summary = ""
        try:
            guard.begin_iteration()
            guard.note_tool_call("strategy:refine")
            evidence_block = "\n".join(
                f"[{i}] ({f.category}, {f.verification_status}, confidence "
                f"{f.confidence:.2f}) {f.finding}"
                + (f" <{f.source_url}>" if f.source_url else " (no source)")
                for i, f in enumerate(findings[:40])
            ) or "NO RESEARCH EVIDENCE AVAILABLE."

            llm_out, used_llm = await self.try_llm(
                ctx,
                system_prompt=(
                    "You are the Strategy Agent for a premium developer-tool brand. "
                    "Build a marketing strategy using ONLY the evidence and brand facts "
                    "provided. Never invent features, pricing, integrations, performance "
                    "claims, testimonials, market sizes or competitor facts. Never "
                    "optimise for likes or followers: prioritise measurable developer "
                    "acquisition. Every recommendation must map to one of the allowed "
                    "business objectives. Where the evidence is insufficient, put the "
                    "gap in missing_information instead of guessing. Return JSON."
                ),
                user_prompt=(
                    f"Brand: {brand.company} / {brand.product}\n"
                    f"Tagline: {brand.tagline}\n"
                    f"Positioning: {brand.positioning}\n"
                    f"Audience: {brand.audience}\n"
                    f"Tone: {brand.tone_of_voice}\n"
                    f"Never claim: {brand.do_not_claim}\n"
                    f"Marketing goals: {[g.objective for g in goals]}\n"
                    f"Competitors: {[c.name for c in competitors]}\n"
                    f"Allowed business objectives: {list(BUSINESS_OBJECTIVES)}\n"
                    f"Requested objectives: {objectives}\n"
                    f"Platforms: {[p.value for p in Platform]}\n"
                    f"Campaign duration days: {payload.duration_days}\n"
                    f"Research evidence:\n{evidence_block}"
                ),
                schema=_LLMPlan,
                schema_name="strategy_plan",
                purpose="strategy",
                task_class="strategic_decision",
                budget=guard.budget,
                cancel=guard.cancel,
            )
            if used_llm:
                plan = self._merge_llm_plan(baseline, llm_out, objectives)
                synthesis_summary = (llm_out.summary or "").strip()
        except Exception as exc:  # noqa: BLE001 - model failure must not lose the plan
            from app.core.errors import BudgetExhaustedError, CancelledProblem

            if isinstance(exc, (BudgetExhaustedError, CancelledProblem)):
                raise
            logger.warning(
                "strategy_llm_refinement_failed", error=type(exc).__name__
            )
            warnings.append(
                "Model refinement failed or is unavailable; the deterministic plan is "
                "shown unmodified. It was not refined for tactical sharpness."
            )

        # ---- brand consistency check -------------------------------------
        # The name is founder- or model-supplied display text, so it is checked
        # alongside the plan body rather than trusted separately.
        effective_name = (
            payload.name or plan.campaign_name or f"{brand.company} growth strategy"
        )[:300]

        guard.begin_iteration()
        guard.note_tool_call("strategy:brand_check")
        brand_check = self._brand_check(brand=brand, plan=plan, extra_text=effective_name)
        plan_dict = plan.model_dump(mode="json")
        warnings.extend(brand_check["warnings"])

        # ---- objective alignment + honesty -------------------------------
        alignment = self._objective_alignment(plan_dict, objectives)
        if alignment["advisory_only"]:
            warnings.append(
                "KPIs are reach/engagement only. This strategy is flagged as optimising "
                "for attention rather than developer acquisition."
            )

        confidence = 0.72 if used_llm and findings else 0.55 if findings else 0.35
        if not findings:
            warnings.append(
                "No research evidence was available; this strategy is derived from brand "
                "knowledge alone and has not been market-validated."
            )
            missing_information.append(
                {
                    "area": "research_evidence",
                    "detail": "Run a research job before treating this strategy as validated.",
                    "blocks_decision": True,
                }
            )

        # ---- persist -----------------------------------------------------
        try:
            guard.begin_iteration()
            guard.note_tool_call("strategy:persist")
            version_payload = {
                **{k: plan_dict.get(k) for k in (
                    "positioning_recommendations",
                    "audience_segmentation",
                    "competitor_differentiation",
                    "content_pillars",
                    "campaign_concepts",
                    "platform_strategies",
                    "weekly_objectives",
                    "publishing_frequency",
                    "lead_generation",
                    "growth_experiments",
                    "kpis",
                    "measurement_plan",
                )},
                "business_objectives": objectives,
                "objective_alignment": alignment,
                "research_summary": payload.research_summary
                or synthesis_summary
                or evidence["summary"],
                "rationale": plan_dict.get("rationale", ""),
                "source_finding_ids": [str(f.id) for f in findings[:100]],
                "brand_check": brand_check,
                "missing_information": missing_information,
                "warnings": warnings,
            }

            name = effective_name

            if payload.research_project_id is not None and payload.name:
                # An explicitly named re-run becomes a new version of the same
                # strategy rather than a second strategy with the same name.
                existing = await self._find_strategy_by_name(ctx.session, brand.id, name)
            else:
                existing = None

            if existing is not None:
                version = await strategy_service.add_version(
                    existing,
                    payload=version_payload,
                    change_note="Strategy agent re-run",
                    created_by=ctx.actor_id,
                    created_by_agent=self.name,
                    generation_mode=plan.generation_mode,
                    confidence=confidence,
                    cost_usd=guard.budget.spent_usd,
                    tokens_used=guard.budget.tokens_used,
                )
                strategy = existing
                await ctx.session.refresh(strategy)
            else:
                strategy, version = await strategy_service.create_strategy(
                    brand_profile_id=brand.id,
                    name=name,
                    objective=plan.objective,
                    payload=version_payload,
                    primary_objective_type=payload.primary_objective_type,
                    research_project_id=payload.research_project_id,
                    workflow_thread_id=ctx.workflow_thread_id,
                    created_by=ctx.actor_id,
                    created_by_agent=self.name,
                    generation_mode=plan.generation_mode,
                    confidence=confidence,
                    cost_usd=guard.budget.spent_usd,
                    tokens_used=guard.budget.tokens_used,
                    change_note="Initial strategy",
                )

        except Exception as exc:  # noqa: BLE001 - bounded run boundary
            status_name, error_code, message = classify_failure(exc)
            logger.warning("strategy_run_stopped", status=status_name, error=error_code)
            return StrategyPlanOutput(
                summary=message,
                data_source="none",
                confidence=0.0,
                warnings=[message],
                business_objectives=objectives,
                cost={"spent_usd": round(guard.budget.spent_usd, 6)},
                limits=guard.snapshot(),
                stop_reason=guard.stop_reason or error_code,
            )

        return StrategyPlanOutput(
            summary=(
                synthesis_summary
                or f"Strategy '{strategy.name}' v{version.version_number} drafted from "
                f"{len(findings)} research finding(s). Awaiting founder approval."
            ),
            generation_mode=plan.generation_mode,
            data_source=(
                f"research({len(findings)} findings)+brand_profile"
                if findings
                else "brand_profile"
            ),
            confidence=confidence,
            warnings=warnings,
            requires_human_review=True,
            campaign_name=plan.campaign_name,
            objective=plan.objective,
            messaging_pillars=[p.get("name", "") for p in plan_dict["content_pillars"]][:10],
            channel_mix=[p.get("platform", "") for p in plan_dict["platform_strategies"]][:10],
            content_plan=plan.content_plan,
            success_metrics=[k.get("name", "") for k in plan_dict["kpis"]][:20],
            risks=plan.risks,
            strategy_id=strategy.id,
            strategy_version_id=version.id,
            version_number=version.version_number,
            strategy_status=strategy.status,
            business_objectives=objectives,
            objective_alignment=alignment,
            positioning_recommendations=plan_dict["positioning_recommendations"],
            audience_segmentation=plan_dict["audience_segmentation"],
            competitor_differentiation=plan_dict["competitor_differentiation"],
            content_pillars=plan_dict["content_pillars"],
            campaign_concepts=plan_dict["campaign_concepts"],
            platform_strategies=plan_dict["platform_strategies"],
            weekly_objectives=plan_dict["weekly_objectives"],
            publishing_frequency=plan_dict["publishing_frequency"],
            lead_generation=plan_dict["lead_generation"],
            growth_experiments=plan_dict["growth_experiments"],
            kpis=plan_dict["kpis"],
            measurement_plan=plan_dict["measurement_plan"],
            brand_check=brand_check,
            missing_information=missing_information,
            evidence_used=[
                {
                    "id": str(f.id),
                    "category": f.category,
                    "finding": f.finding,
                    "confidence": f.confidence,
                    "verification_status": f.verification_status,
                    "source_url": f.source_url,
                }
                for f in findings[:40]
            ],
            unverified_claims_avoided=brand_check.get("unverified_claims_avoided", []),
            cost={
                "spent_usd": round(guard.budget.spent_usd, 6),
                "max_cost_usd": guard.limits.max_cost_usd,
                "tokens_used": guard.budget.tokens_used,
                "tier_breakdown": "reasoning tier for plan, auxiliary for extraction",
            },
            limits=guard.snapshot(),
        )

    # ------------------------------------------------------------- evidence
    async def _gather_evidence(
        self, ctx: AgentContext, payload: StrategyInput
    ) -> dict[str, Any]:
        """Collect the findings this strategy is allowed to reason over."""
        brand = ctx.brand
        findings: list[ResearchFinding] = []
        warnings: list[str] = []
        missing: list[dict[str, Any]] = []
        summary = payload.research_summary

        stmt = select(ResearchFinding).where(ResearchFinding.brand_profile_id == brand.id)
        if payload.research_project_id is not None:
            stmt = stmt.where(
                ResearchFinding.research_project_id == payload.research_project_id
            )
        stmt = stmt.order_by(
            ResearchFinding.confidence.desc(), ResearchFinding.created_at.desc()
        ).limit(120)
        findings = list((await ctx.session.execute(stmt)).scalars().all())

        if not findings:
            missing.append(
                {
                    "area": "market_evidence",
                    "detail": "No research findings exist for this brand yet.",
                    "blocks_decision": False,
                }
            )
        else:
            uncited = [f for f in findings if not f.source_url]
            if uncited:
                warnings.append(
                    f"{len(uncited)} of {len(findings)} findings have no source URL and are "
                    "treated as unverified; they inform hypotheses only."
                )
            verified = [f for f in findings if f.verification_status == "verified"]
            if not verified:
                warnings.append(
                    "No verified findings are available. Every recommendation is therefore "
                    "a hypothesis pending founder confirmation."
                )
            if not summary:
                summary = (
                    f"{len(findings)} finding(s) across "
                    f"{len({f.category for f in findings})} categories."
                )
        return {
            "findings": findings,
            "warnings": warnings,
            "missing_information": missing,
            "summary": summary,
        }

    # ------------------------------------------------------------- baseline
    def _baseline_plan(
        self,
        *,
        brand: Any,
        goals: list[MarketingGoal],
        pillars: list[Any],
        competitors: list[Any],
        findings: list[ResearchFinding],
        objectives: list[str],
        payload: StrategyInput,
    ) -> StrategyPlanOutput:
        """A complete, evidence-grounded plan that needs no model to be valid."""
        audience = list(brand.audience or [])
        pillar_names = [p.name for p in pillars] or [
            "Build & ship faster",
            "Developer education",
            "Proof of work",
        ]
        platform_names = [p.value for p in Platform]

        positioning_gaps = [
            f.category
            for f in findings
            if f.verification_status != "verified"
        ]
        gap_categories = sorted({f.category for f in findings if not f.source_url})

        positioning = [
            {
                "statement": brand.positioning or brand.tagline,
                "rationale": "Derived from the founder-maintained brand positioning.",
                "evidence": "brand_profile.positioning",
                "confidence": 0.8 if brand.positioning else 0.3,
            },
            {
                "statement": (
                    f"Lead with '{brand.tagline}' for developers who want to ship faster."
                    if brand.tagline
                    else "Lead with the product's build-and-ship promise."
                ),
                "rationale": "Positions on the stated tagline without adding new claims.",
                "evidence": "brand_profile.tagline",
                "confidence": 0.7 if brand.tagline else 0.3,
            },
        ]
        if gap_categories:
            positioning.append(
                {
                    "statement": (
                        "Do not claim competitive superiority yet: evidence for "
                        f"{', '.join(gap_categories[:3])} is uncited."
                    ),
                    "rationale": "Provenance check found no citable source for these areas.",
                    "evidence": "research_findings.verification_status",
                    "confidence": 0.6,
                }
            )

        segments = [
            {
                "segment": segment,
                "priority": index + 1,
                "why": "Founder-stated audience segment.",
                "acquisition_motion": "Developer-facing content plus a direct signup path.",
                "evidence": "brand_profile.audience",
                "verified": False,
            }
            for index, segment in enumerate(audience[:6])
        ]

        differentiation = [
            {
                "competitor": competitor.name,
                "differentiation": (
                    f"Compete on {pillar_names[0]} rather than feature parity with "
                    f"{competitor.name}."
                ),
                "evidence": "brand_profile.competitors",
                "verified": bool(getattr(competitor, "website", "")),
            }
            for competitor in competitors[:6]
        ]

        content_pillars = [
            {
                "name": name,
                "purpose": "Educate and demonstrate, not advertise.",
                "formats": ["deep-dive post", "short demo", "build log"],
                "objective": objectives[0] if objectives else "developer_acquisition",
            }
            for name in pillar_names[:5]
        ]

        campaign_concepts = [
            {
                "name": f"{brand.company} — build in public",
                "concept": (
                    "Ship a real, verifiable build log each week: problem, approach, "
                    "outcome. No invented benchmarks."
                ),
                "objective": "developer_acquisition",
                "platforms": ["linkedin", "x", "youtube"],
            },
            {
                "name": f"{brand.company} — developer Q&A series",
                "concept": (
                    "Answer recurring developer questions with short, practical demos "
                    "that end in a signup path."
                ),
                "objective": objectives[0] if objectives else "community_growth",
                "platforms": ["youtube", "reddit"],
            },
        ]

        platform_strategies = [
            {
                "platform": platform,
                "role": "Primary developer channel"
                if platform in ("linkedin", "x")
                else "Supporting channel",
                "frequency": "3 posts/week" if platform in ("linkedin", "x") else "1 post/week",
                "format": DEFAULT_FORMAT.get(Platform(platform), "post"),
                "cta": "Start a free trial via the product site.",
                "objective": objectives[0] if objectives else "developer_acquisition",
            }
            for platform in platform_names[:4]
        ]

        weekly_objectives = [
            {
                "week": week,
                "focus": pillar_names[(week - 1) % len(pillar_names)],
                "objective": objectives[0] if objectives else "developer_acquisition",
                "deliverable": f"Week {week} content set: 1 deep-dive, 2 short posts.",
                "success_signal": "Signup link clicks (instrumented), not impressions.",
            }
            for week in range(1, 5)
        ]

        publishing_frequency = {
            "cadence": "3 posts/week on the primary channel, 1/week on supporting channels",
            "per_platform": {
                row["platform"]: row["frequency"] for row in platform_strategies
            },
            "note": "Cadence is a plan, not a measured historical rate.",
        }

        lead_generation = [
            {
                "mechanism": "Developer signup path from every piece of content",
                "detail": "Every post ends in a single, unambiguous next step.",
                "metric": "signup_starts",
                "tracking": "Untracked until a web analytics connector is configured.",
            },
            {
                "mechanism": "Waitlist capture for unreleased capabilities",
                "detail": "Only for capabilities the founder has confirmed.",
                "metric": "waitlist_registrations",
                "tracking": "Requires a waitlist form; not verified in this deployment.",
            },
        ]

        growth_experiments = [
            {
                "name": "Build-log signup attribution",
                "hypothesis": (
                    "Weekly build logs with a single signup CTA convert better than "
                    "feature announcement posts."
                ),
                "channel": "linkedin",
                "objective_type": "developer_acquisition",
                "primary_metric": "signup_starts",
                "expected_effort": "medium",
            },
            {
                "name": "Developer Q&A short-form test",
                "hypothesis": "Answering real questions lowers the barrier to trial.",
                "channel": "youtube",
                "objective_type": objectives[0] if objectives else "community_growth",
                "primary_metric": "trial_starts",
                "expected_effort": "high",
            },
        ]

        kpis = [
            {
                "name": "signup_starts",
                "definition": "Sessions that begin the signup flow from a content link.",
                "source": "web analytics (not configured)",
                "objective": "developer_acquisition",
                "is_vanity": False,
            },
            {
                "name": "waitlist_registrations",
                "definition": "Completed waitlist submissions.",
                "source": "waitlist form (not configured)",
                "objective": "waitlist_registrations",
                "is_vanity": False,
            },
            {
                "name": "activation_rate",
                "definition": "Percentage of new accounts that complete the first project.",
                "source": "product analytics (not configured)",
                "objective": "product_adoption",
                "is_vanity": False,
            },
            {
                "name": "qualified_developer_conversations",
                "definition": "Counted replies or DMs that express concrete intent.",
                "source": "manual log (spreadsheet)",
                "objective": "community_growth",
                "is_vanity": False,
            },
        ]

        measurement_plan = {
            "disclaimer": MEASUREMENT_DISCLAIMER,
            "instrumentation_required": [
                "Web analytics for signup attribution",
                "A waitlist form with a stored submission count",
                "Product analytics for activation",
            ],
            "review_cadence": "Weekly review of signup starts against content shipped.",
            "targets": "None set: no baseline exists, and inventing one would be dishonest.",
            "vanity_metrics_policy": "Tracked only as context, never as the success criterion.",
        }

        return StrategyPlanOutput(
            summary="Deterministic strategy baseline.",
            generation_mode="deterministic",
            data_source="brand_profile+research",
            campaign_name=f"{brand.company} — developer growth",
            objective=payload.research_summary
            or (goals[0].objective if goals else f"Acquire developers for {brand.product}"),
            messaging_pillars=pillar_names[:5],
            channel_mix=platform_names[:4],
            content_plan=[],
            success_metrics=[k["name"] for k in kpis],
            risks=[
                "No analytics connector: KPIs are definitions, not measurements.",
                "Claims must remain inside the brand's confirmed factual envelope.",
                "Competitive claims are unsupported where research is uncited.",
            ],
            positioning_recommendations=positioning,
            audience_segmentation=segments,
            competitor_differentiation=differentiation,
            content_pillars=content_pillars,
            campaign_concepts=campaign_concepts,
            platform_strategies=platform_strategies,
            weekly_objectives=weekly_objectives,
            publishing_frequency=publishing_frequency,
            lead_generation=lead_generation,
            growth_experiments=growth_experiments,
            kpis=kpis,
            measurement_plan=measurement_plan,
            business_objectives=objectives,
            confidence=0.55 if findings else 0.4,
        )

    # ------------------------------------------------------------ merge plan
    def _merge_llm_plan(
        self, baseline: StrategyPlanOutput, llm: _LLMPlan, objectives: list[str]
    ) -> StrategyPlanOutput:
        """Model output fills in, but cannot hollow out, the deterministic plan."""
        merged = baseline.model_copy(deep=True)

        def take(field: str) -> None:
            value = getattr(llm, field)
            if value:
                setattr(merged, field, value)

        for field in (
            "positioning_recommendations",
            "audience_segmentation",
            "competitor_differentiation",
            "content_pillars",
            "campaign_concepts",
            "platform_strategies",
            "weekly_objectives",
            "lead_generation",
            "growth_experiments",
            "kpis",
        ):
            # Accept the model's richer structure only if it is non-empty and
            # structurally usable (list of objects with a name/statement).
            value = getattr(llm, field)
            if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
                cleaned = [v for v in value if any(k in v for k in ("name", "statement", "segment", "platform", "mechanism", "competitor", "week"))]
                if cleaned:
                    setattr(merged, field, cleaned)

        if llm.name:
            merged.campaign_name = llm.name
        if llm.objective:
            merged.objective = llm.objective
        if llm.rationale:
            merged.rationale = llm.rationale  # type: ignore[attr-defined]

        # Objectives are code-owned: a model cannot widen the approved vocabulary.
        merged.business_objectives = objectives
        merged.generation_mode = "llm"
        merged.confidence = 0.72
        return merged

    # ----------------------------------------------------------- brand check
    def _brand_check(
        self, *, brand: Any, plan: StrategyPlanOutput, extra_text: str = ""
    ) -> dict[str, Any]:
        """Detect strategy text that would force a claim the brand forbids."""
        blob = " ".join(
            [
                plan.objective,
                extra_text,
                plan.campaign_name,
                " ".join(plan.messaging_pillars),
                " ".join(str(c.get("concept", "")) for c in plan.campaign_concepts),
                " ".join(str(p.get("statement", "")) for p in plan.positioning_recommendations),
            ]
        ).lower()

        violations: list[str] = []
        for phrase in list(brand.prohibited_phrases or []) + list(brand.do_not_claim or []):
            phrase = str(phrase).strip()
            if phrase and phrase.lower() in blob:
                violations.append(phrase)

        # Digits in claims usually mean an invented statistic; flag for review.
        import re

        numerals = re.findall(r"\b\d+(?:\.\d+)?\s?(?:x|%|percent|k|m|million)\b", blob)
        unverified = sorted({n for n in numerals})

        warnings: list[str] = []
        if violations:
            warnings.append(
                "Brand consistency check found prohibited phrasing: "
                + ", ".join(violations[:5])
                + ". Remove before approval."
            )
        if unverified:
            warnings.append(
                "Strategy text contains quantitative claims ("
                + ", ".join(unverified[:5])
                + ") that have no cited source. They must be removed or sourced."
            )

        passed = not violations and not unverified
        return {
            "passed": passed,
            "violations": violations,
            "unverified_quantitative_claims": unverified,
            "checked_terms": len(list(brand.prohibited_phrases or []) + list(brand.do_not_claim or [])),
            "unverified_claims_avoided": (
                [str(x) for x in brand.do_not_claim or []][:20] + unverified
            ),
            "warnings": warnings,
        }

    # ------------------------------------------------------ objective alignment
    def _objective_alignment(
        self, plan: dict[str, Any], objectives: list[str]
    ) -> dict[str, Any]:
        """Prove each objective is served, and that metrics measure acquisition."""
        kpis = [k for k in plan.get("kpis") or [] if isinstance(k, dict)]
        experiments = [e for e in plan.get("growth_experiments") or [] if isinstance(e, dict)]
        pillars = [p for p in plan.get("content_pillars") or [] if isinstance(p, dict)]

        kpi_names = [str(k.get("name", "")) for k in kpis]
        vanity = [n for n in kpi_names if _is_vanity(n) and not _is_acquisition(n)]
        acquisition = [n for n in kpi_names if _is_acquisition(n)]

        by_objective: dict[str, list[str]] = {objective: [] for objective in objectives}
        for item in pillars + experiments + kpis:
            target = str(item.get("objective") or item.get("objective_type") or "")
            if target in by_objective:
                label = str(item.get("name") or item.get("segment") or "")
                if label:
                    by_objective[target].append(label)

        unmapped = [objective for objective, items in by_objective.items() if not items]
        return {
            "objectives": objectives,
            "map": by_objective,
            "unmapped_objectives": unmapped,
            "acquisition_kpis": acquisition,
            "vanity_kpis": vanity,
            "advisory_only": not acquisition,
            "primary_objective": objectives[0] if objectives else "",
            "note": (
                "Every objective must have at least one pillar, experiment or KPI that "
                "serves it; acquisition KPIs take precedence over reach metrics."
            ),
        }

    async def _find_strategy_by_name(
        self, session: Any, brand_profile_id: uuid.UUID, name: str
    ) -> Any:
        from app.models.strategy import MarketingStrategy

        return (
            await session.execute(
                select(MarketingStrategy).where(
                    MarketingStrategy.brand_profile_id == brand_profile_id,
                    MarketingStrategy.name == name[:300],
                )
            )
        ).scalars().first()


__all__ = [
    "PlannedContent",
    "StrategyAgent",
    "StrategyInput",
    "StrategyOutput",
    "StrategyPlanOutput",
    "VANITY_METRIC_TOKENS",
    "ACQUISITION_METRIC_TOKENS",
    "StrategyStatus",
]
