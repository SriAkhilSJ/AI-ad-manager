"""Idempotency of sensitive operations (approve / publish / schedule)."""

from __future__ import annotations

import uuid


async def test_repeated_approve_returns_same_record(client, auth, content_item):
    iid = content_item["id"]
    await client.post(
        f"/api/v1/content/{iid}/transition", json={"action": "submit"}, headers=auth("marketer")
    )
    key = f"key-{uuid.uuid4()}"
    first = await client.post(
        f"/api/v1/content/{iid}/transition",
        json={"action": "approve"},
        headers={**auth("founder"), "Idempotency-Key": key},
    )
    assert first.status_code == 200

    second = await client.post(
        f"/api/v1/content/{iid}/transition",
        json={"action": "approve"},
        headers={**auth("founder"), "Idempotency-Key": key},
    )
    assert second.status_code == 200
    assert second.headers.get("Idempotency-Replayed") == "true"
    assert second.json()["status"] == "APPROVED"

    ledger = await client.get(f"/api/v1/content/{iid}/approvals", headers=auth("founder"))
    approve_rows = [r for r in ledger.json() if r["action"] == "approve"]
    assert len(approve_rows) == 1, "duplicate approval rows were created"


async def test_repeated_submit_without_key_is_still_idempotent(client, auth, content_item):
    iid = content_item["id"]
    first = await client.post(
        f"/api/v1/content/{iid}/transition", json={"action": "submit"}, headers=auth("marketer")
    )
    second = await client.post(
        f"/api/v1/content/{iid}/transition", json={"action": "submit"}, headers=auth("marketer")
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "IN_REVIEW"

    ledger = await client.get(f"/api/v1/content/{iid}/approvals", headers=auth("founder"))
    submits = [r for r in ledger.json() if r["action"] == "submit"]
    assert len(submits) == 1


async def test_same_key_different_body_conflicts(client, auth, content_item):
    iid = content_item["id"]
    key = f"key-{uuid.uuid4()}"
    first = await client.post(
        f"/api/v1/content/{iid}/transition",
        json={"action": "submit"},
        headers={**auth("marketer"), "Idempotency-Key": key},
    )
    assert first.status_code == 200
    conflict = await client.post(
        f"/api/v1/content/{iid}/transition",
        json={"action": "submit", "reason": "different payload"},
        headers={**auth("marketer"), "Idempotency-Key": key},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_key_reuse"


async def test_concurrent_publish_requests_are_deduplicated(client, auth, brand, content_item):
    """Two identical publish calls must not create two publications."""
    iid = content_item["id"]
    await client.post(
        f"/api/v1/content/{iid}/transition", json={"action": "submit"}, headers=auth("marketer")
    )
    await client.post(
        f"/api/v1/content/{iid}/transition", json={"action": "approve"}, headers=auth("founder")
    )
    account = await client.post(
        "/api/v1/social/accounts",
        json={"brand_profile_id": brand["id"], "platform": "linkedin", "handle": "crusor"},
        headers=auth("marketer"),
    )

    from app.db.session import get_session_factory
    from app.models.social import SocialAccount

    factory = get_session_factory()
    async with factory() as session:
        row = await session.get(SocialAccount, uuid.UUID(account.json()["id"]))
        row.status = "connected"
        session.add(row)
        await session.commit()

    key = f"pub-{uuid.uuid4()}"
    payload = {"social_account_id": account.json()["id"]}
    first = await client.post(
        f"/api/v1/content/{iid}/publish",
        json=payload,
        headers={**auth("marketer"), "Idempotency-Key": key},
    )
    second = await client.post(
        f"/api/v1/content/{iid}/publish",
        json=payload,
        headers={**auth("marketer"), "Idempotency-Key": key},
    )
    # Both are rejected because Phase 1 has no transport; crucially they are
    # rejected identically and no publication row is created.
    assert first.status_code == 409
    assert second.status_code == 409
    pubs = await client.get(f"/api/v1/content/{iid}/publications", headers=auth("marketer"))
    assert pubs.json() == []


async def test_idempotency_store_is_scoped_per_user(client, auth, content_item, users):
    iid = content_item["id"]
    key = f"shared-{uuid.uuid4()}"
    await client.post(
        f"/api/v1/content/{iid}/transition",
        json={"action": "submit"},
        headers={**auth("marketer"), "Idempotency-Key": key},
    )
    # A different user reusing the key must not be served the first user's replay.
    other = await client.post(
        f"/api/v1/content/{iid}/transition",
        json={"action": "approve"},
        headers={**auth("founder"), "Idempotency-Key": key},
    )
    assert other.status_code == 200
    assert other.headers.get("Idempotency-Replayed") is None
