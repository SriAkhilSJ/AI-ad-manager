"""Model usage and cost accounting — no fabricated numbers."""

from __future__ import annotations

import uuid

import pytest

from app.llm.base import LLMResponse, ProviderUnavailable, price_for
from app.llm.gateway import LLMGateway, ModelRouter
from app.services.usage import UsageService


async def test_no_usage_when_no_provider_is_configured(client, auth):
    resp = await client.get("/api/v1/usage/summary", headers=auth("viewer"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_data"] is False
    assert body["totals"]["cost_usd"] == 0
    assert body["totals"]["calls"] == 0
    assert "deterministic" in body["message"]


async def test_agents_record_zero_usage_offline(session, brand_row):
    from app.agents.base import AgentContext
    from app.agents.registry import get_registry

    ctx = AgentContext(session=session, brand=brand_row)
    await get_registry().get("research").run(ctx, {})
    await session.commit()

    summary = await UsageService(session).summary()
    assert summary["has_data"] is False


async def test_usage_recording_and_aggregation(session):
    service = UsageService(session)
    await service.record(
        agent_run_id=None,
        agent_name="content",
        provider="openai",
        model="gpt-4o-mini",
        attempt=1,
        prompt_tokens=1000,
        completion_tokens=500,
        cost_usd=0.00045,
    )
    await service.record(
        agent_run_id=None,
        agent_name="strategy",
        provider="anthropic",
        model="claude-sonnet-4",
        attempt=1,
        prompt_tokens=2000,
        completion_tokens=1000,
        cost_usd=0.021,
    )
    await session.commit()

    summary = await service.summary()
    assert summary["has_data"] is True
    assert summary["totals"]["calls"] == 2
    assert summary["totals"]["prompt_tokens"] == 3000
    assert summary["totals"]["completion_tokens"] == 1500
    assert summary["totals"]["total_tokens"] == 4500
    assert summary["totals"]["cost_usd"] == pytest.approx(0.02145, rel=1e-6)
    assert {row["provider"] for row in summary["by_provider"]} == {"openai", "anthropic"}
    assert {row["agent"] for row in summary["by_agent"]} == {"content", "strategy"}


async def test_usage_unique_per_run_and_attempt(session):
    """A retried call cannot double-count."""
    from sqlalchemy.exc import IntegrityError
    from app.models.agent import AgentRun

    run = AgentRun(agent_name="content", status="running")
    session.add(run)
    await session.flush()

    service = UsageService(session)
    await service.record(
        agent_run_id=run.id, agent_name="content", provider="openai",
        model="gpt-4o-mini", attempt=1, prompt_tokens=10, completion_tokens=5,
        cost_usd=0.001,
    )
    await session.commit()

    with pytest.raises(IntegrityError):
        await service.record(
            agent_run_id=run.id, agent_name="content", provider="openai",
            model="gpt-4o-mini", attempt=1, prompt_tokens=999, completion_tokens=999,
            cost_usd=9.99,
        )
        await session.commit()
    await session.rollback()


async def test_gateway_records_usage_once_per_attempt(session):
    class FakeProvider:
        name = "openai"

        def is_configured(self) -> bool:
            return True

        async def structured(self, request):  # type: ignore[no-untyped-def]
            return LLMResponse(
                provider="openai",
                model="gpt-4o-mini",
                data={"ok": True},
                prompt_tokens=100,
                completion_tokens=50,
                cost_usd=0.0001,
                latency_ms=12,
            )

    from app.models.agent import AgentRun

    run = AgentRun(agent_name="content", status="running")
    session.add(run)
    await session.flush()

    gateway = LLMGateway(session=session, providers={"openai": FakeProvider()})
    gateway.router = ModelRouter(
        type(gateway.settings)(
            llm_default_provider="openai",
            llm_default_model="gpt-4o-mini",
            monthly_budget_usd=100.0,
        )
    )
    from app.llm.base import LLMRequest

    request = LLMRequest(
        system_prompt="s", user_prompt="u", schema_name="x", json_schema={}, max_tokens=100
    )
    for _ in range(2):  # same attempt => second write is a no-op
        await gateway.structured(
            agent_name="content", request=request, agent_run_id=run.id, attempt=1
        )
    await session.commit()

    summary = await UsageService(session).summary()
    assert summary["totals"]["calls"] == 1
    assert summary["totals"]["prompt_tokens"] == 100


async def test_provider_unavailable_when_not_configured(session):
    """No silent substitution: an unconfigured gateway raises a typed error.

    Phase 3A change: the *content* agent is served by the free, self-identifying
    mock provider by default (``CONTENT_PROVIDER=mock``), so it is no longer a
    valid subject for this assertion. The guarantee is checked with an agent that
    has no such default, and again for content with the mock switched off.
    """
    from app.llm.base import LLMRequest

    request = LLMRequest(
        system_prompt="s", user_prompt="u", schema_name="x", json_schema={}, max_tokens=10
    )
    gateway = LLMGateway(session=session)
    with pytest.raises(ProviderUnavailable):
        await gateway.structured(agent_name="research", request=request)

    from app.core.config import Settings

    no_mock = LLMGateway(session=session, settings=Settings(environment="test", content_provider="auto"))
    with pytest.raises(ProviderUnavailable):
        await no_mock.structured(agent_name="content", request=request)


async def test_monthly_budget_is_enforced(session):
    class FakeProvider:
        name = "openai"

        def is_configured(self) -> bool:
            return True

        async def structured(self, request):  # type: ignore[no-untyped-def]
            raise AssertionError("must not be called when budget is exhausted")

    from app.core.config import Settings

    settings = Settings(
        llm_default_provider="openai",
        llm_monthly_budget_usd=0.0,
        environment="test",
    )
    gateway = LLMGateway(session=session, settings=settings, providers={"openai": FakeProvider()})
    gateway.router = ModelRouter(settings)
    await UsageService(session).record(
        agent_run_id=None, agent_name="x", provider="openai", model="gpt-4o-mini",
        attempt=1, prompt_tokens=1, completion_tokens=1, cost_usd=0.5,
    )
    await session.commit()

    from app.llm.base import BudgetExceeded, LLMRequest

    with pytest.raises(BudgetExceeded):
        await gateway.structured(
            agent_name="content",
            request=LLMRequest(
                system_prompt="s", user_prompt="u", schema_name="x", json_schema={}, max_tokens=5
            ),
        )


def test_price_table_is_sane():
    price = price_for("openai", "gpt-4o-mini")
    assert price.cost(1000, 0) == pytest.approx(0.00015)
    assert price.cost(0, 1000) == pytest.approx(0.0006)
    # unknown model falls back to the provider default, never to zero
    assert price_for("openai", "some-future-model").input_per_1k > 0


async def test_usage_endpoint_reports_recorded_rows(client, auth, users, session):
    await UsageService(session).record(
        agent_run_id=None, agent_name="content", provider="openai", model="gpt-4o-mini",
        attempt=1, prompt_tokens=10, completion_tokens=20, cost_usd=0.002,
    )
    await session.commit()
    resp = await client.get("/api/v1/usage/summary", headers=auth("viewer"))
    assert resp.status_code == 200
    assert resp.json()["has_data"] is True
    assert resp.json()["totals"]["cost_usd"] == pytest.approx(0.002)
