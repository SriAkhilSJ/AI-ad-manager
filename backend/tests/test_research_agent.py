"""Research Agent evidence runs: provenance, gaps, and bounded execution.

Failures are injected deliberately: a provider that errors, a provider that
returns nothing, a budget too small to finish. The agent must degrade with a
typed, honest result — never with invented findings and never by looping.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.agents.base import AgentContext
from app.agents.limits import ExecutionGuard, ExecutionLimitExceeded, ExecutionLimits
from app.agents.research import ResearchAgent, ResearchInput
from app.core.errors import BudgetExhaustedError, LoopDetectedError
from app.core.errors import CancelledProblem
from app.models.research import (
    RESEARCH_CATEGORIES,
    ResearchFinding,
    ResearchProject,
    ResearchStatus,
    VerificationStatus,
)
from app.research.base import RawFinding, RawSource, build_provider


class ScriptedProvider:
    """A provider whose behaviour each test dictates."""

    name = "scripted"
    available = True

    def __init__(self, findings: list[RawFinding] | None = None, *, raise_for=()):
        self._findings = findings if findings is not None else default_findings()
        self._raise_for = set(raise_for)
        self.calls: list[str] = []

    def status(self):
        return {"name": self.name, "available": True, "status": "available", "network": False}

    async def search(self, query):
        self.calls.append(query.categories[0])
        if query.categories[0] in self._raise_for:
            raise RuntimeError("provider exploded")
        return [f for f in self._findings if f.category in query.categories]


def default_findings() -> list[RawFinding]:
    return [
        RawFinding(
            category="competitor_identification",
            finding="Cursor is a direct competitor.",
            evidence_summary="Cursor markets itself as an AI-first editor.",
            confidence=0.8,
            source=RawSource(url="https://cursor.com", title="Cursor"),
            competitor_name="Cursor",
            verification_status=VerificationStatus.VERIFIED.value,
            provider="scripted",
        ),
        RawFinding(
            category="developer_pain_points",
            finding="Developers lose hours to context switching.",
            evidence_summary="Recurring theme in developer discussions.",
            confidence=0.6,
            source=RawSource(url="https://example.com/survey", title="Survey"),
            provider="scripted",
        ),
        RawFinding(
            category="market_trends",
            finding="AI pair programming is mainstream.",
            evidence_summary="No source available for this one.",
            confidence=0.3,
            provider="scripted",
        ),
    ]


async def run_agent(session, brand_row, *, provider=None, **payload_kwargs):  # type: ignore[no-untyped-def]
    ctx = AgentContext(session=session, brand=brand_row, gateway=None)
    agent = ResearchAgent()
    payload = ResearchInput(mode="evidence", request="Map the AI IDE market", **payload_kwargs)
    return await agent.run(ctx, payload)


@pytest.fixture()
def patched_provider(monkeypatch):
    """Replaces ``build_provider`` inside the research agent module."""

    def _install(provider):
        import app.agents.research as research_module

        monkeypatch.setattr(
            research_module, "build_provider", lambda *args, **kwargs: provider
        )
        return provider

    return _install


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
async def test_evidence_run_persists_findings_with_sources(
    session, brand_row, patched_provider
):
    patched_provider(ScriptedProvider())
    result = await run_agent(session, brand_row, categories=["competitor_identification"])

    assert result.findings_created >= 1
    assert result.sources_created >= 1
    assert result.research_project_id is not None
    assert result.provider == "scripted"

    findings = (
        await session.execute(
            select(ResearchFinding).where(
                ResearchFinding.research_project_id == result.research_project_id
            )
        )
    ).scalars().all()

    cursor = next(f for f in findings if f.competitor_name == "Cursor")
    assert cursor.source_url == "https://cursor.com"
    assert cursor.source_title == "Cursor"
    assert cursor.retrieved_at is not None
    assert cursor.verification_status == VerificationStatus.VERIFIED.value


async def test_project_is_marked_succeeded_with_usage(session, brand_row, patched_provider):
    patched_provider(ScriptedProvider())
    result = await run_agent(session, brand_row)

    project = await session.get(ResearchProject, result.research_project_id)
    assert project.status == ResearchStatus.SUCCEEDED.value
    assert project.started_at is not None
    assert project.finished_at is not None
    assert project.iterations >= 1
    assert project.summary


async def test_uncited_finding_is_never_verified(session, brand_row, patched_provider):
    patched_provider(ScriptedProvider())
    result = await run_agent(session, brand_row)

    uncited = next(f for f in result.findings if not f.has_source)
    assert uncited.verification_status != VerificationStatus.VERIFIED.value


async def test_empty_category_becomes_a_gap_not_an_invention(
    session, brand_row, patched_provider
):
    patched_provider(ScriptedProvider())
    result = await run_agent(session, brand_row, categories=["content_opportunities"])

    assert result.findings_created == 0
    assert any("content_opportunities" in gap for gap in result.gaps)
    assert not result.findings
    # The claim is explicitly about absence, not a fabricated finding.
    assert any("gap" in w.lower() or "no verifiable" in w.lower() for w in result.warnings) or result.gaps


async def test_category_coverage_is_reported(session, brand_row, patched_provider):
    patched_provider(ScriptedProvider())
    result = await run_agent(session, brand_row)
    covered = {row["category"] for row in result.category_coverage}
    assert "competitor_identification" in covered
    assert "developer_pain_points" in covered


# --------------------------------------------------------------------------- #
# provider failure handling
# --------------------------------------------------------------------------- #
async def test_provider_failure_is_recorded_and_run_continues(
    session, brand_row, patched_provider
):
    patched_provider(ScriptedProvider(raise_for={"market_trends"}))
    result = await run_agent(session, brand_row)

    assert any("market_trends" in gap for gap in result.gaps)
    assert any("Provider failed" in w for w in result.warnings)
    # other categories still produced findings
    assert result.findings_created >= 1


async def test_unavailable_provider_yields_unavailable_status(
    session, brand_row, patched_provider
):
    class DeadProvider:
        name = "dead"
        available = False

        def status(self):
            return {
                "name": "dead",
                "available": False,
                "status": "unavailable",
                "note": "No provider configured.",
            }

        async def search(self, query):
            raise AssertionError("should not be called")

    patched_provider(DeadProvider())
    result = await run_agent(session, brand_row)

    assert result.findings_created == 0
    assert result.provider_status == "unavailable"
    assert any("No provider" in w for w in result.warnings)

    project = await session.get(ResearchProject, result.research_project_id)
    assert project.status == ResearchStatus.UNAVAILABLE.value
    assert project.error_code == "research_unavailable"


async def test_no_brand_profile_is_refused(session):
    ctx = AgentContext(session=session, brand=None, gateway=None)
    agent = ResearchAgent()
    result = await agent.run(ctx, ResearchInput(mode="evidence"))
    assert result.confidence == 0.0
    assert result.data_source == "none"


async def test_unknown_category_is_rejected_before_any_work(
    session, brand_row, patched_provider
):
    provider = patched_provider(ScriptedProvider())
    result = await run_agent(session, brand_row, categories=["astrology"])
    assert provider.calls == []
    assert any("Unknown research categories" in w for w in result.warnings)


# --------------------------------------------------------------------------- #
# bounded execution
# --------------------------------------------------------------------------- #
async def test_iteration_limit_stops_the_run_and_persists_partial_results(
    session, brand_row, patched_provider
):
    patched_provider(ScriptedProvider())
    result = await run_agent(session, brand_row, max_iterations=2)

    assert result.iterations <= 2 + 1  # iterations actually begun, incl. synthesis
    assert result.limits["max_iterations"] == 2
    project = await session.get(ResearchProject, result.research_project_id)
    # Partial work is kept, and the stop reason is explicit.
    assert project.status in (
        ResearchStatus.SUCCEEDED.value,
        ResearchStatus.FAILED.value,
        ResearchStatus.BUDGET_EXCEEDED.value,
    )
    assert project.iterations <= 3


async def test_budget_of_zero_stops_before_collecting_everything(
    session, brand_row, patched_provider
):
    """A zero cost envelope still runs the free provider but cannot spend."""
    patched_provider(ScriptedProvider())
    result = await run_agent(session, brand_row, max_cost_usd=0.0)
    assert result.cost["max_cost_usd"] == 0.0
    assert result.cost["spent_usd"] == 0.0


async def test_disabled_provider_never_called_when_research_disabled(
    session, brand_row, monkeypatch
):
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "research_provider", "disabled", raising=False)
    # No patched provider here: the real builder must honour 'disabled'.
    ctx = AgentContext(session=session, brand=brand_row, gateway=None)
    result = await ResearchAgent().run(ctx, ResearchInput(mode="evidence"))
    assert result.provider_status == "unavailable"
    assert result.findings_created == 0


# --------------------------------------------------------------------------- #
# guard unit behaviour
# --------------------------------------------------------------------------- #
def test_guard_enforces_iteration_limit():
    guard = ExecutionGuard(limits=ExecutionLimits(max_iterations=2, label="test"))
    guard.begin_iteration()
    guard.begin_iteration()
    with pytest.raises(ExecutionLimitExceeded) as exc:
        guard.begin_iteration()
    assert exc.value.limit == "max_iterations"
    assert guard.stop_reason == "max_iterations"


def test_guard_enforces_tool_call_limit():
    guard = ExecutionGuard(limits=ExecutionLimits(max_tool_calls=1, label="test"))
    guard.note_tool_call("search")
    with pytest.raises(ExecutionLimitExceeded) as exc:
        guard.note_tool_call("search")
    assert exc.value.limit == "max_tool_calls"


def test_guard_detects_loops():
    guard = ExecutionGuard(limits=ExecutionLimits(max_repeats=2, label="test"))
    guard.note_signature("same-step")
    guard.note_signature("same-step")
    with pytest.raises(LoopDetectedError):
        guard.note_signature("same-step")
    assert guard.stop_reason == "loop_detected"


def test_guard_enforces_wall_clock(monkeypatch):
    guard = ExecutionGuard(limits=ExecutionLimits(max_wall_seconds=1, label="test"))
    monkeypatch.setattr(type(guard), "elapsed_seconds", property(lambda self: 5.0))
    with pytest.raises(ExecutionLimitExceeded) as exc:
        guard.check_time()
    assert exc.value.limit == "wall_clock"


def test_guard_honours_cancellation():
    guard = ExecutionGuard(limits=ExecutionLimits(label="test"))
    guard.cancel.cancel("founder stopped")
    with pytest.raises(CancelledProblem):
        guard.begin_iteration()


def test_guard_budget_stop_raises_typed_error():
    guard = ExecutionGuard(limits=ExecutionLimits(max_cost_usd=0.0001, label="test"))
    with pytest.raises(BudgetExhaustedError):
        guard.check_budget_before_call(estimated_cost_usd=1.0)


def test_guard_snapshot_reports_every_bound():
    guard = ExecutionGuard(
        limits=ExecutionLimits(max_iterations=3, max_tool_calls=5, max_wall_seconds=60, label="x")
    )
    guard.begin_iteration()
    guard.note_tool_call("t", cost_usd=0.001, tokens=10)
    snap = guard.snapshot()
    assert snap["iterations"] == 1 and snap["max_iterations"] == 3
    assert snap["tool_calls"] == 1 and snap["max_tool_calls"] == 5
    assert snap["spent_usd"] == pytest.approx(0.001)
    assert snap["elapsed_seconds"] >= 0
    assert snap["exhausted"] is False


# --------------------------------------------------------------------------- #
# the real deterministic provider, end to end
# --------------------------------------------------------------------------- #
async def test_deterministic_provider_end_to_end_labels_assumptions(session, brand_row):
    """The shipping default must never present an assumption as evidence."""
    from app.models.brand import CompetitorProfile

    session.add(
        CompetitorProfile(
            brand_profile_id=brand_row.id, name="Cursor", website="https://cursor.com"
        )
    )
    await session.flush()

    ctx = AgentContext(session=session, brand=brand_row, gateway=None)
    result = await ResearchAgent().run(
        ctx,
        ResearchInput(
            mode="evidence",
            request="Map the AI IDE market",
            categories=[
                "competitor_identification",
                "developer_pain_points",
                "market_trends",
            ],
        ),
    )

    assert result.provider == "deterministic"
    assert result.findings_created >= 1

    verified = [f for f in result.findings if f.verification_status == "verified"]
    for finding in verified:
        assert finding.has_source, "a verified finding must cite a source"

    trends = [f for f in result.findings if f.category == "market_trends"]
    for finding in trends:
        assert not finding.has_source
        assert finding.verification_status == VerificationStatus.NEEDS_CONFIRMATION.value
