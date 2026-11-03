"""End-to-end Marketing Director strategy workflow.

These tests drive the real LangGraph graph through its real checkpointer: no
stubbed orchestration. The only thing replaced is the research provider, so the
suite never touches the network (and never spends anything).
"""

from __future__ import annotations

import pytest

from app.director.strategy_graph import strategy_graph_topology
from app.director.strategy_runtime import StrategyWorkflowRuntime
from app.models.research import ResearchFinding, VerificationStatus
from app.models.strategy import MarketingStrategy, StrategyApproval, StrategyVersion


@pytest.fixture()
def runtime(session_factory):
    """The real runtime, on the real test database and real checkpointer."""
    return StrategyWorkflowRuntime(session_factory=session_factory)


async def run_workflow(session, brand_row, runtime, **kwargs):  # type: ignore[no-untyped-def]
    return await runtime.start(
        brand_profile_id=brand_row.id,
        objective=kwargs.pop("objective", "Acquire software developers for Crusor AI"),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
async def test_workflow_produces_a_strategy_and_waits_for_approval(
    session, brand_row, runtime
):
    result = await run_workflow(session, brand_row, runtime)
    state = result["state"]

    assert state["status"] == "awaiting_approval", state.get("errors")
    assert state["interrupted"] is True
    assert state["strategy_id"] is not None
    assert state["strategy_version_number"] == 1
    assert state["approval_request"]["type"] == "strategy_approval"
    assert state["approval_request"]["note"].count("does not publish") == 1


async def test_workflow_persists_an_actual_strategy_version(session, brand_row, runtime):
    result = await run_workflow(session, brand_row, runtime)
    state = result["state"]

    version = await session.get(StrategyVersion, state["strategy_version_id"])
    assert version is not None
    assert version.version_number == 1
    assert version.business_objectives == ["developer_acquisition"]
    assert version.kpis

    strategy = await session.get(MarketingStrategy, state["strategy_id"])
    assert strategy.status == "DRAFT"


async def test_workflow_records_events_and_phases(session, brand_row, runtime):
    result = await run_workflow(session, brand_row, runtime)
    state = result["state"]

    entries = [e["entry"] for e in state["trace"]]
    for expected in (
        "objective_understood",
        "brand_retrieved",
        "research_decision",
        "evidence_evaluated",
        "strategy_generated",
        "consistency_checked",
        "approval_requested",
    ):
        assert expected in entries, f"missing trace entry {expected}"

    events = [e["event"] for e in state["events"]]
    assert "research_decision" in events
    assert "brand_consistency_check" in events


async def test_brand_knowledge_is_retrieved_and_unverified_facts_flagged(
    session, brand_row, runtime
):
    from app.models.research import ProductFact

    session.add(
        ProductFact(
            brand_profile_id=brand_row.id,
            statement="Supports a plugin marketplace",
            verification_status=VerificationStatus.NEEDS_CONFIRMATION.value,
            dedupe_key="plugin-fact",
        )
    )
    await session.commit()

    result = await run_workflow(session, brand_row, runtime)
    state = result["state"]

    snapshot = state["brand_snapshot"]
    assert snapshot["company"] == "Crusor AI"
    assert snapshot["audience"]
    assert snapshot["unverified_fact_count"] == 1
    assert any("Supports a plugin marketplace" in c for c in state["unverified_brand_claims"])
    assert any(
        m["area"] == "unverified_product_facts" for m in state["missing_information"]
    )


async def test_cost_estimate_is_present_and_actual_spend_recorded(session, brand_row, runtime):
    result = await run_workflow(session, brand_row, runtime)
    state = result["state"]

    estimate = result["cost_estimate"]
    assert estimate["estimated_cost_usd"] >= 0
    assert estimate["within_budget"] is True

    actual = state["cost_actual"]
    assert "research_usd" in actual
    assert "total_usd" in actual
    # No provider is configured in tests, so spend must be zero, not invented.
    assert actual["total_usd"] == 0.0

    limits = state["limits"]
    assert limits["max_iterations"] >= 1
    assert limits["research_rounds"] >= 1


# --------------------------------------------------------------------------- #
# research evaluation and the bounded research loop
# --------------------------------------------------------------------------- #
async def test_research_runs_and_produces_findings(session, brand_row, runtime):
    result = await run_workflow(session, brand_row, runtime)
    state = result["state"]

    assert state["research_needed"] is True
    assert len(state["research_rounds"]) == 1
    assert state["research_project_id"] is not None

    findings = (
        await session.execute(
            ResearchFinding.__table__.select().where(
                ResearchFinding.brand_profile_id == brand_row.id
            )
        )
    ).mappings().all()
    assert findings, "the workflow must actually persist research findings"


async def test_deterministic_provider_is_not_asked_twice(session, brand_row, runtime):
    """A stable provider cannot improve on a second identical round."""
    result = await run_workflow(session, brand_row, runtime)
    state = result["state"]

    assessment = state["evidence_assessment"]
    assert assessment["live_provider"] is False
    assert state["needs_more_research"] is False
    assert len(state["research_rounds"]) == 1


async def test_existing_evidence_can_skip_research(session, brand_row, runtime):
    """The Director decides research is not needed when evidence is plentiful."""
    from app.models.research import ResearchProject

    project = ResearchProject(
        brand_profile_id=brand_row.id,
        objective="Prior market study",
        categories=["developer_pain_points"],
        provider="deterministic",
    )
    session.add(project)
    await session.flush()

    for index in range(12):
        session.add(
            ResearchFinding(
                research_project_id=project.id,
                brand_profile_id=brand_row.id,
                category="developer_pain_points",
                finding=f"Developers care about fast feedback loops ({index}).",
                source_url="https://example.com/x",
                confidence=0.7,
                verification_status=VerificationStatus.VERIFIED.value,
                dedupe_key=f"pre-{index}",
                provider="deterministic",
            )
        )
    await session.commit()

    result = await run_workflow(session, brand_row, runtime)
    state = result["state"]

    decision = next(e for e in state["trace"] if e["entry"] == "research_decision")
    assert decision["needed"] is False
    assert state["research_rounds"] == []
    assert state["strategy_id"] is not None, "a strategy must still be produced"


async def test_evidence_verdict_reflects_available_evidence(session, brand_row, runtime):
    result = await run_workflow(session, brand_row, runtime)
    assessment = result["state"]["evidence_assessment"]

    assert assessment["verdict"] in ("sufficient", "insufficient", "unavailable")
    assert "categories_missing" in assessment
    assert assessment["total_findings"] >= 0


# --------------------------------------------------------------------------- #
# approval gating
# --------------------------------------------------------------------------- #
async def test_approval_authorises_the_exact_version(session, brand_row, runtime, users):
    result = await run_workflow(session, brand_row, runtime)
    workflow_id = result["workflow_id"]
    state = result["state"]

    resumed = await runtime.resume(
        workflow_id,
        decision={"action": "approve", "reason": "Aligned with Q4 developer acquisition"},
        actor_id=users["founder"].id,
        actor_role="founder",
    )
    final = resumed["state"]

    assert final["status"] == "approved"
    assert final["phase"] == "completed"

    strategy = await session.get(MarketingStrategy, state["strategy_id"])
    await session.refresh(strategy)
    assert strategy.status == "APPROVED"

    approvals = (
        await session.execute(
            StrategyApproval.__table__.select().where(
                StrategyApproval.strategy_id == strategy.id
            )
        )
    ).mappings().all()
    assert len(approvals) == 1
    assert str(approvals[0]["strategy_version_id"]) == state["strategy_version_id"]
    assert approvals[0]["invalidated_at"] is None
    assert approvals[0]["actor_role"] == "founder"


async def test_request_changes_rejects_and_records_reason(session, brand_row, runtime, users):
    result = await run_workflow(session, brand_row, runtime)
    resumed = await runtime.resume(
        result["workflow_id"],
        decision={"action": "request_changes", "reason": "Sharper developer hook needed"},
        actor_id=users["founder"].id,
    )
    final = resumed["state"]
    assert final["status"] == "rejected"
    assert "Sharper developer hook" in final["summary"]

    strategy = await session.get(MarketingStrategy, result["state"]["strategy_id"])
    await session.refresh(strategy)
    assert strategy.status == "CHANGES_REQUESTED"


async def test_non_founder_decision_is_refused_by_the_service(session, brand_row, runtime, users):
    result = await run_workflow(session, brand_row, runtime)
    resumed = await runtime.resume(
        result["workflow_id"],
        decision={"action": "approve", "reason": "looks fine"},
        actor_id=users["marketer"].id,
        actor_role="marketer",
    )
    final = resumed["state"]
    assert final["status"] == "failed"
    assert any(e["code"] == "permission_denied" or "founder" in e["message"].lower()
               for e in final["errors"])


async def test_invalid_decision_is_rejected(session, brand_row, runtime):
    from app.core.errors import ValidationProblem

    result = await run_workflow(session, brand_row, runtime)
    with pytest.raises(ValidationProblem):
        await runtime.resume(result["workflow_id"], decision={"action": "publish"})


async def test_status_reports_interrupt_and_cost(session, brand_row, runtime):
    result = await run_workflow(session, brand_row, runtime)
    status = await runtime.status(result["workflow_id"])

    assert status["awaiting_human"] is True
    assert status["phase"] == "awaiting_approval"
    assert status["strategy_id"] == result["state"]["strategy_id"]
    assert status["cost_estimate"]["budget_usd"] > 0
    assert status["checkpoint_count"] >= 5


async def test_workflow_state_survives_a_new_runtime_instance(session, brand_row, session_factory, runtime):
    """Resume from a fresh runtime: nothing is held in process memory."""
    result = await run_workflow(session, brand_row, runtime)
    fresh = StrategyWorkflowRuntime(session_factory=session_factory)

    status = await fresh.status(result["workflow_id"])
    assert status["strategy_id"] == result["state"]["strategy_id"]

    resumed = await fresh.resume(
        result["workflow_id"], decision={"action": "approve", "reason": "ok"}, actor_role="founder"
    )
    assert resumed["state"]["status"] == "approved"


# --------------------------------------------------------------------------- #
# failure handling
# --------------------------------------------------------------------------- #
async def test_missing_brand_fails_cleanly(session, runtime):
    import uuid

    result = await runtime.start(
        brand_profile_id=uuid.uuid4(), objective="Acquire developers"
    )
    assert result["state"]["status"] == "failed"
    assert result["state"]["errors"] or result["state"]["summary"]


async def test_blank_objective_fails_with_a_clear_code(session, brand_row, runtime):
    result = await runtime.start(brand_profile_id=brand_row.id, objective="   ")
    state = result["state"]
    assert state["status"] == "failed"
    assert state["errors"][0]["code"] == "missing_objective"


# --------------------------------------------------------------------------- #
# topology is documented for the UI
# --------------------------------------------------------------------------- #
def test_topology_documents_the_loop_and_its_bounds():
    topology = strategy_graph_topology()
    assert topology["interrupts"] == ["human_strategy_approval"]
    assert ["evaluate_evidence", "run_research"] in topology["loops"]
    assert len(topology["loop_bounds"]) >= 3
    assert "does not publish" in topology["note"] or "planning only" in topology["note"]
    assert len(topology["nodes"]) == 12
