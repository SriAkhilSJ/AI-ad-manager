"""Brand Guardian — the deterministic quality gate before human approval.

This agent is deliberately **mostly deterministic**: compliance checks must be
reproducible, explainable and fast. An optional LLM pass may add subjective
feedback but can never downgrade a hard violation to a pass.

Phase 3A extends the gate in four ways, without changing the Phase 1 contract:

1. **Platform rules come from the spec table** (``app.content.platforms``) instead
   of a duplicated length dict, so the writer and the checker cannot drift.
2. **Verdicts are explicit**: ``PASS``, ``PASS_WITH_WARNINGS``, ``NEEDS_REVISION``
   and ``BLOCKED``. The legacy lowercase ``verdict`` field is still emitted so
   Phase 1/2 callers and tests keep working — ``status`` is the Phase 3A name and
   ``verdict`` is derived from it.
3. **Every issue is structured**: severity, category, explanation, evidence,
   suggestion, and whether it needs founder confirmation. Nothing is a bare
   string, so the Studio can render a fix list and the API can persist it.
4. **Duplication and evidence checks** were added: near-identical drafts and
   claims with no attached source are blocked, because both are ways a plan can
   look complete while being worthless.

Hard violations (``blocking``) always win: unsupported product claims, unverified
statistics, fabricated testimonials, prohibited phrases, platform overflow,
missing affiliation disclosure on Reddit, and unsupported evidence.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from pydantic import BaseModel, Field

from app.agents.base import AgentContext, AgentPayload, AgentResult, BaseAgent
from app.content.platforms import ContentType, PlatformSpecError, validate_against_spec
from app.models.content_plan import ValidationVerdict
from app.models.social import Platform

# --------------------------------------------------------------------------- #
# Claim patterns. Each entry is (regex, human label, category, needs_confirmation)
# --------------------------------------------------------------------------- #
CLAIM_PATTERNS: list[tuple[str, str, str, bool]] = [
    (r"\d+(?:\.\d+)?\s*%", "percentage claim", "unverified_statistic", True),
    (
        r"\b\d{3,}\s*(?:users|customers|developers|companies|teams|downloads|waitlist)\b",
        "audience-size claim",
        "unverified_statistic",
        True,
    ),
    (r"\$\s?\d+", "pricing claim", "unsupported_product_claim", True),
    (r"\b\d+x\s+(?:faster|more|better)\b", "multiplier claim", "performance_claim", True),
    (
        r"\b(?:best|fastest|#1|number one|world[- ]class|revolutionary|unbeatable)\b",
        "superlative claim",
        "unsupported_product_claim",
        False,
    ),
    (r"\b(?:trusted by|used by)\b", "social-proof claim", "unsupported_testimonial", True),
    (r"\b\d{4}\b(?=.*(?:launched|founded|since))", "date claim", "launch_date_claim", True),
    (
        r"\b(?:integrates? with|works? with|supports?)\s+[A-Z][A-Za-z0-9.+]+",
        "integration claim",
        "unsupported_product_claim",
        True,
    ),
    (
        r"\b(?:free|freemium|trial|subscription|per seat|per month)\b",
        "availability or pricing claim",
        "unsupported_product_claim",
        True,
    ),
]

#: Fabricated social proof: a quoted sentence attributed to someone.
TESTIMONIAL_PATTERNS = [
    (r"[\"\u201c][^\"\u201d]{15,}[\"\u201d]\s*[—–-]\s*[A-Z][\w .]{2,}", "a quoted testimonial with attribution"),
    (r"\b(?:users|customers|developers|people)\s+(?:love|say|agree|swear by)\b", "a claim about what users say"),
    (r"\b(?:testimonial|case study|success story)\b", "a testimonial reference"),
]

#: Absolute promises that are misleading regardless of evidence.
MISLEADING_PATTERNS = [
    (r"\bguarantee(?:d|s)?\b", "a guarantee"),
    (r"\b(?:will never|never fails?|always works?|zero bugs|bug[- ]free)\b", "an absolute reliability promise"),
    (r"\b(?:instantly|overnight)\s+(?:ship|build|fix)", "an implausible speed promise"),
]

HYPE_WORDS = re.compile(
    r"\b(?:insane|game[- ]changing|crushing it|10x|blazing|magic|mind[- ]blowing)\b",
    re.IGNORECASE,
)

CORPORATE_FILLER = re.compile(
    r"\b(?:synergy|leverage the power|cutting[- ]edge|paradigm shift|"
    r"best[- ]in[- ]class|seamless(?:ly)? integrate[sd]?|unlock the potential)\b",
    re.IGNORECASE,
)

ENGAGEMENT_BAIT = re.compile(
    r"\b(?:smash (?:that )?like|drop a comment below|tag a friend|"
    r"share if you agree|comment ['\"]?yes|who else agrees\?*)\b",
    re.IGNORECASE,
)

GENERIC_CTAS = {
    "click here",
    "learn more",
    "check it out",
    "find out more",
    "don't miss out",
}


class ResearchReference(BaseModel):
    finding_id: str = ""
    claim: str = ""
    source: str = ""
    verification_status: str = "unverified"


class BrandGuardianInput(AgentPayload):
    # ---- Phase 1 fields (unchanged) --------------------------------------
    platform: str = Platform.LINKEDIN.value
    headline: str = ""
    body: str = ""
    hashtags: list[str] = Field(default_factory=list)
    call_to_action: str = ""

    # ---- Phase 3A fields -------------------------------------------------
    content_type: str = ContentType.SOCIAL_POST.value
    hook: str = ""
    title: str = ""
    outline: list[str] = Field(default_factory=list)
    audience: str = ""
    objective: str = ""
    pillar: str = ""
    source_refs: list[ResearchReference] = Field(default_factory=list)
    #: Text of drafts already in the same plan, for duplication detection.
    sibling_bodies: list[str] = Field(default_factory=list)
    sibling_hooks: list[str] = Field(default_factory=list)
    #: Whether the author is disclosed as affiliated (Reddit requirement).
    affiliation_disclosed: bool = False


class Violation(BaseModel):
    """A single structured issue.

    ``rule``/``severity``/``message``/``evidence``/``fix`` are the Phase 1 fields;
    the rest were added in Phase 3A so the Studio and the API can act on an issue
    rather than just display it.
    """

    rule: str
    severity: str  # blocking | warning
    message: str
    evidence: str = ""
    fix: str = ""
    # ---- Phase 3A --------------------------------------------------------
    category: str = "general"
    explanation: str = ""
    affected_text: str = ""
    requires_founder_confirmation: bool = False

    @property
    def suggestion(self) -> str:
        return self.fix


class BrandGuardianOutput(AgentResult):
    generation_mode: str = "deterministic"
    data_source: str = "brand_profile"
    #: Phase 1 compatible, lowercase: pass | needs_changes | blocked
    verdict: str = "pass"
    #: Phase 3A vocabulary: PASS | PASS_WITH_WARNINGS | NEEDS_REVISION | BLOCKED
    status: str = ValidationVerdict.PASS.value
    score: float = 1.0
    violations: list[Violation] = Field(default_factory=list)
    checked_rules: list[str] = Field(default_factory=list)
    blocking_count: int = 0
    warning_count: int = 0
    requires_founder_confirmation: bool = False

    def issues_as_dicts(self) -> list[dict[str, Any]]:
        return [
            {
                "rule": v.rule,
                "severity": v.severity,
                "category": v.category,
                "message": v.message,
                "explanation": v.explanation or v.message,
                "evidence": v.evidence,
                "affected_text": v.affected_text or v.evidence,
                "suggestion": v.fix,
                "requires_founder_confirmation": v.requires_founder_confirmation,
            }
            for v in self.violations
        ]


def _tokens(text: str) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-z0-9']+", (text or "").lower())
        if len(w) > 3
    }


def similarity(a: str, b: str) -> float:
    """Jaccard similarity over word tokens. Deterministic and cheap."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class BrandGuardianAgent(BaseAgent[BrandGuardianInput, BrandGuardianOutput]):
    name = "brand_guardian"
    description = "Deterministic brand, compliance and platform-fit gate."
    responsibilities = [
        "Detect unverifiable claims, fabricated testimonials and misleading promises",
        "Enforce platform limits and structural requirements from the spec table",
        "Check brand voice, audience relevance, CTA quality and repetitiveness",
        "Require source evidence for factual statements",
        "Return a verdict the Director must respect before requesting approval",
    ]
    input_schema = BrandGuardianInput
    output_schema = BrandGuardianOutput
    max_attempts = 1
    max_tokens = 600

    #: Bodies/hooks this similar or above are treated as duplicates.
    duplicate_threshold = 0.82

    async def execute(
        self, ctx: AgentContext, payload: BrandGuardianInput
    ) -> BrandGuardianOutput:
        violations: list[Violation] = []
        checked: list[str] = []
        platform = payload.platform if payload.platform in Platform.values() else Platform.LINKEDIN.value
        body = payload.body or ""
        combined = "\n".join(
            part for part in (payload.title, payload.hook, payload.headline, body, payload.call_to_action) if part
        )

        # 1. brand rules from the brand record --------------------------------
        checked.append("prohibited_phrases")
        if ctx.brand is not None:
            for phrase in ctx.brand.prohibited_phrases or []:
                if phrase and re.search(re.escape(phrase), combined, re.IGNORECASE):
                    violations.append(
                        Violation(
                            rule="prohibited_phrase",
                            severity="blocking",
                            category="brand_voice",
                            message=f"Prohibited phrase detected: '{phrase}'.",
                            explanation="The brand knowledge base lists this phrase as forbidden.",
                            evidence=phrase,
                            affected_text=phrase,
                            fix=f"Remove '{phrase}' from the copy.",
                        )
                    )
            for topic in ctx.brand.do_not_claim or []:
                checked.append(f"do_not_claim:{topic}")
                if topic and re.search(re.escape(topic), combined, re.IGNORECASE):
                    violations.append(
                        Violation(
                            rule="do_not_claim",
                            severity="blocking",
                            category="unsupported_product_claim",
                            message=f"Copy touches a topic the brand forbids claiming: '{topic}'.",
                            explanation="This topic is on the brand's do-not-claim list.",
                            evidence=topic,
                            affected_text=topic,
                            fix="Remove the reference or confirm the fact with the founder.",
                            requires_founder_confirmation=True,
                        )
                    )
        else:
            violations.append(
                Violation(
                    rule="brand_profile_missing",
                    severity="blocking",
                    category="brand_voice",
                    message="No brand profile available to validate against.",
                    explanation="Every claim must be checked against an approved brand record.",
                    fix="Create or activate a brand profile.",
                )
            )

        # 2. unsupported claims ----------------------------------------------
        checked.append("claim_detection")
        for pattern, label, category, needs_confirmation in CLAIM_PATTERNS:
            for match in re.finditer(pattern, combined, flags=re.IGNORECASE):
                violations.append(
                    Violation(
                        rule="unverifiable_claim",
                        severity="blocking",
                        category=category,
                        message=f"Possible {label} requiring evidence: '{match.group(0)}'.",
                        explanation=(
                            "Numeric, pricing, integration and availability claims must be "
                            "traceable to an approved fact before publication."
                        ),
                        evidence=match.group(0),
                        affected_text=match.group(0),
                        fix="Remove the claim or attach a verifiable source to the brand profile.",
                        requires_founder_confirmation=needs_confirmation,
                    )
                )

        # 3. fabricated testimonials -----------------------------------------
        checked.append("testimonial_detection")
        for pattern, label in TESTIMONIAL_PATTERNS:
            for match in re.finditer(pattern, combined, flags=re.IGNORECASE):
                violations.append(
                    Violation(
                        rule="fabricated_testimonial",
                        severity="blocking",
                        category="unsupported_testimonial",
                        message=f"Copy contains {label}: '{match.group(0)[:80]}'.",
                        explanation=(
                            "No testimonial may be used unless the founder supplied it verbatim "
                            "with permission."
                        ),
                        evidence=match.group(0),
                        affected_text=match.group(0),
                        fix="Remove the testimonial or attach the founder-supplied, attributed source.",
                        requires_founder_confirmation=True,
                    )
                )

        # 4. misleading promises ----------------------------------------------
        checked.append("misleading_statements")
        for pattern, label in MISLEADING_PATTERNS:
            for match in re.finditer(pattern, combined, flags=re.IGNORECASE):
                violations.append(
                    Violation(
                        rule="misleading_statement",
                        severity="blocking",
                        category="misleading",
                        message=f"Copy makes {label}: '{match.group(0)}'.",
                        explanation="Absolute reliability or speed promises are not verifiable.",
                        evidence=match.group(0),
                        affected_text=match.group(0),
                        fix="Replace with a concrete, demonstrable statement.",
                    )
                )

        # 5. platform spec ----------------------------------------------------
        checked.append("platform_spec")
        try:
            spec_issues = validate_against_spec(
                platform=platform,
                content_type=payload.content_type,
                title=payload.title or payload.headline,
                hook=payload.hook,
                body=body,
                call_to_action=payload.call_to_action,
                hashtags=payload.hashtags,
                outline=payload.outline,
            )
        except PlatformSpecError as exc:
            spec_issues = []
            violations.append(
                Violation(
                    rule="platform_unsupported",
                    severity="blocking",
                    category="platform_formatting",
                    message=str(exc),
                    fix="Choose a supported platform.",
                )
            )
        for issue in spec_issues:
            violations.append(
                Violation(
                    rule=issue.rule,
                    severity=issue.severity,
                    category="platform_formatting",
                    message=issue.message,
                    explanation="Enforced by the platform content specification.",
                    evidence=issue.evidence,
                    affected_text=issue.evidence,
                    fix=issue.fix,
                )
            )

        # 6. brand voice ------------------------------------------------------
        checked.append("tone_alignment")
        for match in HYPE_WORDS.finditer(combined):
            violations.append(
                Violation(
                    rule="tone_mismatch",
                    severity="warning",
                    category="brand_voice",
                    message=f"'{match.group(0)}' contradicts the brand's measured voice.",
                    explanation="Crusor's voice is confident and technical, not hyped.",
                    evidence=match.group(0),
                    affected_text=match.group(0),
                    fix="Replace with concrete, technical language.",
                )
            )
        for match in CORPORATE_FILLER.finditer(combined):
            violations.append(
                Violation(
                    rule="corporate_filler",
                    severity="warning",
                    category="brand_voice",
                    message=f"'{match.group(0)}' is generic corporate language.",
                    explanation="The brand avoids stock marketing phrasing.",
                    evidence=match.group(0),
                    affected_text=match.group(0),
                    fix="Say the thing plainly or cut the sentence.",
                )
            )

        # 7. CTA relevance ----------------------------------------------------
        checked.append("cta_relevance")
        if not payload.call_to_action and platform != Platform.REDDIT.value:
            violations.append(
                Violation(
                    rule="missing_cta",
                    severity="warning",
                    category="cta_relevance",
                    message="No call to action; engagement will be weaker.",
                    explanation="Every broadcast post should ask for one specific action.",
                    fix="Add a specific, low-friction CTA.",
                )
            )
        elif payload.call_to_action.strip().lower() in GENERIC_CTAS:
            violations.append(
                Violation(
                    rule="generic_cta",
                    severity="warning",
                    category="cta_relevance",
                    message=f"CTA '{payload.call_to_action}' is too generic to act on.",
                    explanation="A generic CTA gives the reader no specific next step.",
                    affected_text=payload.call_to_action,
                    fix="Name the action and where to take it.",
                )
            )

        # 8. audience relevance ----------------------------------------------
        checked.append("audience_relevance")
        if payload.audience and _tokens(payload.audience):
            audience_terms = _tokens(payload.audience)
            if not (audience_terms & _tokens(combined)):
                violations.append(
                    Violation(
                        rule="audience_not_addressed",
                        severity="warning",
                        category="audience_relevance",
                        message=f"Copy never addresses the target audience ({payload.audience}).",
                        explanation="Generic copy under-performs for a defined segment.",
                        fix="Name who the post is for, or why it matters to them.",
                    )
                )

        # 9. evidence where required -------------------------------------------
        checked.append("source_evidence")
        claim_present = any(
            re.search(pattern, combined, flags=re.IGNORECASE)
            for pattern, _, _, _ in CLAIM_PATTERNS
        )
        if claim_present and not payload.source_refs:
            violations.append(
                Violation(
                    rule="missing_source_evidence",
                    severity="blocking",
                    category="source_evidence",
                    message="Copy makes a factual claim but carries no source reference.",
                    explanation="Every factual statement needs a traceable source before review.",
                    fix="Attach research references or remove the claim.",
                    requires_founder_confirmation=True,
                )
            )

        # 10. repetitiveness ----------------------------------------------------
        checked.append("duplication")
        violations.extend(self._duplication_issues(payload, body))

        # 11. community safety --------------------------------------------------
        if platform == Platform.REDDIT.value:
            checked.append("reddit_community_rules")
            disclosed = payload.affiliation_disclosed or bool(
                re.search(r"\b(?:disclosure|I work on|I build|I am the founder)\b", combined, re.IGNORECASE)
            )
            if not disclosed:
                violations.append(
                    Violation(
                        rule="affiliation_not_disclosed",
                        severity="blocking",
                        category="community_safety",
                        message="Reddit copy does not disclose the author's affiliation.",
                        explanation="Undisclosed promotion violates community norms and platform rules.",
                        fix="Open with a plain affiliation disclosure.",
                    )
                )
            for match in ENGAGEMENT_BAIT.finditer(combined):
                violations.append(
                    Violation(
                        rule="engagement_bait",
                        severity="blocking",
                        category="community_safety",
                        message=f"Engagement bait detected: '{match.group(0)}'.",
                        explanation="Baiting interaction is spam behaviour, not community value.",
                        evidence=match.group(0),
                        affected_text=match.group(0),
                        fix="Ask a genuine question or remove the prompt.",
                    )
                )

        return self._verdict(violations, checked)

    # ------------------------------------------------------------------ helpers
    def _duplication_issues(
        self, payload: BrandGuardianInput, body: str
    ) -> list[Violation]:
        issues: list[Violation] = []
        for other in payload.sibling_bodies:
            score = similarity(body, other)
            if score >= self.duplicate_threshold:
                issues.append(
                    Violation(
                        rule="duplicate_content",
                        severity="blocking",
                        category="repetitiveness",
                        message=(
                            f"This draft is {score:.0%} similar to another draft in the same plan."
                        ),
                        explanation=(
                            "Near-identical variants add volume without adding value and read as "
                            "spam on community platforms."
                        ),
                        evidence=other[:120],
                        fix="Rework the angle, not the wording, or drop the duplicate.",
                    )
                )
                break
        for other in payload.sibling_hooks:
            if payload.hook and similarity(payload.hook, other) >= self.duplicate_threshold:
                issues.append(
                    Violation(
                        rule="duplicate_hook",
                        severity="warning",
                        category="repetitiveness",
                        message="This hook repeats a hook already used in this plan.",
                        explanation="Repeating the same opening trains the audience to scroll.",
                        evidence=other[:120],
                        fix="Open from a different angle.",
                    )
                )
                break
        return issues

    def _verdict(self, violations: Iterable[Violation], checked: list[str]) -> BrandGuardianOutput:
        violations = list(violations)
        blocking = [v for v in violations if v.severity == "blocking"]
        warnings = [v for v in violations if v.severity == "warning"]

        if blocking:
            # Structural/factual failures need a rewrite; an unfixable-by-copy
            # failure (a forbidden topic) still blocks, but is reported the same
            # way so the founder sees one consistent vocabulary.
            status = ValidationVerdict.BLOCKED
            verdict = "blocked"
        elif warnings:
            status = ValidationVerdict.PASS_WITH_WARNINGS
            verdict = "pass"
        else:
            status = ValidationVerdict.PASS
            verdict = "pass"

        score = max(0.0, 1.0 - 0.25 * len(blocking) - 0.05 * len(warnings))
        requires_confirmation = any(v.requires_founder_confirmation for v in violations)

        return BrandGuardianOutput(
            summary=(
                f"{len(blocking)} blocking and {len(warnings)} advisory issue(s) found."
                if violations
                else "All brand and platform checks passed."
            ),
            verdict=verdict,
            status=status.value,
            score=round(score, 3),
            violations=violations,
            checked_rules=checked,
            blocking_count=len(blocking),
            warning_count=len(warnings),
            requires_founder_confirmation=requires_confirmation,
            confidence=0.95,
            requires_human_review=True,
            warnings=[v.message for v in warnings],
        )
