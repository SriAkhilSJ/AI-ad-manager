"""Phase 3A API tests: auth, idempotency, variants, calendar, publishing blocked.

Everything runs against the deterministic mock provider. No paid call is made, and
no test can publish anything.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

BASE = "/api/v1/content-studio"


async def seed_brand(client, auth):  # type: ignore[no-untyped-def]
    payload = {
        "name": "Crusor AI",
        "company": "Crusor AI",
        "product": "Crusor IDE",
        "tagline": "Build and Ship with Ease.",
        "positioning": "An AI-powered IDE for developers who ship.",
        "audience": ["engineering students", "software developers", "indie hackers"],
        "personality": ["technical", "premium", "minimal"],
        "keywords": ["AIdev", "DevTools"],
        "prohibited_phrases": ["revolutionary"],
        "do_not_claim": ["pricing"],
    }
    resp = await client.post("/api/v1/brand", json=payload, headers=auth("marketer"))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def seed_campaign(client, auth, brand):  # type: ignore[no-untyped-def]
    resp = await client.post(
        "/api/v1/campaigns",
        json={
            "brand_profile_id": brand["id"],
            "name": "Crusor AI Awareness Week",
            "objective": "Introduce Crusor AI to developers.",
            "platforms": ["instagram", "linkedin", "youtube", "x", "facebook", "reddit"],
            "status": "active",
        },
        headers=auth("marketer"),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# authentication and capabilities
# --------------------------------------------------------------------------- #
async def test_requires_authentication(client):
    for path in (f"{BASE}/platforms", f"{BASE}/overview", f"{BASE}/plans", f"{BASE}/calendar"):
        resp = await client.get(path)
        assert resp.status_code == 401, f"{path} was reachable without a token"


async def test_viewer_cannot_generate(client, auth, brand):
    resp = await client.post(
        f"{BASE}/generate",
        json={"platform": "x", "content_type": "social_post"},
        headers=auth("viewer"),
    )
    assert resp.status_code == 403


async def test_platform_specs_are_exposed(client, auth, brand):
    resp = await client.get(f"{BASE}/platforms", headers=auth("viewer"))
    assert resp.status_code == 200
    specs = {row["platform"]: row for row in resp.json()}
    assert set(specs) == {"instagram", "linkedin", "youtube", "x", "facebook", "reddit"}
    assert specs["x"]["body_max_chars"] == 280
    assert specs["reddit"]["hashtag_max"] == 0


async def test_capabilities_state_that_publishing_is_unavailable(client, auth, brand):
    resp = await client.get(f"{BASE}/capabilities", headers=auth("viewer"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["external_publishing"] is False
    assert body["external_scheduling"] is False
    assert body["is_mock_provider"] is True


# --------------------------------------------------------------------------- #
# single-slot generation
# --------------------------------------------------------------------------- #
async def test_generate_creates_a_mock_draft(client, auth, brand):
    campaign = await seed_campaign(client, auth, brand)
    resp = await client.post(
        f"{BASE}/generate",
        json={
            "campaign_id": campaign["id"],
            "platform": "linkedin",
            "content_type": "social_post",
            "audience": "software developers",
            "variants": 2,
        },
        headers=auth("marketer"),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["platform"] == "linkedin"
    assert body["is_mock"] is True
    assert body["provider"] == "mock"
    assert len(body["variants"]) == 2
    assert body["validation_status"] in {
        "PASS",
        "PASS_WITH_WARNINGS",
        "NEEDS_REVISION",
        "BLOCKED",
    }
    # persisted, and linked to the campaign
    detail = await client.get(f"/api/v1/content/{body['content_item_id']}", headers=auth("viewer"))
    assert detail.status_code == 200
    item = detail.json()
    assert item["campaign_id"] == campaign["id"]
    assert item["status"] == "DRAFT"
    assert item["version_count"] == 1


async def test_generate_is_idempotent_with_the_same_key(client, auth, brand):
    headers = {**auth("marketer"), "Idempotency-Key": "phase3a-idem-1"}
    payload = {"platform": "x", "content_type": "social_post", "primary_message": "fewer decisions"}
    first = await client.post(f"{BASE}/generate", json=payload, headers=headers)
    assert first.status_code == 201, first.text
    second = await client.post(f"{BASE}/generate", json=payload, headers=headers)
    assert second.status_code in (200, 201)
    assert second.json()["content_item_id"] == first.json()["content_item_id"]

    # no duplicate item was created
    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models.content import ContentItem

    async with get_session_factory()() as s:
        rows = (await s.execute(select(ContentItem))).scalars().all()
        assert len(rows) == 1, f"idempotency failed: {len(rows)} items created"


async def test_generate_rejects_unsupported_content_type(client, auth, brand):
    resp = await client.post(
        f"{BASE}/generate",
        json={"platform": "youtube", "content_type": "community_discussion"},
        headers=auth("marketer"),
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# weekly plans
# --------------------------------------------------------------------------- #
async def test_weekly_plan_creates_platform_specific_drafts(client, auth, brand):
    campaign = await seed_campaign(client, auth, brand)
    resp = await client.post(
        f"{BASE}/plans",
        json={
            "campaign_id": campaign["id"],
            "week_start": "2026-11-09",
            "config": {"posts_per_week": 6, "variants": 1},
            "generate": True,
        },
        headers=auth("marketer"),
    )
    assert resp.status_code == 201, resp.text
    plan = resp.json()
    assert plan["status"] == "generated"
    assert plan["slot_count"] == 6
    assert plan["item_count"] == 6
    assert plan["generated_by"] == "mock"
    assert plan["completeness_score"] > 0

    platforms = [slot["platform"] for slot in plan["slots"]]
    assert set(platforms) == {"instagram", "linkedin", "youtube", "x", "facebook", "reddit"}
    for slot in plan["slots"]:
        assert slot["content_item_id"], "every generated slot must link to a draft"
        assert slot["validation_status"]
        assert slot["is_mock"] is True
        # every platform gets a *different* content type mix, not a clone
    types = {slot["platform"]: slot["content_type"] for slot in plan["slots"]}
    assert len(set(types.values())) > 1


async def test_plan_detail_and_list(client, auth, brand):
    campaign = await seed_campaign(client, auth, brand)
    created = await client.post(
        f"{BASE}/plans",
        json={"campaign_id": campaign["id"], "week_start": "2026-11-16",
              "config": {"posts_per_week": 3, "platforms": ["linkedin", "x", "facebook"]}},
        headers=auth("marketer"),
    )
    assert created.status_code == 201, created.text
    plan_id = created.json()["id"]

    detail = await client.get(f"{BASE}/plans/{plan_id}", headers=auth("viewer"))
    assert detail.status_code == 200
    assert detail.json()["slot_count"] == 3

    listing = await client.get(f"{BASE}/plans", headers=auth("viewer"))
    assert listing.status_code == 200
    assert any(p["id"] == plan_id for p in listing.json())


async def test_plan_generate_is_idempotent(client, auth, brand):
    campaign = await seed_campaign(client, auth, brand)
    created = await client.post(
        f"{BASE}/plans",
        json={"campaign_id": campaign["id"], "week_start": "2026-11-23",
              "config": {"posts_per_week": 2, "platforms": ["linkedin", "x"]}},
        headers=auth("marketer"),
    )
    plan_id = created.json()["id"]

    first = await client.post(
        f"{BASE}/plans/{plan_id}/generate", headers={**auth("marketer"), "Idempotency-Key": "g1"}
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        f"{BASE}/plans/{plan_id}/generate", headers={**auth("marketer"), "Idempotency-Key": "g2"}
    )
    assert second.status_code == 200
    assert second.json()["item_count"] == first.json()["item_count"]

    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models.content import ContentItem

    async with get_session_factory()() as s:
        rows = (await s.execute(select(ContentItem))).scalars().all()
        assert len(rows) == first.json()["item_count"], "regeneration duplicated content"


async def test_over_capacity_plan_is_rejected(client, auth, brand):
    campaign = await seed_campaign(client, auth, brand)
    resp = await client.post(
        f"{BASE}/plans",
        json={"campaign_id": campaign["id"], "config": {"posts_per_week": 12}},
        headers=auth("marketer"),
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# variants
# --------------------------------------------------------------------------- #
async def test_variants_can_be_compared_and_selected(client, auth, brand):
    generated = await client.post(
        f"{BASE}/generate",
        json={"platform": "linkedin", "content_type": "social_post", "variants": 2},
        headers=auth("marketer"),
    )
    item_id = generated.json()["content_item_id"]

    listing = await client.get(f"{BASE}/items/{item_id}/variants", headers=auth("viewer"))
    assert listing.status_code == 200
    body = listing.json()
    assert len(body["variants"]) == 2
    assert body["selected_variant"] is not None
    assert body["is_mock"] is True
    bodies = {v["body"] for v in body["variants"]}
    assert len(bodies) == 2, "variants must not be identical"

    other = 1 - body["selected_variant"]
    selected = await client.post(
        f"{BASE}/items/{item_id}/variants/select",
        json={"variant_index": other},
        headers=auth("marketer"),
    )
    assert selected.status_code == 200, selected.text
    result = selected.json()
    assert result["version_number"] == 2
    assert result["selected_variant"] == other

    detail = await client.get(f"/api/v1/content/{item_id}", headers=auth("viewer"))
    assert detail.json()["version_count"] == 2
    assert detail.json()["current_version"]["body"] == body["variants"][other]["body"]


async def test_selecting_an_unknown_variant_is_rejected(client, auth, brand):
    generated = await client.post(
        f"{BASE}/generate",
        json={"platform": "x", "content_type": "social_post", "variants": 1},
        headers=auth("marketer"),
    )
    item_id = generated.json()["content_item_id"]
    resp = await client.post(
        f"{BASE}/items/{item_id}/variants/select",
        json={"variant_index": 2},
        headers=auth("marketer"),
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
async def test_validate_endpoint_records_verdict(client, auth, brand):
    generated = await client.post(
        f"{BASE}/generate",
        json={"platform": "linkedin", "content_type": "social_post"},
        headers=auth("marketer"),
    )
    item_id = generated.json()["content_item_id"]

    resp = await client.post(f"{BASE}/items/{item_id}/validate", headers=auth("marketer"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verdict"] in {"PASS", "PASS_WITH_WARNINGS", "NEEDS_REVISION", "BLOCKED"}
    assert body["content_version_id"]

    history = await client.get(f"{BASE}/items/{item_id}/validations", headers=auth("viewer"))
    assert history.status_code == 200
    assert len(history.json()) >= 2, "generation and the explicit check are both recorded"


async def test_blocked_content_cannot_enter_review(client, auth, brand):
    created = await client.post(
        "/api/v1/content",
        json={
            "brand_profile_id": brand["id"],
            "title": "Claim-heavy draft",
            "platform": "linkedin",
            "format": "social_post",
            "body": "We grew 340% this year and are 5x faster than every competitor.",
            "call_to_action": "Follow",
        },
        headers=auth("marketer"),
    )
    assert created.status_code == 201, created.text
    item_id = created.json()["id"]

    validation = await client.post(f"{BASE}/items/{item_id}/validate", headers=auth("marketer"))
    assert validation.json()["verdict"] == "BLOCKED"

    submit = await client.post(
        f"/api/v1/content/{item_id}/transition",
        json={"action": "submit"},
        headers=auth("marketer"),
    )
    assert submit.status_code == 409
    assert submit.json()["error"]["code"] == "invalid_state_transition"
    assert submit.json()["error"]["details"]["verdict"] == "BLOCKED"


# --------------------------------------------------------------------------- #
# calendar
# --------------------------------------------------------------------------- #
async def test_calendar_lists_internal_planning_entries(client, auth, brand):
    campaign = await seed_campaign(client, auth, brand)
    await client.post(
        f"{BASE}/plans",
        json={
            "campaign_id": campaign["id"],
            "week_start": (date.today() + timedelta(days=7)).isoformat(),
            "config": {"posts_per_week": 2, "platforms": ["linkedin", "x"]},
            "generate": True,
        },
        headers=auth("marketer"),
    )
    resp = await client.get(
        f"{BASE}/calendar",
        params={"view": "week", "anchor": (date.today() + timedelta(days=8)).isoformat()},
        headers=auth("viewer"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["external_scheduling"] is False
    assert body["publishing_available"] is False
    assert len(body["entries"]) == 2
    for entry in body["entries"]:
        assert entry["external_scheduling"] is False
        assert entry["label"] in {"planned", "draft", "in_review", "approved"}
        assert entry["campaign_id"] == campaign["id"]


async def test_calendar_move_updates_the_plan(client, auth, brand):
    generated = await client.post(
        f"{BASE}/generate",
        json={"platform": "x", "content_type": "social_post", "planned_date": date.today().isoformat()},
        headers=auth("marketer"),
    )
    item_id = generated.json()["content_item_id"]
    target = date.today() + timedelta(days=5)

    resp = await client.patch(
        f"{BASE}/calendar/{item_id}",
        json={"planned_date": target.isoformat(), "planned_time": "14:45"},
        headers=auth("marketer"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["planned_date"] == target.isoformat()
    assert resp.json()["planned_time"] == "14:45"

    bad = await client.patch(
        f"{BASE}/calendar/{item_id}",
        json={"planned_time": "99:99"},
        headers=auth("marketer"),
    )
    assert bad.status_code == 422


async def test_calendar_rejects_unknown_item(client, auth, brand):
    resp = await client.patch(
        f"{BASE}/calendar/{uuid.uuid4()}",
        json={"planned_date": date.today().isoformat()},
        headers=auth("marketer"),
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# overview
# --------------------------------------------------------------------------- #
async def test_overview_counts_drafts_and_flags_mock(client, auth, brand):
    await client.post(
        f"{BASE}/generate",
        json={"platform": "instagram", "content_type": "caption"},
        headers=auth("marketer"),
    )
    resp = await client.get(f"{BASE}/overview", headers=auth("viewer"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_drafts"] >= 1
    assert body["is_mock_provider"] is True
    assert body["live_model_calls_enabled"] is False
    assert body["by_platform"].get("instagram", 0) >= 1
    assert "PASS" in body["by_validation_status"] or body["by_validation_status"]


# --------------------------------------------------------------------------- #
# publishing stays impossible
# --------------------------------------------------------------------------- #
async def test_publishing_is_still_blocked(client, auth, brand):
    generated = await client.post(
        f"{BASE}/generate",
        json={"platform": "linkedin", "content_type": "social_post"},
        headers=auth("marketer"),
    )
    item_id = generated.json()["content_item_id"]
    resp = await client.post(
        f"/api/v1/content/{item_id}/publish",
        json={},
        headers=auth("founder"),
    )
    assert resp.status_code in (403, 409, 422, 503), resp.text
    assert resp.status_code != 200


async def test_no_external_schedule_route_is_exposed_from_the_studio(client, auth, brand):
    """The Studio must not become a back door to scheduling."""
    for path in (
        f"{BASE}/publish",
        f"{BASE}/schedule",
        f"{BASE}/calendar/publish",
    ):
        resp = await client.post(path, json={}, headers=auth("founder"))
        assert resp.status_code in (404, 405), f"{path} unexpectedly exists"


async def test_approval_still_requires_the_founder_role(client, auth, brand):
    generated = await client.post(
        f"{BASE}/generate",
        json={"platform": "x", "content_type": "social_post"},
        headers=auth("marketer"),
    )
    item_id = generated.json()["content_item_id"]
    submit = await client.post(
        f"/api/v1/content/{item_id}/transition", json={"action": "submit"}, headers=auth("marketer")
    )
    assert submit.status_code == 200, submit.text
    approve = await client.post(
        f"/api/v1/content/{item_id}/transition", json={"action": "approve"}, headers=auth("marketer")
    )
    assert approve.status_code == 403, "a marketer must not be able to approve"


async def test_plan_generated_items_store_their_variant_set(client, auth, brand):
    """Weekly planning must preserve variants per item, not only the chosen one.

    Regression: plan-generated drafts stored ``variant_count`` but not the variant
    payloads, so ``GET /items/{id}/variants`` returned an empty list for every item
    that came from a weekly plan.
    """
    campaign = await seed_campaign(client, auth, brand)
    resp = await client.post(
        f"{BASE}/plans",
        json={
            "campaign_id": campaign["id"],
            "config": {"posts_per_week": 3, "variants": 2},
            "generate": True,
        },
        headers=auth("marketer"),
    )
    assert resp.status_code in (200, 201), resp.text
    item_ids = [slot["content_item_id"] for slot in resp.json()["slots"] if slot["content_item_id"]]
    assert item_ids

    for item_id in item_ids:
        variants = await client.get(f"{BASE}/items/{item_id}/variants", headers=auth("viewer"))
        assert variants.status_code == 200, variants.text
        payload = variants.json()
        assert payload["is_mock"] is True
        assert len(payload["variants"]) >= 2, payload
        assert payload["selected_variant"] in [v["index"] for v in payload["variants"]]
        bodies = {v["body"] for v in payload["variants"]}
        assert len(bodies) == len(payload["variants"])


async def test_variant_regeneration_creates_a_new_version_and_keeps_history(client, auth, brand):
    """Regenerating variants must never rewrite a version (nor an approval)."""
    campaign = await seed_campaign(client, auth, brand)
    created = await client.post(
        f"{BASE}/generate",
        json={
            "campaign_id": campaign["id"],
            "platform": "linkedin",
            "content_type": "social_post",
            "variants": 2,
        },
        headers=auth("marketer"),
    )
    assert created.status_code == 201, created.text
    item_id = created.json()["content_item_id"]

    submitted = await client.post(
        f"/api/v1/content/{item_id}/transition",
        json={"action": "submit"},
        headers=auth("marketer"),
    )
    assert submitted.status_code == 200, submitted.text
    approved = await client.post(
        f"/api/v1/content/{item_id}/transition",
        json={"action": "approve"},
        headers=auth("founder"),
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["has_valid_approval"] is True
    approved_version_id = approved.json()["current_version"]["id"]

    regenerated = await client.post(
        f"{BASE}/items/{item_id}/variants/regenerate",
        json={"variants": 3, "change_note": "test regenerate"},
        headers=auth("marketer"),
    )
    assert regenerated.status_code == 200, regenerated.text
    body = regenerated.json()
    assert len(body["variants"]) == 3
    assert body["is_mock"] is True
    assert body["version_number"] == 2
    assert body["approvals_invalidated"] >= 1

    item = await client.get(f"/api/v1/content/{item_id}", headers=auth("viewer"))
    detail = item.json()
    assert detail["has_valid_approval"] is False
    assert detail["status"] == "DRAFT"

    versions = await client.get(f"/api/v1/content/{item_id}/versions", headers=auth("viewer"))
    original = next(v for v in versions.json() if v["id"] == approved_version_id)
    assert original["body"] == approved.json()["current_version"]["body"]

    stored = await client.get(f"{BASE}/items/{item_id}/variants", headers=auth("viewer"))
    assert len(stored.json()["variants"]) == 3
    assert stored.json()["version_number"] == 2
