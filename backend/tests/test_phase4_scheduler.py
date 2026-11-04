"""Phase 4.1 — scheduled publishing and analytics refresh actually run.

Before this module a scheduled post was a row nothing acted on. These tests prove
the scheduler publishes a due post **through the normal gates**, refuses when the
approval no longer matches, and never double-publishes.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.core.config import Settings
from app.integrations.vault import TokenRecord, build_vault
from app.models.social import PublishedPost, ScheduledPost, SocialAccount, SocialAccountStatus
from app.publishing.adapters import AdapterRegistry
from app.services.scheduler import SchedulerService, refresh_analytics
import app.services.publishing as publishing_module

KEY = "scheduler-test-vault-key-0123456789"


def live_settings() -> Settings:
    return Settings(
        environment="test",
        token_vault_backend="db",
        token_vault_key=KEY,
        integration_max_retries=1,
        integration_timeout_seconds=5,
        meta_app_id="a",
        meta_app_secret="b",
        linkedin_client_id="c",
        linkedin_client_secret="d",
        x_client_id="e",
        x_client_secret="f",
        google_client_id="g",
        google_client_secret="h",
        reddit_client_id="i",
        reddit_client_secret="j",
    )


def x_handler(request: httpx.Request) -> httpx.Response:
    if request.method == "POST" and request.url.path.endswith("/tweets"):
        return httpx.Response(201, json={"data": {"id": "777"}})
    if request.method == "GET" and request.url.path.endswith("/tweets/777"):
        return httpx.Response(200, json={"data": {"id": "777", "text": "hi"}})
    return httpx.Response(404)


async def _approved_x_item(client, auth, brand) -> str:
    created = await client.post(
        "/api/v1/content",
        json={
            "brand_profile_id": brand["id"],
            "title": "Scheduled post",
            "platform": "x",
            "body": "Ship faster with Crusor.",
            "headline": "Crusor",
            "hashtags": ["AIdev"],
            "call_to_action": "Try it",
        },
        headers=auth("marketer"),
    )
    assert created.status_code == 201, created.text
    item_id = created.json()["id"]
    await client.post(
        f"/api/v1/content/{item_id}/transition", json={"action": "submit"}, headers=auth("marketer")
    )
    approved = await client.post(
        f"/api/v1/content/{item_id}/transition", json={"action": "approve"}, headers=auth("founder")
    )
    assert approved.status_code in (200, 409), approved.text
    return item_id


async def _connected_account(session, brand_id, platform="x") -> SocialAccount:
    vault = build_vault(session, live_settings())
    ref = await vault.store(
        brand_profile_id=brand_id,
        platform=platform,
        account_key="acct-1",
        record=TokenRecord(access_token="token", refresh_token="refresh"),
    )
    account = SocialAccount(
        brand_profile_id=brand_id,
        platform=platform,
        handle="crusor",
        external_account_id="42",
        status=SocialAccountStatus.CONNECTED.value,
        token_ref=ref,
    )
    session.add(account)
    await session.flush()
    # The API runs in its own transaction; commit so it can see the credential.
    await session.commit()
    return account


def _install_registry(monkeypatch, handler) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://test")
    registry = AdapterRegistry(live_settings(), http_client=client)
    monkeypatch.setattr(publishing_module, "get_adapter_registry", lambda *a, **k: registry)


async def test_due_post_is_published_through_the_normal_gates(
    client, auth, brand, session, monkeypatch
):
    item_id = await _approved_x_item(client, auth, brand)
    account = await _connected_account(session, uuid.UUID(brand["id"]))
    _install_registry(monkeypatch, x_handler)

    scheduled = await client.post(
        f"/api/v1/content/{item_id}/schedule",
        json={
            "social_account_id": str(account.id),
            "scheduled_for": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        },
        headers=auth("marketer"),
    )
    assert scheduled.status_code == 201, scheduled.text

    result = await SchedulerService(session, live_settings()).run_due()
    await session.commit()

    assert result["due"] == 1, result
    assert result["published"] == 1, json.dumps(result, indent=2)
    assert result["failed"] == 0

    scheduled_rows = (await session.execute(ScheduledPost.__table__.select())).all()
    assert scheduled_rows and scheduled_rows[0].status == "published"

    rows = (await session.execute(PublishedPost.__table__.select())).all()
    assert len(rows) == 1 and rows[0].external_post_id == "777"


async def test_a_second_run_does_not_publish_twice(client, auth, brand, session, monkeypatch):
    item_id = await _approved_x_item(client, auth, brand)
    account = await _connected_account(session, uuid.UUID(brand["id"]))
    _install_registry(monkeypatch, x_handler)
    await client.post(
        f"/api/v1/content/{item_id}/schedule",
        json={
            "social_account_id": str(account.id),
            "scheduled_for": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        },
        headers=auth("marketer"),
    )

    first = await SchedulerService(session, live_settings()).run_due()
    second = await SchedulerService(session, live_settings()).run_due()
    await session.commit()

    assert first["published"] == 1
    assert second["due"] == 0 and second["published"] == 0
    assert len((await session.execute(PublishedPost.__table__.select())).all()) == 1


async def test_editing_content_after_scheduling_blocks_the_publish(
    client, auth, brand, session, monkeypatch
):
    """The approval is bound to a version; a later edit must not ship."""
    item_id = await _approved_x_item(client, auth, brand)
    account = await _connected_account(session, uuid.UUID(brand["id"]))
    _install_registry(monkeypatch, x_handler)
    await client.post(
        f"/api/v1/content/{item_id}/schedule",
        json={
            "social_account_id": str(account.id),
            "scheduled_for": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        },
        headers=auth("marketer"),
    )

    edited = await client.post(
        f"/api/v1/content/{item_id}/versions",
        json={"body": "A completely different claim."},
        headers=auth("marketer"),
    )
    assert edited.status_code == 201, edited.text

    result = await SchedulerService(session, live_settings()).run_due()
    await session.commit()

    assert result["published"] == 0
    assert result["failed"] == 1
    assert "approval" in result["results"][0]["detail"].lower()
    assert (await session.execute(PublishedPost.__table__.select())).all() == []


async def test_no_transport_means_no_publish(client, auth, brand, session):
    item_id = await _approved_x_item(client, auth, brand)
    account = await _connected_account(session, uuid.UUID(brand["id"]))
    await client.post(
        f"/api/v1/content/{item_id}/schedule",
        json={
            "social_account_id": str(account.id),
            "scheduled_for": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        },
        headers=auth("marketer"),
    )

    # Default application settings: no vault, no transport.
    result = await SchedulerService(session, Settings(environment="test")).run_due()
    await session.commit()

    assert result["published"] == 0 and result["failed"] == 1
    assert (await session.execute(PublishedPost.__table__.select())).all() == []


async def test_future_posts_are_left_alone(client, auth, brand, session, monkeypatch):
    item_id = await _approved_x_item(client, auth, brand)
    account = await _connected_account(session, uuid.UUID(brand["id"]))
    _install_registry(monkeypatch, x_handler)
    await client.post(
        f"/api/v1/content/{item_id}/schedule",
        json={
            "social_account_id": str(account.id),
            "scheduled_for": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        },
        headers=auth("marketer"),
    )

    result = await SchedulerService(session, live_settings()).run_due()
    assert result["due"] == 0 and result["published"] == 0
    row = (await session.execute(ScheduledPost.__table__.select())).first()
    assert row.status == "pending" and row.attempts == 0


async def test_run_due_endpoint_is_founder_only(client, auth):
    denied = await client.post("/api/v1/social/scheduled/run", headers=auth("marketer"))
    assert denied.status_code == 403
    allowed = await client.post("/api/v1/social/scheduled/run", headers=auth("founder"))
    assert allowed.status_code == 200
    assert allowed.json()["due"] == 0


async def test_refresh_analytics_reports_rather_than_invents(session, brand_row):
    result = await refresh_analytics(session, Settings(environment="test"))
    assert result["brands"] >= 1
    assert result["ingested"] == 0
    assert result["unavailable"], "an unconfigured vault must be reported, not hidden"
    assert "vault" in result["unavailable"][0]["reason"].lower()
