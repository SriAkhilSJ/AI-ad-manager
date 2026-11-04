"""Phase 4.2 — the Director drives the whole lifecycle, not just the copy.

The chain under test is the one the product promises:

    goal → research → strategy → campaign → content → creative → brand guardian
         → founder approval → schedule → publish → measure → optimise

These tests use the real graph, the real database checkpointer and the real
services. Nothing here publishes to a live platform: there are no platform
credentials in the test environment, and the assertions check that the workflow
records *why* it could not publish rather than pretending it did.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.director.graph import graph_topology
from app.director.lifecycle import LifecycleActions
from app.director.runtime import WorkflowRuntime, workflow_key
from app.models.social import SocialAccount, SocialAccountStatus


def _decision(**overrides) -> dict:
    base = {"action": "approve", "reason": "Ship it", "schedule": True, "publish_now": False}
    base.update(overrides)
    return base


async def _start(client, auth, objective: str, platforms=("linkedin",)) -> dict:
    resp = await client.post(
        "/api/v1/director/workflows",
        json={"objective": objective, "platforms": list(platforms)},
        headers=auth("marketer"),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ------------------------------------------------------------------ topology
def test_topology_continues_past_the_approval_interrupt():
    topo = graph_topology()
    nodes = topo["nodes"]
    for node in ("schedule", "publish", "measure", "optimize"):
        assert node in nodes
    edges = [tuple(edge) for edge in topo["edges"]]
    assert ("human_approval", "apply_decision") in edges
    assert ("apply_decision", "schedule|finalize") in edges
    assert ("schedule", "publish") in edges
    assert ("publish", "measure") in edges
    assert ("measure", "optimize") in edges
    assert ("optimize", "finalize") in edges


# --------------------------------------------------------- approval lifecycle
async def test_approval_drives_schedule_publish_measure_and_optimise(client, auth, brand):
    started = await _start(client, auth, "Grow developer signups this quarter")
    workflow_id = started["workflow_id"]

    decision = await client.post(
        f"/api/v1/director/workflows/{workflow_id}/decision",
        json=_decision(),
        headers=auth("founder"),
    )
    assert decision.status_code == 200, decision.text
    body = decision.json()

    assert body["status"] == "completed"
    assert body["lifecycle_stage"] == "handed_off"

    # Nothing is claimed to be scheduled without a connected account.
    assert body["schedule_plan"] == []
    blocked = body["schedule_blocked"]
    assert blocked and all("connected" in row["reason"] for row in blocked)

    # Publishing was not attempted, because the founder did not ask for it now.
    publish = body["publish_results"]
    assert publish["published"] == []

    # Measurement reports unavailability instead of zeroes.
    assert body["analytics"]["available"] is False
    assert body["optimizations"] == []
    assert "no optimisation was proposed" in body["optimization_note"].lower()

    # The trace shows the whole chain executed, in order.
    entries = [row["entry"] for row in body["trace"]]
    for entry in (
        "approval_requested",
        "decision_applied",
        "content_scheduled",
        "publish_attempted",
        "analytics_read",
        "optimisation_skipped",
        "finalized",
    ):
        assert entry in entries, f"{entry} missing from the trace: {entries}"
    assert entries.index("content_scheduled") < entries.index("publish_attempted")
    assert entries.index("publish_attempted") < entries.index("analytics_read")


async def test_rejecting_stops_at_finalize_without_touching_the_scheduler(client, auth, brand):
    started = await _start(client, auth, "Announce the v2 release")
    workflow_id = started["workflow_id"]

    resp = await client.post(
        f"/api/v1/director/workflows/{workflow_id}/decision",
        json={"action": "reject", "reason": "Off brand"},
        headers=auth("founder"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    entries = [row["entry"] for row in body["trace"]]
    assert "content_scheduled" not in entries
    assert "human_decision_received" in entries


async def test_approval_without_scheduling_consent_skips_the_lifecycle(client, auth, brand):
    started = await _start(client, auth, "Teaser series for launch week")
    workflow_id = started["workflow_id"]

    resp = await client.post(
        f"/api/v1/director/workflows/{workflow_id}/decision",
        json=_decision(schedule=False),
        headers=auth("founder"),
    )
    assert resp.status_code == 200, resp.text
    entries = [row["entry"] for row in resp.json()["trace"]]
    assert "content_scheduled" not in entries


# --------------------------------------------------------- durable scheduling
async def _connect(session, brand_profile_id: str, platform: str = "linkedin", suffix: str = "42"):
    account = SocialAccount(
        brand_profile_id=uuid.UUID(str(brand_profile_id)),
        platform=platform,
        external_account_id=f"urn:li:organization:{suffix}",
        display_name="Crusor AI",
        status=SocialAccountStatus.CONNECTED.value,
        token_ref=f"vault:{uuid.uuid4()}",
    )
    session.add(account)
    await session.commit()
    return account


async def test_approved_content_is_scheduled_on_a_connected_account(client, auth, brand, session):
    """With a connected account the workflow really creates a scheduled post."""
    started = await _start(client, auth, "Schedule the launch series")
    workflow_id = started["workflow_id"]
    status = (
        await client.get(f"/api/v1/director/workflows/{workflow_id}", headers=auth("viewer"))
    ).json()
    assert status["content_item_ids"]

    # Connect a LinkedIn account for the brand this workflow actually runs on (the
    # OAuth transport is not under test here; the account row is what the scheduler
    # requires).
    await _connect(session, status["brand_profile_id"], suffix="42")
    resp = await client.post(
        f"/api/v1/director/workflows/{workflow_id}/decision",
        json=_decision(),
        headers=auth("founder"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert len(body["scheduled_post_ids"]) == len(body["content_item_ids"])
    plan = body["schedule_plan"]
    assert all(row["status"] == "pending" for row in plan)
    slots = [datetime.fromisoformat(row["scheduled_for"]) for row in plan]
    assert slots == sorted(slots), "slots must be in chronological order"
    assert all(slot > datetime.now(timezone.utc) for slot in slots)

    # Publishing still did not happen: the founder asked for a schedule, not a send.
    assert body["publish_results"]["published"] == []
    assert len(body["publish_results"]["deferred"]) == len(slots)


async def test_publish_now_reports_the_real_blocker(client, auth, brand, session):
    """publish_now attempts publication and records the platform's refusal."""
    started = await _start(client, auth, "Publish the launch note now")
    workflow_id = started["workflow_id"]
    status = (
        await client.get(f"/api/v1/director/workflows/{workflow_id}", headers=auth("viewer"))
    ).json()
    await _connect(session, status["brand_profile_id"], suffix="43")
    resp = await client.post(
        f"/api/v1/director/workflows/{workflow_id}/decision",
        json=_decision(publish_now=True),
        headers=auth("founder"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    publish = body["publish_results"]
    assert publish["published"] == [], "nothing may be claimed as published"
    assert publish["failed"], "the refusal must be recorded per item"
    assert all("reason" in row for row in publish["failed"])


# ------------------------------------------------------------- duplicate work
def test_workflow_key_is_stable_and_request_scoped():
    brand = uuid.uuid4()
    first = workflow_key(brand_profile_id=brand, objective="Grow signups")
    assert first == workflow_key(brand_profile_id=brand, objective="  grow SIGNUPS ")
    assert first != workflow_key(brand_profile_id=brand, objective="Grow revenue")
    assert first != workflow_key(brand_profile_id=uuid.uuid4(), objective="Grow signups")


async def test_repeat_start_reuses_the_open_workflow(client, auth, brand):
    first = await _start(client, auth, "Same goal, twice")
    second = await _start(client, auth, "Same goal, twice")
    assert second["reused"] is True
    assert second["workflow_id"] == first["workflow_id"]


async def test_a_finished_workflow_does_not_absorb_a_new_request(client, auth, brand):
    first = await _start(client, auth, "A goal that completes")
    await client.post(
        f"/api/v1/director/workflows/{first['workflow_id']}/decision",
        json=_decision(schedule=False),
        headers=auth("founder"),
    )
    second = await _start(client, auth, "A goal that completes")
    assert second["reused"] is False
    assert second["workflow_id"] != first["workflow_id"]


# ------------------------------------------------------------------ cancel
async def test_cancelled_workflow_cannot_be_resumed(client, auth, brand):
    started = await _start(client, auth, "A goal we will stop")
    workflow_id = started["workflow_id"]

    cancel = await client.post(
        f"/api/v1/director/workflows/{workflow_id}/cancel",
        json={"reason": "changed priorities"},
        headers=auth("founder"),
    )
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "cancelled"
    assert "changed priorities" in cancel.json()["summary"]

    decision = await client.post(
        f"/api/v1/director/workflows/{workflow_id}/decision",
        json=_decision(),
        headers=auth("founder"),
    )
    assert decision.status_code == 422, decision.text
    assert decision.json()["error"]["code"] == "workflow_cancelled"


async def test_cancel_is_founder_only(client, auth, brand):
    started = await _start(client, auth, "A goal for permission tests")
    resp = await client.post(
        f"/api/v1/director/workflows/{started['workflow_id']}/cancel",
        json={"reason": "nope"},
        headers=auth("marketer"),
    )
    assert resp.status_code == 403


async def test_cancel_records_who_and_why_and_keeps_the_history(client, auth, brand):
    started = await _start(client, auth, "A goal with an audit trail")
    workflow_id = started["workflow_id"]
    await client.post(
        f"/api/v1/director/workflows/{workflow_id}/cancel",
        json={"reason": "budget freeze"},
        headers=auth("founder"),
    )
    status = (
        await client.get(f"/api/v1/director/workflows/{workflow_id}", headers=auth("viewer"))
    ).json()
    assert status["status"] == "cancelled"
    assert status["checkpoint_count"] > 0, "cancelling must not erase the history"
    assert any(row["entry"] == "workflow_cancelled" for row in status["trace"])


# ---------------------------------------------------------------- costs/budgets
async def test_workflow_records_its_own_token_usage(client, auth, brand, session):
    """The Director's LLM calls are billed to the workflow, so cost is attributable."""
    from sqlalchemy import select

    from app.models.agent import AgentRun

    started = await _start(client, auth, "Measure our spend attribution")
    workflow_id = started["workflow_id"]
    runs = (
        (
            await session.execute(
                select(AgentRun).where(AgentRun.workflow_thread_id == workflow_id)
            )
        )
        .scalars()
        .all()
    )
    assert runs, "agent runs must be attributed to the workflow thread"
    assert all(run.workflow_node for run in runs)


# --------------------------------------------------------------- honest limits
async def test_lifecycle_never_infers_performance(session, brand_row):
    actions = LifecycleActions(session)
    measured = await actions.measure(brand_profile_id=brand_row.id)
    assert measured["available"] is False
    assert measured["ingested"] == 0
    assert measured["detail"]
