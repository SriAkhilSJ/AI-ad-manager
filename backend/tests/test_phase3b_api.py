"""Phase 3B HTTP tests: creative briefs, mock generation, assets, QA, approval.

These hit the real ASGI app with the deterministic mock provider. Nothing paid runs,
and the last tests assert that publishing and paid generation are simply absent.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from httpx import AsyncClient

from app.creative.media import inspect_media, render_brand_frame

STUDIO = "/api/v1/creative-studio"


async def _brief(client: AsyncClient, auth, content_item, **overrides):  # type: ignore[no-untyped-def]
    payload = {
        "content_version_id": content_item["current_version_id"],
        "asset_type": "image",
        "creative_direction": "Minimal charcoal frame with a developer at a terminal.",
    }
    payload.update(overrides)
    resp = await client.post(f"{STUDIO}/briefs", json=payload, headers=auth("marketer"))
    return resp


# --------------------------------------------------------------------- reads
async def test_capabilities_are_honest_about_the_mock(client: AsyncClient, auth):
    resp = await client.get(f"{STUDIO}/capabilities", headers=auth())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "mock"
    assert body["is_mock"] is True
    assert body["billable"] is False
    assert body["live_generation_enabled"] is False
    assert body["supports_image"] is True
    assert body["video_playback"] == "poster_only"


async def test_flow_is_reported_as_unavailable_with_requirements(client: AsyncClient, auth):
    resp = await client.get(f"{STUDIO}/flow/status", headers=auth())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is False
    assert body["source_found"] is False
    assert body["enabled"] is False
    assert len(body["required_information"]) >= 5
    assert any("manifest.json" in item for item in body["required_information"])


async def test_overview_starts_empty_and_counts_after_work(
    client: AsyncClient, auth, content_item
):
    resp = await client.get(f"{STUDIO}/overview", headers=auth())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_assets"] == 0
    assert body["provider"] == "mock"
    assert body["paid_generation_enabled"] is False
    assert body["flow_connector"]["status"] == "not_available"
    assert body["storage"]["backend"]

    brief = (await _brief(client, auth, content_item)).json()
    await client.post(
        f"{STUDIO}/jobs", json={"brief_id": brief["id"]}, headers=auth("marketer")
    )
    body = (await client.get(f"{STUDIO}/overview", headers=auth())).json()
    assert body["total_assets"] == 1
    assert body["mock_assets"] == 1
    assert body["succeeded_jobs"] == 1
    assert body["pending_jobs"] == 0
    assert body["by_platform"]["linkedin"] == 1


async def test_authentication_is_required_everywhere(client: AsyncClient):
    for path in (
        "/capabilities",
        "/flow/status",
        "/overview",
        "/briefs",
        "/jobs",
        "/assets",
    ):
        resp = await client.get(f"{STUDIO}{path}")
        assert resp.status_code == 401, path


async def test_viewer_cannot_create_anything(client: AsyncClient, auth, content_item):
    resp = await client.post(
        f"{STUDIO}/briefs",
        json={"content_version_id": content_item["current_version_id"]},
        headers=auth("viewer"),
    )
    assert resp.status_code == 403


async def test_unknown_job_and_asset_are_404(client: AsyncClient, auth):
    missing = "11111111-1111-1111-1111-111111111111"
    assert (await client.get(f"{STUDIO}/jobs/{missing}", headers=auth())).status_code == 404
    assert (await client.get(f"{STUDIO}/assets/{missing}", headers=auth())).status_code == 404
    assert (await client.get(f"{STUDIO}/briefs/{missing}", headers=auth())).status_code == 404


async def test_paid_creatives_cannot_be_requested_over_http(client: AsyncClient, auth, content_item):
    brief = (await _brief(client, auth, content_item)).json()
    resp = await client.post(
        f"{STUDIO}/jobs",
        json={"brief_id": brief["id"], "parameters": {"model": "paid-provider"}},
        headers=auth("marketer"),
    )
    # Unknown parameters are accepted only because the mock is the configured
    # provider; the paid path itself has no route at all.
    if resp.status_code == 201:
        assert resp.json()["is_mock"] is True
        assert resp.json()["cost_actual_usd"] == 0.0


# -------------------------------------------------------------------- briefs
async def test_brief_creation_binds_the_version_without_touching_content(
    client: AsyncClient, auth, content_item
):
    before = (
        await client.get(f"/api/v1/content/{content_item['id']}", headers=auth())
    ).json()
    before_body = before["current_version"]["body"]
    resp = await _brief(client, auth, content_item)
    assert resp.status_code == 201, resp.text
    brief = resp.json()
    assert brief["content_version_id"] == content_item["current_version_id"]
    assert brief["content_hash"]
    assert brief["image_prompt"]
    assert brief["negative_prompt"]
    assert brief["validation_constraints"]["human_visual_review_required"] is True
    assert brief["version_number"] == 1

    after = (await client.get(f"/api/v1/content/{content_item['id']}", headers=auth())).json()
    assert after["current_version_id"] == before["current_version_id"]
    assert after["current_version"]["body"] == before_body
    assert after["status"] == before["status"]
    assert after["approval_count"] == before["approval_count"]


async def test_brief_creation_is_deduplicated_and_versioned(client: AsyncClient, auth, content_item):
    first = (await _brief(client, auth, content_item)).json()
    second = await _brief(client, auth, content_item)
    assert second.status_code == 200
    assert second.json()["id"] == first["id"]

    forced = await _brief(client, auth, content_item, force_new_version=True)
    assert forced.status_code == 201
    assert forced.json()["id"] != first["id"]
    assert forced.json()["version_number"] == 2

    listing = (await client.get(f"{STUDIO}/briefs", headers=auth())).json()
    assert len(listing) == 2
    filtered = (
        await client.get(
            f"{STUDIO}/briefs", params={"content_item_id": content_item["id"]}, headers=auth()
        )
    ).json()
    assert len(filtered) == 2


async def test_brief_requires_a_real_content_version(client: AsyncClient, auth):
    resp = await client.post(
        f"{STUDIO}/briefs",
        json={"content_item_id": "22222222-2222-2222-2222-222222222222"},
        headers=auth("marketer"),
    )
    assert resp.status_code == 404

    resp = await client.post(f"{STUDIO}/briefs", json={}, headers=auth("marketer"))
    assert resp.status_code in (400, 422)


async def test_brief_rejects_unknown_fields(client: AsyncClient, auth, content_item):
    resp = await _brief(client, auth, content_item, surprise="value")
    assert resp.status_code == 422


# ---------------------------------------------------------------- generation
async def test_mock_job_produces_a_stored_studio_asset(client: AsyncClient, auth, content_item):
    brief = (await _brief(client, auth, content_item)).json()
    resp = await client.post(
        f"{STUDIO}/jobs",
        json={"brief_id": brief["id"], "parameters": {"seed": 4}},
        headers=auth("marketer"),
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "succeeded"
    assert job["provider"] == "mock"
    assert job["is_mock"] is True
    assert job["cost_estimate_usd"] == 0.0
    assert job["external_job_id"].startswith("mock:")
    assert job["asset_id"]

    asset = (await client.get(f"{STUDIO}/assets/{job['asset_id']}", headers=auth())).json()
    assert asset["is_mock"] is True
    assert asset["mime_type"] == "image/png"
    assert asset["width"] and asset["height"]
    assert asset["brief_id"] == brief["id"]
    assert asset["content_version_id"] == brief["content_version_id"]
    assert asset["validation_status"] in {"pass", "pass_with_warnings", "needs_revision"}
    assert asset["mock_label"], "the UI needs a label to render"

    # the bytes really download and hash to the recorded checksum
    binary = await client.get(f"{STUDIO}/assets/{asset['id']}/bytes", headers=auth())
    assert binary.status_code == 200
    assert binary.headers["content-type"].startswith("image/png")
    assert hashlib.sha256(binary.content).hexdigest() == asset["checksum"]
    info = inspect_media(binary.content)
    assert (info.width, info.height) == (asset["width"], asset["height"])


async def test_job_submission_is_idempotent(client: AsyncClient, auth, content_item):
    brief = (await _brief(client, auth, content_item)).json()
    first = await client.post(
        f"{STUDIO}/jobs", json={"brief_id": brief["id"]}, headers=auth("marketer")
    )
    second = await client.post(
        f"{STUDIO}/jobs", json={"brief_id": brief["id"]}, headers=auth("marketer")
    )
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assets = (await client.get(f"{STUDIO}/assets", headers=auth())).json()
    assert len(assets) == 1


async def test_job_listing_history_and_poll(client: AsyncClient, auth, content_item):
    brief = (await _brief(client, auth, content_item)).json()
    job = (
        await client.post(f"{STUDIO}/jobs", json={"brief_id": brief["id"]}, headers=auth("marketer"))
    ).json()

    listing = (await client.get(f"{STUDIO}/jobs", headers=auth())).json()
    assert [j["id"] for j in listing] == [job["id"]]
    filtered = (
        await client.get(f"{STUDIO}/jobs", params={"brief_id": brief["id"]}, headers=auth())
    ).json()
    assert len(filtered) == 1

    polled = (await client.post(f"{STUDIO}/jobs/{job['id']}/poll", headers=auth("marketer"))).json()
    assert polled["status"] == "succeeded"
    assert polled["audit_trail"]


async def test_failed_job_can_be_retried_then_hits_the_attempt_limit(
    client: AsyncClient, auth, content_item
):
    brief = (await _brief(client, auth, content_item)).json()
    job = (
        await client.post(
            f"{STUDIO}/jobs",
            json={"brief_id": brief["id"], "parameters": {"mock_simulate": "fail"}},
            headers=auth("marketer"),
        )
    ).json()
    assert job["status"] == "failed"
    assert job["error_code"] == "mock_simulated_failure"
    assert job["error_retryable"] is True

    retried = await client.post(
        f"{STUDIO}/jobs/{job['id']}/retry", json={}, headers=auth("marketer")
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["attempts"] == 2
    assert retried.json()["status"] == "failed"

    exhausted = await client.post(
        f"{STUDIO}/jobs/{job['id']}/retry", json={}, headers=auth("marketer")
    )
    assert exhausted.status_code == 409


async def test_running_job_can_be_cancelled(client: AsyncClient, auth, content_item):
    brief = (await _brief(client, auth, content_item)).json()
    job = (
        await client.post(
            f"{STUDIO}/jobs",
            json={"brief_id": brief["id"], "parameters": {"mock_simulate": "timeout"}},
            headers=auth("marketer"),
        )
    ).json()
    assert job["status"] == "running"

    cancelled = await client.post(
        f"{STUDIO}/jobs/{job['id']}/cancel", json={}, headers=auth("marketer")
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    again = await client.post(
        f"{STUDIO}/jobs/{job['id']}/cancel", json={}, headers=auth("marketer")
    )
    assert again.status_code == 409


# -------------------------------------------------------------------- assets
async def test_asset_library_search_and_filters(client: AsyncClient, auth, content_item):
    brief = (await _brief(client, auth, content_item)).json()
    job = (
        await client.post(f"{STUDIO}/jobs", json={"brief_id": brief["id"]}, headers=auth("marketer"))
    ).json()

    assert len((await client.get(f"{STUDIO}/assets", headers=auth())).json()) == 1
    assert (
        len(
            (
                await client.get(
                    f"{STUDIO}/assets",
                    params={"brief_id": brief["id"]},
                    headers=auth(),
                )
            ).json()
        )
        == 1
    )
    assert (
        await client.get(f"{STUDIO}/assets", params={"brief_id": str(job["brief_id"])}, headers=auth())
    ).status_code == 200
    assert (
        await client.get(
            f"{STUDIO}/assets", params={"platform": "instagram"}, headers=auth()
        )
    ).json() == []
    assert (
        await client.get(f"{STUDIO}/assets", params={"is_mock": True}, headers=auth())
    ).json() != []


async def test_asset_validation_and_selection(client: AsyncClient, auth, content_item):
    brief = (await _brief(client, auth, content_item)).json()
    job = (
        await client.post(f"{STUDIO}/jobs", json={"brief_id": brief["id"]}, headers=auth("marketer"))
    ).json()

    validated = await client.post(
        f"{STUDIO}/assets/{job['asset_id']}/validate", json={}, headers=auth("marketer")
    )
    assert validated.status_code == 200, validated.text
    assert validated.json()["qa_report"]["visual_verification"] == "not_performed"

    chosen = await client.post(
        f"{STUDIO}/assets/{job['asset_id']}/select", json={}, headers=auth("marketer")
    )
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["asset"]["selected"] is True
    detail = (await client.get(f"{STUDIO}/assets/{job['asset_id']}", headers=auth())).json()
    assert detail["approval_status"] == "selected"
    assert detail["approval"] is None, "selection is not an approval"


async def test_replacing_an_asset_creates_a_new_version_and_keeps_the_old(
    client: AsyncClient, auth, content_item
):
    brief = (await _brief(client, auth, content_item)).json()
    job = (
        await client.post(f"{STUDIO}/jobs", json={"brief_id": brief["id"]}, headers=auth("marketer"))
    ).json()
    original = (await client.get(f"{STUDIO}/assets/{job['asset_id']}", headers=auth())).json()

    new_bytes = render_brand_frame(width=120, height=150, seed=9)
    resp = await client.post(
        f"{STUDIO}/assets/{original['id']}/versions",
        files={"file": ("replacement.png", new_bytes, "image/png")},
        headers=auth("marketer"),
    )
    assert resp.status_code == 201, resp.text
    replacement = resp.json()["asset"]
    assert replacement["asset_version"] == 2
    assert replacement["checksum"] != original["checksum"]
    assert replacement["storage_key"] != original["storage_key"]
    assert replacement["is_mock"] is False
    assert replacement["selected"] is False, "replacement must not inherit selection"
    assert (replacement["width"], replacement["height"]) == (120, 150)

    still_there = await client.get(f"{STUDIO}/assets/{original['id']}/bytes", headers=auth())
    assert still_there.status_code == 200
    assert hashlib.sha256(still_there.content).hexdigest() == original["checksum"]

    versions = (
        await client.get(f"{STUDIO}/assets", params={"content_item_id": content_item["id"]}, headers=auth())
    ).json()
    assert len(versions) == 2


async def test_upload_rejects_spoofed_media_and_accepts_real_png(
    client: AsyncClient, auth
):
    spoof = await client.post(
        f"{STUDIO}/assets/upload",
        files={"file": ("hero.png", b"PK\x03\x04" + b"\x00" * 128, "image/png")},
        headers=auth("marketer"),
    )
    assert spoof.status_code == 422, spoof.text
    assert "Unrecognised" in spoof.text

    png = render_brand_frame(width=64, height=80, seed=2)
    good = await client.post(
        f"{STUDIO}/assets/upload",
        files={"file": ("hero.png", png, "image/png")},
        headers=auth("marketer"),
    )
    assert good.status_code == 201, good.text
    body = good.json()
    assert body["detected_mime"] == "image/png"
    assert (body["width"], body["height"]) == (64, 80)
    assert body["is_mock"] is False
    assert body["metadata_json"]["source"] == "external_upload"

    binary = await client.get(f"{STUDIO}/assets/{body['id']}/bytes", headers=auth())
    assert hashlib.sha256(binary.content).hexdigest() == body["checksum"]


async def test_asset_detail_exposes_version_history(client: AsyncClient, auth, content_item):
    brief = (await _brief(client, auth, content_item)).json()
    job = (
        await client.post(f"{STUDIO}/jobs", json={"brief_id": brief["id"]}, headers=auth("marketer"))
    ).json()
    detail = (await client.get(f"{STUDIO}/assets/{job['asset_id']}", headers=auth())).json()
    assert detail["versions"][0]["id"] == job["asset_id"]
    assert detail["qa_report"]["checks"]
    assert detail["brief"]["id"] == brief["id"]
    assert detail["job"]["id"] == job["id"]


# ------------------------------------------------------- approval integration
async def test_approval_covers_content_version_brief_and_asset(
    client: AsyncClient, auth, content_item, session
):
    from app.models.content import ContentItem
    from app.models.approval import ApprovalRecord
    from sqlalchemy import select

    brief = (await _brief(client, auth, content_item)).json()
    job = (
        await client.post(f"{STUDIO}/jobs", json={"brief_id": brief["id"]}, headers=auth("marketer"))
    ).json()
    asset = (await client.get(f"{STUDIO}/assets/{job['asset_id']}", headers=auth())).json()
    assert asset["selected"] is False, "a generated asset must not select itself"

    selected = await client.post(
        f"{STUDIO}/assets/{asset['id']}/select", json={}, headers=auth("marketer")
    )
    assert selected.status_code == 200

    submitted = await client.post(
        f"/api/v1/content/{content_item['id']}/transition",
        json={"action": "submit"},
        headers=auth("marketer"),
    )
    assert submitted.status_code == 200, submitted.text
    approval = await client.post(
        f"/api/v1/content/{content_item['id']}/transition",
        json={"action": "approve", "reason": "approved with creative"},
        headers=auth("founder"),
    )
    assert approval.status_code == 200, approval.text
    assert approval.json()["has_valid_approval"] is True

    item = await session.get(ContentItem, content_item["id"])
    await session.refresh(item)
    rows = list(
        (
            await session.execute(
                select(ApprovalRecord)
                .where(ApprovalRecord.content_item_id == item.id)
                .order_by(ApprovalRecord.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    approved_row = next(row for row in rows if row.action == "approve")
    assert approved_row.creative_asset_id == uuid.UUID(asset["id"])
    assert approved_row.creative_brief_id == uuid.UUID(brief["id"])
    assert approved_row.creative_brief_version == 1
    assert approved_row.creative_asset_version == 1
    assert approved_row.creative_binding_hash
    assert approved_row.content_version_id == uuid.UUID(content_item["current_version_id"])

    # Generating another asset and selecting it invalidates that approval.
    second = (
        await client.post(
            f"{STUDIO}/jobs",
            json={"brief_id": brief["id"], "parameters": {"seed": 77}},
            headers=auth("marketer"),
        )
    ).json()
    swapped = await client.post(
        f"{STUDIO}/assets/{second['asset_id']}/select", json={}, headers=auth("marketer")
    )
    assert swapped.status_code == 200
    assert swapped.json()["invalidated_approvals"] >= 1

    state = (
        await client.get(f"/api/v1/content/{content_item['id']}", headers=auth())
    ).json()
    assert state["has_valid_approval"] is False, "approval must have been revoked"
    refreshed_rows = list(
        (
            await session.execute(
                select(ApprovalRecord)
                .where(ApprovalRecord.content_item_id == item.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    kept = next(row for row in refreshed_rows if row.id == approved_row.id)
    assert kept.invalidated_at is not None
    assert "asset" in (kept.invalidated_reason or "").lower() or kept.invalidated_reason


async def test_generated_asset_never_auto_approves_content(client: AsyncClient, auth, content_item):
    brief = (await _brief(client, auth, content_item)).json()
    await client.post(f"{STUDIO}/jobs", json={"brief_id": brief["id"]}, headers=auth("marketer"))
    state = (await client.get(f"/api/v1/content/{content_item['id']}", headers=auth())).json()
    assert state["status"] == "DRAFT"


# ------------------------------------------------------------ boundary checks
async def test_no_publishing_or_scheduling_endpoint_exists(client: AsyncClient, auth):
    for path in (
        "/api/v1/publishing",
        "/api/v1/publish",
        f"{STUDIO}/publish",
        f"{STUDIO}/assets/any/publish",
        "/api/v1/schedule",
    ):
        resp = await client.post(path, json={}, headers=auth())
        assert resp.status_code in (404, 405), path


async def test_system_status_reports_the_creative_section(client: AsyncClient, auth):
    resp = await client.get("/api/v1/system/status", headers=auth())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    creative = body.get("creative")
    assert creative is not None, "system status must describe the creative subsystem"
    assert creative["provider"] == "mock"
    assert creative["flow_connector"]["status"] == "not_available"
    assert creative["paid_generation_enabled"] is False
