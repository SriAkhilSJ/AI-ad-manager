"""Strategy Studio API, plus the Phase 2 Director workflow endpoints.

Authorisation:
* reads: any authenticated user;
* generate / edit: marketer or above;
* approve / reject / request changes / revoke: founder only.

Execution safety: nothing in this router publishes, sends, spends or modifies a
live account. ``/execution-status`` exists to show *why* execution is blocked,
not to execute anything.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from app.api.deps import DB, CurrentUser, EditorUser, FounderUser
from app.core.errors import NotFoundError, ValidationProblem
from app.director.strategy_runtime import StrategyWorkflowRuntime
from app.models.research import BUSINESS_OBJECTIVES
from app.schemas.strategy import (
    StrategyDecisionRequest,
    StrategyDetail,
    StrategyDirectorDecision,
    StrategyDirectorStartRequest,
    StrategyGenerateRequest,
    StrategyOut,
    StrategySummary,
    StrategyVersionCreate,
    StrategyVersionOut,
    VersionDiffOut,
)
from app.services.brand import BrandService
from app.services.strategy import StrategyService, VERSION_PAYLOAD_FIELDS

router = APIRouter(prefix="/strategy", tags=["strategy"])
director_router = APIRouter(prefix="/director/strategy", tags=["director"])


# --------------------------------------------------------------------------- #
# capability
# --------------------------------------------------------------------------- #
@router.get("/capabilities", summary="Strategy generation capabilities and budget")
async def capabilities(user: CurrentUser) -> dict[str, Any]:
    from app.core.config import get_settings
    from app.director.strategy_graph import strategy_graph_topology

    settings = get_settings()
    runtime = StrategyWorkflowRuntime()
    return {
        "business_objectives": list(BUSINESS_OBJECTIVES),
        "budget_usd": settings.strategy_budget_usd,
        "budget_inr_equivalent": round(
            settings.strategy_budget_usd * settings.usd_inr_rate, 2
        ),
        "max_iterations": settings.strategy_max_iterations,
        "models": {
            "reasoning": settings.llm_reasoning_model,
            "auxiliary": settings.llm_auxiliary_model,
        },
        "topology": strategy_graph_topology(),
        "estimate_example": runtime.estimate_cost(objective="example"),
        "note": (
            "Strategy generation drafts a plan for founder approval. It never publishes "
            "content, sends email, spends ad budget or changes a live account."
        ),
    }


# --------------------------------------------------------------------------- #
# generate
# --------------------------------------------------------------------------- #
@router.post(
    "/generate",
    response_model=StrategyDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a strategy version from research evidence",
)
async def generate(
    payload: StrategyGenerateRequest, session: DB, user: EditorUser
) -> StrategyDetail:
    from app.agents.base import AgentContext
    from app.agents.registry import get_registry

    brand = await BrandService(session).get(payload.brand_profile_id)
    if brand is None:
        raise NotFoundError(f"Brand profile {payload.brand_profile_id} not found.")

    unknown = [o for o in payload.business_objectives if o not in BUSINESS_OBJECTIVES]
    if unknown:
        raise ValidationProblem(
            f"Unknown business objective(s): {', '.join(unknown)}",
            details={"allowed": list(BUSINESS_OBJECTIVES)},
        )

    ctx = AgentContext(session=session, brand=brand, actor_id=user.id)
    agent = get_registry().get("strategy")
    try:
        result = await agent.run(
            ctx,
            {
                "brand_profile_id": str(brand.id),
                "request": payload.objective,
                "mode": "strategy",
                "name": payload.name,
                "business_objectives": payload.business_objectives,
                "primary_objective_type": payload.primary_objective_type,
                "platforms": payload.platforms,
                "research_project_id": (
                    str(payload.research_project_id) if payload.research_project_id else None
                ),
                "max_cost_usd": payload.max_cost_usd,
            },
        )
    except Exception as exc:  # noqa: BLE001 - API boundary
        await session.rollback()
        raise ValidationProblem(
            f"Strategy generation failed: {str(exc)[:300]}",
        ) from exc

    dump = result.model_dump(mode="json")
    strategy_id = dump.get("strategy_id")
    if not strategy_id:
        await session.commit()
        raise ValidationProblem(
            "Strategy generation produced no strategy.",
            details={"warnings": dump.get("warnings", [])},
        )

    await session.commit()
    return await _detail(session, uuid.UUID(str(strategy_id)))


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #
async def _detail(session, strategy_id: uuid.UUID) -> StrategyDetail:  # type: ignore[no-untyped-def]
    service = StrategyService(session)
    strategy = await service.get_strategy(strategy_id)
    version = await service.current_version(strategy)
    approval = await service.valid_approval(strategy)
    versions = await service.list_versions(strategy.id)
    history = await service.approval_history(strategy.id)
    experiments = await service.list_experiments(strategy.id)

    blocked = ""
    can_execute = False
    try:
        await service.require_approved(strategy.id)
        can_execute = True
    except Exception as exc:  # noqa: BLE001 - the reason is the payload
        blocked = str(exc)

    version_rows = [
        {
            "id": str(v.id),
            "version_number": v.version_number,
            "content_hash": v.content_hash,
            "change_note": v.change_note,
            "confidence": v.confidence,
            "cost_usd": v.cost_usd,
            "generation_mode": v.generation_mode,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "business_objectives": v.business_objectives,
        }
        for v in versions
    ]

    return StrategyDetail(
        strategy=StrategyOut.model_validate(strategy),
        current_version=(
            StrategyVersionOut.model_validate(version) if version is not None else None
        ),
        versions=version_rows,
        approval=(
            {
                "id": str(approval.id),
                "strategy_version_id": str(approval.strategy_version_id),
                "actor_id": str(approval.actor_id) if approval.actor_id else None,
                "actor_role": approval.actor_role,
                "reason": approval.reason,
                "content_hash": approval.content_hash,
                "created_at": approval.created_at.isoformat() if approval.created_at else None,
            }
            if approval is not None
            else None
        ),
        approval_history=history,
        can_execute=can_execute,
        execution_blocked_reason=blocked,
        experiments=[
            {
                "id": str(e.id),
                "name": e.name,
                "hypothesis": e.hypothesis,
                "channel": e.channel,
                "objective_type": e.objective_type,
                "primary_metric": e.primary_metric,
                "expected_effort": e.expected_effort,
                "status": e.status,
            }
            for e in experiments
        ],
    )


@router.get("/", response_model=list[StrategySummary], summary="List strategies")
async def list_strategies(
    session: DB,
    user: CurrentUser,
    brand_profile_id: uuid.UUID | None = None,
    strategy_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[StrategySummary]:
    service = StrategyService(session)
    rows, _ = await service.list_strategies(
        brand_profile_id=brand_profile_id, status=strategy_status, limit=limit, offset=offset
    )
    out: list[StrategySummary] = []
    for strategy in rows:
        approval = await service.valid_approval(strategy)
        version = await service.current_version(strategy)
        out.append(
            StrategySummary(
                **StrategyOut.model_validate(strategy).model_dump(),
                has_valid_approval=approval is not None,
                approved_version_id=approval.strategy_version_id if approval else None,
                approved_at=approval.created_at if approval else None,
                latest_version_number=version.version_number if version else 0,
                confidence=version.confidence if version else 0.0,
                business_objectives=list(version.business_objectives) if version else [],
            )
        )
    return out


@router.get("/{strategy_id}", response_model=StrategyDetail, summary="Strategy with versions")
async def get_strategy(strategy_id: uuid.UUID, session: DB, user: CurrentUser) -> StrategyDetail:
    return await _detail(session, strategy_id)


@router.get(
    "/{strategy_id}/versions/{version_id}",
    response_model=StrategyVersionOut,
    summary="A specific strategy version",
)
async def get_version(
    strategy_id: uuid.UUID, version_id: uuid.UUID, session: DB, user: CurrentUser
) -> StrategyVersionOut:
    service = StrategyService(session)
    version = await service.get_version(version_id)
    if version.strategy_id != strategy_id:
        raise NotFoundError("That version does not belong to this strategy.")
    return StrategyVersionOut.model_validate(version)


@router.get(
    "/{strategy_id}/diff",
    response_model=VersionDiffOut,
    summary="Compare two versions",
)
async def diff_versions(
    strategy_id: uuid.UUID,
    session: DB,
    user: CurrentUser,
    left: Annotated[int, Query(ge=1)],
    right: Annotated[int, Query(ge=1)],
) -> VersionDiffOut:
    service = StrategyService(session)
    versions = await service.list_versions(strategy_id)
    by_number = {v.version_number: v for v in versions}
    if left not in by_number or right not in by_number:
        raise NotFoundError(
            "Both versions must exist for this strategy.",
            details={"available": sorted(by_number)},
        )
    return VersionDiffOut(**await service.diff_versions(by_number[left], by_number[right]))


# --------------------------------------------------------------------------- #
# edit (always a new version; invalidates approval)
# --------------------------------------------------------------------------- #
@router.post(
    "/{strategy_id}/versions",
    response_model=StrategyDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new version (invalidates any approval)",
)
async def create_version(
    strategy_id: uuid.UUID,
    payload: StrategyVersionCreate,
    session: DB,
    user: EditorUser,
) -> StrategyDetail:
    service = StrategyService(session)
    strategy = await service.get_strategy(strategy_id)
    current = await service.current_version(strategy)
    if current is None:
        raise ValidationProblem("Strategy has no current version to derive from.")

    # Unspecified fields carry forward; specified fields replace.
    merged: dict[str, Any] = {f: getattr(current, f) for f in VERSION_PAYLOAD_FIELDS}
    supplied = payload.model_dump(exclude_none=True, exclude={"change_note"})
    merged.update(supplied)

    await service.add_version(
        strategy,
        payload=merged,
        change_note=payload.change_note or "Founder edit",
        created_by=user.id,
        created_by_agent=None,
        generation_mode="manual",
        confidence=current.confidence,
    )
    await session.commit()
    return await _detail(session, strategy_id)


# --------------------------------------------------------------------------- #
# approval
# --------------------------------------------------------------------------- #
@router.post(
    "/{strategy_id}/decision",
    response_model=StrategyDetail,
    summary="Founder decision on a strategy version",
)
async def decide(
    strategy_id: uuid.UUID,
    payload: StrategyDecisionRequest,
    session: DB,
    user: FounderUser,
) -> StrategyDetail:
    service = StrategyService(session)
    strategy = await service.get_strategy(strategy_id)
    try:
        await service.decide(
            strategy,
            action=payload.action,
            actor_id=user.id,
            actor_role=user.role,
            reason=payload.reason,
            version_id=payload.version_id,
        )
    except Exception as exc:  # noqa: BLE001 - domain error surfaces as 4xx
        await session.rollback()
        raise
    await session.commit()
    return await _detail(session, strategy_id)


@router.get(
    "/{strategy_id}/execution-status",
    summary="Whether this strategy may be executed, and why not",
)
async def execution_status(
    strategy_id: uuid.UUID, session: DB, user: CurrentUser
) -> dict[str, Any]:
    service = StrategyService(session)
    strategy = await service.get_strategy(strategy_id)
    version = await service.current_version(strategy)
    try:
        _, approved_version, approval = await service.require_approved(strategy_id)
        allowed = True
        reason = ""
        approved_version_id = str(approved_version.id)
        approval_id = str(approval.id)
    except Exception as exc:  # noqa: BLE001 - the refusal is the answer
        allowed = False
        reason = str(exc)
        approved_version_id = ""
        approval_id = ""

    return {
        "strategy_id": str(strategy.id),
        "status": strategy.status,
        "current_version_id": str(version.id) if version else None,
        "current_version_number": version.version_number if version else None,
        "can_execute": allowed,
        "blocked_reason": reason,
        "approved_version_id": approved_version_id,
        "approval_id": approval_id,
        "actions_available": (
            "Planning only. Content publishing, email sending, ad spend and live "
            "account changes remain unavailable in this deployment."
        ),
    }


@router.get(
    "/{strategy_id}/approval-history",
    summary="Append-only approval ledger for this strategy",
)
async def approval_history(
    strategy_id: uuid.UUID, session: DB, user: CurrentUser
) -> list[dict[str, Any]]:
    return await StrategyService(session).approval_history(strategy_id)


# --------------------------------------------------------------------------- #
# Phase 2 Director workflow
# --------------------------------------------------------------------------- #
@director_router.post(
    "/start",
    summary="Run the strategy workflow: research -> strategy -> approval",
)
async def start_workflow(
    payload: StrategyDirectorStartRequest, session: DB, user: EditorUser
) -> dict[str, Any]:
    brand = await BrandService(session).get(payload.brand_profile_id)
    if brand is None:
        raise NotFoundError(f"Brand profile {payload.brand_profile_id} not found.")

    unknown = [o for o in payload.business_objectives if o not in BUSINESS_OBJECTIVES]
    if unknown:
        raise ValidationProblem(
            f"Unknown business objective(s): {', '.join(unknown)}",
            details={"allowed": list(BUSINESS_OBJECTIVES)},
        )

    runtime = StrategyWorkflowRuntime()
    try:
        return await runtime.start(
            brand_profile_id=brand.id,
            objective=payload.objective,
            actor_id=user.id,
            business_objectives=payload.business_objectives,
            primary_objective_type=payload.primary_objective_type,
            platforms=payload.platforms,
            research_project_id=(
                str(payload.research_project_id) if payload.research_project_id else None
            ),
            budget_usd=payload.budget_usd,
            max_iterations=payload.max_iterations,
        )
    except Exception as exc:  # noqa: BLE001 - API boundary
        raise ValidationProblem(f"Strategy workflow failed to start: {str(exc)[:300]}") from exc


@director_router.get("/estimate", summary="Pre-flight cost estimate before running")
async def estimate(
    user: CurrentUser,
    budget_usd: Annotated[float | None, Query(ge=0, le=100)] = None,
) -> dict[str, Any]:
    return StrategyWorkflowRuntime().estimate_cost(
        objective="estimate", budget_usd=budget_usd
    )


@director_router.get("/topology", summary="The workflow graph as data")
async def topology(user: CurrentUser) -> dict[str, Any]:
    return StrategyWorkflowRuntime.topology()


@director_router.get("/workflows", summary="Recent strategy workflows")
async def list_workflows(
    user: CurrentUser, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[dict[str, Any]]:
    return await StrategyWorkflowRuntime().list_workflows(limit=limit)


@director_router.get("/workflows/{workflow_id}", summary="Full workflow status")
async def workflow_status(workflow_id: str, user: CurrentUser) -> dict[str, Any]:
    return await StrategyWorkflowRuntime().status(workflow_id)


@director_router.post(
    "/workflows/{workflow_id}/decision",
    summary="Founder decision on the pending strategy version",
)
async def workflow_decision(
    workflow_id: str, payload: StrategyDirectorDecision, user: FounderUser
) -> dict[str, Any]:
    return await StrategyWorkflowRuntime().resume(
        workflow_id,
        decision={"action": payload.action, "reason": payload.reason},
        actor_id=user.id,
        actor_role=user.role,
    )


@director_router.delete("/workflows/{workflow_id}", summary="Delete a workflow's checkpoints")
async def delete_workflow(workflow_id: str, user: FounderUser) -> dict[str, Any]:
    await StrategyWorkflowRuntime().delete(workflow_id)
    return {"workflow_id": workflow_id, "deleted": True}
