"""Graph state for the Marketing Director.

The state is JSON-serialisable by design (LangGraph persists it through the
checkpoint saver, and it must survive a process restart unchanged).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict


def _replace(_old: Any, new: Any) -> Any:
    """Last-write-wins reducer for scalar fields."""
    return new


def _merge_list(old: list[Any] | None, new: list[Any] | None) -> list[Any]:
    """Append-only reducer for traces/events."""
    return [*(old or []), *(new or [])]


class PlanTask(TypedDict, total=False):
    id: str
    agent: str
    action: str
    params: dict[str, Any]
    depends_on: list[str]
    status: Literal["pending", "running", "done", "failed", "skipped"]


class DirectorState(TypedDict, total=False):
    """Persisted workflow state."""

    # identity
    workflow_id: str
    brand_profile_id: str
    campaign_id: str | None
    actor_id: str | None
    objective: str
    platforms: list[str]

    # lifecycle
    status: str  # initialized|planning|executing|reviewing|awaiting_approval|completed|failed|cancelled
    phase: str
    iteration: int
    max_iterations: int

    # planning
    plan: list[PlanTask]
    current_task_index: int

    # results keyed by task id (structured agent outputs)
    results: dict[str, Any]
    research: dict[str, Any] | None
    strategy: dict[str, Any] | None
    drafts: list[dict[str, Any]]
    creative_brief: dict[str, Any] | None

    # QA
    qa_verdict: str | None
    qa_findings: list[dict[str, Any]]

    # human-in-the-loop
    approval_request: dict[str, Any] | None
    approval_decision: dict[str, Any] | None

    # observability
    errors: Annotated[list[dict[str, Any]], _merge_list]
    trace: Annotated[list[dict[str, Any]], _merge_list]
    agent_runs: Annotated[list[str], _merge_list]

    # post-approval lifecycle (schedule → publish → measure → optimise)
    lifecycle_stage: str  # none|scheduled|published|measured|optimised|handed_off
    decision_applied: list[dict[str, Any]]
    schedule_plan: list[dict[str, Any]]
    scheduled_post_ids: list[str]
    schedule_blocked: list[dict[str, Any]]
    publish_results: dict[str, Any] | None
    analytics: dict[str, Any] | None
    optimizations: list[dict[str, Any]]
    optimization_note: str

    # outputs
    content_item_ids: list[str]
    created_campaign_id: str | None
    summary: str
    interrupt_reason: str | None


def initial_state(
    *,
    workflow_id: str,
    brand_profile_id: str,
    objective: str,
    actor_id: str | None = None,
    campaign_id: str | None = None,
    platforms: list[str] | None = None,
    max_iterations: int = 12,
) -> DirectorState:
    return DirectorState(
        workflow_id=workflow_id,
        brand_profile_id=brand_profile_id,
        campaign_id=campaign_id,
        actor_id=actor_id,
        objective=objective,
        platforms=platforms or ["linkedin"],
        status="initialized",
        phase="intake",
        iteration=0,
        max_iterations=max_iterations,
        plan=[],
        current_task_index=0,
        results={},
        research=None,
        strategy=None,
        drafts=[],
        creative_brief=None,
        qa_verdict=None,
        qa_findings=[],
        approval_request=None,
        approval_decision=None,
        errors=[],
        trace=[],
        agent_runs=[],
        lifecycle_stage="none",
        decision_applied=[],
        schedule_plan=[],
        scheduled_post_ids=[],
        schedule_blocked=[],
        publish_results=None,
        analytics=None,
        optimizations=[],
        optimization_note="",
        content_item_ids=[],
        created_campaign_id=None,
        summary="",
        interrupt_reason=None,
    )
