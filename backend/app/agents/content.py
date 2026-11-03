"""Content Agent — writes platform-appropriate copy as structured drafts.

Copy is produced as *structured fields* (headline, body, hashtags, CTA), not as a
blob, so the Brand Guardian can check it mechanically and the approval UI can
show exactly what the founder is approving.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents.base import AgentContext, AgentPayload, AgentResult, BaseAgent
from app.models.social import Platform

PLATFORM_RULES: dict[str, dict[str, object]] = {
    Platform.LINKEDIN.value: {"max_chars": 2600, "hashtags": 3, "tone": "professional, insight-led"},
    Platform.X.value: {"max_chars": 900, "hashtags": 2, "tone": "crisp, technical"},
    Platform.INSTAGRAM.value: {"max_chars": 2100, "hashtags": 8, "tone": "visual-first, energetic"},
    Platform.REDDIT.value: {"max_chars": 4000, "hashtags": 0, "tone": "plain, non-promotional"},
    Platform.YOUTUBE.value: {"max_chars": 900, "hashtags": 3, "tone": "demo-oriented"},
    Platform.FACEBOOK.value: {"max_chars": 1200, "hashtags": 2, "tone": "conversational"},
}


class ContentInput(AgentPayload):
    platform: str = Platform.LINKEDIN.value
    format: str = "post"
    angle: str = ""
    pillar: str = ""
    hook: str = ""
    tone: str = ""
    target_length: int | None = None


class ContentDraftOut(AgentResult):
    generation_mode: str = "deterministic"
    data_source: str = "brand_profile"
    platform: str = ""
    headline: str = ""
    body: str = ""
    hashtags: list[str] = Field(default_factory=list)
    call_to_action: str = ""
    character_count: int = 0
    detected_claims: list[str] = Field(default_factory=list)


class _LLMContent(BaseModel):
    headline: str = ""
    body: str = ""
    hashtags: list[str] = Field(default_factory=list)
    call_to_action: str = ""
    summary: str = ""


class ContentAgent(BaseAgent[ContentInput, ContentDraftOut]):
    name = "content"
    description = "Drafts platform-specific copy from a strategy slot."
    responsibilities = [
        "Write copy that respects platform length and tone rules",
        "Keep messaging inside the brand's approved factual envelope",
        "Return structured fields (headline/body/hashtags/CTA)",
        "Flag any sentence that looks like an unverifiable claim",
    ]
    input_schema = ContentInput
    output_schema = ContentDraftOut
    max_tokens = 1600

    async def execute(self, ctx: AgentContext, payload: ContentInput) -> ContentDraftOut:
        if ctx.brand is None:
            return ContentDraftOut(
                summary="No brand profile; cannot draft on-brand copy.",
                data_source="none",
                confidence=0.0,
                warnings=["No active brand profile."],
            )

        platform = payload.platform if payload.platform in Platform.values() else Platform.LINKEDIN.value
        rules = PLATFORM_RULES[platform]
        brand = ctx.brand
        max_chars = int(rules["max_chars"])  # type: ignore[arg-type]

        headline = payload.hook or f"{brand.tagline}"
        body = self._deterministic_body(brand, payload, platform)
        body = body[:max_chars]
        hashtags = self._hashtags(brand, int(rules["hashtags"]))  # type: ignore[arg-type]
        cta = f"Build and ship with {brand.product}." if brand.product else ""

        output = ContentDraftOut(
            summary=f"Drafted a {platform} {payload.format} ({len(body)} chars).",
            platform=platform,
            headline=headline,
            body=body,
            hashtags=hashtags,
            call_to_action=cta,
            character_count=len(body),
            detected_claims=self._detect_claims(body),
            confidence=0.55,
        )

        llm_out, used_llm = await self.try_llm(
            ctx,
            system_prompt=(
                "You are the Content Agent for a premium AI developer tool. Write copy "
                "that is technical and confident, never hype. Forbidden: invented "
                "metrics, customer counts, pricing, benchmarks, testimonials, "
                f"superlatives. Platform rules: {rules}. Return JSON per the schema."
            ),
            user_prompt=(
                f"Brand: {brand.company}/{brand.product}. Tagline: {brand.tagline}. "
                f"Audience: {brand.audience}. Personality: {brand.personality}. "
                f"Angle: {payload.angle or 'product value'}. Pillar: {payload.pillar or 'n/a'}. "
                f"Hard limit: {max_chars} characters of body copy."
            ),
            schema=_LLMContent,
            schema_name="content_draft",
            purpose="content",
        )
        if used_llm and llm_out.body:
            output.headline = (llm_out.headline or output.headline)[:300]
            output.body = llm_out.body[:max_chars]
            output.hashtags = (llm_out.hashtags or output.hashtags)[: int(rules["hashtags"])]  # type: ignore[arg-type]
            output.call_to_action = llm_out.call_to_action or output.call_to_action
            output.character_count = len(output.body)
            output.detected_claims = self._detect_claims(output.body)
            output.summary = llm_out.summary or output.summary
            output.generation_mode = "llm"
            output.confidence = 0.74
        return output

    # ------------------------------------------------------------------ helpers
    def _deterministic_body(self, brand, payload: ContentInput, platform: str) -> str:  # type: ignore[no-untyped-def]
        angle = payload.angle or payload.pillar or "faster iteration"
        audience = ", ".join(brand.audience[:3]) or "developers"
        lines = [
            f"{brand.tagline}",
            "",
            f"{brand.company} builds {brand.product} — an environment for developers who "
            f"care about shipping.",
            "",
            f"This week: {angle}.",
            f"Built for {audience}.",
            "",
            f"Constraint we hold ourselves to: no promises we cannot demonstrate.",
        ]
        if platform == Platform.REDDIT.value:
            lines = [
                f"Context: we build {brand.product}.",
                "",
                f"Question for this community: how do you currently handle {angle}?",
                "We are documenting our approach and want to avoid assuming our workflow "
                "matches yours.",
            ]
        return "\n".join(lines)

    def _hashtags(self, brand, limit: int) -> list[str]:  # type: ignore[no-untyped-def]
        if limit <= 0:
            return []
        candidates = [k.lstrip("#") for k in brand.keywords] or ["AIdev", "DevTools", "BuildAndShip"]
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
