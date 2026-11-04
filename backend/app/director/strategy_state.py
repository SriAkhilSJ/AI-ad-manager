"""State for the Phase 2 Marketing Director strategy workflow.

This is a **second** graph, not a modification of the Phase 1 campaign graph
(``app/director/state.py``). The two flows answer different questions:

* Phase 1 campaign graph: "produce approved content for a campaign"
* Phase 2 strategy graph: "research the market, then produce an approvable
  strategy"

Keeping them separate means a Phase 2 change can never alter Phase 1 behaviour,
which is a hard requirement for this phase.

The state is JSON-serialisable: LangGraph persists it through the checkpointer,
so it must survive a process restart unchanged.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict


def _replace(_old: Any, new: Any) -> Any:
    return new


def _merge_list(old: list[Any] | None, new: list[Any] | None) -> list[Any]:
    return [*(old or []), *(new or [])]


def _merge_dict(old: dict[str, Any] | None, new: dict[str, Any] | None) -> dict[str, Any]:
    return {**(old or {}), **(new or {})}


class ResearchRound(TypedDict, total=False):
    """One research/evaluate cycle. Recorded so the run is reproducible."""

    round: int
    categories_requested: list[str]
    findings_created: int
    gaps: list[str]
    provider: str
    stop_reason: str
    at: str


class StrategyWorkflowState(TypedDict, total=False):
    """Persisted state for the strategy workflow."""

    # identity
    workflow_id: str
    brand_profile_id: str
    actor_id: str | None
    objective: str
    business_objectives: list[str]
    primary_objective_type: str
    platforms: list[str]
    research_project_id: str | None

    # lifecycle
    status: str
    #   initialized|understanding|retrieving|researching|evaluating|strategising
    #   |checking|persisting|awaiting_approval|approved|rejected|completed
    #   |partial|failed|cancelled|budget_exhausted
    phase: str
    iteration: int
    max_iterations: int
    stop_reason: str | None

    # brand knowledge snapshot (what the run actually read)
    brand_snapshot: dict[str, Any]
    unverified_brand_claims: list[str]

    # research
    research_needed: bool
    research_rounds: Annotated[list[ResearchRound], _merge_list]
    research: dict[str, Any] | None
    findings_summary: dict[str, Any]

    # evidence evaluation
    evidence_verdict: str | None  # sufficient | insufficient | unavailable
    evidence_assessment: dict[str, Any]
    needs_more_research: bool

    # strategy
    strategy: dict[str, Any] | None
    strategy_id: str | None
    strategy_version_id: str | None
    strategy_version_number: int
    brand_check: dict[str, Any]
    missing_information: list[dict[str, Any]]
    unverified_claims_avoided: list[str]

    # cost / limits
    budget: dict[str, Any]
    cost_estimate: dict[str, Any]
    cost_actual: dict[str, Any]
    limits: dict[str, Any]

    # human-in-the-loop
    approval_request: dict[str, Any] | None
    approval_decision: dict[str, Any] | None

    # observability
    errors: Annotated[list[dict[str, Any]], _merge_list]
    trace: Annotated[list[dict[str, Any]], _merge_list]
    agent_runs: Annotated[list[str], _merge_list]
    events: Annotated[list[dict[str, Any]], _merge_list]

    summary: str
    interrupt_reason: str | None


def initial_strategy_state(
    *,
    workflow_id: str,
    brand_profile_id: str,
    objective: str,
    actor_id: str | None = None,
    business_objectives: list[str] | None = None,
    primary_objective_type: str = "developer_acquisition",
    platforms: list[str] | None = None,
    research_project_id: str | None = None,
    max_iterations: int = 4,
    budget: dict[str, Any] | None = None,
    cost_estimate: dict[str, Any] | None = None,
) -> StrategyWorkflowState:
    return StrategyWorkflowState(
        workflow_id=workflow_id,
        brand_profile_id=brand_profile_id,
        actor_id=actor_id,
        objective=objective,
        business_objectives=list(business_objectives or ["developer_acquisition"]),
        primary_objective_type=primary_objective_type,
        platforms=list(platforms or ["linkedin", "x"]),
        research_project_id=research_project_id,
        status="initialized",
        phase="intake",
        iteration=0,
        max_iterations=max_iterations,
        stop_reason=None,
        brand_snapshot={},
        unverified_brand_claims=[],
        research_needed=True,
        research_rounds=[],
        research=None,
        findings_summary={},
        evidence_verdict=None,
        evidence_assessment={},
        needs_more_research=False,
        strategy=None,
        strategy_id=None,
        strategy_version_id=None,
        strategy_version_number=0,
        brand_check={},
        missing_information=[],
        unverified_claims_avoided=[],
        budget=budget or {},
        cost_estimate=cost_estimate or {},
        cost_actual={},
        limits={},
        approval_request=None,
        approval_decision=None,
        errors=[],
        trace=[],
        agent_runs=[],
        events=[],
        summary="",
        interrupt_reason=None,
    )


PHASE_LABELS: dict[str, str] = {
    "intake": "Understanding the objective",
    "retrieving": "Loading brand knowledge",
    "researching": "Researching the market",
    "evaluating": "Evaluating the evidence",
    "strategising": "Building the strategy",
    "checking": "Checking brand consistency",
    "persisting": "Saving the strategy",
    "awaiting_approval": "Waiting for founder approval",
    "approved": "Approved",
    "completed": "Completed",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "budget_exhausted": "Stopped: budget exhausted",
    "partial": "Stopped: execution limit reached",
}

EvidenceVerdict = Literal["sufficient", "insufficient", "unavailable"]
