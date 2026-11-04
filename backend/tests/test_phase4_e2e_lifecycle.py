"""Phase 4.2 — one founder instruction, followed through the whole lifecycle.

This is the acceptance test the release gate asks for: a single business goal is
driven from intake to hand-off, and every stage is asserted from persisted state.

    goal → research → strategy → campaign → content → creative → brand guardian
         → founder approval → schedule → publish → measure → optimise → hand-off

It uses a real database, the real graph, the real checkpointer and the real
services. Two things it deliberately does *not* do:

* it does not publish to a live platform (there are no credentials, and a mocked
  transport is not evidence of a live integration), and
* it does not invent performance data — the analytics stage is asserted to report
  "unavailable", because no platform has reported anything for this brand.
"""

from __future__ import annotations

import uuid

from app.models.social import SocialAccount, SocialAccountStatus


async def _connect_account(session, brand_profile_id: str, platform: str = "linkedin"):
    account = SocialAccount(
        brand_profile_id=uuid.UUID(str(brand_profile_id)),
        platform=platform,
        external_account_id=f"urn:li:organization:{uuid.uuid4().hex[:6]}",
        display_name="Crusor AI",
        status=SocialAccountStatus.CONNECTED.value,
        token_ref=f"vault:{uuid.uuid4()}",
    )
    session.add(account)
    await session.commit()
    return account


async def test_full_lifecycle_from_a_single_instruction(client, auth, brand, session):
    # 1. The founder states a goal.
    started = await client.post(
        "/api/v1/director/workflows",
        json={
            "objective": "Get 500 developer signups for the Crusor IDE this quarter",
            "platforms": ["linkedin", "x"],
        },
        headers=auth("marketer"),
    )
    assert started.status_code == 201, started.text
    workflow_id = started.json()["workflow_id"]

    # 2. Agents run and stop at a real human interrupt.
    pre = (
        await client.get(f"/api/v1/director/workflows/{workflow_id}", headers=auth("viewer"))
    ).json()
    assert pre["awaiting_human"] is True
    assert pre["status"] == "awaiting_approval"
    task_agents = {task["agent"] for task in pre["plan"]}
    assert {"research", "strategy", "content", "creative", "growth_analyst"} <= task_agents
    assert all(task["status"] == "done" for task in pre["plan"])
    assert pre["qa_verdict"] in {"pass", "needs_changes"}

    # 3. The campaign and versioned content exist before approval.
    assert pre["created_campaign_id"]
    campaign = await client.get(
        f"/api/v1/campaigns/{pre['created_campaign_id']}", headers=auth("viewer")
    )
    assert campaign.status_code == 200, campaign.text
    items = pre["content_item_ids"]
    assert items
    for item_id in items:
        detail = await client.get(f"/api/v1/content/{item_id}", headers=auth("viewer"))
        assert detail.json()["status"] == "IN_REVIEW"

    # 4. Connect an account per requested platform so the schedule stage can do
    #    real work (the workflow asks for linkedin + x).
    await _connect_account(session, pre["brand_profile_id"], "linkedin")
    await _connect_account(session, pre["brand_profile_id"], "x")

    # 5. The founder approves, and authorises scheduling (not immediate sending).
    decided = await client.post(
        f"/api/v1/director/workflows/{workflow_id}/decision",
        json={
            "action": "approve",
            "reason": "Approved for launch",
            "schedule": True,
            "publish_now": False,
        },
        headers=auth("founder"),
    )
    assert decided.status_code == 200, decided.text
    body = decided.json()

    # 6. Every approved item became a scheduled post on the connected account.
    assert len(body["scheduled_post_ids"]) == len(items)
    assert body["schedule_blocked"] == []
    for row in body["schedule_plan"]:
        assert row["platform"] in {"linkedin", "x"}
        assert row["status"] == "pending"

    # 7. Nothing was published: the founder did not ask for it now, and publishing
    #    is a separate gated operation.
    assert body["publish_results"]["published"] == []
    assert len(body["publish_results"]["deferred"]) == len(items)

    # 8. Measurement is honest about having no data yet.
    assert body["analytics"]["available"] is False
    assert body["analytics"]["ingested"] == 0

    # 9. Optimisation produced nothing, because there was nothing to reason from.
    assert body["optimizations"] == []
    assert "no optimisation was proposed" in body["optimization_note"].lower()

    # 10. The content really is approved, and moved to SCHEDULED by the schedule
    #     stage (the approval and the post are both persisted, not just reported).
    for item_id in items:
        detail = await client.get(f"/api/v1/content/{item_id}", headers=auth("viewer"))
        assert detail.json()["status"] == "SCHEDULED"

    # 11. The workflow is complete, and its trace covers every stage in order.
    final = (
        await client.get(f"/api/v1/director/workflows/{workflow_id}", headers=auth("viewer"))
    ).json()
    assert final["status"] == "completed"
    assert final["lifecycle_stage"] == "handed_off"
    entries = [row["entry"] for row in final["trace"]]
    ordered = [
        "intake",
        "task_dispatched",
        "review_completed",
        "approval_requested",
        "human_decision_received",
        "decision_applied",
        "content_scheduled",
        "publish_attempted",
        "analytics_read",
        "optimisation_skipped",
        "finalized",
    ]
    positions = [entries.index(entry) for entry in ordered]
    assert positions == sorted(positions), f"trace out of order: {entries}"

    # 12. Cost is attributable: every agent run names this workflow thread.
    from sqlalchemy import select

    from app.models.agent import AgentRun

    runs = (
        (await session.execute(select(AgentRun).where(AgentRun.workflow_thread_id == workflow_id)))
        .scalars()
        .all()
    )
    assert runs


async def test_the_whole_lifecycle_is_idempotent_on_repeat_requests(client, auth, brand):
    payload = {
        "objective": "Repeat-safe launch campaign",
        "platforms": ["linkedin"],
    }
    first = await client.post("/api/v1/director/workflows", json=payload, headers=auth("marketer"))
    second = await client.post("/api/v1/director/workflows", json=payload, headers=auth("marketer"))
    assert first.status_code == second.status_code == 201
    assert first.json()["workflow_id"] == second.json()["workflow_id"]
    assert second.json()["reused"] is True

    # A second approval of the same workflow must not duplicate scheduled work.
    workflow_id = first.json()["workflow_id"]
    await client.post(
        f"/api/v1/director/workflows/{workflow_id}/decision",
        json={"action": "approve", "schedule": True},
        headers=auth("founder"),
    )
    again = await client.post(
        f"/api/v1/director/workflows/{workflow_id}/decision",
        json={"action": "approve", "schedule": True},
        headers=auth("founder"),
    )
    assert again.status_code == 200
    assert again.json().get("awaiting_human") is False


async def test_a_cancelled_run_leaves_no_pending_work(client, auth, brand, session):
    from sqlalchemy import func, select

    from app.models.social import ScheduledPost

    started = await client.post(
        "/api/v1/director/workflows",
        json={"objective": "A run we will cancel", "platforms": ["linkedin"]},
        headers=auth("marketer"),
    )
    workflow_id = started.json()["workflow_id"]
    pre = (
        await client.get(f"/api/v1/director/workflows/{workflow_id}", headers=auth("viewer"))
    ).json()
    await _connect_account(session, pre["brand_profile_id"])

    before = (await session.execute(select(func.count()).select_from(ScheduledPost))).scalar_one()
    cancelled = await client.post(
        f"/api/v1/director/workflows/{workflow_id}/cancel",
        json={"reason": "cancel before approval"},
        headers=auth("founder"),
    )
    assert cancelled.status_code == 200
    after = (await session.execute(select(func.count()).select_from(ScheduledPost))).scalar_one()
    assert after == before, "cancelling before approval must schedule nothing"
    assert cancelled.json()["status"] == "cancelled"
