"""The Crusor knowledge base: only given facts are asserted.

The brief is explicit that nothing may be invented — no features, pricing,
integrations, performance claims or testimonials. These tests check that the
seed obeys that, and that everything unknown is stored as *unverified* and
surfaced for founder confirmation.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.research import AudienceSegment, MarketingObjective, ProductFact
from app.seed import brand_crusor as seed


@pytest.fixture()
async def seeded(session, brand_row):
    counts = await seed.seed_knowledge_base(brand_row)
    return brand_row, counts


async def test_seed_creates_the_documented_knowledge(session, brand_row):
    counts = await seed.seed_knowledge_base(brand_row)
    assert counts["facts"] == len(seed.CRUSOR_PRODUCT_FACTS)
    assert counts["segments"] == 5
    assert counts["competitors"] == 4
    assert counts["objectives"] == 5


async def test_seed_is_idempotent(session, brand_row):
    await seed.seed_knowledge_base(brand_row)
    second = await seed.seed_knowledge_base(brand_row)
    assert second == {"facts": 0, "segments": 0, "competitors": 0, "objectives": 0}

    facts = (
        await session.execute(
            select(ProductFact).where(ProductFact.brand_profile_id == brand_row.id)
        )
    ).scalars().all()
    assert len(facts) == len(seed.CRUSOR_PRODUCT_FACTS)


async def test_only_given_facts_are_verified(session, brand_row):
    """Four verified facts: identity, tagline, website, positioning. Nothing else."""
    await seed.seed_knowledge_base(brand_row)
    facts = (
        await session.execute(
            select(ProductFact).where(ProductFact.brand_profile_id == brand_row.id)
        )
    ).scalars().all()

    verified = [f for f in facts if f.verification_status == "verified"]
    assert len(verified) == 4
    assert {f.category for f in verified} == {"identity", "tagline", "website", "positioning"}

    # Every verified fact must carry a real source.
    for fact in verified:
        assert fact.source_url, f"verified fact '{fact.statement}' has no source"


async def test_unknown_areas_are_unverified_and_not_public(session, brand_row):
    await seed.seed_knowledge_base(brand_row)
    facts = (
        await session.execute(
            select(ProductFact).where(ProductFact.brand_profile_id == brand_row.id)
        )
    ).scalars().all()

    unknown = [f for f in facts if f.verification_status != "verified"]
    categories = {f.category for f in unknown}
    assert {"pricing", "integrations", "capability", "performance", "social_proof"} <= categories

    for fact in unknown:
        assert fact.is_public is False, "an unconfirmed claim must not be publishable"
        assert fact.detail, "an unknown must explain what the founder needs to confirm"


async def test_no_invented_pricing_or_benchmarks(session, brand_row):
    """Guard against the seed drifting into fabricated specifics."""
    forbidden = ("$", "per month", "/mo", "free tier", "10x", "2x", "faster than")
    for fact in seed.CRUSOR_PRODUCT_FACTS:
        statement = fact["statement"].lower()
        detail = (fact.get("detail") or "").lower()
        for token in forbidden:
            assert token not in statement, (
                f"seed statement asserts an unprompted claim: {fact['statement']}"
            )
        # The word may appear in a *prohibition* note, but never as a claim.
        if "10x" in detail:
            assert "do not" in detail or "unconfirmed" in detail


async def test_competitors_are_names_only(session, brand_row):
    await seed.seed_knowledge_base(brand_row)
    from app.models.brand import CompetitorProfile

    rows = (
        await session.execute(
            select(CompetitorProfile).where(CompetitorProfile.brand_profile_id == brand_row.id)
        )
    ).scalars().all()

    assert {r.name for r in rows} == {
        "Cursor",
        "GitHub Copilot",
        "Windsurf",
        "VS Code with AI extensions",
    }
    for row in rows:
        # No positioning or capability was provided, so none may be asserted.
        assert row.positioning == ""
        assert row.strengths == []
        assert row.weaknesses == []
        assert row.website is None
        assert "founder brief" in row.source


async def test_five_audiences_from_the_brief(session, brand_row):
    await seed.seed_knowledge_base(brand_row)
    rows = (
        await session.execute(
            select(AudienceSegment).where(AudienceSegment.brand_profile_id == brand_row.id)
        )
    ).scalars().all()

    names = {r.name for r in rows}
    assert names == {
        "Students and engineering students",
        "Software developers",
        "Indie hackers",
        "Startup founders",
        "AI enthusiasts",
    }
    for row in rows:
        assert row.verification_status == "needs_confirmation", (
            "audience motivations are assumptions, not verified market data"
        )
        assert row.source == "founder brief"


async def test_marketing_objectives_cover_the_five_business_outcomes(session, brand_row):
    await seed.seed_knowledge_base(brand_row)
    rows = (
        await session.execute(
            select(MarketingObjective).where(
                MarketingObjective.brand_profile_id == brand_row.id
            )
        )
    ).scalars().all()

    types = {r.objective_type for r in rows}
    assert types == {
        "brand_awareness",
        "developer_acquisition",
        "waitlist_registrations",
        "product_adoption",
        "community_growth",
    }
    # developer acquisition is the priority
    assert min(rows, key=lambda r: r.priority).objective_type == "developer_acquisition"

    # No fabricated targets: a target with no measurement basis is worse than none.
    for row in rows:
        assert row.target_value is None
        assert row.statement


async def test_seeded_knowledge_makes_research_productive(session, brand_row):
    """With the KB in place, the offline provider produces real sourced findings."""
    from app.agents.base import AgentContext
    from app.agents.research import ResearchAgent

    await seed.seed_knowledge_base(brand_row)
    ctx = AgentContext(session=session, brand=brand_row, gateway=None)
    result = await ResearchAgent().run(
        ctx, {"mode": "evidence", "request": "Map the AI IDE market"}
    )

    assert result.findings_created >= 10
    assert result.sources_created >= 5
    categories = {f.category for f in result.findings}
    assert "competitor_identification" in categories
    assert "feature_comparison" in categories

    for finding in result.findings:
        if finding.verification_status == "verified":
            assert finding.has_source
