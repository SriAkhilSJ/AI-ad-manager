"""LangGraph wiring for the Phase 2 strategy workflow.

Topology:

    START
      → understand_objective
      → retrieve_brand_knowledge
      → decide_research ─┬→ run_research → evaluate_evidence ─┐
                         │                        │            │ (more needed)
                         │                        │◄───────────┘  back to run_research
                         │                        └→ generate_strategy
                         └──────────────────────────────────→ generate_strategy
      → check_consistency
      → prepare_approval
      → human_strategy_approval   (real LangGraph interrupt)
      → finalize
      → settle
      → END

The only cycle is ``evaluate_evidence → run_research``. It is bounded three ways:
the node can only return "more" while rounds < max_iterations, while budget
remains, and while the provider is live *and* the last round actually produced
something. A cycle that cannot progress is not entered.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.director.strategy_nodes import StrategyNodes
from app.director.strategy_state import PHASE_LABELS, StrategyWorkflowState

# Node names must not collide with state keys (LangGraph requirement).
NODES = (
    "understand_objective",
    "retrieve_brand_knowledge",
    "decide_research",
    "run_research",
    "evaluate_evidence",
    "generate_strategy",
    "check_consistency",
    "prepare_approval",
    "human_strategy_approval",
    "finalize",
    "settle",
    "fail",
)


def _after_intake(state: StrategyWorkflowState) -> str:
    return "retrieve_brand" if state.get("status") != "failed" else "fail"


def _after_research_decision(state: StrategyWorkflowState) -> str:
    if state.get("status") in ("failed", "budget_exhausted", "cancelled"):
        return "fail"
    return "run_research" if state.get("research_needed") else "generate_strategy"


def _after_evaluation(state: StrategyWorkflowState) -> str:
    if state.get("status") in ("failed", "budget_exhausted", "cancelled"):
        return "fail"
    return "run_research" if state.get("needs_more_research") else "generate_strategy"


def _after_strategy(state: StrategyWorkflowState) -> str:
    if state.get("status") in ("failed", "budget_exhausted", "cancelled"):
        return "fail"
    return "check_consistency"


def _after_consistency(state: StrategyWorkflowState) -> str:
    """Block the approval step when the brand check found violations."""
    brand_check = state.get("brand_check") or {}
    if state.get("status") in ("failed", "budget_exhausted", "cancelled"):
        return "fail"
    if brand_check.get("violations"):
        return "fail"
    if not state.get("strategy_id"):
        return "fail"
    return "prepare_approval"


def build_strategy_graph(nodes: StrategyNodes, checkpointer: Any = None):
    builder = StateGraph(StrategyWorkflowState)

    builder.add_node("understand_objective", nodes.understand_objective)
    builder.add_node("retrieve_brand_knowledge", nodes.retrieve_brand_knowledge)
    builder.add_node("decide_research", nodes.decide_research)
    builder.add_node("run_research", nodes.run_research)
    builder.add_node("evaluate_evidence", nodes.evaluate_evidence)
    builder.add_node("generate_strategy", nodes.generate_strategy)
    builder.add_node("check_consistency", nodes.check_consistency)
    builder.add_node("prepare_approval", nodes.prepare_approval)
    builder.add_node("finalize", nodes.finalize)
    builder.add_node("settle", nodes.persist_summary)
    builder.add_node("fail", nodes.fail)

    async def human_strategy_approval(state: StrategyWorkflowState) -> dict[str, Any]:
        """Suspend until the founder decides on this exact strategy version.

        Resuming without a decision is impossible: ``interrupt`` returns only
        through ``Command(resume=...)``.
        """
        packet = state.get("approval_request") or {}
        decision = interrupt(
            {
                "type": "strategy_approval",
                "workflow_id": state.get("workflow_id"),
                "phase": PHASE_LABELS.get("awaiting_approval", "Waiting for founder approval"),
                "message": (
                    "Founder approval required for this exact strategy version. "
                    "Respond with {'action': 'approve'|'request_changes'|'reject', "
                    "'reason': str}. Approval authorises planning only: it does not "
                    "publish, send, spend or change any live account."
                ),
                "approval_request": packet,
                "strategy_id": state.get("strategy_id"),
                "version_number": state.get("strategy_version_number"),
                "brand_check": state.get("brand_check"),
                "cost": state.get("cost_actual"),
            }
        )
        decision = decision if isinstance(decision, dict) else {"action": "reject"}
        return {
            "approval_decision": decision,
            "trace": [
                {
                    "entry": "founder_decision_received",
                    "action": decision.get("action"),
                }
            ],
        }

    builder.add_node("human_strategy_approval", human_strategy_approval)

    # ---- edges -----------------------------------------------------------
    builder.add_edge(START, "understand_objective")
    builder.add_conditional_edges(
        "understand_objective", _after_intake,
        {"retrieve_brand": "retrieve_brand_knowledge", "fail": "fail"},
    )
    builder.add_edge("retrieve_brand_knowledge", "decide_research")
    builder.add_conditional_edges(
        "decide_research", _after_research_decision,
        {"run_research": "run_research", "generate_strategy": "generate_strategy", "fail": "fail"},
    )
    builder.add_edge("run_research", "evaluate_evidence")
    builder.add_conditional_edges(
        "evaluate_evidence", _after_evaluation,
        {"run_research": "run_research", "generate_strategy": "generate_strategy", "fail": "fail"},
    )
    builder.add_conditional_edges(
        "generate_strategy", _after_strategy,
        {"check_consistency": "check_consistency", "fail": "fail"},
    )
    builder.add_conditional_edges(
        "check_consistency", _after_consistency,
        {"prepare_approval": "prepare_approval", "fail": "fail"},
    )
    builder.add_edge("prepare_approval", "human_strategy_approval")
    builder.add_edge("human_strategy_approval", "finalize")
    builder.add_edge("finalize", "settle")
    builder.add_edge("settle", END)
    builder.add_edge("fail", "settle")

    return builder.compile(checkpointer=checkpointer)


def strategy_graph_topology() -> dict[str, Any]:
    """Static description for the API and the Director UI."""
    return {
        "entry": "understand_objective",
        "nodes": list(NODES),
        "edges": [
            ["START", "understand_objective"],
            ["understand_objective", "retrieve_brand_knowledge|fail"],
            ["retrieve_brand_knowledge", "decide_research"],
            ["decide_research", "run_research|generate_strategy|fail"],
            ["run_research", "evaluate_evidence"],
            ["evaluate_evidence", "run_research|generate_strategy|fail"],
            ["generate_strategy", "check_consistency|fail"],
            ["check_consistency", "prepare_approval|fail"],
            ["prepare_approval", "human_strategy_approval"],
            ["human_strategy_approval", "finalize"],
            ["finalize", "settle"],
            ["settle", "END"],
            ["fail", "settle"],
        ],
        "loops": [["evaluate_evidence", "run_research"]],
        "loop_bounds": [
            "max_iterations",
            "budget remaining > 0",
            "live provider only",
            "stops when a round produces no new findings",
        ],
        "interrupts": ["human_strategy_approval"],
        "checkpointer": "database",
        "phases": PHASE_LABELS,
        "note": (
            "Approval authorises planning only. No node publishes content, sends email, "
            "spends ad budget or modifies a live social account."
        ),
    }


__all__ = ["build_strategy_graph", "strategy_graph_topology", "NODES"]
