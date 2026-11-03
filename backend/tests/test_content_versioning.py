"""Content creation, versioning and approval invalidation."""

from __future__ import annotations

import uuid

import pytest


async def test_create_content_starts_in_draft_with_version_one(client, auth, content_item):
    assert content_item["status"] == "DRAFT"
    assert content_item["version_count"] == 1
    assert content_item["current_version"]["version_number"] == 1
    assert content_item["current_version_id"] == content_item["current_version"]["id"]
    assert content_item["current_version"]["content_hash"]


async def test_versions_are_immutable_and_append_only(client, auth, content_item):
    iid = content_item["id"]
    original = (
        await client.get(f"/api/v1/content/{iid}/versions", headers=auth("marketer"))
    ).json()
    assert len(original) == 1
    original_body = original[0]["body"]

    await client.post(
        f"/api/v1/content/{iid}/versions",
        json={"body": "A different body.", "change_note": "rewrite"},
        headers=auth("marketer"),
    )
    versions = (
        await client.get(f"/api/v1/content/{iid}/versions", headers=auth("marketer"))
    ).json()
    assert len(versions) == 2
    # newest first
    assert versions[0]["version_number"] == 2
    # version 1 is untouched
    assert versions[1]["body"] == original_body


async def test_noop_edit_is_rejected(client, auth, content_item):
    iid = content_item["id"]
    resp = await client.post(
        f"/api/v1/content/{iid}/versions",
        json={"body": content_item["current_version"]["body"]},
        headers=auth("marketer"),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "no_change"


async def test_edit_invalidates_approval_and_returns_to_draft(client, auth, content_item):
    """Approval invalidation on edit (required behaviour)."""
    iid = content_item["id"]
    await client.post(
        f"/api/v1/content/{iid}/transition", json={"action": "submit"}, headers=auth("marketer")
    )
    approved = await client.post(
        f"/api/v1/content/{iid}/transition", json={"action": "approve"}, headers=auth("founder")
    )
    assert approved.json()["status"] == "APPROVED"

    edit = await client.post(
        f"/api/v1/content/{iid}/versions",
        json={"body": "Edited after approval.", "change_note": "post-approval tweak"},
        headers=auth("marketer"),
    )
    body = edit.json()
    assert body["status"] == "DRAFT"
    assert body["has_valid_approval"] is False
    assert body["version_count"] == 2

    ledger = await client.get(f"/api/v1/content/{iid}/approvals", headers=auth("founder"))
    rows = ledger.json()
    approve_rows = [r for r in rows if r["action"] == "approve"]
    assert approve_rows
    assert all(r["invalidated_at"] for r in approve_rows)
    assert "edited to version 2" in approve_rows[0]["invalidated_reason"]


async def test_edit_does_not_allow_stale_version_transition(client, auth, content_item):
    iid = content_item["id"]
    stale_version = content_item["current_version_id"]
    await client.post(
        f"/api/v1/content/{iid}/versions",
        json={"body": "second version"},
        headers=auth("marketer"),
    )
    resp = await client.post(
        f"/api/v1/content/{iid}/transition",
        json={"action": "submit", "content_version_id": stale_version},
        headers=auth("marketer"),
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "invalid_state_transition"


async def test_replacing_creative_invalidates_approval(client, auth, content_item):
    """Approval invalidation on creative asset replacement (required behaviour)."""
    iid = content_item["id"]
    upload = await client.post(
        "/api/v1/creative/upload",
        files={"file": ("hero.png", b"\x89PNG-first-bytes", "image/png")},
        data={"content_item_id": iid, "kind": "image"},
        headers=auth("marketer"),
    )
    assert upload.status_code == 201, upload.text
    asset_id = upload.json()["id"]
    checksum = upload.json()["checksum"]
    assert checksum

    await client.post(
        f"/api/v1/content/{iid}/transition", json={"action": "submit"}, headers=auth("marketer")
    )
    await client.post(
        f"/api/v1/content/{iid}/transition", json={"action": "approve"}, headers=auth("founder")
    )

    replaced = await client.post(
        f"/api/v1/creative/{asset_id}/replace",
        files={"file": ("hero.png", b"\x89PNG-second-bytes-different", "image/png")},
        headers=auth("marketer"),
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["invalidated_approvals"] >= 0

    detail = await client.get(f"/api/v1/content/{iid}", headers=auth("marketer"))
    assert detail.json()["has_valid_approval"] is False


async def test_unsupported_platform_rejected(client, auth, brand):
    resp = await client.post(
        "/api/v1/content",
        json={"brand_profile_id": brand["id"], "title": "x", "platform": "myspace"},
        headers=auth("marketer"),
    )
    assert resp.status_code == 422


async def test_campaign_content_counts(client, auth, brand, content_item):
    campaign = await client.post(
        "/api/v1/campaigns",
        json={
            "brand_profile_id": brand["id"],
            "name": "Launch",
            "platforms": ["linkedin"],
        },
        headers=auth("marketer"),
    )
    assert campaign.status_code == 201
    item = await client.post(
        "/api/v1/content",
        json={
            "brand_profile_id": brand["id"],
            "campaign_id": campaign.json()["id"],
            "title": "In campaign",
            "platform": "linkedin",
        },
        headers=auth("marketer"),
    )
    assert item.status_code == 201
    detail = await client.get(
        f"/api/v1/campaigns/{campaign.json()['id']}", headers=auth("marketer")
    )
    assert detail.status_code == 200
    assert detail.json()["content_item_count"] >= 1
    assert detail.json()["content_counts"].get("DRAFT", 0) >= 1
