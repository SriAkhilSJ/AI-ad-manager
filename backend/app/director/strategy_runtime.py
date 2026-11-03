"""Runtime for the Phase 2 strategy workflow: start, resume, status, list.

Deliberately separate from :class:`~app.director.runtime.WorkflowRuntime` so the
Phase 1 campaign workflow is untouched. The persistence discipline is identical:

* checkpoints are drained after every superstep **inside** the same transaction
  as that superstep's domain writes, so a crash cannot record work that was
  rolled back;
* ``resume`` re-hydrates from the database and requires a real decision;
* the approval halt is a genuine LangGraph interrupt.

Cost honesty: ``start`` computes and returns a pre-flight estimate before any
paid call happens, and records it in state so the UI can show "estimated vs
actual" side by side.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from langgraph.types import Command
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationProblem
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.director.checkpointer import DatabaseCheckpointer
from app.director.strategy_graph import build_strategy_graph, strategy_graph_topology
from app.director.strategy_nodes import StrategyNodes
from app.director.strategy_state import PHASE_LABELS, initial_strategy_state
from app.llm.gateway import LLMGateway
from app.models.workflow import WorkflowCheckpoint

logger = get_logger("crusor.director.strategy.runtime")

STRATEGY_RECURSION_LIMIT = 80


def _config(thread_id: str) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": STRATEGY_RECURSION_LIMIT,
    }


class StrategyWorkflowRuntime:
    def __init__(self, session_factory: Any = None) -> None:
        self.session_factory = session_factory or get_session_factory()

    # ---------------------------------------------------------------- helpers
    def _graph(self, session: Any) -> tuple[Any, DatabaseCheckpointer]:
        nodes = StrategyNodes(session, gateway=LLMGateway(session=session))
        checkpointer = DatabaseCheckpointer(self.session_factory)
        return build_strategy_graph(nodes, checkpointer=checkpointer), checkpointer

    async def _step(
        self,
        graph: Any,
        checkpointer: DatabaseCheckpointer,
        session: Any,
        stream_input: Any,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        last: Any = None
        async for chunk in graph.astream(stream_input, config, stream_mode="values"):
            last = chunk
            await checkpointer.drain(session)
            await session.commit()
        return dict(last or {})

    @staticmethod
    def _normalise(result: dict[str, Any]) -> dict[str, Any]:
        payload = {k: v for k, v in (result or {}).items() if not k.startswith("__")}
        interrupts = (result or {}).get("__interrupt__") or []
        payload["interrupted"] = bool(interrupts)
        if interrupts:
            first = interrupts[0]
            payload["interrupt_payload"] = getattr(first, "value", None)
        payload["phase_label"] = PHASE_LABELS.get(
            str(payload.get("phase") or ""), payload.get("phase")
        )
        return payload

    # ----------------------------------------------------------- cost estimate
    def estimate_cost(
        self,
        *,
        objective: str,
        budget_usd: float | None = None,
        research: bool = True,
    ) -> dict[str, Any]:
        """Pre-flight estimate shown to the founder before anything is spent.

        This is an estimate, and it is labelled as one. The brief is explicit that
        a ₹5 envelope is not assumed to be achievable for a complex research task;
        the estimate therefore reports whether the configured budget is plausibly
        sufficient rather than promising it.
        """
        settings = get_settings()
        budget = float(
            budget_usd if budget_usd is not None else settings.strategy_budget_usd
        )

        research_calls = 3 if research else 0  # classification/extraction/synthesis
        strategy_calls = 2  # plan + brand check
        # Conservative per-call assumptions, priced off the configured models.
        per_research_call = 0.0025
        per_strategy_call = 0.006
        estimated = round(
            research_calls * per_research_call + strategy_calls * per_strategy_call, 6
        )

        return {
            "estimated_cost_usd": estimated,
            "estimated_cost_inr": round(estimated * settings.usd_inr_rate, 2),
            "budget_usd": budget,
            "budget_inr": round(budget * settings.usd_inr_rate, 2),
            "within_budget": estimated <= budget,
            "research_calls": research_calls,
            "strategy_calls": strategy_calls,
            "models": {
                "reasoning": settings.llm_reasoning_model,
                "auxiliary": settings.llm_auxiliary_model,
            },
            "assumptions": [
                "Estimates assume typical prompt sizes for a short objective.",
                "Research cost scales with the number of categories and sources.",
                "A complex research task can exceed a ₹5-equivalent envelope; the run "
                "stops rather than overspending, and reports what it did not do.",
            ],
            "note": "Estimated before execution from list prices; actual cost is recorded per call.",
        }

    # ------------------------------------------------------------------ start
    async def start(
        self,
        *,
        brand_profile_id: uuid.UUID,
        objective: str,
        actor_id: uuid.UUID | None = None,
        business_objectives: list[str] | None = None,
        primary_objective_type: str = "developer_acquisition",
        platforms: list[str] | None = None,
        research_project_id: str | None = None,
        budget_usd: float | None = None,
        max_iterations: int | None = None,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        thread_id = workflow_id or f"strat_{uuid.uuid4().hex[:16]}"
        budget = float(budget_usd if budget_usd is not None else settings.strategy_budget_usd)
        estimate = self.estimate_cost(objective=objective, budget_usd=budget)

        state = initial_strategy_state(
            workflow_id=thread_id,
            brand_profile_id=str(brand_profile_id),
            objective=objective,
            actor_id=str(actor_id) if actor_id else None,
            business_objectives=business_objectives,
            primary_objective_type=primary_objective_type,
            platforms=platforms,
            research_project_id=research_project_id,
            max_iterations=int(max_iterations or settings.strategy_max_iterations),
            budget={
                "max_cost_usd": budget,
                "max_tokens": settings.agent_max_tokens_per_run,
                "currency": "USD",
                "budget_inr_equivalent": round(budget * settings.usd_inr_rate, 2),
            },
            cost_estimate=estimate,
        )

        async with self.session_factory() as session:
            graph, checkpointer = self._graph(session)
            try:
                result = await self._step(graph, checkpointer, session, state, _config(thread_id))
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                if checkpointer.pending_count:
                    await checkpointer.flush_standalone()

        payload = self._normalise(result)
        payload["interrupted"] = payload["interrupted"] or await self.awaiting_human(thread_id)
        logger.info(
            "strategy_workflow_started",
            thread_id=thread_id,
            status=payload.get("status"),
            interrupted=payload.get("interrupted"),
        )
        return {
            "workflow_id": thread_id,
            "cost_estimate": estimate,
            "state": payload,
        }

    # ----------------------------------------------------------------- resume
    async def resume(
        self,
        workflow_id: str,
        *,
        decision: dict[str, Any],
        actor_id: uuid.UUID | None = None,
        actor_role: str = "founder",
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
                raise NotFoundError(f"Strategy workflow {workflow_id} not found.")
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
                "actor_role": actor_role,
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
        payload["interrupted"] = payload["interrupted"] or await self.awaiting_human(workflow_id)
        logger.info("strategy_workflow_resumed", thread_id=workflow_id, action=action)
        return {"workflow_id": workflow_id, "state": payload}

    async def awaiting_human(self, workflow_id: str, graph: Any = None) -> bool:
        """Is the thread parked on a real interrupt?

        The streamed state chunk does not reliably carry ``__interrupt__``, so the
        authoritative answer comes from the checkpointed snapshot — the same
        source :meth:`status` uses.
        """
        async with self.session_factory() as session:
            graph = graph or self._graph(session)[0]
            snapshot = await graph.aget_state(_config(workflow_id))
            if not snapshot:
                return False
            for task in snapshot.tasks or []:
                if getattr(task, "interrupts", ()):
                    return True
        return False

    # ----------------------------------------------------------------- status
    async def status(self, workflow_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            graph, _ = self._graph(session)
            snapshot = await graph.aget_state(_config(workflow_id))
            if not snapshot or not snapshot.values:
                raise NotFoundError(f"Strategy workflow {workflow_id} not found.")
            values = dict(snapshot.values)
            next_nodes = list(snapshot.next or [])
            interrupts = []
            for task in snapshot.tasks or []:
                interrupts.extend(getattr(task, "interrupts", ()) or [])

        return {
            "workflow_id": workflow_id,
            "status": values.get("status"),
            "phase": values.get("phase"),
            "phase_label": PHASE_LABELS.get(str(values.get("phase") or "")),
            "next": next_nodes,
            "awaiting_human": bool(interrupts),
            "interrupts": [{"value": getattr(i, "value", None)} for i in interrupts],
            "objective": values.get("objective"),
            "iteration": values.get("iteration"),
            "max_iterations": values.get("max_iterations"),
            "research_needed": values.get("research_needed"),
            "research_rounds": values.get("research_rounds"),
            "findings_summary": values.get("findings_summary"),
            "evidence_verdict": values.get("evidence_verdict"),
            "evidence_assessment": values.get("evidence_assessment"),
            "brand_snapshot": values.get("brand_snapshot"),
            "unverified_brand_claims": values.get("unverified_brand_claims"),
            "strategy_id": values.get("strategy_id"),
            "strategy_version_id": values.get("strategy_version_id"),
            "strategy_version_number": values.get("strategy_version_number"),
            "strategy": values.get("strategy"),
            "brand_check": values.get("brand_check"),
            "missing_information": values.get("missing_information"),
            "approval_request": values.get("approval_request"),
            "approval_decision": values.get("approval_decision"),
            "cost_estimate": values.get("cost_estimate"),
            "cost_actual": values.get("cost_actual"),
            "limits": values.get("limits"),
            "events": values.get("events"),
            "trace": values.get("trace"),
            "errors": values.get("errors"),
            "summary": values.get("summary"),
            "checkpoint_count": await self._checkpoint_count(workflow_id),
        }

    async def _checkpoint_count(self, workflow_id: str) -> int:
        async with self.session_factory() as session:
            stmt = select(func.count()).select_from(WorkflowCheckpoint).where(
                WorkflowCheckpoint.thread_id == workflow_id
            )
            return int((await session.execute(stmt)).scalar_one())

    async def list_workflows(self, *, limit: int = 50) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            stmt = (
                select(WorkflowCheckpoint)
                .where(WorkflowCheckpoint.thread_id.like("strat_%"))
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
        return strategy_graph_topology()


__all__ = ["StrategyWorkflowRuntime", "STRATEGY_RECURSION_LIMIT"]
