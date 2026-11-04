"""Research Agent — evidence-first market and developer research.

Phase 1 behaviour (audience insight derived from stored brand knowledge) is
preserved as the default path. Phase 2 adds an **evidence run**: the agent asks a
pluggable :class:`ResearchProvider` for findings across the nine research
categories, persists them with full provenance through
:class:`~app.services.research.ResearchService`, and reports exactly what it
found, what it could not verify, and what it never established.

Hard rules enforced here, not merely documented:

* a finding without a source URL can never be stored as verified;
* a category the provider could not answer becomes an explicit *gap*, never a
  plausible-sounding invention;
* every run is bounded by :class:`ExecutionGuard` (iterations, tool calls, wall
  clock, cost, tokens, loop detection);
* provider failure degrades with a typed, user-visible warning.
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
from app.models.brand import CompetitorProfile, MarketingGoal
from app.models.research import (
    RESEARCH_CATEGORIES,
    AudienceSegment,
    ProductFact,
    ResearchStatus,
    VerificationStatus,
)
from app.research.base import (
    RESEARCH_QUESTIONS,
    RawFinding,
    ResearchProvider,
    ResearchQuery,
    build_provider,
    research_status,
)
from app.services.brand import BrandService
from app.services.research import ResearchService

logger = get_logger("crusor.agents.research")


class AudienceInsight(BaseModel):
    segment: str
    motivation: list[str] = Field(default_factory=list)
    friction: list[str] = Field(default_factory=list)
    content_angle: str = ""


class ResearchInput(AgentPayload):
    request: str = ""
    platforms: list[str] = Field(default_factory=list)
    focus: str = "audience"

    # ---- Phase 2 -------------------------------------------------------
    # ``insight`` keeps the Phase 1 contract. ``evidence`` runs a provider-backed
    # research pass and persists findings.
    mode: str = Field(default="insight", description="insight | evidence")
    research_project_id: uuid.UUID | None = None
    categories: list[str] = Field(default_factory=list)
    max_cost_usd: float | None = None
    max_iterations: int | None = None


class FindingPreview(BaseModel):
    """A compact finding reference for the agent result payload."""

    category: str
    finding: str
    confidence: float
    verification_status: str
    source_url: str = ""
    has_source: bool = False


class ResearchOutput(AgentResult):
    generation_mode: str = "deterministic"
    data_source: str = "brand_profile"
    audience_insights: list[AudienceInsight] = Field(default_factory=list)
    positioning_gaps: list[str] = Field(default_factory=list)
    competitor_observations: list[str] = Field(default_factory=list)
    recommended_focus: list[str] = Field(default_factory=list)

    # ---- Phase 2 -------------------------------------------------------
    research_project_id: uuid.UUID | None = None
    provider: str = ""
    provider_status: str = ""
    findings_created: int = 0
    sources_created: int = 0
    findings: list[FindingPreview] = Field(default_factory=list)
    category_coverage: list[dict[str, Any]] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    cost: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    stop_reason: str = ""
    iterations: int = 0


class _LLMResearch(BaseModel):
    audience_insights: list[AudienceInsight] = Field(default_factory=list)
    positioning_gaps: list[str] = Field(default_factory=list)
    recommended_focus: list[str] = Field(default_factory=list)
    summary: str = ""


class _LLMSynthesis(BaseModel):
    """Structured synthesis of *already retrieved* findings.

    The model is explicitly constrained to the supplied findings; it may rank and
    summarise them but it cannot introduce new claims. Every item it returns is
    matched back to a stored finding, and unmatched items are discarded.
    """

    synthesis: str = ""
    priority_findings: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


FRICTION_BY_SEGMENT = {
    "software developers": ["context switching between tools", "slow feedback loops"],
    "students": ["learning curve vs. shipping pressure", "limited project time"],
    "engineering students": ["theory-to-practice gap", "portfolio deadlines"],
    "indie hackers": ["wearing every hat alone", "shipping velocity"],
    "startup founders": ["technical debt vs. speed", "small team bandwidth"],
    "ai enthusiasts": ["keeping up with model/tooling change"],
}


class ResearchAgent(BaseAgent[ResearchInput, ResearchOutput]):
    name = "research"
    description = "Produces structured, source-backed audience and market insight."
    responsibilities = [
        "Read brand profile, goals, competitor profiles and marketing memory",
        "Run pluggable research providers and persist findings with provenance",
        "Derive audience motivations and frictions without inventing statistics",
        "Identify positioning gaps and record unresolved questions as gaps",
        "Respect execution limits and report what was not researched",
    ]
    input_schema = ResearchInput
    output_schema = ResearchOutput
    max_tokens = 1200
    max_attempts = 2

    # ------------------------------------------------------------------ entry
    async def execute(self, ctx: AgentContext, payload: ResearchInput) -> ResearchOutput:
        if payload.mode == "evidence":
            return await self._evidence_run(ctx, payload)
        return await self._insight_run(ctx, payload)

    # ------------------------------------------------- Phase 1: insight path
    async def _insight_run(self, ctx: AgentContext, payload: ResearchInput) -> ResearchOutput:
        if ctx.brand is None:
            return ResearchOutput(
                summary="No brand profile available; research cannot be grounded.",
                data_source="none",
                confidence=0.0,
                warnings=["No active brand profile. Create one before running research."],
            )

        brand = ctx.brand
        service = BrandService(ctx.session)
        goals: list[MarketingGoal] = await service.list_goals(brand.id)
        competitors: list[CompetitorProfile] = await service.list_competitors(brand.id)
        pillars = await service.list_pillars(brand.id)

        warnings: list[str] = []
        if not goals:
            warnings.append("No marketing goals defined; research focus is unconstrained.")

        insights: list[AudienceInsight] = []
        for segment in brand.audience:
            angle = next(
                (p.name for p in pillars if segment.split()[0].lower() in p.description.lower()),
                "",
            )
            insights.append(
                AudienceInsight(
                    segment=segment,
                    motivation=["ship software faster", "reduce tooling overhead"],
                    friction=FRICTION_BY_SEGMENT.get(segment.lower(), []),
                    content_angle=angle or f"Show how {brand.product} helps {segment} ship.",
                )
            )

        gaps = [
            f"Positioning states '{brand.positioning}' but no proof-of-work content pillar exists."
            for _ in [0]
            if brand.positioning and not any("proof" in p.name.lower() for p in pillars)
        ]

        output = ResearchOutput(
            summary=(
                f"Analysed {len(insights)} audience segment(s) and {len(competitors)} "
                f"competitor profile(s) for {brand.company}."
            ),
            audience_insights=insights,
            positioning_gaps=gaps,
            competitor_observations=[
                f"{c.name}: {c.positioning}" for c in competitors if c.positioning
            ],
            recommended_focus=(
                [g.objective for g in goals[:3]]
                or [f"Establish {brand.tagline} as the developer-first promise."]
            ),
            confidence=0.62 if insights else 0.3,
            warnings=warnings,
            data_source="brand_profile+goals+competitors",
        )

        llm_out, used_llm = await self.try_llm(
            ctx,
            system_prompt=(
                "You are the Research Agent for a developer-tool brand. Use ONLY the "
                "provided brand facts. Never invent market size, customer counts, "
                "benchmarks or testimonials. Return compact JSON matching the schema."
            ),
            user_prompt=(
                f"Brand: {brand.company} / {brand.product}. Tagline: {brand.tagline}. "
                f"Positioning: {brand.positioning}. Audience: {brand.audience}. "
                f"Goals: {[g.objective for g in goals]}. "
                f"Competitors: {[c.name for c in competitors]}. "
                f"Request: {payload.request or 'general audience research'}."
            ),
            schema=_LLMResearch,
            schema_name="research_output",
            purpose="research",
            task_class="research_synthesis",
        )
        if used_llm and llm_out.audience_insights:
            output.audience_insights = llm_out.audience_insights
            output.positioning_gaps = llm_out.positioning_gaps or output.positioning_gaps
            output.recommended_focus = llm_out.recommended_focus or output.recommended_focus
            output.summary = llm_out.summary or output.summary
            output.generation_mode = "llm"
            output.confidence = 0.78
        return output

    # ------------------------------------------------ Phase 2: evidence path
    async def _evidence_run(self, ctx: AgentContext, payload: ResearchInput) -> ResearchOutput:
        if ctx.brand is None:
            return ResearchOutput(
                summary="No brand profile available; research cannot be grounded.",
                data_source="none",
                confidence=0.0,
                warnings=["No active brand profile. Create one before running research."],
            )

        settings = get_settings()
        brand = ctx.brand
        service = ResearchService(ctx.session)
        brand_service = BrandService(ctx.session)

        # ---- limits -----------------------------------------------------
        limits = ExecutionLimits.for_research(settings)
        if payload.max_cost_usd is not None:
            # Phase 2.6: a provided envelope is authoritative, including 0.0,
            # which now blocks all paid work instead of meaning "unlimited".
            limits.max_cost_usd = max(0.0, float(payload.max_cost_usd))
        # The Director may hand this run a budget it already owns, so that
        # research and strategy draw down one ledger rather than two.
        shared_budget = (getattr(ctx, "extra", {}) or {}).get("budget_guard")
        if payload.max_iterations is not None:
            limits.max_iterations = max(1, int(payload.max_iterations))
        guard = ExecutionGuard(limits=limits, shared_budget=shared_budget)

        categories = payload.categories or list(RESEARCH_CATEGORIES)
        unknown = [c for c in categories if c not in RESEARCH_CATEGORIES]
        if unknown:
            return ResearchOutput(
                summary="Research request rejected: unknown category.",
                data_source="none",
                confidence=0.0,
                warnings=[f"Unknown research categories: {', '.join(unknown)}"],
            )

        # ---- provider ---------------------------------------------------
        competitors: list[CompetitorProfile] = await brand_service.list_competitors(brand.id)
        facts: list[ProductFact] = list(
            (
                await ctx.session.execute(
                    select(ProductFact).where(ProductFact.brand_profile_id == brand.id)
                )
            )
            .scalars()
            .all()
        )
        segments: list[AudienceSegment] = list(
            (
                await ctx.session.execute(
                    select(AudienceSegment).where(AudienceSegment.brand_profile_id == brand.id)
                )
            )
            .scalars()
            .all()
        )
        provider: ResearchProvider = build_provider(
            settings,
            brand_profile=brand,
            product_facts=facts,
            competitors=competitors,
            segments=segments,
        )
        status = provider.status()

        # ---- project ----------------------------------------------------
        project = None
        if payload.research_project_id is not None:
            project = await service.get_project(payload.research_project_id)
        else:
            project = await service.create_project(
                brand_profile_id=brand.id,
                objective=payload.request or f"Research the market for {brand.company}",
                categories=categories,
                provider=provider.name,
                max_cost_usd=limits.max_cost_usd,
                estimated_cost_usd=0.0,
                workflow_thread_id=ctx.workflow_thread_id,
                created_by=ctx.actor_id,
            )

        warnings: list[str] = []
        gaps: list[str] = []
        previews: list[FindingPreview] = []

        if not status.get("available"):
            await service.finish_project(
                project,
                status=ResearchStatus.UNAVAILABLE.value,
                error_code="research_unavailable",
                error_message=status.get("note", "No research provider is available."),
            )
            return ResearchOutput(
                summary="Research could not run: no provider is available.",
                data_source="none",
                confidence=0.0,
                warnings=[status.get("note", "No research provider configured.")],
                research_project_id=project.id,
                provider=provider.name,
                provider_status=status.get("status", "unavailable"),
                gaps=categories,
                limits=guard.snapshot(),
            )

        project.provider = provider.name
        await service.mark_running(project)

        try:
            # One *round* covers every requested category; the outer loop is the
            # research/evaluate cycle. Retrying a round only makes sense against a
            # live provider — a deterministic one returns the same findings, so a
            # second pass would burn budget for nothing.
            live = bool(status.get("network"))
            pending = list(categories)
            round_number = 0

            while pending:
                round_number += 1
                guard.begin_iteration()
                round_gaps: list[str] = []

                for category in pending:
                    guard.note_tool_call(f"research:{category}")
                    guard.note_signature(f"{provider.name}:{category}:{round_number}")

                    query = ResearchQuery(
                        objective=project.objective,
                        categories=[category],
                        brand_company=brand.company,
                        brand_product=brand.product,
                        brand_domain="",
                        competitors=[c.name for c in competitors],
                        audiences=list(brand.audience or []),
                        max_sources=settings.research_max_sources,
                        existing_keys=set(),
                    )

                    try:
                        raw_findings = await provider.search(query)
                    except Exception as exc:  # noqa: BLE001 - provider boundary
                        logger.warning(
                            "research_provider_failed",
                            provider=provider.name,
                            category=category,
                            error=type(exc).__name__,
                        )
                        round_gaps.append(
                            f"{category}: provider failed ({type(exc).__name__})."
                        )
                        warnings.append(
                            f"Provider failed for '{category}'; that category is incomplete."
                        )
                        continue

                    created = await service.add_findings(
                        project,
                        [raw for raw in raw_findings if raw.category == category],
                        agent_run_id=ctx.run.id if ctx.run else None,
                    )
                    if not created:
                        round_gaps.append(
                            f"{category}: no verifiable findings "
                            f"({RESEARCH_QUESTIONS.get(category, '')})."
                        )
                    previews.extend(
                        FindingPreview(
                            category=row.category,
                            finding=row.finding,
                            confidence=row.confidence,
                            verification_status=row.verification_status,
                            source_url=row.source_url,
                            has_source=bool(row.source_url),
                        )
                        for row in created
                    )

                gaps.extend(round_gaps)

                # Evaluate: ask for more research only where it can help.
                retryable = [
                    gap.split(":", 1)[0]
                    for gap in round_gaps
                    if "provider failed" in gap
                ]
                can_retry = (
                    live
                    and retryable
                    and round_number < guard.limits.max_iterations
                    and not guard.budget.exhausted
                )
                if can_retry:
                    warnings.append(
                        f"Re-running {len(retryable)} categor"
                        f"{'y' if len(retryable) == 1 else 'ies'} after a provider failure "
                        f"(round {round_number + 1})."
                    )
                    pending = retryable
                    continue
                break

            # ---- synthesis over stored findings only --------------------
            synthesis = ""
            open_questions: list[str] = []
            if previews:
                guard.begin_iteration()
                evidence_block = "\n".join(
                    f"[{i}] ({p.category}, confidence {p.confidence:.2f}, "
                    f"{p.verification_status}) {p.finding}"
                    for i, p in enumerate(previews[:40])
                )
                llm_out, used_llm = await self.try_llm(
                    ctx,
                    system_prompt=(
                        "You synthesise research findings for a developer-tool brand. "
                        "Use ONLY the numbered findings supplied. Do not add facts, "
                        "statistics, competitor claims or citations that are not present. "
                        "If the evidence is thin, say so. Return JSON matching the schema."
                    ),
                    user_prompt=(
                        f"Objective: {project.objective}\n"
                        f"Brand: {brand.company} / {brand.product}\n"
                        f"Findings:\n{evidence_block}"
                    ),
                    schema=_LLMSynthesis,
                    schema_name="research_synthesis",
                    purpose="research_synthesis",
                    task_class="research_synthesis",
                    budget=guard.budget,
                    cancel=guard.cancel,
                )
                if used_llm:
                    synthesis = (llm_out.synthesis or "").strip()
                    open_questions = [q for q in llm_out.open_questions if q.strip()][:10]
                else:
                    warnings.append(
                        "Synthesis ran without a model provider; findings are stored "
                        "verbatim and are not summarised."
                    )

            # Budget is already updated by the gateway; nothing to add here.

            snapshot = guard.snapshot()
            coverage = await service.category_summary(project.id)
            pain_point_rows = [
                row
                for row in (
                    await service.list_findings(project_id=project.id, limit=500)
                )[0]
                if row.category == "developer_pain_points"
            ]
            await service.record_pain_points(project, pain_point_rows)

            summary = (
                synthesis
                or f"Collected {len(previews)} finding(s) across "
                f"{len(coverage)} categor{'y' if len(coverage) == 1 else 'ies'} "
                f"for {brand.company}."
            )

            await service.finish_project(
                project,
                status=ResearchStatus.SUCCEEDED.value,
                summary=summary,
                actual_cost_usd=guard.budget.spent_usd,
                tokens_used=guard.budget.tokens_used,
                tool_calls=guard.tool_calls,
                iterations=guard.iterations,
            )
            await ctx.session.refresh(project)

            confidence = (
                round(
                    sum(p.confidence for p in previews) / len(previews),
                    3,
                )
                if previews
                else 0.0
            )
            if gaps:
                warnings.append(
                    f"{len(gaps)} categor{'y' if len(gaps) == 1 else 'ies'} could not be "
                    "researched; they are recorded as gaps, not filled with assumptions."
                )

            return ResearchOutput(
                summary=summary,
                generation_mode="llm" if synthesis else "deterministic",
                data_source=f"research_provider:{provider.name}",
                confidence=confidence,
                warnings=warnings,
                research_project_id=project.id,
                provider=provider.name,
                provider_status=status.get("status", "available"),
                findings_created=project.finding_count,
                sources_created=project.source_count,
                findings=previews,
                category_coverage=coverage,
                gaps=gaps,
                unresolved_questions=open_questions,
                cost={
                    "spent_usd": round(guard.budget.spent_usd, 6),
                    "max_cost_usd": guard.limits.max_cost_usd,
                    "tokens_used": guard.budget.tokens_used,
                    "estimated_before_run_usd": project.estimated_cost_usd,
                },
                limits=snapshot,
                iterations=guard.iterations,
            )

        except Exception as exc:  # noqa: BLE001 - bounded run boundary
            status_name, error_code, message = classify_failure(exc)
            # Persist whatever was collected: a partial run is still evidence.
            project_status = {
                "budget_exhausted": ResearchStatus.BUDGET_EXCEEDED.value,
                "cancelled": ResearchStatus.CANCELLED.value,
                "partial": ResearchStatus.PARTIAL.value,
            }.get(status_name, ResearchStatus.FAILED.value)
            await service.finish_project(
                project,
                status=project_status,
                summary="; ".join(gaps)[:4000],
                error_code=error_code,
                error_message=message,
                actual_cost_usd=guard.budget.spent_usd,
                tokens_used=guard.budget.tokens_used,
                tool_calls=guard.tool_calls,
                iterations=guard.iterations,
            )
            logger.warning(
                "research_run_stopped",
                project=str(project.id),
                status=status_name,
                reason=guard.stop_reason,
            )
            return ResearchOutput(
                summary=message,
                data_source=f"research_provider:{provider.name}",
                confidence=0.0,
                warnings=[message],
                research_project_id=project.id,
                provider=provider.name,
                provider_status=status.get("status", "unknown"),
                findings_created=project.finding_count,
                sources_created=project.source_count,
                gaps=gaps,
                cost={
                    "spent_usd": round(guard.budget.spent_usd, 6),
                    "max_cost_usd": guard.limits.max_cost_usd,
                    "tokens_used": guard.budget.tokens_used,
                },
                limits=guard.snapshot(),
                stop_reason=guard.stop_reason or error_code,
                iterations=guard.iterations,
                requires_human_review=True,
            )


__all__ = [
    "AudienceInsight",
    "FindingPreview",
    "ResearchAgent",
    "ResearchInput",
    "ResearchOutput",
    "research_status",
    "VerificationStatus",
]
