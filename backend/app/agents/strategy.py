"""Strategy Agent — converts research into an executable campaign plan."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents.base import AgentContext, AgentPayload, AgentResult, BaseAgent
from app.models.social import Platform
from app.services.brand import BrandService


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


DEFAULT_FORMAT = {
    Platform.LINKEDIN: "text_post",
    Platform.X: "thread",
    Platform.INSTAGRAM: "carousel",
    Platform.REDDIT: "discussion_post",
    Platform.YOUTUBE: "short",
    Platform.FACEBOOK: "text_post",
}


class StrategyAgent(BaseAgent[StrategyInput, StrategyOutput]):
    name = "strategy"
    description = "Builds the campaign plan: pillars, channel mix, content slots, metrics."
    responsibilities = [
        "Translate research into a campaign objective and messaging pillars",
        "Choose a channel mix constrained to supported platforms",
        "Emit concrete content slots (platform, format, pillar, angle)",
        "Define measurable success metrics without inventing targets",
    ]
    input_schema = StrategyInput
    output_schema = StrategyOutput
    max_tokens = 1600

    async def execute(self, ctx: AgentContext, payload: StrategyInput) -> StrategyOutput:
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

        # Deterministic plan: distribute slots across active pillars × channels.
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
