"""Marketing Director graph nodes.

Each node is a pure(ish) function of state → partial state update. Side effects
(agent runs, DB writes) are performed against a request-scoped session injected
via the graph's ``config``. Nodes never talk to each other directly: they read
and write the shared, checkpointed state.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import AgentContext
from app.agents.registry import get_registry
from app.core.logging import get_logger
from app.director.state import DirectorState, PlanTask
from app.llm.gateway import LLMGateway
from app.models.brand import BrandProfile
from app.models.content import ContentItem
from app.services.approval import ApprovalService
from app.services.brand import BrandService
from app.services.campaign import CampaignService
from app.services.content import ContentService

logger = get_logger("crusor.director")

# Agent → which state field receives its structured result.
RESULT_FIELD = {
    "research": "research",
    "strategy": "strategy",
    "content": "drafts",
    "creative": "creative_brief",
    "growth_analyst": "growth",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trace(entry: str, **fields: Any) -> dict[str, Any]:
    return {"at": _now(), "entry": entry, **fields}


class DirectorNodes:
    """Bundle of node callables sharing one DB session."""

    def __init__(self, session: AsyncSession, *, gateway: LLMGateway | None = None) -> None:
        self.session = session
        self.gateway = gateway or LLMGateway(session=session)
        self.registry = get_registry()
        self.content = ContentService(session)
        self.approvals = ApprovalService(session)

    # ---------------------------------------------------------------- helpers
    async def _brand(self, state: DirectorState) -> BrandProfile | None:
        brand_id = state.get("brand_profile_id")
        if not brand_id:
            return None
        try:
            return await self.session.get(BrandProfile, uuid.UUID(brand_id))
        except ValueError:
            return None

    def _ctx(
        self, state: DirectorState, brand: BrandProfile | None, node: str
    ) -> AgentContext:
        return AgentContext(
            session=self.session,
            brand=brand,
            gateway=self.gateway,
            actor_id=uuid.UUID(state["actor_id"]) if state.get("actor_id") else None,
            workflow_thread_id=state.get("workflow_id"),
            workflow_node=node,
        )

    # ------------------------------------------------------------------ intake
    async def intake(self, state: DirectorState) -> dict[str, Any]:
        """Ground the workflow in the brand knowledge base and goal set."""
        brand = await self._brand(state)
        updates: dict[str, Any] = {
            "phase": "intake",
            "status": "planning",
            "iteration": state.get("iteration", 0),
            "trace": [_trace("intake", brand=str(brand.id) if brand else None)],
        }
        if brand is None:
            updates["errors"] = [
                {
                    "at": _now(),
                    "stage": "intake",
                    "code": "brand_missing",
                    "message": "Brand profile not found; cannot plan marketing work.",
                }
            ]
            updates["status"] = "failed"
            updates["summary"] = "Workflow failed: brand profile missing."
            return updates

        goals = await BrandService(self.session).list_goals(brand.id)
        updates["trace"] = [
            *_list(updates.get("trace")),
            _trace(
                "context_loaded",
                goals=len(goals),
                audience=len(brand.audience or []),
                personality=len(brand.personality or []),
            ),
        ]
        return updates

    # -------------------------------------------------------------------- plan
    async def plan(self, state: DirectorState) -> dict[str, Any]:
        """Build a dependency-ordered task DAG (not a fixed linear chain)."""
        brand = await self._brand(state)
        platforms = state.get("platforms") or ["linkedin"]
        content_slots = max(1, min(len(platforms), 3))

        plan: list[PlanTask] = [
            PlanTask(
                id="research-1",
                agent="research",
                action="audience_research",
                params={"request": state.get("objective", ""), "platforms": platforms},
                depends_on=[],
                status="pending",
            ),
            PlanTask(
                id="strategy-1",
                agent="strategy",
                action="campaign_plan",
                params={"platforms": platforms, "duration_days": 14},
                depends_on=["research-1"],
                status="pending",
            ),
        ]
        for index, platform in enumerate(platforms[:content_slots]):
            plan.append(
                PlanTask(
                    id=f"content-{index + 1}",
                    agent="content",
                    action="draft_copy",
                    params={"platform": platform, "slot_index": index},
                    depends_on=["strategy-1"],
                    status="pending",
                )
            )
        plan.append(
            PlanTask(
                id="creative-1",
                agent="creative",
                action="art_direction",
                params={"platform": platforms[0], "format": "carousel"},
                depends_on=["strategy-1"],
                status="pending",
            )
        )
        plan.append(
            PlanTask(
                id="growth-1",
                agent="growth_analyst",
                action="baseline_review",
                params={"window_days": 30},
                depends_on=["strategy-1"],
                status="pending",
            )
        )

        return {
            "plan": plan,
            "current_task_index": 0,
            "phase": "executing",
            "status": "executing",
            "trace": [
                _trace(
                    "plan_created",
                    tasks=len(plan),
                    agents=sorted({t["agent"] for t in plan}),
                    deps={t["id"]: t.get("depends_on", []) for t in plan},
                    mode="deterministic_executor",
                )
            ],
            "summary": f"Plan created with {len(plan)} tasks for {brand.company if brand else 'brand'}.",
        }

    # ---------------------------------------------------------------- dispatch
    async def dispatch(self, state: DirectorState) -> dict[str, Any]:
        """Select the next *runnable* task (dependency-aware supervisor step)."""
        plan: list[PlanTask] = list(state.get("plan") or [])
        iteration = state.get("iteration", 0) + 1
        max_iterations = state.get("max_iterations", 12)

        if iteration > max_iterations:
            return {
                "status": "failed",
                "phase": "aborted",
                "iteration": iteration,
                "errors": [
                    {
                        "at": _now(),
                        "stage": "dispatch",
                        "code": "max_iterations_exceeded",
                        "message": f"Iteration cap {max_iterations} reached.",
                    }
                ],
                "trace": [_trace("dispatch_aborted", iteration=iteration)],
            }

        done = {t["id"] for t in plan if t.get("status") == "done"}
        next_task: PlanTask | None = None
        for task in plan:
            if task.get("status") != "pending":
                continue
            deps = task.get("depends_on", []) or []
            if all(dep in done for dep in deps):
                next_task = task
                break

        if next_task is None:
            pending = [t for t in plan if t.get("status") == "pending"]
            if pending:
                # Dependency deadlock: a prerequisite failed.
                return {
                    "status": "reviewing",
                    "phase": "review",
                    "iteration": iteration,
                    "errors": [
                        {
                            "at": _now(),
                            "stage": "dispatch",
                            "code": "dependency_unresolvable",
                            "message": "Pending tasks could not be scheduled.",
                            "tasks": [t["id"] for t in pending],
                        }
                    ],
                    "trace": [_trace("dispatch_blocked", pending=len(pending))],
                }
            return {
                "status": "reviewing",
                "phase": "review",
                "iteration": iteration,
                "trace": [_trace("dispatch_complete", iteration=iteration)],
            }

        # Mark running so the checkpoint reflects in-flight work.
        for task in plan:
            if task["id"] == next_task["id"]:
                task["status"] = "running"
        return {
            "plan": plan,
            "iteration": iteration,
            "phase": f"executing:{next_task['agent']}",
            "trace": [
                _trace(
                    "task_dispatched",
                    task=next_task["id"],
                    agent=next_task["agent"],
                    iteration=iteration,
                )
            ],
        }

    # ------------------------------------------------------------- agent nodes
    async def execute_agent(self, state: DirectorState, agent_name: str) -> dict[str, Any]:
        """Run whichever task is currently ``running`` for ``agent_name``."""
        plan: list[PlanTask] = list(state.get("plan") or [])
        task = next(
            (
                t
                for t in plan
                if t["agent"] == agent_name and t.get("status") == "running"
            ),
            None,
        )
        if task is None:
            return {"trace": [_trace("agent_skipped", agent=agent_name, reason="no running task")]}

        brand = await self._brand(state)
        ctx = self._ctx(state, brand, agent_name)
        agent = self.registry.get(agent_name)

        payload = self._build_payload(state, task, brand)
        try:
            result = await agent.run(ctx, payload)
        except Exception as exc:  # noqa: BLE001 - node boundary
            logger.exception("director_agent_failed", agent=agent_name, task=task["id"])
            for item in plan:
                if item["id"] == task["id"]:
                    item["status"] = "failed"
            return {
                "plan": plan,
                "errors": [
                    {
                        "at": _now(),
                        "stage": f"agent:{agent_name}",
                        "task": task["id"],
                        "code": getattr(exc, "code", "agent_error"),
                        "message": str(exc)[:500],
                    }
                ],
                "trace": [_trace("agent_failed", agent=agent_name, task=task["id"])],
            }

        dump = result.model_dump(mode="json")
        for item in plan:
            if item["id"] == task["id"]:
                item["status"] = "done"

        results = dict(state.get("results") or {})
        results[task["id"]] = {"agent": agent_name, "output": dump}

        update: dict[str, Any] = {
            "plan": plan,
            "results": results,
            "trace": [
                _trace(
                    "agent_completed",
                    agent=agent_name,
                    task=task["id"],
                    mode=dump.get("generation_mode"),
                    confidence=dump.get("confidence"),
                )
            ],
            "agent_runs": [str(ctx.run.id)] if ctx.run else [],
        }
        field = RESULT_FIELD.get(agent_name)
        if field == "drafts":
            # Drafts accumulate; each entry keeps its agent attribution.
            update["drafts"] = [
                *(state.get("drafts") or []),
                {"agent": agent_name, "output": dump},
            ]
        elif field:
            update[field] = dump
        return update

    def _build_payload(
        self, state: DirectorState, task: PlanTask, brand: BrandProfile | None
    ) -> dict[str, Any]:
        """Pass *structured* upstream results — never prose from another agent."""
        base: dict[str, Any] = {
            "brand_profile_id": state.get("brand_profile_id"),
            "campaign_id": state.get("campaign_id"),
            "request": state.get("objective", ""),
        }
        params = dict(task.get("params") or {})
        agent_name = task["agent"]

        if agent_name == "research":
            return {**base, **params}

        research = state.get("research") or None
        strategy = state.get("strategy") or None

        if agent_name == "strategy":
            return {
                **base,
                **params,
                "research_summary": (research or {}).get("summary", ""),
                "audience_segments": [
                    i.get("segment", "")
                    for i in (research or {}).get("audience_insights", [])
                ],
                "positioning_gaps": (research or {}).get("positioning_gaps", []),
            }

        if agent_name == "content":
            slot_index = int(params.get("slot_index", 0))
            plan_slots = (strategy or {}).get("content_plan", []) or []
            slot = plan_slots[slot_index] if slot_index < len(plan_slots) else {}
            return {
                **base,
                "platform": params.get("platform") or slot.get("platform") or "linkedin",
                "format": slot.get("format", "post"),
                "angle": slot.get("angle", ""),
                "pillar": slot.get("pillar", ""),
                "hook": slot.get("hook", ""),
            }

        if agent_name == "creative":
            return {
                **base,
                **params,
                "concept": (strategy or {}).get("messaging_pillars", [""])[0]
                if (strategy or {}).get("messaging_pillars")
                else "",
            }

        if agent_name == "growth_analyst":
            return {**base, **params}

        return {**base, **params}

    # ---------------------------------------------------------------- QA gate
    async def review(self, state: DirectorState) -> dict[str, Any]:
        """Quality gate over every draft, using the Brand Guardian."""
        drafts = list(state.get("drafts") or [])
        brand = await self._brand(state)
        ctx = self._ctx(state, brand, "brand_guardian")
        guardian = self.registry.get("brand_guardian")

        findings: list[dict[str, Any]] = []
        verdicts: list[str] = []

        for draft in drafts:
            output = draft.get("output") if isinstance(draft, dict) else None
            if not output:
                continue
            try:
                result = await guardian.run(
                    ctx,
                    {
                        "brand_profile_id": state.get("brand_profile_id"),
                        "platform": output.get("platform", "linkedin"),
                        "headline": output.get("headline", ""),
                        "body": output.get("body", ""),
                        "hashtags": output.get("hashtags", []),
                        "call_to_action": output.get("call_to_action", ""),
                    },
                )
                payload = result.model_dump(mode="json")
                verdicts.append(payload["verdict"])
                findings.append(
                    {
                        "platform": output.get("platform"),
                        "verdict": payload["verdict"],
                        "score": payload["score"],
                        "violations": payload["violations"],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                findings.append(
                    {
                        "platform": output.get("platform"),
                        "verdict": "blocked",
                        "error": str(exc)[:300],
                        "violations": [],
                    }
                )
                verdicts.append("blocked")

        if not drafts:
            overall = "no_content"
        elif "blocked" in verdicts:
            overall = "blocked"
        elif "needs_changes" in verdicts:
            overall = "needs_changes"
        else:
            overall = "pass"

        return {
            "phase": "approval",
            "status": "awaiting_approval",
            "qa_verdict": overall,
            "qa_findings": findings,
            "content_item_ids": state.get("content_item_ids") or [],
            "trace": [
                _trace(
                    "review_completed",
                    verdict=overall,
                    drafts=len(drafts),
                    findings=len(findings),
                )
            ],
            "summary": (
                f"QA verdict: {overall}. {len(drafts)} draft(s) prepared for founder review."
            ),
        }

    # ---------------------------------------------------------- persist content
    async def persist_drafts(self, state: DirectorState) -> dict[str, Any]:
        """Materialise drafts as versioned content items in ``DRAFT`` state.

        Content is created *before* approval is requested so the founder approves a
        real, versioned artefact — not a preview blob in graph state.
        """
        drafts = list(state.get("drafts") or [])
        if not drafts:
            return {"trace": [_trace("persist_skipped", reason="no drafts")]}

        brand = await self._brand(state)
        if brand is None:
            return {"trace": [_trace("persist_skipped", reason="no brand")]}

        campaign_id: uuid.UUID | None = None
        if state.get("created_campaign_id"):
            try:
                campaign_id = uuid.UUID(state["created_campaign_id"])
            except ValueError:
                campaign_id = None

        created: list[str] = []
        for draft in drafts:
            output = draft.get("output") if isinstance(draft, dict) else None
            if not output or not output.get("body"):
                continue
            item: ContentItem = await self.content.create_item(
                brand_profile_id=brand.id,
                campaign_id=campaign_id,
                title=(output.get("headline") or "Untitled")[:280],
                platform=output.get("platform", "linkedin"),
                format="post",
                body=output.get("body", ""),
                headline=output.get("headline", ""),
                hashtags=output.get("hashtags", []),
                call_to_action=output.get("call_to_action", ""),
                created_by_agent="content",
                generation_metadata={
                    "workflow_id": state.get("workflow_id"),
                    "generation_mode": output.get("generation_mode"),
                    "confidence": output.get("confidence"),
                },
            )
            created.append(str(item.id))

        # Record the QA outcome against each item for the approval UI.
        return {
            "content_item_ids": [*(state.get("content_item_ids") or []), *created],
            "trace": [_trace("drafts_persisted", count=len(created))],
        }

    async def ensure_campaign(self, state: DirectorState) -> dict[str, Any]:
        """Create (or reuse) the campaign that owns this workflow's content."""
        if state.get("campaign_id") or state.get("created_campaign_id"):
            return {"trace": [_trace("campaign_reused")]}
        brand = await self._brand(state)
        if brand is None:
            return {}
        strategy = state.get("strategy") or {}
        name = strategy.get("campaign_name") or f"{brand.company} campaign {state.get('workflow_id', '')[:8]}"
        service = CampaignService(self.session)
        # Starting a second workflow with the same objective produces the same
        # campaign name. That is a *reuse*, not an error: the campaign is the
        # container for the content, and duplicating it would fragment reporting.
        existing = await service.find_by_name(brand_profile_id=brand.id, name=name[:190])
        if existing is not None:
            return {
                "created_campaign_id": str(existing.id),
                "trace": [_trace("campaign_reused", name=existing.name)],
            }
        try:
            campaign = await service.create(
                brand_profile_id=brand.id,
                name=name[:190],
                objective=strategy.get("objective", state.get("objective", "")),
                platforms=state.get("platforms") or [],
                plan={
                    "messaging_pillars": strategy.get("messaging_pillars", []),
                    "channel_mix": strategy.get("channel_mix", []),
                    "content_plan": strategy.get("content_plan", []),
                    "success_metrics": strategy.get("success_metrics", []),
                },
                created_by_agent="strategy",
                actor_id=uuid.UUID(state["actor_id"]) if state.get("actor_id") else None,
            )
        except Exception as exc:  # validation or a concurrent create of the same name
            logger.warning(
                "campaign_create_failed", error=type(exc).__name__, reason=str(exc)[:200]
            )
            # A concurrent writer may have won the race; re-check before failing.
            raced = await service.find_by_name(brand_profile_id=brand.id, name=name[:190])
            if raced is not None:
                return {
                    "created_campaign_id": str(raced.id),
                    "trace": [_trace("campaign_reused", name=raced.name)],
                }
            return {
                "trace": [_trace("campaign_not_created", error=type(exc).__name__)],
                "errors": [
                    {
                        "at": _now(),
                        "stage": "campaign",
                        "code": "campaign_create_failed",
                        "message": str(exc)[:300],
                    }
                ],
            }
        return {
            "created_campaign_id": str(campaign.id),
            "trace": [_trace("campaign_created", name=campaign.name)],
        }

    # ------------------------------------------------------ approval request
    async def request_approval(self, state: DirectorState) -> dict[str, Any]:
        """Move each draft into ``IN_REVIEW`` and build the founder approval packet.

        This node is followed by a LangGraph ``interrupt`` — the graph suspends
        here and cannot advance until a human resumes it.
        """
        items: list[dict[str, Any]] = []
        actor_id = uuid.UUID(state["actor_id"]) if state.get("actor_id") else None
        for item_id in state.get("content_item_ids") or []:
            try:
                item, record, _ = await self.approvals.apply(
                    uuid.UUID(item_id),
                    "submit",
                    actor_id=actor_id,
                    actor_role="system",
                    reason="Marketing Director submitted generated content for review",
                )
                items.append(
                    {
                        "content_item_id": str(item.id),
                        "title": item.title,
                        "platform": item.platform,
                        "status": item.status,
                        "content_version_id": str(item.current_version_id),
                        "version_count": item.version_count,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("submit_for_review_failed", item=item_id, error=str(exc)[:200])

        request = {
            "workflow_id": state.get("workflow_id"),
            "requested_at": _now(),
            "items": items,
            "qa_verdict": state.get("qa_verdict"),
            "qa_findings": state.get("qa_findings", []),
            "summary": state.get("summary", ""),
            "decision_options": ["approve", "request_changes", "reject"],
            "note": (
                "Founder approval is required. No content is published by this workflow; "
                "publishing is a separate, approval-gated operation."
            ),
        }
        return {
            "approval_request": request,
            "status": "awaiting_approval",
            "phase": "awaiting_approval",
            "trace": [_trace("approval_requested", items=len(items))],
        }

    # ------------------------------------------------------------- finalize
    async def finalize(self, state: DirectorState) -> dict[str, Any]:
        decision = state.get("approval_decision") or {}
        action = decision.get("action", "none")
        actor_id = uuid.UUID(state["actor_id"]) if state.get("actor_id") else None

        applied: list[dict[str, Any]] = []
        for item_id in state.get("content_item_ids") or []:
            try:
                if action == "approve":
                    item, record, _ = await self.approvals.apply(
                        uuid.UUID(item_id),
                        "approve",
                        actor_id=actor_id,
                        actor_role="founder",
                        reason=decision.get("reason", "Approved by founder"),
                    )
                    applied.append({"content_item_id": item_id, "status": item.status})
                elif action == "request_changes":
                    item, _, _ = await self.approvals.apply(
                        uuid.UUID(item_id),
                        "request_changes",
                        actor_id=actor_id,
                        actor_role="founder",
                        reason=decision.get("reason", ""),
                    )
                    applied.append({"content_item_id": item_id, "status": item.status})
                elif action == "reject":
                    item, _, _ = await self.approvals.apply(
                        uuid.UUID(item_id),
                        "reject",
                        actor_id=actor_id,
                        actor_role="founder",
                        reason=decision.get("reason", ""),
                    )
                    applied.append({"content_item_id": item_id, "status": item.status})
            except Exception as exc:  # noqa: BLE001
                applied.append({"content_item_id": item_id, "error": str(exc)[:200]})

        status = "completed" if action in {"approve", "reject", "request_changes"} else "completed"
        return {
            "status": status,
            "phase": "completed",
            "summary": (
                f"Workflow completed. Founder decision: {action}. "
                f"{len(applied)} item(s) updated. Publishing remains a separate, "
                "approval-gated action."
            ),
            "trace": [_trace("finalized", action=action, items=len(applied))],
        }

    # --------------------------------------------------------- failure escape
    async def fail(self, state: DirectorState) -> dict[str, Any]:
        errors = state.get("errors") or []
        last = errors[-1] if errors else {}
        return {
            "status": "failed",
            "phase": "failed",
            "summary": f"Workflow failed: {last.get('message', 'unknown error')}",
            "trace": [_trace("workflow_failed", error=last.get("code"))],
        }

    def cancelled(self, state: DirectorState) -> dict[str, Any]:
        return {
            "status": "cancelled",
            "phase": "cancelled",
            "summary": "Workflow cancelled by operator.",
            "trace": [_trace("workflow_cancelled")],
        }


def _list(value: Any) -> list[Any]:
    return list(value or []) if isinstance(value, list) else []
