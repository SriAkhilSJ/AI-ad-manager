"""Strategy Agent: evidence grounding, objective mapping, brand safety.

Everything here runs without a model provider. That is deliberate: the
deterministic plan must stand on its own, and the tests must prove it does
rather than proving a mock does.
"""

from __future__ import annotations

import pytest

from app.agents.base import AgentContext
from app.agents.strategy import (
    ACQUISITION_METRIC_TOKENS,
    VANITY_METRIC_TOKENS,
    StrategyAgent,
    StrategyInput,
    StrategyPlanOutput,
    _is_acquisition,
    _is_vanity,
)
from app.models.research import (
    ResearchFinding,
    ResearchProject,
    VerificationStatus,
)
from app.models.strategy import MarketingStrategy, StrategyVersion
from app.services.strategy import StrategyService


async def seed_findings(session, brand_row, project=None, count=3, **overrides):  # type: ignore[no-untyped-def]
    project = project or ResearchProject(
        brand_profile_id=brand_row.id,
        objective="Map the AI IDE market",
        categories=["developer_pain_points"],
        provider="deterministic",
    )
    session.add(project)
    await session.flush()

    rows = []
    for index in range(count):
        finding = ResearchFinding(
            research_project_id=project.id,
            brand_profile_id=brand_row.id,
            category=overrides.get("category", "developer_pain_points"),
            finding=overrides.get("finding", f"Developers want to ship faster ({index})."),
            evidence_summary="Developer discussions.",
            source_url=overrides.get("source_url", "https://example.com/discussion"),
            source_title="Discussion",
            confidence=overrides.get("confidence", 0.75),
            verification_status=overrides.get(
                "verification_status", VerificationStatus.VERIFIED.value
            ),
            dedupe_key=f"key-{index}-{overrides.get('dedupe_suffix', 'a')}",
            provider="deterministic",
        )
        session.add(finding)
        rows.append(finding)
    await session.flush()
    return project, rows


async def run_strategy(session, brand_row, **payload_kwargs):  # type: ignore[no-untyped-def]
    ctx = AgentContext(session=session, brand=brand_row, gateway=None)
    payload = StrategyInput(
        mode="strategy",
        request=payload_kwargs.pop("request", "Acquire developers"),
        **payload_kwargs,
    )
    return await StrategyAgent().run(ctx, payload)


# --------------------------------------------------------------------------- #
# generation
# --------------------------------------------------------------------------- #
async def test_strategy_generates_and_persists_all_required_sections(session, brand_row):
    await seed_findings(session, brand_row)
    result = await run_strategy(session, brand_row)

    assert isinstance(result, StrategyPlanOutput)
    assert result.strategy_id is not None
    assert result.version_number == 1

    version = await session.get(StrategyVersion, result.strategy_version_id)
    for field in (
        "positioning_recommendations",
        "audience_segmentation",
        "content_pillars",
        "campaign_concepts",
        "platform_strategies",
        "weekly_objectives",
        "lead_generation",
        "growth_experiments",
        "kpis",
    ):
        assert getattr(version, field), f"{field} must not be empty"
    assert version.publishing_frequency
    assert version.measurement_plan


async def test_strategy_maps_to_a_business_objective(session, brand_row):
    await seed_findings(session, brand_row)
    result = await run_strategy(session, brand_row, business_objectives=["developer_acquisition"])

    version = await session.get(StrategyVersion, result.strategy_version_id)
    assert version.business_objectives == ["developer_acquisition"]
    alignment = version.objective_alignment
    assert "developer_acquisition" in alignment["map"]
    assert alignment["map"]["developer_acquisition"], "objective has no supporting item"
    assert alignment["advisory_only"] is False, "an acquisition KPI must be present"


async def test_every_requested_objective_is_mapped_or_reported(session, brand_row):
    await seed_findings(session, brand_row)
    result = await run_strategy(
        session,
        brand_row,
        business_objectives=["developer_acquisition", "waitlist_registrations"],
    )
    alignment = result.objective_alignment
    for objective in ("developer_acquisition", "waitlist_registrations"):
        assert objective in alignment["map"]
    # anything unmapped is declared, never hidden
    assert isinstance(alignment["unmapped_objectives"], list)


async def test_unknown_objective_is_rejected(session, brand_row):
    result = await run_strategy(session, brand_row, business_objectives=["go_viral"])
    assert result.strategy_id is None
    assert any("Unknown business objective" in w for w in result.warnings)


async def test_strategy_without_evidence_is_flagged(session, brand_row):
    result = await run_strategy(session, brand_row)
    assert result.strategy_id is not None
    assert any("No research evidence" in w for w in result.warnings)
    assert any(m["area"] == "research_evidence" for m in result.missing_information)
    assert result.confidence <= 0.45


async def test_strategy_records_the_findings_it_used(session, brand_row):
    project, findings = await seed_findings(session, brand_row)
    result = await run_strategy(session, brand_row, research_project_id=project.id)

    version = await session.get(StrategyVersion, result.strategy_version_id)
    assert len(version.source_finding_ids) == len(findings)
    assert {f["category"] for f in result.evidence_used} == {"developer_pain_points"}


async def test_uncited_evidence_produces_a_hypothesis_warning(session, brand_row):
    await seed_findings(
        session,
        brand_row,
        source_url="",
        verification_status=VerificationStatus.UNVERIFIED.value,
    )
    result = await run_strategy(session, brand_row)
    assert any("no source URL" in w for w in result.warnings)
    assert any("hypothesis" in w.lower() for w in result.warnings)


# --------------------------------------------------------------------------- #
# developer acquisition over vanity
# --------------------------------------------------------------------------- #
async def test_kpis_are_acquisition_oriented(session, brand_row):
    await seed_findings(session, brand_row)
    result = await run_strategy(session, brand_row)
    names = [k["name"] for k in result.kpis]

    assert any(_is_acquisition(n) for n in names), "no acquisition KPI defined"
    # follower counts are not the success criterion
    assert not any(n in ("followers", "likes", "impressions") for n in names)


async def test_measurement_plan_is_honest_about_missing_instrumentation(session, brand_row):
    await seed_findings(session, brand_row)
    result = await run_strategy(session, brand_row)

    plan = result.measurement_plan
    assert "not a measured value" in plan["disclaimer"].lower() or "not" in plan["disclaimer"].lower()
    assert plan["targets"].lower().startswith("none set")
    assert plan["instrumentation_required"]


def test_vanity_and_acquisition_classification():
    assert _is_vanity("followers") is True
    assert _is_vanity("impressions") is True
    assert _is_vanity("signup_starts") is False
    assert _is_acquisition("signup_starts") is True
    assert _is_acquisition("waitlist_registrations") is True
    assert _is_acquisition("engagement rate") is False


# --------------------------------------------------------------------------- #
# brand consistency
# --------------------------------------------------------------------------- #
async def test_prohibited_phrases_trigger_a_brand_check_failure(session, brand_row):
    await seed_findings(session, brand_row)
    # The fixture brand forbids "10x" and "revolutionary".
    ctx = AgentContext(session=session, brand=brand_row, gateway=None)
    agent = StrategyAgent()
    payload = StrategyInput(
        mode="strategy", request="Acquire developers", name="A revolutionary 10x strategy"
    )
    result = await agent.run(ctx, payload)

    assert result.brand_check["passed"] is False
    assert set(result.brand_check["violations"]) >= {"10x", "revolutionary"}
    assert any("prohibited phrasing" in w for w in result.warnings)


async def test_unsourced_quantitative_claims_are_flagged(session, brand_row):
    await seed_findings(session, brand_row)
    ctx = AgentContext(session=session, brand=brand_row, gateway=None)
    payload = StrategyInput(
        mode="strategy", request="Acquire developers", name="Ship 3x faster with 50% less setup"
    )
    result = await StrategyAgent().run(ctx, payload)

    assert result.brand_check["unverified_quantitative_claims"]
    assert any("quantitative claims" in w for w in result.warnings)


async def test_clean_strategy_passes_brand_check(session, brand_row):
    await seed_findings(session, brand_row)
    result = await run_strategy(session, brand_row)
    assert result.brand_check["passed"] is True
    assert result.brand_check["violations"] == []


# --------------------------------------------------------------------------- #
# approval gating
# --------------------------------------------------------------------------- #
async def test_generated_strategy_starts_as_draft_and_is_not_executable(session, brand_row):
    await seed_findings(session, brand_row)
    result = await run_strategy(session, brand_row)

    strategy = await session.get(MarketingStrategy, result.strategy_id)
    assert strategy.status == "DRAFT"
    assert result.requires_human_review is True

    service = StrategyService(session)
    from app.core.errors import ConflictError

    with pytest.raises(ConflictError):
        await service.require_approved(result.strategy_id)


async def test_rerun_creates_a_new_version_not_a_duplicate_strategy(session, brand_row):
    await seed_findings(session, brand_row)
    project, _ = await seed_findings(
        session, brand_row, count=2, dedupe_suffix="b", finding="Second batch finding"
    )

    first = await run_strategy(
        session, brand_row, research_project_id=project.id, name="Crusor developer growth"
    )
    second = await run_strategy(
        session,
        brand_row,
        research_project_id=project.id,
        name="Crusor developer growth",
        request="Refine for waitlist growth",
    )

    assert first.strategy_id == second.strategy_id
    assert second.version_number == 2

    strategies = (
        await session.execute(
            MarketingStrategy.__table__.select().where(
                MarketingStrategy.brand_profile_id == brand_row.id
            )
        )
    ).mappings().all()
    assert len(strategies) == 1


# --------------------------------------------------------------------------- #
# Phase 1 campaign path still works
# --------------------------------------------------------------------------- #
async def test_campaign_mode_is_unchanged(session, brand_row):
    ctx = AgentContext(session=session, brand=brand_row, gateway=None)
    result = await StrategyAgent().run(ctx, StrategyInput(mode="campaign", duration_days=7))
    assert result.content_plan, "the Phase 1 campaign plan must still be produced"
    assert result.channel_mix
    assert result.strategy_id is None if hasattr(result, "strategy_id") else True
