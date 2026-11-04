"""Nodes for the Phase 2 Marketing Director strategy workflow.

The flow the brief asks for, node by node:

    understand objective → retrieve brand knowledge → decide whether research is
    needed → delegate research → evaluate evidence → (request more research) →
    generate strategy → brand consistency check → identify missing information →
    save → present for approval

Two design points worth stating:

* **Evaluation is a real gate.** ``evaluate_evidence`` can send the flow back to
  research, but only while the round budget allows and only if another round
  could plausibly help. It cannot loop forever: ``max_iterations`` and a
  no-progress check both terminate it.
* **Approval is a real LangGraph interrupt**, reusing the Phase 1 pattern. The
  graph suspends; resuming without a decision is impossible.

Nothing in this module publishes, sends, spends, or touches a live account. The
only side effects are database writes to the research and strategy tables.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import AgentContext
from app.agents.registry import get_registry
from app.core.config import get_settings
from app.core.errors import BudgetExhaustedError, CancelledProblem
from app.core.logging import get_logger
from app.director.strategy_state import (
    StrategyWorkflowState,
)
from app.llm.gateway import LLMGateway
from app.llm.routing import BudgetGuard
from app.models.brand import BrandProfile
from app.models.research import RESEARCH_CATEGORIES, ProductFact, VerificationStatus
from app.services.brand import BrandService
from app.services.research import ResearchService
from app.services.strategy import StrategyService

logger = get_logger("crusor.director.strategy")

# A research round is worth repeating only for these categories, and only when a
# live provider could return something different. A deterministic provider is
# stable, so asking it twice is pure waste.
RETRYABLE_CATEGORIES = (
    "competitor_identification",
    "competitor_positioning",
    "market_trends",
    "developer_pain_points",
    "content_opportunities",
)

MIN_FINDINGS_FOR_SUFFICIENT = 5
MIN_CATEGORIES_FOR_SUFFICIENT = 2
SUFFICIENT_VERIFIED_FINDINGS = 10


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trace(entry: str, **fields: Any) -> dict[str, Any]:
    return {"at": _now(), "entry": entry, **fields}


class StrategyNodes:
    """Strategy workflow nodes; one DB session per invocation."""

    def __init__(self, session: AsyncSession, *, gateway: LLMGateway | None = None) -> None:
        self.session = session
        self.gateway = gateway or LLMGateway(session=session)
        self.registry = get_registry()
        self.brand_service = BrandService(session)
        self.research_service = ResearchService(session)
        self.strategy_service = StrategyService(session)
        self.settings = get_settings()

    # ---------------------------------------------------------------- helpers
    async def _brand(self, state: StrategyWorkflowState) -> BrandProfile | None:
        brand_id = state.get("brand_profile_id")
        if not brand_id:
            return None
        return await self.session.get(BrandProfile, uuid.UUID(str(brand_id)))

    def _remaining_budget(self, state: StrategyWorkflowState) -> float:
        """Money this run may still spend, derived from persisted state.

        Phase 2.6: the result is authoritative. If research consumed the whole
        envelope this returns exactly ``0.0``, and a zero budget now blocks paid
        work instead of granting unlimited spending.
        """
        budget = dict(state.get("budget") or {})
        cost = dict(state.get("cost_actual") or {})
        ceiling = float(budget.get("max_cost_usd", self.settings.strategy_budget_usd))
        spent = float(
            cost.get("total_usd", cost.get("research_usd", 0.0)) or 0.0
        )
        return round(max(0.0, ceiling - spent), 6)

    def _ctx(self, state: StrategyWorkflowState, brand: BrandProfile | None, node: str) -> AgentContext:
        # One guard per node, owned by the Director and handed to the agent. The
        # agent adopts it (``shared_budget``) instead of building a private one,
        # so primary and auxiliary model calls in this node settle against the
        # same reservation ledger.
        guard = BudgetGuard(
            max_cost_usd=self._remaining_budget(state),
            max_tokens=getattr(self.settings, "agent_max_tokens_per_run", 20_000),
            label=f"director:{node}",
            allow_unknown_pricing=bool(
                getattr(self.settings, "llm_allow_unverified_pricing", False)
            ),
        )
        return AgentContext(
            session=self.session,
            brand=brand,
            gateway=self.gateway,
            actor_id=uuid.UUID(state["actor_id"]) if state.get("actor_id") else None,
            workflow_thread_id=state.get("workflow_id"),
            workflow_node=node,
            extra={"budget_guard": guard},
        )

    def _limits(
        self,
        state: StrategyWorkflowState,
        *,
        research_rounds: int | None = None,
        iterations: int | None = None,
        spent_usd: float | None = None,
    ) -> dict[str, Any]:
        """Live limit snapshot so status() is truthful mid-run, not only at settle."""
        budget = dict(state.get("budget") or {})
        cost = dict(state.get("cost_actual") or {})
        max_cost = float(budget.get("max_cost_usd", 0.0))
        spent = float(spent_usd if spent_usd is not None else cost.get("total_usd", 0.0))
        return {
            "max_iterations": int(
                state.get("max_iterations") or self.settings.strategy_max_iterations
            ),
            "iterations_used": int(
                iterations if iterations is not None else state.get("iteration", 0)
            ),
            "research_rounds": int(
                research_rounds
                if research_rounds is not None
                else len(state.get("research_rounds") or [])
            ),
            "max_cost_usd": max_cost,
            "max_tool_calls": self.settings.strategy_max_tool_calls,
            "max_wall_seconds": self.settings.strategy_max_wall_seconds,
            "spent_usd": round(spent, 6),
            "budget_exhausted": bool(max_cost and spent > max_cost),
        }

    # ------------------------------------------------------------- 1. objective
    async def understand_objective(self, state: StrategyWorkflowState) -> dict[str, Any]:
        """Validate the objective and decide what this run is allowed to do."""
        objective = (state.get("objective") or "").strip()
        if not objective:
            return {
                "status": "failed",
                "phase": "intake",
                "errors": [
                    {
                        "at": _now(),
                        "stage": "understand_objective",
                        "code": "missing_objective",
                        "message": "An objective is required to run the strategy workflow.",
                    }
                ],
                "trace": [_trace("objective_missing")],
            }

        brand = await self._brand(state)
        if brand is None:
            return {
                "status": "failed",
                "phase": "intake",
                "errors": [
                    {
                        "at": _now(),
                        "stage": "understand_objective",
                        "code": "brand_missing",
                        "message": "No brand profile: a strategy cannot be grounded without one.",
                    }
                ],
                "trace": [_trace("brand_missing")],
            }

        budget = dict(state.get("budget") or {})
        budget.setdefault("max_cost_usd", self.settings.strategy_budget_usd)
        budget.setdefault("max_tokens", self.settings.agent_max_tokens_per_run)
        budget.setdefault("currency", "USD")
        budget.setdefault("budget_inr_equivalent", round(
            float(budget["max_cost_usd"]) * self.settings.usd_inr_rate, 2
        ))

        max_iterations = int(
            state.get("max_iterations") or self.settings.strategy_max_iterations
        )

        return {
            "status": "understanding",
            "phase": "retrieving",
            "budget": budget,
            "max_iterations": max_iterations,
            "limits": {
                "max_iterations": max_iterations,
                "iterations_used": 0,
                "research_rounds": 0,
                "max_cost_usd": float(budget["max_cost_usd"]),
                "max_tool_calls": self.settings.strategy_max_tool_calls,
                "max_wall_seconds": self.settings.strategy_max_wall_seconds,
                "spent_usd": 0.0,
                "budget_exhausted": False,
            },
            "trace": [
                _trace(
                    "objective_understood",
                    objective=objective[:200],
                    business_objectives=state.get("business_objectives"),
                    budget_usd=budget["max_cost_usd"],
                )
            ],
        }

    # ------------------------------------------------------- 2. brand knowledge
    async def retrieve_brand_knowledge(self, state: StrategyWorkflowState) -> dict[str, Any]:
        """Load the editable knowledge base and list what is still unconfirmed."""
        brand = await self._brand(state)
        if brand is None:
            return {"status": "failed", "phase": "retrieving"}

        goals = await self.brand_service.list_goals(brand.id)
        pillars = await self.brand_service.list_pillars(brand.id)
        competitors = await self.brand_service.list_competitors(brand.id)

        facts = list(
            (
                await self.session.execute(
                    ProductFact.__table__.select().where(ProductFact.brand_profile_id == brand.id)
                )
            ).mappings()
        )
        unverified = [
            f"{row['statement']}"
            for row in facts
            if row["verification_status"] != VerificationStatus.VERIFIED.value
        ]

        snapshot = {
            "company": brand.company,
            "product": brand.product,
            "tagline": brand.tagline,
            "positioning": brand.positioning,
            "audience": list(brand.audience or []),
            "tone_of_voice": list(brand.tone_of_voice or []),
            "do_not_claim": list(brand.do_not_claim or []),
            "prohibited_phrases": list(brand.prohibited_phrases or []),
            "goals": [g.objective for g in goals],
            "pillars": [p.name for p in pillars],
            "competitors": [c.name for c in competitors],
            "verified_fact_count": len(facts) - len(unverified),
            "unverified_fact_count": len(unverified),
            "brand_version": brand.version,
        }

        warnings = []
        if unverified:
            warnings.append(
                f"{len(unverified)} product fact(s) are unconfirmed and will not be used as "
                "claims. Confirm them on the Brand Knowledge page."
            )
        if not goals:
            warnings.append(
                "No marketing goals defined; the strategy will rely on the objective only."
            )

        return {
            "status": "retrieving",
            "phase": "researching",
            "brand_snapshot": snapshot,
            "unverified_brand_claims": unverified[:50],
            "trace": [
                _trace(
                    "brand_retrieved",
                    competitors=len(competitors),
                    goals=len(goals),
                    unverified_facts=len(unverified),
                    warnings=warnings,
                )
            ],
        }

    # ------------------------------------------------------ 3. research decision
    async def decide_research(self, state: StrategyWorkflowState) -> dict[str, Any]:
        """Decide whether research is needed *and* possible.

        Both halves matter: the brief requires the Director to decide whether
        research is needed, and honesty requires it to notice when research
        cannot produce anything.
        """
        from app.research.base import research_status

        status = research_status(self.settings)
        brand_id = uuid.UUID(str(state["brand_profile_id"]))

        existing, total = await self.research_service.list_findings(
            brand_profile_id=brand_id, limit=1
        )
        # Count usable evidence, not just rows: a pile of uncited findings is not
        # a reason to skip research.
        usable, usable_total = await self.research_service.list_findings(
            brand_profile_id=brand_id,
            verification_status=VerificationStatus.VERIFIED.value,
            min_confidence=0.6,
            limit=1,
        )

        needed = True
        reasons: list[str] = []
        if total == 0:
            reasons.append("No research findings exist for this brand yet.")
        else:
            reasons.append(
                f"{total} existing finding(s) will be reused "
                f"({usable_total} verified and reasonably confident)."
            )
            if usable_total >= SUFFICIENT_VERIFIED_FINDINGS:
                reasons.append(
                    f"{usable_total} verified finding(s) already exist: enough to attempt a "
                    "strategy without a new research run."
                )
                needed = False

        provider_available = bool(status.get("available"))
        if not provider_available:
            reasons.append(
                "No research provider is available; the strategy will be built from brand "
                "knowledge and existing findings only."
            )
            needed = False

        return {
            "status": "researching",
            "phase": "researching" if needed else "strategising",
            "research_needed": needed,
            "trace": [
                _trace(
                    "research_decision",
                    needed=needed,
                    provider=status.get("provider"),
                    provider_status=status.get("status"),
                    existing_findings=total,
                    reasons=reasons,
                )
            ],
            "events": [
                {
                    "at": _now(),
                    "event": "research_decision",
                    "needed": needed,
                    "provider_status": status.get("status"),
                    "reasons": reasons,
                }
            ],
        }

    # ---------------------------------------------------------- 4. research run
    async def run_research(self, state: StrategyWorkflowState) -> dict[str, Any]:
        """Delegate to the Research Agent in evidence mode."""
        brand = await self._brand(state)
        if brand is None:
            return {"status": "failed", "phase": "researching"}

        round_number = len(state.get("research_rounds") or []) + 1
        categories = list(RESEARCH_CATEGORIES)
        previous = state.get("research_rounds") or []
        if previous:
            # Second and later rounds target only what is still weak.
            categories = [
                c
                for c in (previous[-1].get("gaps") or [])
                if any(c.startswith(r) for r in RETRYABLE_CATEGORIES)
            ] or list(RETRYABLE_CATEGORIES)

        ctx = self._ctx(state, brand, "research")
        agent = self.registry.get("research")
        payload = {
            "brand_profile_id": str(brand.id),
            "request": state.get("objective", ""),
            "mode": "evidence",
            "categories": categories,
            "research_project_id": state.get("research_project_id"),
            "max_iterations": max(1, int(state.get("max_iterations") or 4)),
            "max_cost_usd": float((state.get("budget") or {}).get("max_cost_usd", 0.12)),
        }

        try:
            result = await agent.run(ctx, payload)
        except (BudgetExhaustedError, CancelledProblem) as exc:
            return {
                "status": "budget_exhausted"
                if isinstance(exc, BudgetExhaustedError)
                else "cancelled",
                "phase": "researching",
                "stop_reason": getattr(exc, "code", "stopped"),
                "errors": [
                    {
                        "at": _now(),
                        "stage": "research",
                        "code": getattr(exc, "code", "stopped"),
                        "message": str(exc)[:500],
                    }
                ],
                "trace": [_trace("research_stopped", reason=str(exc)[:200])],
            }
        except Exception as exc:  # noqa: BLE001 - node boundary
            logger.exception("strategy_research_failed")
            return {
                "status": "partial",
                "phase": "strategising",
                "errors": [
                    {
                        "at": _now(),
                        "stage": "research",
                        "code": getattr(exc, "code", "agent_error"),
                        "message": str(exc)[:500],
                    }
                ],
                "trace": [_trace("research_failed", error=type(exc).__name__)],
            }

        dump = result.model_dump(mode="json")
        cost = dict(state.get("cost_actual") or {})
        cost["research_usd"] = round(
            float(cost.get("research_usd", 0.0)) + float((dump.get("cost") or {}).get("spent_usd", 0.0)),
            6,
        )
        cost["research_tokens"] = int(cost.get("research_tokens", 0)) + int(
            (dump.get("cost") or {}).get("tokens_used", 0)
        )

        summary = {
            "findings": dump.get("findings_created", 0),
            "sources": dump.get("sources_created", 0),
            "coverage": dump.get("category_coverage", []),
            "gaps": dump.get("gaps", []),
            "provider": dump.get("provider"),
            "provider_status": dump.get("provider_status"),
        }

        rounds_after = len(state.get("research_rounds") or []) + 1
        return {
            "status": "researching",
            "phase": "evaluating",
            "limits": self._limits(
                state,
                research_rounds=rounds_after,
                spent_usd=float(cost.get("total_usd", 0.0)),
            ),
            "research_project_id": dump.get("research_project_id"),
            "research": dump,
            "findings_summary": summary,
            "research_rounds": [
                {
                    "round": round_number,
                    "categories_requested": categories,
                    "findings_created": dump.get("findings_created", 0),
                    "gaps": dump.get("gaps", []),
                    "provider": dump.get("provider", ""),
                    "stop_reason": dump.get("stop_reason", ""),
                    "at": _now(),
                }
            ],
            "cost_actual": cost,
            "agent_runs": [str(ctx.run.id)] if ctx.run else [],
            "trace": [
                _trace(
                    "research_completed",
                    round=round_number,
                    findings=dump.get("findings_created", 0),
                    gaps=len(dump.get("gaps") or []),
                    provider=dump.get("provider"),
                )
            ],
            "events": [
                {
                    "at": _now(),
                    "event": "research_round",
                    "round": round_number,
                    "findings": dump.get("findings_created", 0),
                    "gaps": dump.get("gaps", []),
                }
            ],
        }

    # ------------------------------------------------------ 5. evidence gate
    async def evaluate_evidence(self, state: StrategyWorkflowState) -> dict[str, Any]:
        """Judge whether the evidence supports a strategy, and whether to dig more."""
        brand_id = uuid.UUID(str(state["brand_profile_id"]))
        findings, total = await self.research_service.list_findings(
            brand_profile_id=brand_id, limit=500
        )
        verified = [f for f in findings if f.verification_status == VerificationStatus.VERIFIED.value]
        cited = [f for f in findings if f.source_url]
        categories = {f.category for f in findings}

        # Reusing existing findings never runs a round, so there is no findings
        # summary yet: describe the evidence we are actually judging.
        summary = state.get("findings_summary") or {}
        if not summary:
            from app.research.base import research_status

            provider_info = research_status(self.settings)
            summary = {
                "findings": total,
                "sources": len(cited),
                "coverage": sorted(categories),
                "gaps": [c for c in RESEARCH_CATEGORIES if c not in categories],
                "provider": provider_info.get("provider") or "",
                "provider_status": provider_info.get("status") or "",
                "reused": True,
            }
        provider_status = summary.get("provider_status") or ""
        provider_name = summary.get("provider") or ""
        rounds = len(state.get("research_rounds") or [])
        max_iterations = int(state.get("max_iterations") or 4)

        if total == 0:
            verdict = "unavailable"
        elif total >= MIN_FINDINGS_FOR_SUFFICIENT and len(categories) >= MIN_CATEGORIES_FOR_SUFFICIENT:
            verdict = "sufficient"
        else:
            verdict = "insufficient"

        # Ask for more research only when it can actually change the answer:
        # a live provider, budget remaining, rounds left, and a real shortfall.
        # Only a provider that actually reaches the network can return something
        # different on a second pass. Status "degraded" means an offline provider.
        live_provider = (
            provider_status in ("available", "configured")
            and provider_name not in ("deterministic", "none", "")
        )
        budget_left = float((state.get("budget") or {}).get("max_cost_usd", 0.0)) - float(
            (state.get("cost_actual") or {}).get("research_usd", 0.0)
        )
        prev = (state.get("research_rounds") or [])
        no_progress = bool(prev) and prev[-1].get("findings_created", 0) == 0

        needs_more = (
            verdict == "insufficient"
            and live_provider
            and rounds < max_iterations
            and budget_left > 0
            and not no_progress
        )

        assessment = {
            "reused_existing_findings": bool(summary.get("reused")),
            "total_findings": total,
            "verified_findings": len(verified),
            "cited_findings": len(cited),
            "categories_covered": sorted(categories),
            "categories_missing": [
                c for c in RESEARCH_CATEGORIES if c not in categories
            ],
            "verdict": verdict,
            "rounds_completed": rounds,
            "live_provider": live_provider,
            "budget_remaining_usd": round(max(0.0, budget_left), 6),
            "no_progress": no_progress,
            "reasoning": (
                "No evidence at all: any strategy is a hypothesis."
                if verdict == "unavailable"
                else (
                    f"{total} finding(s) across {len(categories)} categor"
                    f"{'y' if len(categories) == 1 else 'ies'}."
                )
            ),
        }

        iterations_after = int(state.get("iteration") or 0) + 1
        return {
            "status": "evaluating",
            "phase": "researching" if needs_more else "strategising",
            "limits": self._limits(state, iterations=iterations_after),
            "evidence_verdict": verdict,
            "evidence_assessment": assessment,
            "findings_summary": summary,
            "needs_more_research": needs_more,
            "iteration": iterations_after,
            "trace": [
                _trace(
                    "evidence_evaluated",
                    verdict=verdict,
                    findings=total,
                    verified=len(verified),
                    needs_more=needs_more,
                )
            ],
            "events": [
                {
                    "at": _now(),
                    "event": "evidence_evaluated",
                    "verdict": verdict,
                    "findings": total,
                    "needs_more_research": needs_more,
                }
            ],
        }

    # ------------------------------------------------------- 6. strategy build
    async def generate_strategy(self, state: StrategyWorkflowState) -> dict[str, Any]:
        """Delegate to the Strategy Agent in strategy mode."""
        brand = await self._brand(state)
        if brand is None:
            return {"status": "failed", "phase": "strategising"}

        ctx = self._ctx(state, brand, "strategy")
        agent = self.registry.get("strategy")
        # Same derivation as the guard in ``_ctx``: 0.0 here means "blocked",
        # never "unlimited".
        remaining = self._remaining_budget(state)
        payload = {
            "brand_profile_id": str(brand.id),
            "request": state.get("objective", ""),
            "mode": "strategy",
            "business_objectives": state.get("business_objectives") or ["developer_acquisition"],
            "primary_objective_type": state.get("primary_objective_type", "developer_acquisition"),
            "platforms": state.get("platforms") or [],
            "research_project_id": state.get("research_project_id"),
            "max_cost_usd": round(remaining, 6),
        }

        try:
            result = await agent.run(ctx, payload)
        except (BudgetExhaustedError, CancelledProblem) as exc:
            return {
                "status": "budget_exhausted"
                if isinstance(exc, BudgetExhaustedError)
                else "cancelled",
                "phase": "strategising",
                "stop_reason": getattr(exc, "code", "stopped"),
                "errors": [
                    {
                        "at": _now(),
                        "stage": "strategy",
                        "code": getattr(exc, "code", "stopped"),
                        "message": str(exc)[:500],
                    }
                ],
                "trace": [_trace("strategy_stopped", reason=str(exc)[:200])],
            }
        except Exception as exc:  # noqa: BLE001 - node boundary
            logger.exception("strategy_generation_failed")
            return {
                "status": "failed",
                "phase": "strategising",
                "errors": [
                    {
                        "at": _now(),
                        "stage": "strategy",
                        "code": getattr(exc, "code", "agent_error"),
                        "message": str(exc)[:500],
                    }
                ],
                "trace": [_trace("strategy_failed", error=type(exc).__name__)],
            }

        dump = result.model_dump(mode="json")
        cost = dict(state.get("cost_actual") or {})
        cost["strategy_usd"] = round(
            float(cost.get("strategy_usd", 0.0)) + float((dump.get("cost") or {}).get("spent_usd", 0.0)),
            6,
        )
        cost["strategy_tokens"] = int(cost.get("strategy_tokens", 0)) + int(
            (dump.get("cost") or {}).get("tokens_used", 0)
        )
        cost["total_usd"] = round(
            float(cost.get("research_usd", 0.0)) + float(cost.get("strategy_usd", 0.0)), 6
        )

        return {
            "status": "strategising",
            "phase": "checking",
            "limits": self._limits(state, spent_usd=float(cost.get("total_usd", 0.0))),
            "strategy": dump,
            "strategy_id": dump.get("strategy_id"),
            "strategy_version_id": dump.get("strategy_version_id"),
            "strategy_version_number": dump.get("version_number", 0),
            "brand_check": dump.get("brand_check", {}),
            "missing_information": dump.get("missing_information", []),
            "unverified_claims_avoided": dump.get("unverified_claims_avoided", []),
            "cost_actual": cost,
            "agent_runs": [str(ctx.run.id)] if ctx.run else [],
            "trace": [
                _trace(
                    "strategy_generated",
                    strategy_id=dump.get("strategy_id"),
                    version=dump.get("version_number"),
                    confidence=dump.get("confidence"),
                    spend_usd=cost.get("total_usd"),
                )
            ],
            "events": [
                {
                    "at": _now(),
                    "event": "strategy_generated",
                    "strategy_id": dump.get("strategy_id"),
                    "version": dump.get("version_number"),
                }
            ],
        }

    # --------------------------------------------------- 7. consistency + gaps
    async def check_consistency(self, state: StrategyWorkflowState) -> dict[str, Any]:
        """Final gate before the strategy is shown to the founder."""
        strategy = state.get("strategy") or {}
        brand_check = dict(strategy.get("brand_check") or state.get("brand_check") or {})

        missing = list(state.get("missing_information") or [])
        if not missing:
            missing.append(
                {
                    "area": "founder_confirmation",
                    "detail": "Founder must confirm the audience and product claims before use.",
                    "blocks_decision": False,
                }
            )
        unverified = list(state.get("unverified_brand_claims") or [])
        if unverified:
            missing.append(
                {
                    "area": "unverified_product_facts",
                    "detail": (
                        f"{len(unverified)} product fact(s) are unconfirmed and were excluded "
                        "from the strategy."
                    ),
                    "blocks_decision": True,
                }
            )

        assessment = state.get("evidence_assessment") or {}
        if assessment.get("verdict") != "sufficient":
            missing.append(
                {
                    "area": "market_evidence",
                    "detail": (
                        f"Evidence verdict is '{assessment.get('verdict')}': "
                        f"{assessment.get('total_findings', 0)} finding(s), "
                        f"{assessment.get('verified_findings', 0)} verified."
                    ),
                    "blocks_decision": False,
                }
            )

        blocked = bool(brand_check.get("violations")) or not strategy.get("strategy_id")
        return {
            "status": "checking",
            "phase": "persisting" if not blocked else "checking",
            "brand_check": brand_check,
            "missing_information": missing,
            "trace": [
                _trace(
                    "consistency_checked",
                    passed=brand_check.get("passed"),
                    violations=brand_check.get("violations", []),
                    missing_items=len(missing),
                )
            ],
            "events": [
                {
                    "at": _now(),
                    "event": "brand_consistency_check",
                    "passed": brand_check.get("passed", False),
                    "violations": brand_check.get("violations", []),
                }
            ],
        }

    # ------------------------------------------------------------ 8. approval
    async def prepare_approval(self, state: StrategyWorkflowState) -> dict[str, Any]:
        """Assemble the approval packet. This is what the founder sees."""
        strategy = state.get("strategy") or {}
        brand_check = state.get("brand_check") or {}
        cost = state.get("cost_actual") or {}

        request = {
            "type": "strategy_approval",
            "workflow_id": state.get("workflow_id"),
            "strategy_id": state.get("strategy_id"),
            "strategy_version_id": state.get("strategy_version_id"),
            "version_number": state.get("strategy_version_number"),
            "objective": state.get("objective"),
            "business_objectives": state.get("business_objectives"),
            "summary": strategy.get("summary", ""),
            "confidence": strategy.get("confidence"),
            "evidence_verdict": state.get("evidence_verdict"),
            "evidence": (state.get("evidence_assessment") or {}),
            "brand_check": brand_check,
            "missing_information": state.get("missing_information", []),
            "warnings": strategy.get("warnings", []),
            "cost": cost,
            "budget": state.get("budget", {}),
            "kpis": strategy.get("kpis", []),
            "objective_alignment": strategy.get("objective_alignment", {}),
            "requested_at": _now(),
            "note": (
                "Approving authorises this exact strategy version for planning only. "
                "It does not publish content, send email, spend ad budget, or change any "
                "live account."
            ),
        }

        return {
            "status": "awaiting_approval",
            "phase": "awaiting_approval",
            "approval_request": request,
            "summary": strategy.get("summary", "")[:2000],
            "cost_actual": cost,
            "trace": [_trace("approval_requested", strategy_id=state.get("strategy_id"))],
        }

    # ------------------------------------------------------------ 9. finalise
    async def finalize(self, state: StrategyWorkflowState) -> dict[str, Any]:
        """Record the founder's decision against the exact version."""
        decision = state.get("approval_decision") or {}
        action = str(decision.get("action") or "reject")
        reason = str(decision.get("reason") or "")
        strategy_id = state.get("strategy_id")
        cost = state.get("cost_actual") or {}

        action_map = {
            "approve": "approve",
            "request_changes": "request_changes",
            "reject": "reject",
        }
        mapped = action_map.get(action, "reject")

        if not strategy_id:
            return {
                "status": "failed",
                "phase": "completed",
                "summary": "No strategy was produced, so there is nothing to approve.",
                "trace": [_trace("finalize_without_strategy")],
            }

        try:
            strategy = await self.strategy_service.get_strategy(uuid.UUID(str(strategy_id)))
            actor_id = uuid.UUID(state["actor_id"]) if state.get("actor_id") else None
            actor_role = "founder" if action in ("approve", "request_changes", "reject") else "system"
            await self.strategy_service.decide(
                strategy,
                action=mapped,
                actor_id=actor_id,
                actor_role=decision.get("actor_role") or actor_role,
                reason=reason,
                version_id=(
                    uuid.UUID(str(state["strategy_version_id"]))
                    if state.get("strategy_version_id")
                    else None
                ),
            )
        except Exception as exc:  # noqa: BLE001 - node boundary
            logger.warning("finalize_decision_failed", error=type(exc).__name__)
            return {
                "status": "failed",
                "phase": "completed",
                "errors": [
                    {
                        "at": _now(),
                        "stage": "finalize",
                        "code": getattr(exc, "code", "decision_error"),
                        "message": str(exc)[:500],
                    }
                ],
                "trace": [_trace("decision_failed", error=str(exc)[:200])],
            }

        status_map = {
            "approve": "approved",
            "request_changes": "rejected",
            "reject": "rejected",
        }
        final_status = status_map.get(action, "rejected")
        summary = {
            "approve": (
                f"Strategy v{state.get('strategy_version_number')} approved. "
                "Planning may proceed; nothing has been published or spent."
            ),
            "request_changes": (
                f"Changes requested on strategy v{state.get('strategy_version_number')}: "
                f"{reason[:200]}"
            ),
            "reject": (
                f"Strategy v{state.get('strategy_version_number')} rejected: {reason[:200]}"
            ),
        }.get(action, "Decision recorded.")

        return {
            "status": final_status,
            "phase": "completed",
            "summary": summary,
            "cost_actual": cost,
            "trace": [
                _trace(
                    "founder_decision",
                    action=action,
                    version=state.get("strategy_version_number"),
                    reason=reason[:200],
                )
            ],
            "events": [
                {
                    "at": _now(),
                    "event": "approval_decision",
                    "action": action,
                    "version": state.get("strategy_version_number"),
                }
            ],
        }

    # ---------------------------------------------------------------- failure
    async def fail(self, state: StrategyWorkflowState) -> dict[str, Any]:
        errors = state.get("errors") or []
        first = errors[0] if errors else {}
        return {
            "status": "failed",
            "phase": "completed",
            "summary": first.get("message", "The strategy workflow failed.")[:2000],
            "trace": [_trace("workflow_failed", code=first.get("code"))],
        }

    async def persist_summary(self, state: StrategyWorkflowState) -> dict[str, Any]:
        """Attach cost, limits and evidence to the final state for the UI."""
        cost = dict(state.get("cost_actual") or {})
        cost.setdefault("total_usd", 0.0)
        budget = state.get("budget") or {}
        max_cost = float(budget.get("max_cost_usd", 0.0))
        return {
            "cost_actual": cost,
            "limits": {
                "max_iterations": state.get("max_iterations"),
                "iterations_used": state.get("iteration", 0),
                "research_rounds": len(state.get("research_rounds") or []),
                "max_cost_usd": max_cost,
                "spent_usd": cost.get("total_usd", 0.0),
                "budget_exhausted": bool(max_cost and cost.get("total_usd", 0.0) > max_cost),
            },
            "trace": [
                _trace(
                    "workflow_settled",
                    status=state.get("status"),
                    spent_usd=cost.get("total_usd"),
                    rounds=len(state.get("research_rounds") or []),
                )
            ],
        }


__all__ = ["StrategyNodes", "RETRYABLE_CATEGORIES"]
