"""Workflow runtime: start, resume, inspect, list.

Persistence guarantees:

* every step writes a checkpoint row through :class:`DatabaseCheckpointer`;
* ``resume`` re-hydrates the graph from the database — it does not rely on
  anything held in process memory, so a workflow started before a restart can be
  resumed after one;
* the human approval halt is a real LangGraph ``interrupt``, so resuming without
  a decision is impossible.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from langgraph.types import Command
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationProblem
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.director.checkpointer import DatabaseCheckpointer
from app.director.graph import build_graph, graph_topology
from app.director.nodes import DirectorNodes
from app.director.state import initial_state
from app.llm.gateway import LLMGateway
from app.models.workflow import WorkflowCheckpoint

logger = get_logger("crusor.director.runtime")

RECURSION_LIMIT = 120


def _config(thread_id: str) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": RECURSION_LIMIT,
    }


class WorkflowRuntime:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.session_factory = session_factory or get_session_factory()
        self.checkpointer = DatabaseCheckpointer(self.session_factory)

    # ---------------------------------------------------------------- helpers
    def _graph(self, session: AsyncSession) -> tuple[Any, DatabaseCheckpointer]:
        """Compile the graph + its buffering checkpointer for one transaction."""
        nodes = DirectorNodes(session, gateway=LLMGateway(session=session))
        checkpointer = DatabaseCheckpointer(self.session_factory)
        return build_graph(nodes, checkpointer=checkpointer), checkpointer

    async def _step(
        self,
        graph: Any,
        checkpointer: DatabaseCheckpointer,
        session: AsyncSession,
        stream_input: Any,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """Advance the graph one superstep at a time, persisting checkpoints.

        Checkpoints are drained *after* each superstep inside the same
        transaction as that superstep's domain writes, so a crash cannot leave a
        recorded checkpoint describing work that was rolled back.
        """
        last: Any = None
        async for chunk in graph.astream(stream_input, config, stream_mode="values"):
            last = chunk
            await checkpointer.drain(session)
            await session.commit()
        return dict(last or {})

    @staticmethod
    def _normalise(result: dict[str, Any]) -> dict[str, Any]:
        """Strip LangGraph internals and expose the interrupt flag."""
        payload = {k: v for k, v in (result or {}).items() if not k.startswith("__")}
        interrupts = (result or {}).get("__interrupt__") or []
        payload["interrupted"] = bool(interrupts)
        if interrupts:
            first = interrupts[0]
            value = getattr(first, "value", None)
            payload["interrupt_payload"] = value
        return payload

    # ------------------------------------------------------------------ start
    async def start(
        self,
        *,
        brand_profile_id: uuid.UUID,
        objective: str,
        actor_id: uuid.UUID | None = None,
        campaign_id: uuid.UUID | None = None,
        platforms: list[str] | None = None,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        thread_id = workflow_id or f"wf_{uuid.uuid4().hex[:16]}"
        state = initial_state(
            workflow_id=thread_id,
            brand_profile_id=str(brand_profile_id),
            objective=objective,
            actor_id=str(actor_id) if actor_id else None,
            campaign_id=str(campaign_id) if campaign_id else None,
            platforms=platforms,
            max_iterations=settings.director_max_iterations,
        )
        async with self.session_factory() as session:
            graph, checkpointer = self._graph(session)
            try:
                result = await self._step(
                    graph, checkpointer, session, state, _config(thread_id)
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                if checkpointer.pending_count:
                    await checkpointer.flush_standalone()
        payload = self._normalise(result)
        logger.info(
            "workflow_started",
            thread_id=thread_id,
            status=payload.get("status"),
            interrupted=payload.get("interrupted"),
        )
        return {"workflow_id": thread_id, "state": payload}

    # ----------------------------------------------------------------- resume
    async def resume(
        self,
        workflow_id: str,
        *,
        decision: dict[str, Any],
        actor_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        action = (decision or {}).get("action")
        if action not in {"approve", "request_changes", "reject"}:
            raise ValidationProblem(
                "decision.action must be one of approve|request_changes|reject",
                details={"received": action},
            )
        async with self.session_factory() as session:
            graph, checkpointer = self._graph(session)
            snapshot = await graph.aget_state(_config(workflow_id))
            if not snapshot or not snapshot.values:
                raise NotFoundError(f"Workflow {workflow_id} not found.")
            if not snapshot.next:
                return {
                    "workflow_id": workflow_id,
                    "state": self._normalise(dict(snapshot.values)),
                    "already_completed": True,
                }
            envelope = {
                "action": action,
                "reason": (decision or {}).get("reason", ""),
                "actor_id": str(actor_id) if actor_id else None,
                "decided_at": datetime.now(timezone.utc).isoformat(),
            }
            try:
                result = await self._step(
                    graph, checkpointer, session, Command(resume=envelope),
                    _config(workflow_id),
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                if checkpointer.pending_count:
                    await checkpointer.flush_standalone()
        payload = self._normalise(result)
        logger.info("workflow_resumed", thread_id=workflow_id, action=action)
        return {"workflow_id": workflow_id, "state": payload}

    # ----------------------------------------------------------------- status
    async def status(self, workflow_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            graph, checkpointer = self._graph(session)
            snapshot = await graph.aget_state(_config(workflow_id))
            if not snapshot or not snapshot.values:
                raise NotFoundError(f"Workflow {workflow_id} not found.")
            values = dict(snapshot.values)
            next_nodes = list(snapshot.next or [])
            interrupts = []
            for task in snapshot.tasks or []:
                interrupts.extend(getattr(task, "interrupts", ()) or [])
        return {
            "workflow_id": workflow_id,
            "status": values.get("status"),
            "phase": values.get("phase"),
            "next": next_nodes,
            "awaiting_human": bool(interrupts),
            "interrupts": [
                {"value": getattr(i, "value", None)} for i in interrupts
            ],
            "iteration": values.get("iteration"),
            "plan": values.get("plan"),
            "qa_verdict": values.get("qa_verdict"),
            "qa_findings": values.get("qa_findings"),
            "approval_request": values.get("approval_request"),
            "approval_decision": values.get("approval_decision"),
            "content_item_ids": values.get("content_item_ids"),
            "created_campaign_id": values.get("created_campaign_id"),
            "summary": values.get("summary"),
            "trace": values.get("trace"),
            "errors": values.get("errors"),
            "results": values.get("results"),
            "checkpoint_count": await self._checkpoint_count(workflow_id),
        }

    async def _checkpoint_count(self, workflow_id: str) -> int:
        async with self.session_factory() as session:
            stmt = select(func.count()).select_from(WorkflowCheckpoint).where(
                WorkflowCheckpoint.thread_id == workflow_id
            )
            return int((await session.execute(stmt)).scalar_one())

    async def list_workflows(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Latest checkpoint per thread, for the Director page."""
        async with self.session_factory() as session:
            stmt = (
                select(WorkflowCheckpoint)
                .order_by(WorkflowCheckpoint.created_at.desc())
                .limit(500)
            )
            rows = list((await session.execute(stmt)).scalars().all())
        seen: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row.thread_id in seen:
                continue
            seen[row.thread_id] = {
                "workflow_id": row.thread_id,
                "status": row.run_status,
                "node": row.node,
                "step": row.step,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            if len(seen) >= limit:
                break
        return list(seen.values())

    async def delete(self, workflow_id: str) -> None:
        checkpointer = DatabaseCheckpointer(self.session_factory)
        await checkpointer.adelete_thread(workflow_id)

    @staticmethod
    def topology() -> dict[str, Any]:
        return graph_topology()
