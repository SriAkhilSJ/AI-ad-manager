"""Approval state machine: legal transitions, illegal transitions, audit trail."""

from __future__ import annotations

import pytest

from app.models.content import ContentStatus
from app.services.approval import TRANSITIONS

DRAFT = ContentStatus.DRAFT.value
IN_REVIEW = ContentStatus.IN_REVIEW.value
APPROVED = ContentStatus.APPROVED.value
SCHEDULED = ContentStatus.SCHEDULED.value
PUBLISHING = ContentStatus.PUBLISHING.value
PUBLISHED = ContentStatus.PUBLISHED.value
REJECTED = ContentStatus.REJECTED.value
CHANGES = ContentStatus.CHANGES_REQUESTED.value
CANCELLED = ContentStatus.CANCELLED.value


async def _act(client, auth, item_id, action, role="founder", **extra):
    return await client.post(
        f"/api/v1/content/{item_id}/transition",
        json={"action": action, **extra},
        headers=auth(role),
    )


async def test_full_happy_path(client, auth, content_item):
    iid = content_item["id"]
    assert content_item["status"] == DRAFT

    submitted = await _act(client, auth, iid, "submit", "marketer")
    assert submitted.status_code == 200
    assert submitted.json()["status"] == IN_REVIEW

    approved = await _act(client, auth, iid, "approve", "founder", reason="on brand")
    assert approved.status_code == 200
    assert approved.json()["status"] == APPROVED
    assert approved.json()["has_valid_approval"] is True


async def test_draft_cannot_be_approved_directly(client, auth, content_item):
    resp = await _act(client, auth, content_item["id"], "approve")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "invalid_state_transition"
    assert "submit" in resp.json()["error"]["details"]["allowed_actions"]


async def test_request_changes_then_resubmit(client, auth, content_item):
    iid = content_item["id"]
    await _act(client, auth, iid, "submit", "marketer")
    changes = await _act(client, auth, iid, "request_changes", "founder", reason="tighten hook")
    assert changes.status_code == 200
    assert changes.json()["status"] == CHANGES

    resubmit = await _act(client, auth, iid, "submit", "marketer")
    assert resubmit.status_code == 200
    assert resubmit.json()["status"] == IN_REVIEW


async def test_reject_is_terminal_for_publishing(client, auth, content_item):
    iid = content_item["id"]
    await _act(client, auth, iid, "submit", "marketer")
    rejected = await _act(client, auth, iid, "reject", "founder", reason="off-brand claim")
    assert rejected.status_code == 200
    assert rejected.json()["status"] == REJECTED

    # Cannot approve a rejected item without going back through review.
    again = await _act(client, auth, iid, "approve", "founder")
    assert again.status_code == 409


async def test_cancel_from_draft(client, auth, content_item):
    resp = await _act(client, auth, content_item["id"], "cancel", "marketer")
    assert resp.status_code == 200
    assert resp.json()["status"] == CANCELLED


async def test_cancelled_is_terminal(client, auth, content_item):
    iid = content_item["id"]
    await _act(client, auth, iid, "cancel", "marketer")
    resp = await _act(client, auth, iid, "submit", "marketer")
    assert resp.status_code == 409


async def test_every_transition_is_audited(client, auth, content_item):
    iid = content_item["id"]
    await _act(client, auth, iid, "submit", "marketer")
    await _act(client, auth, iid, "approve", "founder", reason="ship it")
    ledger = await client.get(f"/api/v1/content/{iid}/approvals", headers=auth("founder"))
    assert ledger.status_code == 200
    actions = [row["action"] for row in ledger.json()]
    assert actions[0] == "approve"
    assert "submit" in actions
    approve_row = ledger.json()[0]
    assert approve_row["actor_role"] == "founder"
    assert approve_row["reason"] == "ship it"
    assert approve_row["content_version_id"] == content_item["current_version_id"]


async def test_transition_table_is_explicit_and_total():
    """Every status in the enum has a declared transition entry (possibly empty)."""
    for status in ContentStatus:
        assert status in TRANSITIONS, f"no transition entry for {status.value}"
    # PUBLISHED has no outgoing transitions: it is terminal.
    assert TRANSITIONS[ContentStatus.PUBLISHED] == {}
    # PUBLISHED is only reachable from PUBLISHING.
    reachable_from = {
        src.value
        for src, actions in TRANSITIONS.items()
        for target in actions.values()
        if target is ContentStatus.PUBLISHED
    }
    assert reachable_from == {PUBLISHING}


async def test_no_api_endpoint_sets_published_directly(client, auth, content_item):
    """Guard against an endpoint that could bypass the machine."""
    routes = [r for r in client._transport.app.routes if hasattr(r, "path")]  # type: ignore[attr-defined]
    for route in routes:
        path = getattr(route, "path", "")
        if "publish" in path and path.endswith("/publish"):
            assert getattr(route, "methods", set()) == {"POST"}
    # The transition endpoint rejects "publish" as an action (not in the enum).
    resp = await _act(client, auth, content_item["id"], "publish")
    assert resp.status_code == 422
