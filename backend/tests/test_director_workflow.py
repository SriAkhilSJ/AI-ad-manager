"""Marketing Director workflow: non-linear routing, persistence, HITL, resume."""

from __future__ import annotations

import uuid

import pytest

from app.director.graph import graph_topology
from app.director.runtime import WorkflowRuntime


async def test_topology_is_not_a_sequential_chain():
    topo = graph_topology()
    nodes = topo["nodes"]
    assert "dispatch" in nodes
    assert "human_approval" in nodes
    # every agent returns to the supervisor
    for agent in ("research_agent", "strategy_agent", "content_agent"):
        assert [agent, "dispatch"] in topo["edges"]
    # the dispatch node fans out to more than one successor
    dispatch_edges = [e for e in topo["edges"] if e[0] == "dispatch"]
    assert dispatch_edges and "|" in dispatch_edges[0][1]
    assert topo["interrupts"] == ["human_approval"]


async def test_workflow_runs_to_approval_interrupt(client, auth, brand):
    resp = await client.post(
        "/api/v1/director/workflows",
        json={"objective": "Launch Crusor AI to developers", "platforms": ["linkedin", "x"]},
        headers=auth("marketer"),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    wf = data["workflow_id"]
    assert wf.startswith("wf_")

    status = await client.get(f"/api/v1/director/workflows/{wf}", headers=auth("viewer"))
    assert status.status_code == 200
    body = status.json()
    assert body["awaiting_human"] is True
    assert "human_approval" in body["next"]
    assert body["qa_verdict"] in {"pass", "needs_changes", "blocked"}
    assert body["content_item_ids"], "drafts should be persisted as content items"
    assert body["approval_request"]["items"]
    assert body["checkpoint_count"] > 0

    # each persisted item is a real, versioned content item in IN_REVIEW
    for item_id in body["content_item_ids"]:
        detail = await client.get(f"/api/v1/content/{item_id}", headers=auth("viewer"))
        assert detail.status_code == 200
        assert detail.json()["status"] == "IN_REVIEW"
        assert detail.json()["version_count"] >= 1


async def test_plan_is_dependency_ordered_and_uses_multiple_agents(client, auth, brand):
    resp = await client.post(
        "/api/v1/director/workflows",
        json={"objective": "Developer education series", "platforms": ["linkedin"]},
        headers=auth("marketer"),
    )
    wf = resp.json()["workflow_id"]
    body = (
        await client.get(f"/api/v1/director/workflows/{wf}", headers=auth("viewer"))
    ).json()
    plan = body["plan"]
    agents = {task["agent"] for task in plan}
    assert {"research", "strategy", "content", "creative", "growth_analyst"} <= agents
    assert all(task["status"] == "done" for task in plan)

    # dependency respected: strategy ran only after research
    strategy = next(t for t in plan if t["agent"] == "strategy")
    assert "research-1" in strategy["depends_on"]
    assert body["iteration"] > len(plan)  # supervisor looped, not straight-line


async def test_workflow_state_persists_across_runtime_instances(client, auth, brand):
    """Acceptance: workflow state survives a process restart (new runtime object,
    no in-memory state carried over)."""
    resp = await client.post(
        "/api/v1/director/workflows",
        json={"objective": "Persistence check", "platforms": ["linkedin"]},
        headers=auth("marketer"),
    )
    wf = resp.json()["workflow_id"]

    runtime_a = WorkflowRuntime()
    status_a = await runtime_a.status(wf)

    runtime_b = WorkflowRuntime()  # simulates a fresh process
    status_b = await runtime_b.status(wf)

    assert status_a["plan"] == status_b["plan"]
    assert status_a["content_item_ids"] == status_b["content_item_ids"]
    assert status_a["checkpoint_count"] == status_b["checkpoint_count"]
    assert status_b["awaiting_human"] is True


async def test_only_founder_can_resume_a_workflow(client, auth, brand):
    resp = await client.post(
        "/api/v1/director/workflows",
        json={"objective": "Decision gating", "platforms": ["linkedin"]},
        headers=auth("marketer"),
    )
    wf = resp.json()["workflow_id"]

    denied = await client.post(
        f"/api/v1/director/workflows/{wf}/decision",
        json={"action": "approve"},
        headers=auth("marketer"),
    )
    assert denied.status_code == 403

    ok = await client.post(
        f"/api/v1/director/workflows/{wf}/decision",
        json={"action": "approve", "reason": "founder approved"},
        headers=auth("founder"),
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["status"] == "completed"
    assert body["awaiting_human"] is False
    for item_id in body["content_item_ids"]:
        detail = await client.get(f"/api/v1/content/{item_id}", headers=auth("viewer"))
        assert detail.json()["status"] == "APPROVED"
        assert detail.json()["has_valid_approval"] is True


async def test_request_changes_decision_sets_item_state(client, auth, brand):
    resp = await client.post(
        "/api/v1/director/workflows",
        json={"objective": "Revision loop", "platforms": ["linkedin"]},
        headers=auth("marketer"),
    )
    wf = resp.json()["workflow_id"]
    res = await client.post(
        f"/api/v1/director/workflows/{wf}/decision",
        json={"action": "request_changes", "reason": "sharpen the hook"},
        headers=auth("founder"),
    )
    assert res.status_code == 200
    for item_id in res.json()["content_item_ids"]:
        detail = await client.get(f"/api/v1/content/{item_id}", headers=auth("viewer"))
        assert detail.json()["status"] == "CHANGES_REQUESTED"
        assert detail.json()["has_valid_approval"] is False


async def test_invalid_decision_action_is_rejected(client, auth, brand):
    resp = await client.post(
        "/api/v1/director/workflows",
        json={"objective": "Bad decision", "platforms": ["linkedin"]},
        headers=auth("marketer"),
    )
    wf = resp.json()["workflow_id"]
    bad = await client.post(
        f"/api/v1/director/workflows/{wf}/decision",
        json={"action": "publish_everything"},
        headers=auth("founder"),
    )
    assert bad.status_code == 422


async def test_unknown_workflow_returns_404(client, auth):
    resp = await client.get("/api/v1/director/workflows/wf_does_not_exist", headers=auth("viewer"))
    assert resp.status_code == 404


async def test_workflow_requires_brand(client, auth):
    resp = await client.post(
        "/api/v1/director/workflows",
        json={"objective": "No brand configured"},
        headers=auth("marketer"),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "brand_required"


async def test_workflow_without_brand_id_uses_active_brand(client, auth, brand):
    resp = await client.post(
        "/api/v1/director/workflows",
        json={"objective": "Uses active brand", "platforms": ["linkedin"]},
        headers=auth("marketer"),
    )
    assert resp.status_code == 201
    assert resp.json()["state"]["brand_profile_id"] == brand["id"]


async def test_workflow_list(client, auth, brand):
    await client.post(
        "/api/v1/director/workflows",
        json={"objective": "Listed workflow", "platforms": ["linkedin"]},
        headers=auth("marketer"),
    )
    resp = await client.get("/api/v1/director/workflows", headers=auth("viewer"))
    assert resp.status_code == 200
    assert len(resp.json()) >= 1
    assert {"workflow_id", "status", "node", "step"} <= set(resp.json()[0])


async def test_agent_runs_are_recorded_by_workflow(client, auth, brand):
    await client.post(
        "/api/v1/director/workflows",
        json={"objective": "Ledger check", "platforms": ["linkedin"]},
        headers=auth("marketer"),
    )
    runs = await client.get("/api/v1/agents/runs", headers=auth("viewer"))
    assert runs.status_code == 200
    agents = {r["agent"] for r in runs.json()}
    assert {"research", "strategy", "content", "brand_guardian"} & agents
    assert all(r["status"] in {"succeeded", "running"} for r in runs.json())
