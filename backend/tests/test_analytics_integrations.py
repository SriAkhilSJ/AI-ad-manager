"""Analytics honesty, storage/task integrations, and system status."""

from __future__ import annotations

import uuid

import pytest

from app.services.analytics import AnalyticsService


async def test_analytics_reports_no_data_rather_than_zeros(client, auth, brand):
    resp = await client.get("/api/v1/analytics/overview", headers=auth("viewer"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_data"] is False
    assert body["data_source"] == "none"
    assert body["series"] == []
    assert body["by_platform"] == []
    assert body["engagement_rate"] is None
    assert "nothing is estimated" in body["message"].lower()


async def test_analytics_without_brand_profile(client, auth):
    resp = await client.get("/api/v1/analytics/overview", headers=auth("viewer"))
    assert resp.status_code == 200
    assert resp.json()["has_data"] is False


async def test_analytics_aggregates_recorded_snapshots(session, brand_row):
    service = AnalyticsService(session)
    await service.record_snapshot(
        brand_profile_id=brand_row.id,
        platform="linkedin",
        impressions=1000,
        engagements=50,
        clicks=20,
        source="manual-test",
    )
    await service.record_snapshot(
        brand_profile_id=brand_row.id,
        platform="x",
        impressions=500,
        engagements=10,
        clicks=5,
        source="manual-test",
    )
    await session.commit()

    overview = await service.overview(brand_profile_id=brand_row.id, days=30)
    assert overview["has_data"] is True
    assert overview["totals"]["impressions"] == 1500
    assert overview["totals"]["engagements"] == 60
    assert overview["engagement_rate"] == pytest.approx(0.04)
    assert len(overview["by_platform"]) == 2
    assert overview["data_source"] == ["manual-test"]


async def test_growth_analyst_refuses_to_invent_baselines(session, brand_row):
    from app.agents.base import AgentContext
    from app.agents.registry import get_registry

    ctx = AgentContext(session=session, brand=brand_row)
    result = await get_registry().get("growth_analyst").run(ctx, {"window_days": 30})
    assert result.data_source == "none"
    assert result.measured["snapshots"] == 0
    assert result.measurement_plan
    assert any("instrument" in r.action.lower() for r in result.recommendations)


async def test_storage_local_backend_roundtrip(tmp_path):
    from app.storage.base import LocalFilesystemStorage

    storage = LocalFilesystemStorage(tmp_path)
    key = await storage.put("creatives/a/file.txt", b"hello", "text/plain")
    assert await storage.get(key) == b"hello"
    await storage.delete(key)
    with pytest.raises(Exception):
        await storage.get(key)


async def test_storage_unavailable_reports_status():
    from app.core.config import Settings
    from app.storage.base import storage_status

    settings = Settings(environment="test", storage_backend="disabled")
    status = storage_status(settings)
    assert status["status"] == "unavailable"
    assert status["backend"] == "none"


async def test_dispatcher_inline_executes_real_work():
    from app.tasks.dispatcher import InlineDispatcher

    dispatcher = InlineDispatcher()
    ran = {"value": False}

    async def work() -> str:
        ran["value"] = True
        return "done"

    handle = await dispatcher.submit("test-job", work)
    assert handle.accepted is True
    await dispatcher.drain()
    assert ran["value"] is True
    assert dispatcher.status(handle.task_id)["state"] == "succeeded"


async def test_disabled_dispatcher_rejects_submission():
    from app.core.errors import IntegrationUnavailableError
    from app.tasks.dispatcher import DisabledDispatcher

    dispatcher = DisabledDispatcher()

    async def work() -> None:
        return None

    with pytest.raises(IntegrationUnavailableError):
        await dispatcher.submit("job", work)


async def test_system_status_shape(client):
    resp = await client.get("/api/v1/system/status")
    body = resp.json()
    assert set(body) >= {
        "app", "database", "llm", "tasks", "storage", "publishing", "auth", "director"
    }
    assert body["database"]["status"] == "available"
    assert body["database"]["vector_search"] in {"pgvector", "python-cosine"}
    assert body["app"]["bootstrap_required"] in {True, False}


async def test_scheduled_and_published_listings_start_empty(client, auth):
    scheduled = await client.get("/api/v1/social/scheduled", headers=auth("viewer"))
    published = await client.get("/api/v1/social/published", headers=auth("viewer"))
    assert scheduled.status_code == 200
    assert published.status_code == 200
    assert scheduled.json() == []
    assert published.json() == []
