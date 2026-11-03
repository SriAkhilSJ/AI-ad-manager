"""Creative Agent — art direction briefs and creative asset registration.

Phase 1 does **not** call an image model. Instead it produces a structured,
executable brief (composition, palette, typography, negative constraints) derived
from the brand's visual direction, and registers creative assets through the
storage layer when bytes are supplied.

This is the honest boundary: a brief is real work; a generated image we cannot
produce would be a fake.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents.base import AgentContext, AgentPayload, AgentResult, BaseAgent
from app.models.social import Platform


class CreativeInput(AgentPayload):
    platform: str = Platform.INSTAGRAM.value
    format: str = "carousel"
    concept: str = ""
    headline: str = ""
    aspect_ratio: str = "4:5"


class CreativeBrief(BaseModel):
    concept: str
    composition: list[str] = Field(default_factory=list)
    palette: dict[str, str] = Field(default_factory=dict)
    typography: list[str] = Field(default_factory=list)
    motion_notes: list[str] = Field(default_factory=list)
    negative_prompt: list[str] = Field(default_factory=list)
    aspect_ratio: str = "4:5"


class CreativeOutput(AgentResult):
    generation_mode: str = "deterministic"
    data_source: str = "brand_profile"
    brief: CreativeBrief | None = None
    image_generation_available: bool = False
    provider_status: str = "not_configured"


DEFAULT_PALETTE = {
    "background": "#0A0A0B",
    "surface": "#141416",
    "primary_text": "#E8E8EA",
    "accent": "#8B5CF6",
    "metallic": "#C0C0C8",
}


class _LLMBrief(BaseModel):
    concept: str = ""
    composition: list[str] = Field(default_factory=list)
    typography: list[str] = Field(default_factory=list)
    motion_notes: list[str] = Field(default_factory=list)
    summary: str = ""


class CreativeAgent(BaseAgent[CreativeInput, CreativeOutput]):
    name = "creative"
    description = "Produces art-direction briefs and registers creative assets."
    responsibilities = [
        "Translate a content angle into a production-ready visual brief",
        "Apply the brand's visual direction consistently",
        "Enumerate negative constraints (no fake UI, no invented logos)",
        "Report creative generation availability honestly",
    ]
    input_schema = CreativeInput
    output_schema = CreativeOutput
    max_tokens = 900

    async def execute(self, ctx: AgentContext, payload: CreativeInput) -> CreativeOutput:
        if ctx.brand is None:
            return CreativeOutput(
                summary="No brand profile; visual direction unavailable.",
                data_source="none",
                confidence=0.0,
                warnings=["No active brand profile."],
            )

        visual = dict(ctx.brand.visual_direction or {})
        palette = {**DEFAULT_PALETTE, **{k: v for k, v in visual.items() if isinstance(v, str)}}

        brief = CreativeBrief(
            concept=payload.concept or f"{ctx.brand.product}: {ctx.brand.tagline}",
            composition=[
                "Dark charcoal canvas, generous negative space",
                "Headline top-left, product context bottom-right",
                "Single metallic accent line as visual anchor",
            ],
            palette=palette,
            typography=[
                visual.get("typography", "Geometric sans, tight tracking, two weights maximum")
            ],
            motion_notes=(
                ["Static frame; if animated, 200ms ease-out on the accent line subject"]
                if payload.format == "carousel"
                else []
            ),
            negative_prompt=[
                "no fabricated product screenshots",
                "no invented customer logos",
                "no stock-photo people",
                "no percentage or benchmark overlays",
            ],
            aspect_ratio=payload.aspect_ratio,
        )

        output = CreativeOutput(
            summary=f"Creative brief prepared for {payload.platform} {payload.format}.",
            brief=brief,
            image_generation_available=False,
            provider_status=(
                "not_configured: no image model is wired in Phase 1. Register assets "
                "manually or via the storage API."
            ),
            confidence=0.7,
            data_source="brand_profile.visual_direction",
        )

        llm_out, used_llm = await self.try_llm(
            ctx,
            system_prompt=(
                "You are the Creative Agent for a premium, minimal developer-tool brand. "
                "Produce an executable art-direction brief. Dark backgrounds, metallic "
                "silver, subtle purple accents. Never depict invented product UI. "
                "Return JSON per the schema."
            ),
            user_prompt=(
                f"Visual direction: {visual}. Concept: {payload.concept or payload.headline}. "
                f"Platform: {payload.platform}; format: {payload.format}; "
                f"aspect ratio: {payload.aspect_ratio}."
            ),
            schema=_LLMBrief,
            schema_name="creative_brief",
            purpose="creative",
        )
        if used_llm and llm_out.concept and output.brief is not None:
            output.brief.concept = llm_out.concept
            output.brief.composition = llm_out.composition or output.brief.composition
            output.brief.typography = llm_out.typography or output.brief.typography
            output.brief.motion_notes = llm_out.motion_notes or output.brief.motion_notes
            output.summary = llm_out.summary or output.summary
            output.generation_mode = "llm"
            output.confidence = 0.78
        return output
