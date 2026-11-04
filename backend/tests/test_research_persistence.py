"""Research persistence: provenance, deduplication, source validation."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.errors import NotFoundError, ValidationProblem
from app.models.research import (
    AudiencePainPoint,
    ProductFact,
    ResearchFinding,
    ResearchSource,
    ResearchStatus,
    VerificationStatus,
)
from app.research.base import RawFinding, RawSource
from app.services.research import ResearchService


def finding(
    category: str = "developer_pain_points",
    text: str = "Developers lose time to slow feedback loops.",
    *,
    url: str = "https://example.com/survey",
    confidence: float = 0.8,
    status: str = VerificationStatus.UNVERIFIED.value,
) -> RawFinding:
    return RawFinding(
        category=category,
        finding=text,
        evidence_summary="Survey of developer workflows.",
        confidence=confidence,
        source=RawSource(url=url, title="Developer survey", publisher="example.com")
        if url
        else None,
        verification_status=status,
        provider="test",
    )


async def make_project(session, brand_row, **kwargs):  # type: ignore[no-untyped-def]
    service = ResearchService(session)
    project = await service.create_project(
        brand_profile_id=brand_row.id,
        objective=kwargs.pop("objective", "Understand the developer tooling market"),
        **kwargs,
    )
    return service, project


# --------------------------------------------------------------------------- #
# project lifecycle
# --------------------------------------------------------------------------- #
async def test_create_project_persists_and_audits(session, brand_row):
    service, project = await make_project(session, brand_row, max_cost_usd=0.12)
    assert project.status == ResearchStatus.QUEUED.value
    assert project.max_cost_usd == pytest.approx(0.12)
    assert len(project.categories) == 9

    fetched = await service.get_project(project.id)
    assert fetched.id == project.id

    from app.models.audit import AuditLog

    audit = (
        await session.execute(
            select(AuditLog).where(AuditLog.entity_id == str(project.id))
        )
    ).scalars().all()
    assert any(row.action == "research.project_created" for row in audit)


async def test_unknown_project_raises_not_found(session, brand_row):
    import uuid

    with pytest.raises(NotFoundError):
        await ResearchService(session).get_project(uuid.uuid4())


async def test_unknown_category_is_rejected(session, brand_row):
    with pytest.raises(ValidationProblem, match="Unknown research categor"):
        await ResearchService(session).create_project(
            brand_profile_id=brand_row.id,
            objective="x",
            categories=["not_a_category"],
        )


async def test_empty_objective_is_rejected(session, brand_row):
    with pytest.raises(ValidationProblem):
        await ResearchService(session).create_project(
            brand_profile_id=brand_row.id, objective="   "
        )


async def test_negative_budget_is_rejected(session, brand_row):
    with pytest.raises(ValidationProblem):
        await ResearchService(session).create_project(
            brand_profile_id=brand_row.id, objective="x", max_cost_usd=-1
        )


async def test_project_status_transitions_and_counts(session, brand_row):
    service, project = await make_project(session, brand_row)
    await service.mark_running(project)
    assert project.status == ResearchStatus.RUNNING.value
    assert project.started_at is not None

    await service.add_findings(project, [finding(), finding(text="A second point entirely.")])
    await service.finish_project(
        project, status=ResearchStatus.SUCCEEDED.value, summary="done", actual_cost_usd=0.004
    )
    assert project.status == ResearchStatus.SUCCEEDED.value
    assert project.finding_count == 2
    assert project.source_count == 1, "two findings citing one URL must not create two sources"
    assert project.actual_cost_usd == pytest.approx(0.004)
    assert project.finished_at is not None


# --------------------------------------------------------------------------- #
# findings + provenance
# --------------------------------------------------------------------------- #
async def test_findings_persist_with_full_provenance(session, brand_row):
    service, project = await make_project(session, brand_row)
    created = await service.add_findings(
        project,
        [finding(status=VerificationStatus.VERIFIED.value, confidence=0.9)],
    )
    assert len(created) == 1
    row = created[0]
    await session.refresh(row)

    assert row.category == "developer_pain_points"
    assert row.finding
    assert row.source_url == "https://example.com/survey"
    assert row.source_title == "Developer survey"
    assert row.retrieved_at is not None, "retrieval date is mandatory provenance"
    assert row.confidence == pytest.approx(0.9)
    assert row.verification_status == VerificationStatus.VERIFIED.value
    assert row.evidence_summary
    assert row.source_id is not None, "finding must link to its source row"


async def test_finding_without_source_cannot_be_verified_at_db_level(session, brand_row):
    """The CHECK constraint is the last line of defence against fabrication."""
    service, project = await make_project(session, brand_row)
    row = ResearchFinding(
        research_project_id=project.id,
        brand_profile_id=brand_row.id,
        category="market_trends",
        finding="AI adoption is exploding",
        source_url="",
        verification_status=VerificationStatus.VERIFIED.value,
        dedupe_key="manual-1",
    )
    session.add(row)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_service_downgrades_verified_without_source(session, brand_row):
    """Even if a caller bypasses the raw check, the service fixes the status."""
    service, project = await make_project(session, brand_row)
    created = await service.add_findings(
        project,
        [finding(url="", status=VerificationStatus.VERIFIED.value)],
    )
    assert created[0].verification_status == VerificationStatus.UNVERIFIED.value


async def test_duplicate_findings_are_skipped(session, brand_row):
    service, project = await make_project(session, brand_row)
    batch = [finding(), finding(), finding(text="Different claim entirely.")]
    created = await service.add_findings(project, batch)
    assert len(created) == 2, "identical findings must be collapsed"

    again = await service.add_findings(project, [finding()])
    assert again == [], "re-adding an existing finding must be a no-op"

    rows = (
        await session.execute(
            select(ResearchFinding).where(ResearchFinding.research_project_id == project.id)
        )
    ).scalars().all()
    assert len(rows) == 2


async def test_sources_are_deduplicated_by_url(session, brand_row):
    service, project = await make_project(session, brand_row)
    await service.add_findings(
        project,
        [
            finding(text="Claim one about tooling."),
            finding(text="Claim two about tooling."),
            finding(text="Claim three.", url="https://other.example.com/x"),
        ],
    )
    sources = (
        await session.execute(
            select(ResearchSource).where(ResearchSource.research_project_id == project.id)
        )
    ).scalars().all()
    assert len(sources) == 2


async def test_source_requires_a_url(session, brand_row):
    service, project = await make_project(session, brand_row)
    row = ResearchSource(
        brand_profile_id=brand_row.id,
        research_project_id=project.id,
        url="",
        title="No URL",
    )
    session.add(row)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


# --------------------------------------------------------------------------- #
# filtering (the UI relies on this)
# --------------------------------------------------------------------------- #
async def test_findings_filter_by_category_confidence_and_verification(session, brand_row):
    service, project = await make_project(session, brand_row)
    await service.add_findings(
        project,
        [
            finding("developer_pain_points", "Pain A", confidence=0.9),
            finding("developer_pain_points", "Pain B", confidence=0.3),
            finding("market_trends", "Trend A", confidence=0.9),
            finding(
                "market_trends",
                "Trend B",
                confidence=0.95,
                url="https://example.com/trend",
                status=VerificationStatus.VERIFIED.value,
            ),
        ],
    )

    by_category, total = await service.list_findings(
        project_id=project.id, category="market_trends"
    )
    assert total == 2
    assert {f.category for f in by_category} == {"market_trends"}

    high, total_high = await service.list_findings(project_id=project.id, min_confidence=0.85)
    # 0.9, 0.9 and 0.95 clear the bar; the 0.3 finding does not.
    assert total_high == 3
    assert all(f.confidence >= 0.85 for f in high)

    verified, total_verified = await service.list_findings(
        project_id=project.id, verification_status=VerificationStatus.VERIFIED.value
    )
    assert total_verified == 1
    assert verified[0].verification_status == VerificationStatus.VERIFIED.value

    # ordered by confidence descending
    assert high[0].confidence >= high[1].confidence


async def test_category_summary_and_missing_categories(session, brand_row):
    service, project = await make_project(session, brand_row)
    await service.add_findings(
        project,
        [
            finding("developer_pain_points", "Pain A", confidence=0.6),
            finding("developer_pain_points", "Pain B", confidence=0.8),
            finding("market_trends", "Trend A", confidence=0.4),
        ],
    )
    summary = {row["category"]: row for row in await service.category_summary(project.id)}
    assert summary["developer_pain_points"]["findings"] == 2
    assert summary["developer_pain_points"]["avg_confidence"] == pytest.approx(0.7, abs=0.01)

    missing = await service.missing_categories(project)
    assert "developer_pain_points" not in missing
    assert "competitor_identification" in missing
    assert len(missing) == 7


async def test_list_projects_filters_by_status(session, brand_row):
    service, first = await make_project(session, brand_row, objective="First")
    await service.mark_running(first)
    await service.finish_project(first, status=ResearchStatus.SUCCEEDED.value)
    await make_project(session, brand_row, objective="Second")

    succeeded, total = await service.list_projects(
        brand_profile_id=brand_row.id, status=ResearchStatus.SUCCEEDED.value
    )
    assert total == 1
    assert succeeded[0].objective == "First"

    all_projects, total_all = await service.list_projects(brand_profile_id=brand_row.id)
    assert total_all == 2
    # Both projects may share a timestamp on SQLite (second granularity), so the
    # contract asserted here is completeness, not tie order.
    assert {p.objective for p in all_projects} == {"First", "Second"}


# --------------------------------------------------------------------------- #
# pain points + product facts
# --------------------------------------------------------------------------- #
async def test_pain_points_are_promoted_and_deduplicated(session, brand_row):
    service, project = await make_project(session, brand_row)
    findings = await service.add_findings(
        project,
        [finding("developer_pain_points", "Slow feedback loops waste hours.")],
    )
    promoted = await service.record_pain_points(project, findings)
    assert len(promoted) == 1

    again = await service.record_pain_points(project, findings)
    assert again == [], "the same pain point must not be promoted twice"

    rows = (
        await session.execute(
            select(AudiencePainPoint).where(
                AudiencePainPoint.brand_profile_id == brand_row.id
            )
        )
    ).scalars().all()
    assert len(rows) == 1


async def test_verify_fact_requires_a_source(session, brand_row):
    service = ResearchService(session)
    fact = ProductFact(
        brand_profile_id=brand_row.id,
        statement="Runs fully in the browser",
        verification_status=VerificationStatus.NEEDS_CONFIRMATION.value,
        dedupe_key="fact-1",
    )
    session.add(fact)
    await session.flush()

    with pytest.raises(ValidationProblem, match="requires a source URL"):
        await service.verify_fact(fact.id, source_url="   ")

    verified = await service.verify_fact(fact.id, source_url="https://crusor.in/features")
    assert verified.verification_status == VerificationStatus.VERIFIED.value
    assert verified.verified_at is not None
    assert verified.is_public is True


async def test_unverified_facts_are_surfaced_for_confirmation(session, brand_row):
    service = ResearchService(session)
    session.add_all(
        [
            ProductFact(
                brand_profile_id=brand_row.id,
                statement="Unconfirmed capability",
                verification_status=VerificationStatus.NEEDS_CONFIRMATION.value,
                dedupe_key="f-1",
            ),
            ProductFact(
                brand_profile_id=brand_row.id,
                statement="Confirmed capability",
                verification_status=VerificationStatus.VERIFIED.value,
                source_url="https://crusor.in",
                dedupe_key="f-2",
            ),
        ]
    )
    await session.flush()

    pending = await service.unverified_facts(brand_row.id)
    assert len(pending) == 1
    assert pending[0].statement == "Unconfirmed capability"


# --------------------------------------------------------------------------- #
# competitor snapshot
# --------------------------------------------------------------------------- #
async def test_competitor_snapshot_counts_supporting_findings(session, brand_row):
    from app.models.brand import CompetitorProfile

    session.add_all(
        [
            CompetitorProfile(
                brand_profile_id=brand_row.id, name="Cursor", website="https://cursor.com"
            ),
            CompetitorProfile(brand_profile_id=brand_row.id, name="Quiet"),
        ]
    )
    await session.flush()

    service, project = await make_project(session, brand_row)
    await service.add_findings(
        project,
        [
            RawFinding(
                category="competitor_identification",
                finding="Cursor is a direct competitor.",
                competitor_name="Cursor",
                source=RawSource(url="https://cursor.com", title="Cursor"),
            ),
            RawFinding(
                category="competitor_positioning",
                finding="Cursor positions on AI-first editing.",
                competitor_name="Cursor",
                source=RawSource(url="https://cursor.com/pricing", title="Cursor pricing"),
            ),
        ],
    )

    snapshot = {row["name"]: row for row in await service.competitor_snapshot(brand_row.id)}
    assert snapshot["Cursor"]["findings"] == 2
    assert snapshot["Quiet"]["findings"] == 0
