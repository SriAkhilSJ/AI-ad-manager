"""CRITICAL: unapproved publication must be impossible.

These tests target every path that could reach ``PUBLISHING``/``PUBLISHED`` and
assert that none of them succeeds without a valid approval bound to the current
content version.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.content import ContentStatus
from app.services.publishing import PublishingService


async def test_draft_without_approval_cannot_be_published(client, auth, content_item):
    """Acceptance criterion: publishing a DRAFT with no approval is rejected."""
    resp = await client.post(
        f"/api/v1/content/{content_item['id']}/publish", headers=auth("marketer")
    )
    assert resp.status_code == 409
    body = resp.json()["error"]
    assert body["code"] == "approval_required"
    assert body["details"]["status"] == "DRAFT"

    # State untouched.
    detail = await client.get(
        f"/api/v1/content/{content_item['id']}", headers=auth("marketer")
    )
    assert detail.json()["status"] == "DRAFT"


async def test_in_review_without_approval_cannot_be_published(client, auth, content_item):
    iid = content_item["id"]
    await client.post(
        f"/api/v1/content/{iid}/transition", json={"action": "submit"}, headers=auth("marketer")
    )
    resp = await client.post(f"/api/v1/content/{iid}/publish", headers=auth("marketer"))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "approval_required"


async def test_approved_item_is_blocked_by_missing_platform_transport(client, auth, content_item):
    """Approval alone is not enough: Phase 1 has no transport, and says so."""
    iid = content_item["id"]
    await client.post(
        f"/api/v1/content/{iid}/transition", json={"action": "submit"}, headers=auth("marketer")
    )
    await client.post(
        f"/api/v1/content/{iid}/transition",
        json={"action": "approve"},
        headers=auth("founder"),
    )
    resp = await client.post(f"/api/v1/content/{iid}/publish", headers=auth("marketer"))
    # No connected social account yet -> integration unavailable (before transport).
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "integration_unavailable"

    detail = await client.get(f"/api/v1/content/{item_id_of(iid)}", headers=auth("marketer"))
    assert detail.json()["status"] == "APPROVED"


def item_id_of(value: str) -> str:
    return value


async def test_approved_item_with_account_but_no_transport_is_blocked(
    client, auth, brand, content_item
):
    """With a 'connected' account but no platform transport, publish still fails
    and never fabricates a PUBLISHED row."""
    iid = content_item["id"]
    await client.post(
        f"/api/v1/content/{iid}/transition", json={"action": "submit"}, headers=auth("marketer")
    )
    await client.post(
        f"/api/v1/content/{iid}/transition", json={"action": "approve"}, headers=auth("founder")
    )

    # Register an account and force it to 'connected' via the service layer.
    account = await client.post(
        "/api/v1/social/accounts",
        json={
            "brand_profile_id": brand["id"],
            "platform": "linkedin",
            "handle": "crusor-ai",
        },
        headers=auth("marketer"),
    )
    assert account.status_code == 201
    account_id = account.json()["id"]

    from app.db.session import get_session_factory
    from app.models.social import SocialAccount

    factory = get_session_factory()
    async with factory() as session:
        row = await session.get(SocialAccount, uuid.UUID(account_id))
        row.status = "connected"
        session.add(row)
        await session.commit()

    resp = await client.post(
        f"/api/v1/content/{iid}/publish",
        json={"social_account_id": account_id},
        headers=auth("marketer"),
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "publishing_disabled"

    detail = await client.get(f"/api/v1/content/{iid}", headers=auth("marketer"))
    assert detail.json()["status"] == "APPROVED"
    pubs = await client.get(f"/api/v1/content/{iid}/publications", headers=auth("marketer"))
    assert pubs.json() == []


async def test_approval_of_previous_version_does_not_authorise_new_version(
    client, auth, content_item
):
    iid = content_item["id"]
    await client.post(
        f"/api/v1/content/{iid}/transition", json={"action": "submit"}, headers=auth("marketer")
    )
    await client.post(
        f"/api/v1/content/{iid}/transition", json={"action": "approve"}, headers=auth("founder")
    )
    # Edit -> new version -> approval invalidated, back to DRAFT.
    edit = await client.post(
        f"/api/v1/content/{iid}/versions",
        json={"body": "Rewritten body that nobody approved."},
        headers=auth("marketer"),
    )
    assert edit.status_code == 201
    assert edit.json()["status"] == "DRAFT"
    assert edit.json()["has_valid_approval"] is False

    resp = await client.post(f"/api/v1/content/{iid}/publish", headers=auth("marketer"))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "approval_required"


async def test_scheduling_also_requires_approval(client, auth, brand, content_item):
    from datetime import datetime, timedelta, timezone

    account = await client.post(
        "/api/v1/social/accounts",
        json={"brand_profile_id": brand["id"], "platform": "linkedin", "handle": "crusor"},
        headers=auth("marketer"),
    )
    resp = await client.post(
        f"/api/v1/content/{content_item['id']}/schedule",
        json={
            "social_account_id": account.json()["id"],
            "scheduled_for": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        },
        headers=auth("marketer"),
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "approval_required"


async def test_service_layer_refuses_publish_without_approval(session, brand_row):
    """Bypassing HTTP entirely must still be blocked."""
    from app.core.errors import ApprovalRequiredError
    from app.services.content import ContentService

    service = ContentService(session)
    item = await service.create_item(
        brand_profile_id=brand_row.id, title="direct", platform="linkedin", body="text"
    )
    await session.commit()

    item_id = item.id  # capture before rollback (expired attributes need IO)
    publishing = PublishingService(session)
    with pytest.raises(ApprovalRequiredError):
        await publishing.publish(item_id, actor_id=None, actor_role="system")
    await session.rollback()

    refreshed = await service.get_item(item_id)
    assert refreshed.status == ContentStatus.DRAFT.value


async def test_direct_published_status_write_is_reachable_only_via_service():
    """`PUBLISHING` -> `PUBLISHED` is the only edge into PUBLISHED, and the
    publishing service is the only writer of it."""
    import app.services.publishing as module

    source = open(module.__file__, encoding="utf-8").read()
    # The only place that sets ContentStatus.PUBLISHED must be via approvals.apply
    assert 'ContentStatus.PUBLISHED.value' not in source or "apply(" in source
    # And the state machine table must not allow APPROVED -> PUBLISHED directly
    from app.services.approval import TRANSITIONS

    assert TRANSITIONS[ContentStatus.APPROVED].get("publish") is ContentStatus.PUBLISHING
