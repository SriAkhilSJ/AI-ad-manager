"""Marketing Director workflow endpoints (start / status / resume / list)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status

from app.api.deps import DB, CurrentUser, EditorUser, FounderUser, IdempotencyKeyDep
from app.core.errors import ValidationProblem
from app.core.idempotency import with_idempotency
from app.director.graph import graph_topology
from app.director.runtime import WorkflowRuntime
from app.schemas.director import (
    WorkflowDecisionRequest,
    WorkflowStartRequest,
    WorkflowStartResponse,
    WorkflowStatusResponse,
    WorkflowSummary,
)
from app.services.brand import BrandService

router = APIRouter(prefix="/director", tags=["director"])


@router.get("/topology", summary="Graph topology (documentation for the UI)")
async def topology(user: CurrentUser) -> dict:
    return graph_topology()


@router.post(
    "/workflows",
    response_model=WorkflowStartResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a marketing workflow (runs until the approval interrupt)",
)
async def start_workflow(
    payload: WorkflowStartRequest,
    session: DB,
    user: EditorUser,
    key: IdempotencyKeyDep = None,
) -> WorkflowStartResponse:
    brand_id = payload.brand_profile_id
    if brand_id is None:
        brand = await BrandService(session).get_active()
        if brand is None:
            raise ValidationProblem(
                "No brand profile available. Create one before starting a workflow.",
                code="brand_required",
            )
        brand_id = brand.id

    runtime = WorkflowRuntime()
    cache: dict[str, dict] = {}

    async def operation() -> dict:
        if not cache:
            result = await runtime.start(
                brand_profile_id=brand_id,  # type: ignore[arg-type]
                objective=payload.objective,
                actor_id=user.id,
                campaign_id=payload.campaign_id,
                platforms=payload.platforms,
            )
            cache.update(result)
        return {
            "workflow_id": cache["workflow_id"],
            "state": cache["state"],
        }

    result, _replayed = await with_idempotency(
        session,
        key=key,
        user_id=user.id,
        endpoint="director.start",
        payload={
            "brand": str(brand_id),
            "objective": payload.objective,
            "platforms": payload.platforms,
            "campaign_id": str(payload.campaign_id) if payload.campaign_id else None,
        },
        operation=operation,
    )
    return WorkflowStartResponse(**result)


@router.get(
    "/workflows", response_model=list[WorkflowSummary], summary="List workflows"
)
async def list_workflows(user: CurrentUser, limit: int = 50) -> list[WorkflowSummary]:
    rows = await WorkflowRuntime().list_workflows(limit=limit)
    return [WorkflowSummary(**row) for row in rows]


@router.get(
    "/workflows/{workflow_id}",
    response_model=WorkflowStatusResponse,
    summary="Workflow status, including whether a human decision is pending",
)
async def workflow_status(
    workflow_id: str, user: CurrentUser
) -> WorkflowStatusResponse:
    return WorkflowStatusResponse(**await WorkflowRuntime().status(workflow_id))


@router.post(
    "/workflows/{workflow_id}/decision",
    response_model=WorkflowStatusResponse,
    summary="Resume a workflow with the founder's decision (founder only)",
)
async def workflow_decision(
    workflow_id: str,
    payload: WorkflowDecisionRequest,
    founder: FounderUser,
    response_body: None = None,
) -> WorkflowStatusResponse:
    runtime = WorkflowRuntime()
    await runtime.resume(
        workflow_id,
        decision={"action": payload.action, "reason": payload.reason},
        actor_id=founder.id,
    )
    return WorkflowStatusResponse(**await runtime.status(workflow_id))


@router.delete(
    "/workflows/{workflow_id}",
    status_code=204,
    summary="Delete a workflow and its checkpoints",
)
async def delete_workflow(workflow_id: str, user: EditorUser) -> Response:
    await WorkflowRuntime().delete(workflow_id)
    return Response(status_code=204)
