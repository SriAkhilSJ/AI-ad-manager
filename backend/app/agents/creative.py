"""Creative Agent — art direction briefs and creative asset registration.

Phase 1 does **not** call an image model. Instead it produces a structured,
executable brief (composition, palette, typography, negative constraints) derived
from the brand's visual direction, and registers creative assets through the
storage layer when bytes are supplied.

This is the honest boundary: a brief is real work; a generated image we cannot
produce would be a fake.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from app.agents.base import AgentContext, AgentPayload, AgentResult, BaseAgent
from app.models.social import Platform


class CreativeInput(AgentPayload):
    platform: str = Platform.INSTAGRAM.value
    format: str = "carousel"
    concept: str = ""
    headline: str = ""
    aspect_ratio: str = "4:5"

    # ---- Phase 3B: Creative Director inputs (all optional) -----------------
    #: When any of the content fields below are supplied the agent also produces a
    #: full director brief. Nothing here is required, so every Phase 1 caller keeps
    #: exactly the behaviour it had.
    request_director_brief: bool = False
    campaign_id: uuid.UUID | None = None
    campaign_objective: str = ""
    content_item_id: uuid.UUID | None = None
    content_version_id: uuid.UUID | None = None
    content_hash: str = ""
    content_body: str = ""
    content_cta: str = ""
    content_hashtags: list[str] = Field(default_factory=list)
    audience: str = ""
    asset_type: str = "image"
    #: Phase 3B: explicit frame shape for the director brief. Empty means "use the
    #: platform default"; the Phase 1 ``aspect_ratio`` field keeps its own default
    #: so existing callers see no change.
    preferred_aspect_ratio: str = ""
    creative_direction: str = ""
    strategy_reference: str = ""
    #: Brand facts the copy already used, propagated so the visual brief cannot
    #: silently introduce a different claim.
    brand_facts: list[str] = Field(default_factory=list)
    prohibited_claims: list[str] = Field(default_factory=list)
    duration_seconds: int = 0


class CreativeBrief(BaseModel):
    concept: str
    composition: list[str] = Field(default_factory=list)
    palette: dict[str, str] = Field(default_factory=dict)
    typography: list[str] = Field(default_factory=list)
    motion_notes: list[str] = Field(default_factory=list)
    negative_prompt: list[str] = Field(default_factory=list)
    aspect_ratio: str = "4:5"


@dataclass
class DirectorBrief:
    """The Phase 3B creative brief, in the shape production actually needs."""

    campaign_id: str = ""
    content_id: str = ""
    content_version_id: str = ""
    content_hash: str = ""
    strategy_reference: str = ""
    platform: str = ""
    asset_type: str = "image"
    creative_objective: str = ""
    visual_concept: str = ""
    scenes: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)
    background: str = ""
    lighting: str = ""
    composition: list[str] = field(default_factory=list)
    colour_palette: dict[str, str] = field(default_factory=dict)
    typography: list[str] = field(default_factory=list)
    brand_asset_requirements: list[str] = field(default_factory=list)
    image_prompt: str = ""
    negative_prompt: list[str] = field(default_factory=list)
    aspect_ratio: str = "4:5"
    resolution: str = ""
    duration_seconds: int | None = None
    reference_assets: list[dict[str, Any]] = field(default_factory=list)
    validation_constraints: dict[str, Any] = field(default_factory=dict)
    brand_facts_used: list[str] = field(default_factory=list)
    generation_mode: str = "deterministic"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class CreativeOutput(AgentResult):
    generation_mode: str = "deterministic"
    data_source: str = "brand_profile"
    brief: CreativeBrief | None = None
    image_generation_available: bool = False
    provider_status: str = "not_configured"
    #: Phase 3B addition; ``None`` unless a director brief was requested.
    director_brief: DirectorBrief | None = None


DEFAULT_PALETTE = {
    "background": "#0A0A0B",
    "surface": "#141416",
    "primary_text": "#E8E8EA",
    "accent": "#8B5CF6",
    "metallic": "#C0C0C8",
}



#: Sensible frame ratios per platform, applied when the caller does not dictate one.
PLATFORM_ASPECT = {
    "instagram": "4:5",
    "linkedin": "1.91:1",
    "youtube": "16:9",
    "x": "16:9",
    "facebook": "1.91:1",
    "reddit": "16:9",
}

PLATFORM_VIDEO_SECONDS = {
    "instagram": 15,
    "linkedin": 20,
    "youtube": 30,
    "x": 15,
    "facebook": 15,
    "reddit": 15,
}

#: Negative constraints that apply to every Crusor visual. These are not
#: decoration: each one corresponds to a rule the Creative QA service checks.
BASE_NEGATIVE_PROMPT = [
    "no fabricated product screenshots or invented Crusor UI",
    "no invented customer logos, testimonials or partner marks",
    "no performance percentages, benchmarks or user counts",
    "no stock-photo people or staged office scenes",
    "no competitor branding",
    "no watermarks, signatures or garbled pseudo-text",
    "no purple-heavy gradients that swamp the charcoal base",
]

BRAND_ASSET_REQUIREMENTS = [
    "Use the official Crusor logo file from the brand asset library when one exists.",
    "If no official logo asset is registered, omit the mark entirely rather than redrawing it.",
    "Keep clear space around the mark equal to its cap height.",
    "Never place the logo over the purple glow's brightest point.",
]


def _resolve_resolution(aspect_ratio: str, asset_type: str) -> str:
    from app.creative.media import dimensions_for

    longest = 1920 if asset_type == "video" else 1080
    width, height = dimensions_for(aspect_ratio, longest)
    return f"{width}x{height}"


def _content_excerpt(payload: "CreativeInput", limit: int = 220) -> str:
    text = (payload.content_body or payload.headline or payload.concept or "").strip()
    text = " ".join(text.split())
    return text[:limit]


def build_director_brief(ctx: AgentContext, payload: "CreativeInput") -> DirectorBrief:
    """Turn a content version + brand into a production-ready creative brief.

    Purely deterministic: it reads the content, the brand's visual direction and the
    platform, and writes a brief. It never edits the content, and it records the
    content hash it was derived from so that claim is checkable.
    """
    brand = ctx.brand
    visual: dict[str, Any] = dict(getattr(brand, "visual_direction", {}) or {})
    palette = {**DEFAULT_PALETTE, **{k: v for k, v in visual.items() if isinstance(v, str) and v.startswith("#")}}
    platform = (payload.platform or "instagram").lower()
    asset_type = payload.asset_type if payload.asset_type in ("image", "video") else "image"
    aspect_ratio = (
        payload.preferred_aspect_ratio
        or PLATFORM_ASPECT.get(platform)
        or payload.aspect_ratio
        or "4:5"
    )
    excerpt = _content_excerpt(payload)
    product = getattr(brand, "product", "") or "Crusor AI"
    tagline = getattr(brand, "tagline", "") or ""
    audience = payload.audience or ", ".join((getattr(brand, "audience", []) or [])[:3]) or "software developers"
    objective = payload.campaign_objective or payload.concept or "Make the value of AI-assisted development concrete."

    concept = payload.creative_direction or (
        f"A developer pauses mid-task while {product} carries the mechanical work: "
        "the frame is calm, quiet and precise, never loud."
    )

    if asset_type == "video":
        scenes = [
            f"00:00–00:03 — Charcoal void; a single silver rule draws across the frame.",
            f"00:03–00:08 — Abstract code strata resolve out of the dark; purple rim light traces the edges.",
            f"00:08–00:12 — The strata settle into a clean build-like lattice; motion slows to near still.",
            f"00:12–00:15 — Lattice resolves; caption-safe space held at the lower third.",
        ][: 4 if PLATFORM_VIDEO_SECONDS.get(platform, 15) >= 15 else 3]
        duration = payload.duration_seconds or PLATFORM_VIDEO_SECONDS.get(platform, 15)
    else:
        scenes = [
            "Primary frame — charcoal field with a single metallic anchor line.",
            f"Mid-ground — abstract source structure suggesting {excerpt[:60] or 'developer work'}.",
            "Foreground — generous negative space reserved for on-image typography.",
        ]
        duration = None

    subjects = [
        "Abstract structural lattice representing source code, not a literal editor window",
        "Metallic silver rule as the only geometric anchor",
        "Diffuse purple light source, off-frame right, never a purple wash",
    ]
    if asset_type == "video":
        subjects.append("Slow parallax drift over the lattice; no camera shake, no zoom punch")

    composition = [
        "Rule-of-thirds: subject mass right of centre, type safe area left",
        "Horizon-free framing; no visible desk, no props, no device bezels",
        "Negative space of at least 40% for headline placement",
        "Keep the bottom 18% clear of detail for platform UI overlays",
    ]

    typography = [
        visual.get("typography", "Geometric sans, two weights maximum, tight tracking"),
        "Headline: 1 line preferred, 3 lines absolute maximum",
        "Body/CTA: single accent colour from the approved palette only",
        "Minimum text height on video: 5% of frame height for legibility",
    ]

    hero = _content_excerpt(payload, 90)
    image_prompt = (
        "Premium minimal developer-tool visual, dark charcoal background (#0A0A0B to #17171B "
        "vertical falloff), metallic silver geometric accent, subtle purple rim light "
        f"({palette.get('accent', '#8B5CF6')}) bleeding in from the upper right, "
        f"{asset_type} frame, aspect ratio {aspect_ratio}. "
        f"Subject: {concept} "
        f"Content intent: {hero or tagline or objective} "
        f"Audience: {audience}. "
        "Photographic lighting, high contrast, fine grain, no legible text, no UI chrome, "
        "no people, composed for a developer audience."
    )

    negative = list(BASE_NEGATIVE_PROMPT)
    for claim in payload.prohibited_claims:
        if claim:
            negative.append(f"no claim or visual reference to: {claim}")

    reference_assets: list[dict[str, Any]] = []
    if payload.content_item_id:
        reference_assets.append(
            {
                "kind": "content",
                "id": str(payload.content_item_id),
                "version_id": str(payload.content_version_id) if payload.content_version_id else "",
                "note": "Source copy for this visual — read-only reference.",
            }
        )

    constraints: dict[str, Any] = {
        "required_palette": {
            key: palette[key]
            for key in ("background", "metallic", "accent")
            if key in palette
        },
        "max_text_coverage_percent": 20,
        "min_contrast_ratio": 4.5,
        "logo_required": False,
        "logo_source": "brand_asset_library_only",
        "product_ui": "conceptual_only",
        "prohibited": list(BASE_NEGATIVE_PROMPT),
        "platform": platform,
        "aspect_ratio": aspect_ratio,
        "asset_type": asset_type,
        "human_visual_review_required": True,
    }
    if duration:
        constraints["max_duration_seconds"] = duration

    return DirectorBrief(
        campaign_id=str(payload.campaign_id) if payload.campaign_id else "",
        content_id=str(payload.content_item_id) if payload.content_item_id else "",
        content_version_id=str(payload.content_version_id) if payload.content_version_id else "",
        content_hash=payload.content_hash,
        strategy_reference=payload.strategy_reference,
        platform=platform,
        asset_type=asset_type,
        creative_objective=objective,
        visual_concept=concept,
        scenes=scenes,
        subjects=subjects,
        background=(
            "Near-black charcoal (#0A0A0B) falling to a slightly lifted graphite "
            "(#17171B) at the lower edge; no texture, no vignette artefacts."
        ),
        lighting=(
            "Single off-frame key from upper right in subtle purple, silver specular edge "
            "on the metallic anchor, deep shadow elsewhere. No flat studio lighting."
        ),
        composition=composition,
        colour_palette=palette,
        typography=typography,
        brand_asset_requirements=list(BRAND_ASSET_REQUIREMENTS),
        image_prompt=image_prompt,
        negative_prompt=negative,
        aspect_ratio=aspect_ratio,
        resolution=_resolve_resolution(aspect_ratio, asset_type),
        duration_seconds=duration,
        reference_assets=reference_assets,
        validation_constraints=constraints,
        brand_facts_used=list(payload.brand_facts),
        generation_mode="deterministic",
    )


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
        "Bind every brief to one exact content version without modifying that content",
        "Specify scene, subject, lighting, palette and negative constraints precisely enough to generate from",
        "Apply the brand's visual direction consistently",
        "Enumerate negative constraints (no fake UI, no invented logos)",
        "Report creative generation availability honestly",
    ]
    input_schema = CreativeInput
    output_schema = CreativeOutput
    max_tokens = 900

    def _provider_status(self) -> str:
        choice = (getattr(self.settings, "creative_provider", "mock") or "mock").lower()
        if choice == "mock":
            return (
                "mock: the deterministic mock provider is active. No image or video model "
                "is called and nothing is billed."
            )
        if choice == "flow":
            return (
                "flow_unavailable: the Flow Connector source is not available, so no "
                "generation request can be made."
            )
        return "disabled: creative generation is switched off."

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

        director = None
        if payload.request_director_brief or payload.content_item_id or payload.content_version_id:
            director = build_director_brief(ctx, payload)

        output = CreativeOutput(
            summary=f"Creative brief prepared for {payload.platform} {payload.format}.",
            brief=brief,
            image_generation_available=bool(
                getattr(self.settings, "creative_provider", "mock") == "mock"
            ),
            provider_status=self._provider_status(),
            director_brief=director,
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
