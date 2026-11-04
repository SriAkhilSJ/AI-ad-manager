"""Seed the Crusor AI brand knowledge base.

Only facts supplied in the Phase 1 brief are encoded. Nothing about customers,
funding, pricing, benchmarks, integrations or performance is asserted, and the
``do_not_claim`` list makes that constraint machine-enforceable: the Brand
Guardian blocks copy that presents any of those as fact.

Run with::

    python -m app.seed.brand_crusor            # create if no active brand exists
    python -m app.seed.brand_crusor --replace  # deactivate existing, create fresh
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from sqlalchemy import select

from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine, session_scope
from app.models.brand import BrandProfile

logger = get_logger("crusor.seed")

CRUSOR_BRAND: dict[str, Any] = {
    "name": "Crusor AI",
    "company": "Crusor AI",
    "product": "AI-powered IDE",
    "tagline": "Build and Ship with Ease",
    "positioning": (
        "An AI-powered development environment designed to help developers build "
        "and ship software more efficiently."
    ),
    "audience": [
        "software developers",
        "students and engineering students",
        "indie hackers",
        "startup founders",
        "AI enthusiasts",
        "developers interested in AI-assisted coding",
    ],
    "personality": [
        "innovative",
        "technical",
        "premium",
        "minimal",
        "developer-focused",
        "confident but not exaggerated",
    ],
    "tone_of_voice": [
        "precise",
        "understated confidence",
        "technical rather than promotional",
        "concrete over hype",
        "no superlatives",
    ],
    "visual_direction": {
        "backgrounds": "dark",
        "primary": "#0A0A0B",
        "surface": "#141416",
        "accent": "subtle purple",
        "accent_hex": "#8B5CF6",
        "metallic": "silver",
        "metallic_hex": "#C0C0C8",
        "typography": "clean geometric sans, tight tracking, two weights maximum",
        "aesthetic": "premium technology, minimal, generous negative space",
        "imagery": "interfaces and code context; never fabricated product screenshots",
    },
    "brand_values": [
        "ship-oriented",
        "developer respect",
        "clarity",
        "craft",
    ],
    "keywords": [
        "AI IDE",
        "AI-assisted coding",
        "developer tools",
        "build and ship",
        "Crusor AI",
    ],
    "do_not_claim": [
        "customer or user counts",
        "funding or revenue figures",
        "pricing or plan tiers",
        "performance benchmarks or speed multipliers",
        "testimonials or named customers",
        "security/compliance certifications",
        "model provider partnerships",
        "feature availability not documented in the product",
    ],
    "prohibited_phrases": [
        "10x",
        "revolutionary",
        "world-class",
        "guaranteed",
        "best IDE",
        "trusted by",
        "used by thousands",
    ],
    "compliance_notes": (
        "Do not invent product capabilities, customer numbers, testimonials, pricing "
        "or performance claims. Every quantitative statement must come from an "
        "approved source recorded in this brand profile."
    ),
}

CRUSOR_PILLARS: list[dict[str, Any]] = [
    {
        "name": "Build & Ship",
        "description": (
            "Shipping velocity and the practical craft of getting software out the "
            "door with an AI-assisted environment."
        ),
        "platforms": ["linkedin", "x", "youtube"],
        "weight": 1.0,
    },
    {
        "name": "Developer Education",
        "description": (
            "Technical explanations for developers adopting AI-assisted coding: "
            "workflow, review discipline, context handling."
        ),
        "platforms": ["linkedin", "youtube", "reddit", "x"],
        "weight": 0.9,
    },
    {
        "name": "Product Craft",
        "description": (
            "Design and engineering decisions behind a premium, minimal IDE. "
            "Describe the work, never fabricate screenshots or benchmarks."
        ),
        "platforms": ["instagram", "linkedin", "x"],
        "weight": 0.8,
    },
    {
        "name": "Community Questions",
        "description": (
            "Genuine questions and discussion prompts for developer communities. "
            "No promotional framing on Reddit."
        ),
        "platforms": ["reddit", "x"],
        "weight": 0.6,
    },
]



# --------------------------------------------------------------------------- #
# Phase 2: structured knowledge base
#
# The overriding rule for every row below: if the founder did not state it, it is
# stored as unverified with a note asking for confirmation. Nothing here asserts
# a feature, price, integration, benchmark or testimonial that was not provided.
# The only *verified* facts are the four given in the brief.
# --------------------------------------------------------------------------- #

# Given facts. Each carries the source the founder supplied.
CRUSOR_PRODUCT_FACTS: list[dict[str, Any]] = [
    {
        "category": "identity",
        "statement": "Crusor AI is an AI-powered IDE.",
        "detail": "Stated in the product brief.",
        "source_url": "https://crusor.in",
        "verification_status": "verified",
        "is_public": True,
        "dedupe_key": "fact-identity-ai-ide",
    },
    {
        "category": "tagline",
        "statement": "The product tagline is \"Build and Ship with Ease.\"",
        "detail": "Use verbatim; do not embellish.",
        "source_url": "https://crusor.in",
        "verification_status": "verified",
        "is_public": True,
        "dedupe_key": "fact-tagline-build-and-ship",
    },
    {
        "category": "website",
        "statement": "The product website is https://crusor.in.",
        "detail": "The only confirmed public URL.",
        "source_url": "https://crusor.in",
        "verification_status": "verified",
        "is_public": True,
        "dedupe_key": "fact-website-crusor-in",
    },
    {
        "category": "positioning",
        "statement": (
            "Crusor AI is positioned as an AI-powered development environment that helps "
            "developers build and ship software more efficiently."
        ),
        "detail": "Founder-stated positioning.",
        "source_url": "https://crusor.in",
        "verification_status": "verified",
        "is_public": True,
        "dedupe_key": "fact-positioning-ai-dev-env",
    },
    # --- explicitly unknown: surfaced for founder confirmation -----------------
    {
        "category": "pricing",
        "statement": "Pricing and plan tiers are not documented in this knowledge base.",
        "detail": (
            "UNCONFIRMED. The brief provided no pricing information. Do not state or "
            "imply a price, free tier, trial length or discount until the founder "
            "confirms it here."
        ),
        "verification_status": "needs_confirmation",
        "is_public": False,
        "dedupe_key": "fact-unknown-pricing",
    },
    {
        "category": "integrations",
        "statement": "Supported integrations and languages are not documented.",
        "detail": (
            "UNCONFIRMED. No integration, language, framework or model-provider support "
            "has been confirmed. Any such claim would be fabricated."
        ),
        "verification_status": "needs_confirmation",
        "is_public": False,
        "dedupe_key": "fact-unknown-integrations",
    },
    {
        "category": "capability",
        "statement": "Individual product capabilities are not documented.",
        "detail": (
            "UNCONFIRMED. Beyond 'AI-powered IDE', specific features (autocomplete, "
            "agents, refactors, review tooling) are unspecified. Confirm each before "
            "it appears in marketing."
        ),
        "verification_status": "needs_confirmation",
        "is_public": False,
        "dedupe_key": "fact-unknown-capabilities",
    },
    {
        "category": "performance",
        "statement": "No performance or productivity benchmark has been established.",
        "detail": (
            "UNCONFIRMED. Do not publish speed, latency, token or productivity claims "
            "such as '10x faster'. No measurement exists."
        ),
        "verification_status": "needs_confirmation",
        "is_public": False,
        "dedupe_key": "fact-unknown-performance",
    },
    {
        "category": "social_proof",
        "statement": "No customer count, testimonial or case study is available.",
        "detail": (
            "UNCONFIRMED. Do not use 'trusted by', user counts, logos or testimonials "
            "until the founder supplies verifiable ones."
        ),
        "verification_status": "needs_confirmation",
        "is_public": False,
        "dedupe_key": "fact-unknown-social-proof",
    },
    {
        "category": "availability",
        "statement": "Platform availability (desktop, web, extensions) is not documented.",
        "detail": "UNCONFIRMED. Confirm where the IDE actually runs before saying so.",
        "verification_status": "needs_confirmation",
        "is_public": False,
        "dedupe_key": "fact-unknown-availability",
    },
]

# The five audiences from the brief, with motivations kept deliberately minimal.
CRUSOR_AUDIENCE_SEGMENTS: list[dict[str, Any]] = [
    {
        "name": "Students and engineering students",
        "description": "Learners building projects, portfolios and coursework.",
        "motivations": ["learn by building", "produce portfolio work"],
        "channels": ["linkedin", "youtube", "reddit"],
        "priority": 3,
        "source": "founder brief",
        "verification_status": "needs_confirmation",
    },
    {
        "name": "Software developers",
        "description": "Working developers shipping production code.",
        "motivations": ["ship software faster", "reduce tooling overhead"],
        "channels": ["linkedin", "x", "reddit"],
        "priority": 1,
        "source": "founder brief",
        "verification_status": "needs_confirmation",
    },
    {
        "name": "Indie hackers",
        "description": "Solo builders shipping products end to end.",
        "motivations": ["ship alone", "minimise overhead"],
        "channels": ["x", "reddit", "linkedin"],
        "priority": 2,
        "source": "founder brief",
        "verification_status": "needs_confirmation",
    },
    {
        "name": "Startup founders",
        "description": "Founders building with small engineering teams.",
        "motivations": ["move fast with a small team", "reduce engineering cost"],
        "channels": ["linkedin", "x"],
        "priority": 3,
        "source": "founder brief",
        "verification_status": "needs_confirmation",
    },
    {
        "name": "AI enthusiasts",
        "description": "Developers following AI tooling closely.",
        "motivations": ["explore new AI tooling", "stay current"],
        "channels": ["x", "reddit", "youtube"],
        "priority": 4,
        "source": "founder brief",
        "verification_status": "needs_confirmation",
    },
]

# Competitors are seeded by NAME ONLY. No positioning, pricing or capability is
# asserted, because none was provided and inventing it would be fabrication.
CRUSOR_COMPETITORS: list[dict[str, Any]] = [
    {
        "name": "Cursor",
        "website": None,
        "positioning": "",
        "strengths": [],
        "weaknesses": [],
        "source": "founder brief (name only)",
    },
    {
        "name": "GitHub Copilot",
        "website": None,
        "positioning": "",
        "strengths": [],
        "weaknesses": [],
        "source": "founder brief (name only)",
    },
    {
        "name": "Windsurf",
        "website": None,
        "positioning": "",
        "strengths": [],
        "weaknesses": [],
        "source": "founder brief (name only)",
    },
    {
        "name": "VS Code with AI extensions",
        "website": None,
        "positioning": "",
        "strengths": [],
        "weaknesses": [],
        "source": "founder brief (name only; indirect alternative)",
    },
]

# The five business objectives from the brief, in priority order.
CRUSOR_MARKETING_OBJECTIVES: list[dict[str, Any]] = [
    {
        "objective_type": "developer_acquisition",
        "statement": "Acquire developers who try the product and start building.",
        "priority": 1,
        "horizon": "quarter",
        "primary_metric": "signup_starts",
    },
    {
        "objective_type": "waitlist_registrations",
        "statement": "Convert interested developers into waitlist registrations.",
        "priority": 2,
        "horizon": "quarter",
        "primary_metric": "waitlist_registrations",
    },
    {
        "objective_type": "product_adoption",
        "statement": "Move new developers to a first completed project.",
        "priority": 3,
        "horizon": "quarter",
        "primary_metric": "activation_rate",
    },
    {
        "objective_type": "community_growth",
        "statement": "Build a developer community that asks and answers real questions.",
        "priority": 4,
        "horizon": "quarter",
        "primary_metric": "qualified_developer_conversations",
    },
    {
        "objective_type": "brand_awareness",
        "statement": "Make Crusor AI known to the target developer audiences.",
        "priority": 5,
        "horizon": "quarter",
        "primary_metric": "assisted signup_starts",
    },
]


async def seed_knowledge_base(brand: BrandProfile) -> dict[str, int]:
    """Seed the Phase 2 knowledge base for an existing brand.

    Idempotent: re-running does not duplicate rows (dedupe keys are enforced).
    """
    from app.models.research import (
        AudienceSegment,
        MarketingObjective,
        ProductFact,
    )

    counts = {"facts": 0, "segments": 0, "competitors": 0, "objectives": 0}
    async with session_scope() as session:
        existing_facts = {
            row
            for row in (
                await session.execute(
                    select(ProductFact.dedupe_key).where(
                        ProductFact.brand_profile_id == brand.id
                    )
                )
            ).scalars().all()
        }
        for fact in CRUSOR_PRODUCT_FACTS:
            if fact["dedupe_key"] in existing_facts:
                continue
            session.add(ProductFact(brand_profile_id=brand.id, **fact))
            counts["facts"] += 1

        existing_segments = {
            row
            for row in (
                await session.execute(
                    select(AudienceSegment.name).where(
                        AudienceSegment.brand_profile_id == brand.id
                    )
                )
            ).scalars().all()
        }
        for segment in CRUSOR_AUDIENCE_SEGMENTS:
            if segment["name"] in existing_segments:
                continue
            session.add(AudienceSegment(brand_profile_id=brand.id, **segment))
            counts["segments"] += 1

        from app.models.brand import CompetitorProfile

        existing_competitors = {
            row
            for row in (
                await session.execute(
                    select(CompetitorProfile.name).where(
                        CompetitorProfile.brand_profile_id == brand.id
                    )
                )
            ).scalars().all()
        }
        for competitor in CRUSOR_COMPETITORS:
            if competitor["name"] in existing_competitors:
                continue
            session.add(CompetitorProfile(brand_profile_id=brand.id, **competitor))
            counts["competitors"] += 1

        existing_objectives = {
            row
            for row in (
                await session.execute(
                    select(MarketingObjective.objective_type).where(
                        MarketingObjective.brand_profile_id == brand.id
                    )
                )
            ).scalars().all()
        }
        for objective in CRUSOR_MARKETING_OBJECTIVES:
            if objective["objective_type"] in existing_objectives:
                continue
            session.add(MarketingObjective(brand_profile_id=brand.id, **objective))
            counts["objectives"] += 1

        await session.flush()
        logger.info("knowledge_base_seeded", brand_id=str(brand.id), **counts)

    return counts


async def seed(replace: bool = False) -> BrandProfile:
    async with session_scope() as session:
        existing = (
            await session.execute(
                select(BrandProfile)
                .where(BrandProfile.name == "Crusor AI")
                .order_by(BrandProfile.created_at.desc())
            )
        ).scalars().first()

        if existing is not None and replace:
            existing.is_active = False
            session.add(existing)
            await session.flush()
            existing = None

        if existing is not None:
            logger.info("brand_exists", brand_id=str(existing.id))
            return existing

        brand = BrandProfile(**CRUSOR_BRAND)
        session.add(brand)
        await session.flush()

        from app.models.brand import ContentPillar

        for pillar in CRUSOR_PILLARS:
            session.add(ContentPillar(brand_profile_id=brand.id, **pillar))
        await session.flush()
        logger.info("brand_seeded", brand_id=str(brand.id), pillars=len(CRUSOR_PILLARS))

    await seed_knowledge_base(brand)
    return brand


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the Crusor AI brand profile")
    parser.add_argument(
        "--replace", action="store_true", help="deactivate the current profile and create a new one"
    )
    args = parser.parse_args(argv)
    configure_logging(level="INFO")

    async def _run() -> None:
        brand = await seed(replace=args.replace)
        print(f"Brand ready: {brand.name} ({brand.id}) version={brand.version}")
        await dispose_engine()

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
