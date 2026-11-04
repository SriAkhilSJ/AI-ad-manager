"""Strategy Studio + Director API: authz, versioning, approval enforcement."""

from __future__ import annotations

import uuid

import pytest


async def seed_brand_with_kb(client, auth):  # type: ignore[no-untyped-def]
    from app.db.session import get_session_factory
    from app.models.brand import BrandProfile
    from app.seed import brand_crusor

    payload = {
        "name": "Crusor AI",
        "company": "Crusor AI",
        "product": "AI-powered IDE",
        "tagline": "Build and Ship with Ease",
        "positioning": "AI-powered development environment.",
        "audience": ["software developers", "indie hackers"],
        "prohibited_phrases": ["10x", "revolutionary"],
        "do_not_claim": ["customer or user counts"],
    }
    resp = await client.post("/api/v1/brand", json=payload, headers=auth("marketer"))
    assert resp.status_code == 201, resp.text
    brand = resp.json()

    async with get_session_factory()() as s:
        row = await s.get(BrandProfile, uuid.UUID(brand["id"]))
        await brand_crusor.seed_knowledge_base(row)
        await s.commit()
    return brand


async def generate(client, auth, brand, **overrides):  # type: ignore[no-untyped-def]
    body = {
        "brand_profile_id": brand["id"],
        "objective": "Acquire developers for Crusor AI",
        "business_objectives": ["developer_acquisition"],
    }
    body.update(overrides)
    return await client.post("/api/v1/strategy/generate", json=body, headers=auth("marketer"))


# --------------------------------------------------------------------------- #
# capabilities
# --------------------------------------------------------------------------- #
async def test_capabilities_report_budget_and_topology(client, auth):
    resp = await client.get("/api/v1/strategy/capabilities", headers=auth("viewer"))
    assert resp.status_code == 200
    body = resp.json()

    assert body["business_objectives"] == [
        "brand_awareness",
        "developer_acquisition",
        "waitlist_registrations",
        "product_adoption",
        "community_growth",
    ]
    assert body["budget_usd"] > 0
    assert body["topology"]["interrupts"] == ["human_strategy_approval"]
    assert "never publishes" in body["note"]


async def test_capabilities_require_auth(client):
    assert (await client.get("/api/v1/strategy/capabilities")).status_code == 401


# --------------------------------------------------------------------------- #
# generation
# --------------------------------------------------------------------------- #
async def test_generate_creates_a_draft_version(client, auth):
    brand = await seed_brand_with_kb(client, auth)
    resp = await generate(client, auth, brand)
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["strategy"]["status"] == "DRAFT"
    assert body["current_version"]["version_number"] == 1
    assert body["current_version"]["business_objectives"] == ["developer_acquisition"]
    assert body["current_version"]["kpis"]
    assert body["can_execute"] is False
    assert "approval" in body["execution_blocked_reason"].lower()


async def test_generate_requires_editor_role(client, auth):
    brand = await seed_brand_with_kb(client, auth)
    resp = await client.post(
        "/api/v1/strategy/generate",
        json={"brand_profile_id": brand["id"], "objective": "Acquire developers"},
        headers=auth("viewer"),
    )
    assert resp.status_code == 403


async def test_generate_rejects_unknown_objective(client, auth):
    brand = await seed_brand_with_kb(client, auth)
    resp = await generate(client, auth, brand, business_objectives=["go_viral"])
    assert resp.status_code == 422


async def test_generate_rejects_unknown_brand(client, auth):
    resp = await client.post(
        "/api/v1/strategy/generate",
        json={"brand_profile_id": str(uuid.uuid4()), "objective": "Acquire developers"},
        headers=auth("marketer"),
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# approval enforcement
# --------------------------------------------------------------------------- #
async def test_founder_can_approve_and_execution_unlocks(client, auth):
    brand = await seed_brand_with_kb(client, auth)
    created = (await generate(client, auth, brand)).json()
    strategy_id = created["strategy"]["id"]

    resp = await client.post(
        f"/api/v1/strategy/{strategy_id}/decision",
        json={"action": "approve", "reason": "Aligned with Q4 developer acquisition"},
        headers=auth("founder"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["strategy"]["status"] == "APPROVED"
    assert body["approval"] is not None
    assert body["approval"]["strategy_version_id"] == created["current_version"]["id"]
    assert body["can_execute"] is True

    status = await client.get(
        f"/api/v1/strategy/{strategy_id}/execution-status", headers=auth("viewer")
    )
    assert status.json()["can_execute"] is True
    # Even when authorised, nothing is published by this deployment.
    assert "Planning only" in status.json()["actions_available"]


async def test_marketer_cannot_approve(client, auth):
    brand = await seed_brand_with_kb(client, auth)
    strategy_id = (await generate(client, auth, brand)).json()["strategy"]["id"]

    resp = await client.post(
        f"/api/v1/strategy/{strategy_id}/decision",
        json={"action": "approve", "reason": "self-approved"},
        headers=auth("marketer"),
    )
    assert resp.status_code == 403


async def test_editing_invalidates_approval(client, auth):
    brand = await seed_brand_with_kb(client, auth)
    created = (await generate(client, auth, brand)).json()
    strategy_id = created["strategy"]["id"]

    await client.post(
        f"/api/v1/strategy/{strategy_id}/decision",
        json={"action": "approve", "reason": "ok"},
        headers=auth("founder"),
    )

    edited = await client.post(
        f"/api/v1/strategy/{strategy_id}/versions",
        json={"rationale": "Changed after approval", "change_note": "Founder edit"},
        headers=auth("marketer"),
    )
    assert edited.status_code == 201, edited.text
    body = edited.json()

    assert body["current_version"]["version_number"] == 2
    assert body["strategy"]["status"] == "DRAFT"
    assert body["approval"] is None
    assert body["can_execute"] is False

    history = body["approval_history"]
    previous = next(h for h in history if h["action"] == "approve")
    assert previous["is_valid"] is False
    assert "superseded" in previous["invalidated_reason"]


async def test_execution_status_explains_refusal(client, auth):
    brand = await seed_brand_with_kb(client, auth)
    strategy_id = (await generate(client, auth, brand)).json()["strategy"]["id"]

    resp = await client.get(
        f"/api/v1/strategy/{strategy_id}/execution-status", headers=auth("viewer")
    )
    body = resp.json()
    assert body["can_execute"] is False
    assert "approval" in body["blocked_reason"].lower()
    assert body["approved_version_id"] == ""


# --------------------------------------------------------------------------- #
# versions
# --------------------------------------------------------------------------- #
async def test_versions_and_diff(client, auth):
    brand = await seed_brand_with_kb(client, auth)
    created = (await generate(client, auth, brand)).json()
    strategy_id = created["strategy"]["id"]

    await client.post(
        f"/api/v1/strategy/{strategy_id}/versions",
        json={"rationale": "Second rationale", "kpis": [{"name": "trial_starts"}]},
        headers=auth("marketer"),
    )

    detail = await client.get(f"/api/v1/strategy/{strategy_id}", headers=auth("viewer"))
    versions = detail.json()["versions"]
    assert [v["version_number"] for v in versions] == [2, 1]

    diff = await client.get(
        f"/api/v1/strategy/{strategy_id}/diff",
        params={"left": 1, "right": 2},
        headers=auth("viewer"),
    )
    assert diff.status_code == 200
    assert "rationale" in diff.json()["changed_fields"]

    missing = await client.get(
        f"/api/v1/strategy/{strategy_id}/diff",
        params={"left": 1, "right": 9},
        headers=auth("viewer"),
    )
    assert missing.status_code == 404


async def test_version_from_another_strategy_is_not_found(client, auth):
    brand = await seed_brand_with_kb(client, auth)
    first = (await generate(client, auth, brand, name="Strategy A")).json()
    second = (await generate(client, auth, brand, name="Strategy B")).json()

    resp = await client.get(
        f"/api/v1/strategy/{first['strategy']['id']}/versions/"
        f"{second['current_version']['id']}",
        headers=auth("viewer"),
    )
    assert resp.status_code == 404


async def test_edit_cannot_drop_the_business_objective(client, auth):
    brand = await seed_brand_with_kb(client, auth)
    strategy_id = (await generate(client, auth, brand)).json()["strategy"]["id"]

    resp = await client.post(
        f"/api/v1/strategy/{strategy_id}/versions",
        json={"business_objectives": []},
        headers=auth("marketer"),
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# director workflow
# --------------------------------------------------------------------------- #
async def test_director_estimate_is_returned_before_running(client, auth):
    resp = await client.get(
        "/api/v1/director/strategy/estimate", params={"budget_usd": 0.06}, headers=auth("viewer")
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["estimated_cost_usd"] >= 0
    assert body["budget_usd"] == 0.06
    assert body["within_budget"] is True
    assert any("can exceed" in a for a in body["assumptions"])


async def test_director_topology_matches_the_graph(client, auth):
    resp = await client.get("/api/v1/director/strategy/topology", headers=auth("viewer"))
    body = resp.json()
    assert body["entry"] == "understand_objective"
    assert "human_strategy_approval" in body["interrupts"]
    assert body["loop_bounds"]


async def test_director_full_run_and_founder_approval(client, auth):
    brand = await seed_brand_with_kb(client, auth)

    start = await client.post(
        "/api/v1/director/strategy/start",
        json={
            "brand_profile_id": brand["id"],
            "objective": "Acquire developers for Crusor AI",
            "business_objectives": ["developer_acquisition"],
        },
        headers=auth("marketer"),
    )
    assert start.status_code == 200, start.text
    body = start.json()
    workflow_id = body["workflow_id"]

    assert body["state"]["status"] == "awaiting_approval"
    assert body["state"]["strategy_id"]

    status = await client.get(
        f"/api/v1/director/strategy/workflows/{workflow_id}", headers=auth("viewer")
    )
    status_body = status.json()
    assert status_body["awaiting_human"] is True
    assert status_body["phase"] == "awaiting_approval"
    assert status_body["cost_estimate"]
    assert status_body["evidence_assessment"]

    refused = await client.post(
        f"/api/v1/director/strategy/workflows/{workflow_id}/decision",
        json={"action": "approve", "reason": "ok"},
        headers=auth("marketer"),
    )
    assert refused.status_code == 403

    approved = await client.post(
        f"/api/v1/director/strategy/workflows/{workflow_id}/decision",
        json={"action": "approve", "reason": "Approved for planning"},
        headers=auth("founder"),
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"]["status"] == "approved"


async def test_director_run_requires_a_brand(client, auth):
    resp = await client.post(
        "/api/v1/director/strategy/start",
        json={"brand_profile_id": str(uuid.uuid4()), "objective": "Acquire developers"},
        headers=auth("marketer"),
    )
    assert resp.status_code == 404


async def test_director_rejects_unknown_objective(client, auth):
    brand = await seed_brand_with_kb(client, auth)
    resp = await client.post(
        "/api/v1/director/strategy/start",
        json={
            "brand_profile_id": brand["id"],
            "objective": "Acquire developers",
            "business_objectives": ["go_viral"],
        },
        headers=auth("marketer"),
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# system status carries the Phase 2 capability report
# --------------------------------------------------------------------------- #
async def test_system_status_reports_research_and_strategy(client, auth):
    resp = await client.get("/api/v1/system/status", headers=auth("viewer"))
    assert resp.status_code == 200
    body = resp.json()

    assert "research" in body
    assert "provider" in body["research"]
    assert body["strategy"]["status"] == "available"
    assert body["strategy"]["budget_usd"] > 0
    assert body["app"]["version"] == "1.1.0-phase2"
