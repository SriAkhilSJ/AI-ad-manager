"""LangGraph wiring for the Marketing Director.

Topology (supervisor + conditional routing, **not** a sequential chain):

    START → intake → plan → dispatch ─┬→ research ─┐
                                       ├→ strategy ─┤
                                       ├→ content ──┤→ dispatch (loop)
                                       ├→ creative ─┤
                                       └→ growth ───┘
    dispatch → review (when no runnable task remains)
    review → ensure_campaign → persist_drafts → request_approval
           → human_approval (interrupt)
           → schedule → publish → measure → optimize → finalize → END   (approved)
           → finalize → END                                            (anything else)

The post-approval branches are the second half of the promise: content that the
founder approved is scheduled against a real connected account, published only if
the founder said "publish now", measured from real analytics, and turned into
recommendations that are never auto-applied.

The loop back to ``dispatch`` means the execution order is decided by the state
machine at run time, guided by task dependencies, not by edge order.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.director.nodes import DirectorNodes
from app.director.state import DirectorState

# Node names must not collide with DirectorState keys (LangGraph requires this),
# so agent nodes are suffixed. The mapping keeps routing logic readable.
AGENT_NODES = ("research", "strategy", "content", "creative", "growth_analyst")
NODE_NAME = {agent: f"{agent}_agent" for agent in AGENT_NODES}
NODE_TO_AGENT = {node: agent for agent, node in NODE_NAME.items()}


def _route_after_intake(state: DirectorState) -> str:
    return "planner" if state.get("status") != "failed" else END


def _route_next_task(state: DirectorState) -> str:
    """Choose the next node from the live plan, or move to review."""
    if state.get("status") == "failed":
        return "fail"
    plan = state.get("plan") or []
    running = [t for t in plan if t.get("status") == "running"]
    if running:
        agent = running[0]["agent"]
        return NODE_NAME.get(agent, "review")
    return "review"


def _route_after_decision(state: DirectorState) -> str:
    """Only an explicit approval continues into the external lifecycle."""
    decision = state.get("approval_decision") or {}
    if decision.get("action") != "approve":
        return "finalize"
    if decision.get("schedule", True) is False:
        return "finalize"
    return "schedule"


def build_graph(nodes: DirectorNodes, checkpointer: Any = None):
    builder = StateGraph(DirectorState)

    builder.add_node("intake", nodes.intake)
    builder.add_node("planner", nodes.plan)
    builder.add_node("dispatch", nodes.dispatch)

    async def research(state: DirectorState) -> dict[str, Any]:
        return await nodes.execute_agent(state, "research")

    async def strategy(state: DirectorState) -> dict[str, Any]:
        return await nodes.execute_agent(state, "strategy")

    async def content(state: DirectorState) -> dict[str, Any]:
        return await nodes.execute_agent(state, "content")

    async def creative(state: DirectorState) -> dict[str, Any]:
        return await nodes.execute_agent(state, "creative")

    async def growth(state: DirectorState) -> dict[str, Any]:
        return await nodes.execute_agent(state, "growth_analyst")

    for agent, fn in (
        ("research", research),
        ("strategy", strategy),
        ("content", content),
        ("creative", creative),
        ("growth_analyst", growth),
    ):
        builder.add_node(NODE_NAME[agent], fn)

    builder.add_node("review", nodes.review)
    builder.add_node("ensure_campaign", nodes.ensure_campaign)
    builder.add_node("persist_drafts", nodes.persist_drafts)
    builder.add_node("request_approval", nodes.request_approval)
    builder.add_node("apply_decision", nodes.apply_decision)
    builder.add_node("schedule", nodes.schedule)
    builder.add_node("publish", nodes.publish)
    builder.add_node("measure", nodes.measure)
    builder.add_node("optimize", nodes.optimize)
    builder.add_node("finalize", nodes.finalize)
    builder.add_node("fail", nodes.fail)

    # ---- human-in-the-loop interrupt -------------------------------------
    async def human_approval(state: DirectorState) -> dict[str, Any]:
        """Suspend until the founder decides. Resumed only via Command(resume=...)."""
        packet = state.get("approval_request") or {}
        decision = interrupt(
            {
                "type": "approval_required",
                "workflow_id": state.get("workflow_id"),
                "message": (
                    "Founder approval required for generated marketing content. "
                    "Respond with {'action': 'approve'|'request_changes'|'reject', "
                    "'reason': str}."
                ),
                "approval_request": packet,
                "qa_verdict": state.get("qa_verdict"),
            }
        )
        decision = decision if isinstance(decision, dict) else {"action": "reject"}
        return {
            "approval_decision": decision,
            "trace": [
                {
                    "entry": "human_decision_received",
                    "action": decision.get("action"),
                }
            ],
        }

    builder.add_node("human_approval", human_approval)

    # ---- edges -----------------------------------------------------------
    builder.add_edge(START, "intake")
    builder.add_conditional_edges(
        "intake", _route_after_intake, {"planner": "planner", END: END}
    )
    builder.add_edge("planner", "dispatch")
    routing_map = {NODE_NAME[a]: NODE_NAME[a] for a in AGENT_NODES}
    routing_map.update({"review": "review", "fail": "fail"})
    builder.add_conditional_edges("dispatch", _route_next_task, routing_map)

    for agent in AGENT_NODES:
        builder.add_edge(NODE_NAME[agent], "dispatch")

    builder.add_edge("review", "ensure_campaign")
    builder.add_edge("ensure_campaign", "persist_drafts")
    builder.add_edge("persist_drafts", "request_approval")
    builder.add_edge("request_approval", "human_approval")
    builder.add_edge("human_approval", "apply_decision")
    builder.add_conditional_edges(
        "apply_decision",
        _route_after_decision,
        {"schedule": "schedule", "finalize": "finalize"},
    )
    builder.add_edge("schedule", "publish")
    builder.add_edge("publish", "measure")
    builder.add_edge("measure", "optimize")
    builder.add_edge("optimize", "finalize")
    builder.add_edge("finalize", END)
    builder.add_edge("fail", END)

    return builder.compile(checkpointer=checkpointer)


def graph_topology() -> dict[str, Any]:
    """Static description used by the API/UI (docs the shape without compiling)."""
    return {
        "entry": "intake",
        "nodes": [
            "intake",
            "planner",
            "dispatch",
            *(NODE_NAME[a] for a in AGENT_NODES),
            "review",
            "ensure_campaign",
            "persist_drafts",
            "request_approval",
            "human_approval",
            "apply_decision",
            "schedule",
            "publish",
            "measure",
            "optimize",
            "finalize",
            "fail",
        ],
        "edges": [
            ["START", "intake"],
            ["intake", "planner|END"],
            ["planner", "dispatch"],
            ["dispatch", "|".join(NODE_NAME.values()) + "|review|fail"],
            *[[NODE_NAME[agent], "dispatch"] for agent in AGENT_NODES],
            ["review", "ensure_campaign"],
            ["ensure_campaign", "persist_drafts"],
            ["persist_drafts", "request_approval"],
            ["request_approval", "human_approval"],
            ["human_approval", "apply_decision"],
            ["apply_decision", "schedule|finalize"],
            ["schedule", "publish"],
            ["publish", "measure"],
            ["measure", "optimize"],
            ["optimize", "finalize"],
            ["finalize", "END"],
        ],
        "interrupts": ["human_approval"],
        "checkpointer": "database",
    }
