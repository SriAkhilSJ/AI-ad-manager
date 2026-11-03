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
