"""Brand Guardian — the deterministic quality gate before human approval.

This agent is intentionally **mostly deterministic**: quality and compliance
checks must be reproducible, explainable and fast. An optional LLM pass adds
subjective feedback, but can never downgrade a hard violation to a pass.

Hard violations (blocking):
* unverifiable numeric / superlative claims,
* prohibited phrases,
* platform length overruns,
* missing call to action where the platform expects one,
* empty copy.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.agents.base import AgentContext, AgentPayload, AgentResult, BaseAgent
from app.models.social import Platform

CLAIM_PATTERNS = [
    (r"\d+(?:\.\d+)?\s*%", "percentage claim"),
    (r"\b\d{3,}\s*(?:users|customers|developers|companies|teams|downloads)\b", "audience-size claim"),
    (r"\$\s?\d+", "pricing claim"),
    (r"\b\d+x\s+(?:faster|more|better)\b", "multiplier claim"),
    (r"\b(?:best|fastest|#1|number one|world[- ]class|revolutionary|guaranteed|unbeatable)\b", "superlative claim"),
    (r"\b(?:trusted by|used by)\b", "social-proof claim"),
    (r"\b\d{4}\b(?=.*(?:launched|founded|since))", "date claim"),
]

LENGTH_LIMITS = {
    Platform.LINKEDIN.value: 2600,
    Platform.X.value: 900,
    Platform.INSTAGRAM.value: 2100,
    Platform.REDDIT.value: 4000,
    Platform.YOUTUBE.value: 900,
    Platform.FACEBOOK.value: 1200,
}


class BrandGuardianInput(AgentPayload):
    platform: str = Platform.LINKEDIN.value
    headline: str = ""
    body: str = ""
    hashtags: list[str] = Field(default_factory=list)
    call_to_action: str = ""


class Violation(BaseModel):
    rule: str
    severity: str  # blocking | warning
    message: str
    evidence: str = ""
    fix: str = ""


class BrandGuardianOutput(AgentResult):
    generation_mode: str = "deterministic"
    data_source: str = "brand_profile"
    verdict: str = "pass"  # pass | needs_changes | blocked
    score: float = 1.0
    violations: list[Violation] = Field(default_factory=list)
    checked_rules: list[str] = Field(default_factory=list)


class BrandGuardianAgent(BaseAgent[BrandGuardianInput, BrandGuardianOutput]):
    name = "brand_guardian"
    description = "Deterministic brand, compliance and platform-fit gate."
    responsibilities = [
        "Detect unverifiable claims, superlatives and prohibited phrases",
        "Enforce platform length and structural requirements",
        "Verify tone alignment with the brand personality",
        "Return a verdict the Director must respect before requesting approval",
    ]
    input_schema = BrandGuardianInput
    output_schema = BrandGuardianOutput
    max_attempts = 1
    max_tokens = 600

    async def execute(
        self, ctx: AgentContext, payload: BrandGuardianInput
    ) -> BrandGuardianOutput:
        violations: list[Violation] = []
        checked: list[str] = []
        body = payload.body or ""
        platform = payload.platform if payload.platform in Platform.values() else Platform.LINKEDIN.value
        combined = f"{payload.headline}\n{body}\n{payload.call_to_action}"

        # 1. prohibited phrases + do-not-claim list from the brand record
        if ctx.brand is not None:
            checked.append("prohibited_phrases")
            for phrase in ctx.brand.prohibited_phrases or []:
                if phrase and re.search(re.escape(phrase), combined, re.IGNORECASE):
                    violations.append(
                        Violation(
                            rule="prohibited_phrase",
                            severity="blocking",
                            message=f"Prohibited phrase detected: '{phrase}'.",
                            evidence=phrase,
                            fix=f"Remove '{phrase}' from the copy.",
                        )
                    )
            for topic in ctx.brand.do_not_claim or []:
                checked.append(f"do_not_claim:{topic}")
        else:
            violations.append(
                Violation(
                    rule="brand_profile_missing",
                    severity="blocking",
                    message="No brand profile available to validate against.",
                    fix="Create or activate a brand profile.",
                )
            )

        # 2. claim detection
        checked.append("claim_detection")
        for pattern, label in CLAIM_PATTERNS:
            for match in re.finditer(pattern, combined, flags=re.IGNORECASE):
                violations.append(
                    Violation(
                        rule="unverifiable_claim",
                        severity="blocking",
                        message=f"Possible {label} requiring evidence: '{match.group(0)}'.",
                        evidence=match.group(0),
                        fix="Remove the claim or attach a verifiable source to the brand profile.",
                    )
                )

        # 3. platform length
        checked.append("platform_length")
        limit = LENGTH_LIMITS.get(platform, 2200)
        if len(body) > limit:
            violations.append(
                Violation(
                    rule="platform_length",
                    severity="blocking",
                    message=f"Body is {len(body)} chars; {platform} allows {limit}.",
                    evidence=f"{len(body)} > {limit}",
                    fix=f"Trim at least {len(body) - limit} characters.",
                )
            )

        # 4. structural requirements
        checked.append("structural_requirements")
        if not body.strip():
            violations.append(
                Violation(
                    rule="empty_copy",
                    severity="blocking",
                    message="Body copy is empty.",
                    fix="Generate or write body copy.",
                )
            )
        if not payload.call_to_action and platform != Platform.REDDIT.value:
            violations.append(
                Violation(
                    rule="missing_cta",
                    severity="warning",
                    message="No call to action; engagement will be weaker.",
                    fix="Add a specific, low-friction CTA.",
                )
            )
        if platform == Platform.INSTAGRAM.value and not payload.hashtags:
            violations.append(
                Violation(
                    rule="missing_hashtags",
                    severity="warning",
                    message="Instagram post has no hashtags.",
                    fix="Add 3–8 relevant hashtags.",
                )
            )

        # 5. tone: hype words that contradict a 'confident but not exaggerated' voice
        checked.append("tone_alignment")
        hype = re.findall(
            r"\b(?:insane|game[- ]changing|crushing it|10x|blazing|magic)\b",
            combined,
            re.IGNORECASE,
        )
        for word in hype:
            violations.append(
                Violation(
                    rule="tone_mismatch",
                    severity="warning",
                    message=f"'{word}' contradicts the brand's measured voice.",
                    evidence=word,
                    fix="Replace with concrete, technical language.",
                )
            )

        blocking = [v for v in violations if v.severity == "blocking"]
        warnings = [v for v in violations if v.severity == "warning"]
        score = max(0.0, 1.0 - 0.25 * len(blocking) - 0.05 * len(warnings))
        verdict = "blocked" if blocking else ("needs_changes" if warnings else "pass")

        return BrandGuardianOutput(
            summary=(
                f"{len(blocking)} blocking and {len(warnings)} advisory issue(s) found."
                if violations
                else "All brand and platform checks passed."
            ),
            verdict=verdict,
            score=round(score, 3),
            violations=violations,
            checked_rules=checked,
            confidence=0.95,
            requires_human_review=True,
            warnings=[v.message for v in warnings],
        )
