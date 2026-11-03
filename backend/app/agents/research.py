"""Research Agent — turns brand context into structured audience/landscape insight.

Phase 1 scope: reasoning over **known** brand facts and the database (goals,
competitors, memory). It does not scrape or browse, and it must not invent market
statistics. Insights are labelled with their provenance.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents.base import AgentContext, AgentPayload, AgentResult, BaseAgent
from app.models.brand import CompetitorProfile, MarketingGoal
from app.services.brand import BrandService


class AudienceInsight(BaseModel):
    segment: str
    motivation: list[str] = Field(default_factory=list)
    friction: list[str] = Field(default_factory=list)
    content_angle: str = ""


class ResearchInput(AgentPayload):
    request: str = ""
    platforms: list[str] = Field(default_factory=list)
    focus: str = "audience"


class ResearchOutput(AgentResult):
    generation_mode: str = "deterministic"
    data_source: str = "brand_profile"
    audience_insights: list[AudienceInsight] = Field(default_factory=list)
    positioning_gaps: list[str] = Field(default_factory=list)
    competitor_observations: list[str] = Field(default_factory=list)
    recommended_focus: list[str] = Field(default_factory=list)


class _LLMResearch(BaseModel):
    audience_insights: list[AudienceInsight] = Field(default_factory=list)
    positioning_gaps: list[str] = Field(default_factory=list)
    recommended_focus: list[str] = Field(default_factory=list)
    summary: str = ""


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
    description = "Produces structured audience, competitor and positioning insight."
    responsibilities = [
        "Read brand profile, goals, competitor profiles and marketing memory",
        "Derive audience motivations and frictions without inventing statistics",
        "Identify positioning gaps the brand can credibly occupy",
        "Recommend a research-backed focus for the campaign",
    ]
    input_schema = ResearchInput
    output_schema = ResearchOutput
    max_tokens = 1200

    async def execute(self, ctx: AgentContext, payload: ResearchInput) -> ResearchOutput:
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

        # Deterministic baseline derived strictly from stored brand facts.
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
        )
        if used_llm and llm_out.audience_insights:
            output.audience_insights = llm_out.audience_insights
            output.positioning_gaps = llm_out.positioning_gaps or output.positioning_gaps
            output.recommended_focus = llm_out.recommended_focus or output.recommended_focus
            output.summary = llm_out.summary or output.summary
            output.generation_mode = "llm"
            output.confidence = 0.78
        return output
