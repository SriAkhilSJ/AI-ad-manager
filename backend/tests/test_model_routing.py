"""Model routing, cost estimation and budget enforcement.

All providers here are fakes. No test in this file can spend real credits, and
the fake asserts that it is reached or not reached as the budget dictates.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.llm.base import LLMRequest, LLMResponse, ProviderUnavailable
from app.llm.gateway import LLMGateway, ModelRouter
from app.llm.routing import (
    BudgetGuard,
    CancellationToken,
    RequestDeduplicator,
    estimate_cost,
    estimate_prompt_tokens,
    price_for_model,
    tier_for_task,
)


def settings(**overrides) -> Settings:
    base = {
        "environment": "test",
        "llm_reasoning_provider": "openrouter",
        "llm_reasoning_model": "deepseek-v4.1-flash",
        "llm_auxiliary_provider": "openrouter",
        "llm_auxiliary_model": "qwen-3.7-flash",
        "llm_default_provider": None,
        "llm_default_model": None,
        "llm_routing_json": {},
        "llm_task_routing_json": {},
        "llm_pricing_json": {},
    }
    base.update(overrides)
    return Settings(**base)


class FakeProvider:
    """Records calls and returns a deterministic structured response."""

    name = "openrouter"

    def __init__(self, *, configured: bool = True, cost: float = 0.001) -> None:
        self.configured = configured
        self.calls: list[LLMRequest] = []
        self.cost = cost

    def is_configured(self) -> bool:
        return self.configured

    async def structured(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        return LLMResponse(
            provider=self.name,
            model=request.metadata.get("model", "unknown"),
            data={"ok": True},
            prompt_tokens=100,
            completion_tokens=40,
            cost_usd=self.cost,
            latency_ms=5,
        )


def gateway(provider: FakeProvider, settings_obj: Settings) -> LLMGateway:
    return LLMGateway(session=None, settings=settings_obj, providers={provider.name: provider})


def request(max_tokens: int = 200) -> LLMRequest:
    return LLMRequest(
        system_prompt="You are a research agent.",
        user_prompt="Analyse competitors for an AI IDE.",
        schema_name="test_schema",
        json_schema={"type": "object"},
        max_tokens=max_tokens,
    )


# --------------------------------------------------------------------------- #
# task-class → tier mapping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "task_class,expected",
    [
        ("classification", "auxiliary"),
        ("extraction", "auxiliary"),
        ("summarisation", "auxiliary"),
        ("formatting", "auxiliary"),
        ("competitor_analysis", "reasoning"),
        ("positioning", "reasoning"),
        ("research_synthesis", "reasoning"),
        ("strategy", "reasoning"),
        ("something_else", "default"),
        ("", "default"),
    ],
)
def test_tier_for_task(task_class, expected):
    assert tier_for_task(task_class) == expected


# --------------------------------------------------------------------------- #
# routing precedence
# --------------------------------------------------------------------------- #
def test_cheap_task_uses_auxiliary_model():
    router = ModelRouter(settings())
    route = router.resolve("research", "classification")
    assert route.tier == "auxiliary"
    assert route.model == "qwen-3.7-flash"
    assert route.source == "auxiliary_default"


def test_reasoning_task_uses_primary_model():
    router = ModelRouter(settings())
    route = router.resolve("strategy", "competitor_analysis")
    assert route.tier == "reasoning"
    assert route.model == "deepseek-v4.1-flash"
    assert route.source == "reasoning_default"


def test_explicit_task_routing_wins_over_agent_routing():
    router = ModelRouter(
        settings(
            llm_routing_json={"research": {"provider": "openai", "model": "gpt-4o"}},
            llm_task_routing_json={
                "extraction": {"provider": "openrouter", "model": "qwen-3.7-flash"}
            },
        )
    )
    route = router.resolve("research", "extraction")
    assert route.source == "task_routing"
    assert route.model == "qwen-3.7-flash"


def test_agent_routing_preserved_from_phase1():
    """Phase 1 config must keep working unchanged when no task class is given."""
    router = ModelRouter(
        settings(llm_routing_json={"strategy": {"provider": "openrouter", "model": "custom-x"}})
    )
    route = router.resolve("strategy")
    assert route.source == "agent_routing"
    assert route.model == "custom-x"


def test_global_default_used_when_no_tier_configured():
    router = ModelRouter(
        settings(
            llm_reasoning_provider=None,
            llm_reasoning_model=None,
            llm_default_provider="openai",
            llm_default_model="gpt-4o-mini",
        )
    )
    route = router.resolve("content", "positioning")
    assert route.source == "global_default"
    assert route.model == "gpt-4o-mini"


def test_no_configuration_raises_typed_error():
    router = ModelRouter(
        settings(llm_reasoning_provider=None, llm_reasoning_model=None, llm_auxiliary_provider=None)
    )
    with pytest.raises(ProviderUnavailable):
        router.resolve("research", "classification")


# --------------------------------------------------------------------------- #
# pricing and estimation
# --------------------------------------------------------------------------- #
def test_unknown_model_is_priced_conservatively_and_flagged():
    price, verified = price_for_model("openrouter", "deepseek-v4.1-flash", settings())
    assert verified is False
    assert price.input_per_1k > 0
    assert price.output_per_1k > 0


def test_operator_pricing_override_is_trusted():
    cfg = settings(
        llm_pricing_json={
            "openrouter/deepseek-v4.1-flash": {"input_per_1k": 0.0001, "output_per_1k": 0.0002}
        }
    )
    price, verified = price_for_model("openrouter", "deepseek-v4.1-flash", cfg)
    assert verified is True
    assert price.input_per_1k == pytest.approx(0.0001)


def test_estimate_scales_with_tokens():
    small = estimate_cost(
        provider="openrouter", model="m", prompt_tokens=100, completion_tokens=50, settings=settings()
    )
    large = estimate_cost(
        provider="openrouter", model="m", prompt_tokens=1000, completion_tokens=500, settings=settings()
    )
    assert large.estimated_cost_usd > small.estimated_cost_usd
    assert small.estimated_cost_usd > 0


def test_estimate_prompt_tokens_grows_with_text():
    assert estimate_prompt_tokens("a" * 100) > estimate_prompt_tokens("a" * 10)
    assert estimate_prompt_tokens("") == 1


def test_router_estimate_reports_route_and_cost():
    router = ModelRouter(settings())
    estimate = router.estimate(
        agent_name="strategy",
        task_class="positioning",
        system_prompt="sys",
        user_prompt="user " * 100,
        max_output_tokens=500,
    )
    assert estimate["route"]["model"] == "deepseek-v4.1-flash"
    assert estimate["tier"] == "reasoning"
    assert estimate["estimated_cost_usd"] > 0
    assert estimate["price_verified"] is False
    assert "estimated" in estimate["currency_note"]


# --------------------------------------------------------------------------- #
# budget enforcement — the provider must not be called when out of budget
# --------------------------------------------------------------------------- #
async def test_budget_allows_call_within_envelope():
    provider = FakeProvider(cost=0.001)
    guard = BudgetGuard(max_cost_usd=1.0, max_tokens=10_000, max_tool_calls=5)
    gw = gateway(provider, settings())
    response = await gw.structured(
        agent_name="research", request=request(), task_class="classification", budget=guard
    )
    assert response.total_tokens == 140
    assert len(provider.calls) == 1
    assert guard.spent_usd == pytest.approx(0.001)
    assert guard.tokens_used == 140


async def test_budget_blocks_before_spending():
    """A call whose *estimate* exceeds the envelope never reaches the provider."""
    provider = FakeProvider(cost=0.5)
    # A budget far below even the cheapest realistic estimate.
    guard = BudgetGuard(max_cost_usd=0.0000001, max_tokens=10_000)
    gw = gateway(provider, settings())

    from app.llm.base import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        await gw.structured(
            agent_name="research", request=request(), task_class="research_synthesis", budget=guard
        )
    assert provider.calls == [], "provider was called despite an exhausted budget"
    assert "cost budget exhausted" in guard.stopped_reason


async def test_actual_cost_overspend_stops_further_calls():
    """Estimates are estimates: if actual cost overspends, the run stops.

    This is the honest failure mode. We cannot know a provider's true cost before
    calling it, so the guarantee is: never *start* a call that cannot fit, and
    hard-stop the moment observed spend exceeds the envelope.
    """
    provider = FakeProvider(cost=0.5)  # far above the estimate for this prompt
    guard = BudgetGuard(max_cost_usd=0.01, max_tokens=10_000)
    gw = gateway(provider, settings())

    await gw.structured(
        agent_name="research", request=request(), task_class="classification", budget=guard
    )
    assert len(provider.calls) == 1
    assert guard.spent_usd == pytest.approx(0.5)
    assert guard.remaining_usd == 0.0
    assert guard.exhausted is True

    from app.llm.base import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        await gw.structured(
            agent_name="research", request=request(), task_class="classification", budget=guard
        )
    assert len(provider.calls) == 1, "a second paid call was allowed after overspend"


async def test_budget_exhausts_after_spending_and_reports_remaining():
    provider = FakeProvider(cost=0.01)
    guard = BudgetGuard(max_cost_usd=0.015, max_tokens=10_000)
    gw = gateway(provider, settings())

    await gw.structured(
        agent_name="research", request=request(), task_class="classification", budget=guard
    )
    assert guard.remaining_usd == pytest.approx(0.005)
    assert guard.exhausted is False

    # Remaining budget ($0.005) is below the estimate of the next call, so it is
    # refused before reaching the provider.
    from app.llm.base import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        await gw.structured(
            agent_name="research",
            request=request(max_tokens=4000),
            task_class="classification",
            budget=guard,
        )
    assert len(provider.calls) == 1, "second call should have been refused"


async def test_token_budget_blocks_call():
    provider = FakeProvider()
    guard = BudgetGuard(max_cost_usd=10.0, max_tokens=10)
    gw = gateway(provider, settings())
    from app.llm.base import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        await gw.structured(
            agent_name="research",
            request=request(max_tokens=2000),
            task_class="research_synthesis",
            budget=guard,
        )
    assert provider.calls == []
    assert "token budget exhausted" in guard.stopped_reason


async def test_tool_call_budget_blocks_call():
    provider = FakeProvider(cost=0.0001)
    guard = BudgetGuard(max_cost_usd=10.0, max_tokens=100_000, max_tool_calls=1)
    guard.record(cost_usd=0.0001, tokens=10, tool_calls=1)
    gw = gateway(provider, settings())
    from app.llm.base import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        await gw.structured(
            agent_name="research",
            request=request(),
            task_class="research_synthesis",
            budget=guard,
        )
    assert provider.calls == []
    assert "tool-call budget" in guard.stopped_reason


async def test_unconfigured_provider_never_calls_out():
    provider = FakeProvider(configured=False)
    gw = gateway(provider, settings())
    with pytest.raises(ProviderUnavailable):
        await gw.structured(agent_name="research", request=request(), task_class="classification")
    assert provider.calls == []


# --------------------------------------------------------------------------- #
# cancellation + dedupe
# --------------------------------------------------------------------------- #
async def test_cancellation_prevents_the_call():
    provider = FakeProvider()
    token = CancellationToken()
    token.cancel("founder stopped the run")
    gw = gateway(provider, settings())

    from app.core.errors import CancelledProblem

    with pytest.raises(CancelledProblem):
        await gw.structured(
            agent_name="research", request=request(), task_class="classification", cancel=token
        )
    assert provider.calls == []


async def test_identical_prompt_is_not_paid_for_twice():
    provider = FakeProvider(cost=0.01)
    dedupe = RequestDeduplicator()
    gw = gateway(provider, settings())

    first = await gw.structured(
        agent_name="research",
        request=request(),
        task_class="classification",
        dedupe=dedupe,
        dedupe_key="research:classify",
    )
    second = await gw.structured(
        agent_name="research",
        request=request(),
        task_class="classification",
        dedupe=dedupe,
        dedupe_key="research:classify",
    )

    assert first is second
    assert len(provider.calls) == 1, "duplicate prompt hit the provider twice"
    assert dedupe.stats()["cache_hits"] == 1


async def test_different_prompts_are_not_deduplicated():
    provider = FakeProvider()
    dedupe = RequestDeduplicator()
    gw = gateway(provider, settings())
    await gw.structured(
        agent_name="research", request=request(), task_class="classification", dedupe=dedupe
    )
    other = request()
    other.user_prompt = "A completely different question about pricing pages."
    await gw.structured(
        agent_name="research", request=other, task_class="classification", dedupe=dedupe
    )
    assert len(provider.calls) == 2


# --------------------------------------------------------------------------- #
# auxiliary usage tracked separately
# --------------------------------------------------------------------------- #
async def test_auxiliary_and_reasoning_usage_are_recorded_on_separate_tiers(session):
    """Both tiers write a ledger row tagged with their tier."""
    from sqlalchemy import select

    from app.models.agent import ModelUsage
    from app.services.usage import UsageService

    usage = UsageService(session)
    await usage.record(
        agent_run_id=None,
        agent_name="research",
        provider="openrouter",
        model="qwen-3.7-flash",
        attempt=1,
        prompt_tokens=100,
        completion_tokens=20,
        cost_usd=0.0002,
        tier="auxiliary",
        task_class="classification",
    )
    await usage.record(
        agent_run_id=None,
        agent_name="strategy",
        provider="openrouter",
        model="deepseek-v4.1-flash",
        attempt=1,
        prompt_tokens=2000,
        completion_tokens=800,
        cost_usd=0.02,
        tier="reasoning",
        task_class="positioning",
    )
    await session.flush()

    rows = list((await session.execute(select(ModelUsage))).scalars().all())
    tiers = {r.tier: r for r in rows}
    assert set(tiers) == {"auxiliary", "reasoning"}
    assert tiers["auxiliary"].cost_usd < tiers["reasoning"].cost_usd
    assert tiers["auxiliary"].task_class == "classification"


def test_budget_guard_snapshot_is_serialisable():
    guard = BudgetGuard(max_cost_usd=0.06, max_tokens=5000, max_tool_calls=10, label="strategy")
    guard.record(cost_usd=0.01, tokens=1200, tool_calls=2)
    snapshot = guard.snapshot()
    assert snapshot["label"] == "strategy"
    assert snapshot["spent_usd"] == pytest.approx(0.01)
    assert snapshot["tokens_used"] == 1200
    assert snapshot["exhausted"] is False
    assert isinstance(snapshot["remaining_usd"], float)
