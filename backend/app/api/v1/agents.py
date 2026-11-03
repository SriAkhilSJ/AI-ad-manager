"""Agent introspection and direct invocation.

Direct invocation exists for operators and tests. It still goes through the full
:class:`BaseAgent` lifecycle (limits, retries, ledger, usage) — there is no
"fast path" that skips governance.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.agents.base import AgentContext
from app.agents.registry import get_registry
from app.api.deps import DB, CurrentUser, EditorUser
from app.core.errors import NotFoundError, ValidationProblem
from app.llm.gateway import LLMGateway
from app.models.agent import AgentRun
from app.schemas.director import AgentRunOut, AgentRunRequest, AgentSpecOut
from app.services.brand import BrandService
from sqlalchemy import select

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=list[AgentSpecOut], summary="Registered agents and contracts")
async def list_agents(user: CurrentUser) -> list[AgentSpecOut]:
    return [AgentSpecOut(**spec) for spec in get_registry().specs()]


@router.get("/runs", summary="Recent agent run ledger")
async def list_runs(session: DB, user: CurrentUser, limit: int = 50) -> list[dict]:
    rows = (
        await session.execute(
            select(AgentRun).order_by(AgentRun.created_at.desc()).limit(min(limit, 200))
        )
    ).scalars().all()
    return [
        {
            "id": str(r.id),
            "agent": r.agent_name,
            "status": r.status,
            "attempts": r.attempts,
            "workflow_thread_id": r.workflow_thread_id,
            "error_code": r.error_code,
            "latency_ms": r.latency_ms,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.post(
    "/{agent_name}/run",
    response_model=AgentRunOut,
    summary="Invoke one agent with a structured payload",
)
async def run_agent(
    agent_name: str,
    payload: AgentRunRequest,
    session: DB,
    user: EditorUser,
) -> AgentRunOut:
    registry = get_registry()
    try:
        agent = registry.get(agent_name)
    except KeyError as exc:
        raise NotFoundError(str(exc)) from exc

    brand = None
    if payload.brand_profile_id:
        brand = await BrandService(session).get(payload.brand_profile_id)
    else:
        brand = await BrandService(session).get_active()

    body = dict(payload.payload)
    body.setdefault("brand_profile_id", str(brand.id) if brand else None)

    ctx = AgentContext(
        session=session,
        brand=brand,
        gateway=LLMGateway(session=session),
        actor_id=user.id,
        workflow_node=agent_name,
    )
    try:
        result = await agent.run(ctx, body)
    except Exception as exc:  # noqa: BLE001 - surfaced as a structured error field
        await session.commit()  # keep the failed AgentRun row
        return AgentRunOut(
            agent=agent_name,
            status="failed",
            error=str(exc)[:500],
            run_id=ctx.run.id if ctx.run else None,
            trace=ctx.run.trace if ctx.run else [],
        )
    await session.commit()
    return AgentRunOut(
        agent=agent_name,
        status="succeeded",
        output=result.model_dump(mode="json"),
        run_id=ctx.run.id if ctx.run else None,
        trace=ctx.run.trace if ctx.run else [],
    )
