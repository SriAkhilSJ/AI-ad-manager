"""Content Agent — turns a strategy slot into platform-specific structured drafts.

Phase 3A extends the Phase 1 agent rather than replacing it:

* the **platform spec** (``app.content.platforms``) is now the single source of
  truth for limits and supported types, instead of a duplicated table here;
* the input carries the campaign, objective, audience, pillar, primary message and
  the exact brand facts the draft is allowed to use, so the output is traceable;
* the output carries every field the Content Studio and the Brand Guardian need,
  including which facts were used and what verification status they have;
* generation is attributed: ``generation_mode`` is ``mock``, ``llm`` or
  ``deterministic`` and is never inferred by the caller.

Copy is still produced as *structured fields*, never a blob, so the Guardian can
check it mechanically and the founder can see exactly what they are approving.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from pydantic import BaseModel, Field

from app.agents.base import AgentContext, AgentPayload, AgentResult, BaseAgent
from app.content.platforms import ContentType, get_spec
from app.models.social import Platform

#: Facts the agent is allowed to assert. Anything not in this list is off-limits;
#: unverified entries are surfaced as restricted rather than quietly used.
DEFAULT_PROHIBITIONS = [
    "customer testimonials",
    "user counts",
    "performance benchmarks",
    "pricing",
    "competitor comparisons",
    "integrations",
    "launch dates",
    "product availability",
]


class ResearchReference(BaseModel):
    """A research finding the draft may lean on, with its provenance."""

    finding_id: str = ""
    claim: str = ""
    source: str = ""
    verification_status: str = "unverified"


class BrandFact(BaseModel):
    """A brand fact supplied to the writer, with an explicit verification state."""

    statement: str = ""
    verification_status: str = "unverified"
    source: str = "brand_profile"


class ContentInput(AgentPayload):
    """One generation slot."""

    platform: str = Platform.LINKEDIN.value
    content_type: str = ContentType.SOCIAL_POST.value
    #: Kept for Phase 1 compatibility (``format`` was the old name for type).
    format: str = ""
    angle: str = ""
    pillar: str = ""
    hook: str = ""
    tone: str = ""
    target_length: int | None = None

    # ---- Phase 3A context -------------------------------------------------
    #: Typed as UUID so the base agent can write it to ``agent_runs`` verbatim.
    #: An empty string here would be an invalid UUID for that FK column.
    campaign_id: uuid.UUID | None = None
    brand_profile_id: uuid.UUID | None = None
    objective: str = ""
    audience: str = ""
    primary_message: str = ""
    research_refs: list[ResearchReference] = Field(default_factory=list)
    brand_facts: list[BrandFact] = Field(default_factory=list)
    prohibited_claims: list[str] = Field(default_factory=list)
    suggested_date: str = ""
    suggested_time: str = ""
    variant_index: int = 0
    variant_count: int = 1
    #: Hooks already used in this plan; the writer must not reuse them.
    avoid_hooks: list[str] = Field(default_factory=list)
    #: Deterministic variety selector for variant generation.
    variant_seed: int = 0


class ContentDraftOut(AgentResult):
    # ---- Phase 1 fields (unchanged names) ---------------------------------
    generation_mode: str = "deterministic"
    data_source: str = "brand_profile"
    platform: str = ""
    headline: str = ""
    body: str = ""
    hashtags: list[str] = Field(default_factory=list)
    call_to_action: str = ""
    character_count: int = 0
    detected_claims: list[str] = Field(default_factory=list)

    # ---- Phase 3A fields --------------------------------------------------
    content_type: str = ContentType.SOCIAL_POST.value
    hook: str = ""
    campaign_id: str = ""
    objective: str = ""
    audience: str = ""
    pillar: str = ""
    primary_message: str = ""
    outline: list[str] = Field(default_factory=list)
    source_refs: list[ResearchReference] = Field(default_factory=list)
    brand_facts_used: list[BrandFact] = Field(default_factory=list)
    verification_status: str = "unverified"
    restricted_claims: list[str] = Field(default_factory=list)
    suggested_date: str = ""
    suggested_time: str = ""
    variant_index: int = 0
    provider: str = "deterministic"
    model: str = ""
    #: True only when the text came from the Phase 3A mock provider. Never set for
    #: real inference, and surfaced in the UI so the distinction is impossible to
    #: lose.
    is_mock: bool = False
    generation_metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        payload = f"{self.headline}|{self.hook}|{self.body}|{self.call_to_action}|{self.hashtags}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class _LLMContent(BaseModel):
    headline: str = ""
    hook: str = ""
    body: str = ""
    hashtags: list[str] = Field(default_factory=list)
    call_to_action: str = ""
    outline: list[str] = Field(default_factory=list)
    summary: str = ""


class ContentAgent(BaseAgent[ContentInput, ContentDraftOut]):
    name = "content"
    description = "Drafts platform-specific copy from a strategy slot."
    responsibilities = [
        "Write copy that respects the platform spec (length, structure, tone)",
        "Use only the brand facts and research references supplied to it",
        "Return structured fields (hook/body/hashtags/CTA/outline)",
        "Attribute generation to a real provider or to the Phase 3A mock",
        "Flag any sentence that looks like an unverifiable claim",
    ]
    input_schema = ContentInput
    output_schema = ContentDraftOut
    max_tokens = 1600

    async def execute(self, ctx: AgentContext, payload: ContentInput) -> ContentDraftOut:
        content_type = (
            payload.content_type
            or payload.format
            or ContentType.SOCIAL_POST.value
        )
        platform = (
            payload.platform if payload.platform in Platform.values() else Platform.LINKEDIN.value
        )
        try:
            spec = get_spec(platform)
        except Exception:  # pragma: no cover - platform validated above
            spec = get_spec(Platform.LINKEDIN.value)

        base = self._deterministic_draft(ctx, payload, platform, content_type)

        if ctx.gateway is None or getattr(ctx.gateway.settings, "content_provider", "mock") == "disabled":
            return base

        llm_out, used_llm = await self._try_model(ctx, payload, platform, content_type, spec)
        if not used_llm:
            return base

        # The router pins the content agent to the mock provider whenever
        # ``content_provider`` is ``mock``, so this flag cannot disagree with the
        # provider that actually answered.
        is_mock = getattr(ctx.gateway.settings, "content_provider", "mock") == "mock"

        draft = base.model_copy(deep=True)
        draft.headline = (llm_out.headline or base.headline)[: spec.title_max_chars]
        draft.hook = (llm_out.hook or base.hook)[: spec.hook_max_chars]
        modelled_body = llm_out.body or base.body
        draft.body = (
            modelled_body
            if content_type == ContentType.THREAD.value
            else modelled_body[: spec.body_max_chars]
        )
        draft.call_to_action = (llm_out.call_to_action or base.call_to_action)[: spec.cta_max_chars]
        if llm_out.hashtags:
            draft.hashtags = [h if h.startswith("#") else f"#{h}" for h in llm_out.hashtags][
                : spec.hashtag_max
            ]
        draft.outline = list(llm_out.outline or base.outline)
        draft.character_count = len(draft.body)
        draft.detected_claims = self._detect_claims(draft.body)
        draft.generation_mode = "mock" if is_mock else "llm"
        draft.is_mock = is_mock
        draft.provider = "mock" if is_mock else "llm"
        draft.model = "mock-content-v1" if is_mock else ""
        draft.data_source = f"content_provider:{draft.provider}"
        draft.summary = llm_out.summary or base.summary
        draft.confidence = 0.55 if is_mock else 0.74
        draft.generation_metadata = {
            **base.generation_metadata,
            "generation_mode": draft.generation_mode,
            "provider": draft.provider,
            "model": draft.model,
            "is_mock": is_mock,
        }
        if is_mock:
            draft.warnings = [
                *base.warnings,
                "Draft composed by the deterministic mock provider, not by a language model.",
            ]
        return draft

    # ------------------------------------------------------------------ model
    async def _try_model(
        self,
        ctx: AgentContext,
        payload: ContentInput,
        platform: str,
        content_type: str,
        spec: Any,
    ) -> tuple[_LLMContent, bool]:
        context = {
            "platform": platform,
            "content_type": content_type,
            "angle": payload.angle or payload.primary_message or payload.pillar,
            "primary_message": payload.primary_message,
            "product": getattr(ctx.brand, "product", "") if ctx.brand else "",
            "company": getattr(ctx.brand, "company", "") if ctx.brand else "",
            "audience": payload.audience
            or (getattr(ctx.brand, "audience", []) if ctx.brand else []),
            "pillar": payload.pillar,
            "objective": payload.objective,
            "hashtags": [k.lstrip("#") for k in (getattr(ctx.brand, "keywords", []) or [])],
            "hashtag_limit": spec.hashtag_max,
            "body_max_chars": spec.body_max_chars,
            "title_max_chars": spec.title_max_chars,
            "hook_max_chars": spec.hook_max_chars,
            "variant_index": payload.variant_index,
            "avoid_hooks": payload.avoid_hooks,
        }
        import json

        shared = (getattr(ctx, "extra", {}) or {}).get("budget_guard")
        out, used = await self.try_llm(
            ctx,
            budget=shared.budget if shared is not None else None,
            system_prompt=(
                "You are the Content Agent for Crusor AI, a premium developer tool. "
                "Write technical, confident copy, never hype. Respect the platform "
                "spec. Use only the facts in the context block: never invent metrics, "
                "customer counts, pricing, benchmarks, testimonials or availability. "
                "Return JSON matching the schema."
            ),
            user_prompt=json.dumps(context, sort_keys=True),
            schema=_LLMContent,
            schema_name="content_draft",
            purpose="content",
            task_class="content_generation",
        )
        if not used:
            return _LLMContent(), False
        return out, True  # type: ignore[return-value]

    # ---------------------------------------------------------- deterministic
    def _deterministic_draft(
        self, ctx: AgentContext, payload: ContentInput, platform: str, content_type: str
    ) -> ContentDraftOut:
        spec = get_spec(platform)
        brand = ctx.brand
        if brand is None:
            return ContentDraftOut(
                summary="No brand profile; cannot draft on-brand copy.",
                data_source="none",
                confidence=0.0,
                warnings=["No active brand profile."],
                platform=platform,
                content_type=content_type,
                campaign_id=str(payload.campaign_id) if payload.campaign_id else "",
            )

        angle = payload.angle or payload.primary_message or payload.pillar or "faster iteration"
        audience = payload.audience or ", ".join(brand.audience[:3]) or "developers"
        hook = payload.hook or self._deterministic_hook(angle, platform, payload.variant_seed)
        body = self._deterministic_body(brand, payload, platform, content_type, angle, audience)
        if content_type != ContentType.THREAD.value:
            body = body[: spec.body_max_chars]
        hashtags = self._hashtags(brand, spec.hashtag_max, platform)
        cta = self._deterministic_cta(brand, platform, content_type)
        outline = self._deterministic_outline(content_type, angle, platform)

        facts = [
            BrandFact(statement=f"Product: {brand.product}", verification_status="verified"),
            BrandFact(statement=f"Company: {brand.company}", verification_status="verified"),
        ]
        if brand.tagline:
            facts.append(
                BrandFact(statement=f"Tagline: {brand.tagline}", verification_status="verified")
            )

        return ContentDraftOut(
            summary=f"Drafted a {platform} {content_type} ({len(body)} chars).",
            generation_mode="deterministic",
            data_source="brand_profile",
            platform=platform,
            content_type=content_type,
            headline=hook[: spec.title_max_chars],
            hook=hook[: spec.hook_max_chars],
            body=body,
            hashtags=hashtags,
            call_to_action=cta,
            outline=outline,
            character_count=len(body),
            detected_claims=self._detect_claims(body),
            campaign_id=str(payload.campaign_id) if payload.campaign_id else "",
            objective=payload.objective,
            audience=audience,
            pillar=payload.pillar,
            primary_message=payload.primary_message or angle,
            source_refs=payload.research_refs,
            brand_facts_used=facts,
            verification_status="verified" if facts else "unverified",
            restricted_claims=[p for p in (payload.prohibited_claims or DEFAULT_PROHIBITIONS)],
            suggested_date=payload.suggested_date,
            suggested_time=payload.suggested_time,
            variant_index=payload.variant_index,
            provider="deterministic",
            model="",
            is_mock=False,
            confidence=0.55,
            generation_metadata={
                "variant_index": payload.variant_index,
                "variant_seed": payload.variant_seed,
                "platform_spec": {
                    "body_max_chars": spec.body_max_chars,
                    "hashtag_max": spec.hashtag_max,
                },
            },
        )

    def _deterministic_hook(self, angle: str, platform: str, seed: int) -> str:
        """A hook that changes substance, not just wording, across variants.

        Variant selection rotates the *angle of entry* (problem / workflow /
        trade-off) instead of shuffling synonyms, so two variants never differ by
        superficial rewording alone.
        """
        rotate = [
            f"The part of {angle} nobody shows you.",
            f"What we changed about {angle}.",
            f"A trade-off we made in {angle}.",
            f"Why {angle} is still hard.",
        ]
        base = rotate[seed % len(rotate)]
        if platform == Platform.REDDIT.value:
            return f"Disclosure: I build developer tooling. Question about {angle}."
        if platform == Platform.X.value:
            return base
        return base

    def _deterministic_body(
        self,
        brand: Any,
        payload: ContentInput,
        platform: str,
        content_type: str,
        angle: str,
        audience: str,
    ) -> str:
        spec = get_spec(platform)
        if platform == Platform.REDDIT.value:
            lines = [
                f"Disclosure: I work on {brand.product}, so treat this as a biased question.",
                "",
                f"The problem I keep running into is {angle}.",
                "",
                "What I am curious about: when you hit that, do you change tooling, change "
                "workflow, or absorb the cost?",
                "",
                "Not asking anyone to try anything — I want to understand how other teams "
                "route around it.",
            ]
        elif content_type == ContentType.THREAD.value:
            lines = [
                f"1/ We are building {brand.product} for {audience}.",
                f"2/ The workflow we care about is the tenth run, not the first.",
                f"3/ So we optimise for fewer decisions mid-change.",
                f"4/ {angle}",
                "5/ What we are not claiming yet: numbers, pricing, availability.",
                "6/ The build log is the honest version.",
            ]
        elif content_type == ContentType.CAROUSEL_OUTLINE.value:
            lines = [
                f"Slide 1: the problem — {angle}.",
                "Slide 2: why the obvious fix does not hold.",
                "Slide 3: the approach we chose.",
                "Slide 4: what we are not claiming yet.",
                "Slide 5: how to follow along.",
            ]
        elif content_type == ContentType.SHORT_FORM_VIDEO_SCRIPT.value:
            lines = [
                "0:00 — HOOK (voiceover): the moment a small change turns into an afternoon.",
                f"0:08 — SCENE 1: the current workflow around {angle}.",
                f"0:20 — SCENE 2: the same workflow in {brand.product}.",
                "0:32 — SCENE 3: what is still missing.",
                "0:40 — CLOSE: what we are building next.",
            ]
        else:
            lines = [
                angle + ".",
                "",
                f"{brand.company} builds {brand.product} for {audience}.",
                "",
                "The constraint we hold ourselves to: no promises we cannot demonstrate.",
            ]
        body = "\n".join(lines)
        # Keep the body inside the spec without cutting a sentence in half.
        while len(body) > spec.body_max_chars and len(lines) > 2:
            lines.pop(-1)
            body = "\n".join(lines)
        return body

    def _deterministic_cta(self, brand: Any, platform: str, content_type: str) -> str:
        if platform == Platform.REDDIT.value:
            return "Happy to share what we tried, if it is useful."
        if platform == Platform.X.value:
            return "Reply with your worst set-up step."
        if content_type == ContentType.CAROUSEL_OUTLINE.value:
            return "Save the thread for later."
        if brand.product:
            return f"Follow the build log for {brand.product}."
        return "Follow along as we build in the open."

    def _deterministic_outline(
        self, content_type: str, angle: str, platform: str
    ) -> list[str]:
        if content_type == ContentType.CAROUSEL_OUTLINE.value:
            return [
                f"The problem: {angle}",
                "Why the obvious fix does not hold",
                "The approach we chose",
                "What we are not claiming yet",
                "How to follow along",
            ]
        if content_type == ContentType.SHORT_FORM_VIDEO_SCRIPT.value:
            return [
                "Hook — the moment a small change turns into an afternoon",
                f"Scene 1 — the current workflow around {angle}",
                "Scene 2 — the same workflow in Crusor",
                "Scene 3 — what is still missing",
                "Close — what we are building next",
            ]
        return []

    def _hashtags(self, brand: Any, limit: int, platform: str) -> list[str]:
        if limit <= 0 or platform == Platform.REDDIT.value:
            return []
        candidates = [k.lstrip("#") for k in getattr(brand, "keywords", [])] or [
            "AIdev",
            "DevTools",
            "BuildAndShip",
        ]
        return [f"#{c.replace(' ', '')}" for c in candidates[:limit]]

    def _detect_claims(self, body: str) -> list[str]:
        """Cheap, deterministic claim heuristic; the Brand Guardian is the authority."""
        import re

        patterns = [
            r"\d+(?:\.\d+)?\s*%",
            r"\b\d{3,}\s*(?:users|customers|developers|companies|teams)\b",
            r"\b(?:best|fastest|#1|number one|world[- ]class|revolutionary|guaranteed)\b",
            r"\$\s?\d+",
            r"\b\d+x\s+(?:faster|more)\b",
        ]
        found: list[str] = []
        for pattern in patterns:
            for match in re.finditer(pattern, body, flags=re.IGNORECASE):
                found.append(match.group(0))
        return found
