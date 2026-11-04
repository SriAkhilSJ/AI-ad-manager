"""Phase 2.6 — fail-closed budget safety.

Every provider in this file is a fake. No test here can spend real money, reach a
real endpoint, or perform live inference. Each test names the guarantee it pins:

    zero budget            blocks every paid request
    missing budget         blocks, rather than meaning "unlimited"
    invalid configuration  negative / NaN / infinity / non-numeric are rejected
    positive budget        permits eligible requests, within the envelope
    untrusted pricing      blocks paid execution until prices are verified
    concurrency            two reservations cannot both claim the same headroom
    failure                a request that never ran releases its reservation
    ambiguous timeout      the reservation is *retained*, because it may have billed
    shared scope           primary and auxiliary calls draw one ledger
"""

from __future__ import annotations

import asyncio
import math

import pytest

from app.core.config import Settings
from app.llm.base import BudgetExceeded, LLMRequest, LLMResponse, ProviderUnavailable
from app.llm.gateway import LLMGateway
from app.llm.routing import (
    BudgetConfigurationError,
    BudgetGuard,
    CostEstimate,
    estimate_cost,
    estimate_prompt_tokens,
)

VERIFIED_PRICES = {
    "deepseek-v4.1-flash": {"input_per_1k": 0.001, "output_per_1k": 0.003},
    "qwen-3.7-flash": {"input_per_1k": 0.001, "output_per_1k": 0.003},
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
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
        "llm_pricing_json": dict(VERIFIED_PRICES),
    }
    base.update(overrides)
    return Settings(**base)


SYSTEM_PROMPT = "You are a strategy agent."
USER_PROMPT = "Draft positioning for an AI IDE."


def call_cost(completion_tokens: int = 200) -> float:
    """What one ``request()`` is estimated to cost, from the verified price table.

    The fakes below charge exactly this, so a concurrency test measures the
    *interleaving* rather than the size of the estimate error (which is covered
    deliberately and separately by ``test_estimate_error_is_bounded_to_one_request``).
    """
    return estimate_cost(
        provider="openrouter",
        model="deepseek-v4.1-flash",
        prompt_tokens=estimate_prompt_tokens(SYSTEM_PROMPT, USER_PROMPT),
        completion_tokens=completion_tokens,
        tier="reasoning",
        task_class="research_synthesis",
        settings=settings(),
    ).estimated_cost_usd


def estimate(cost: float = 0.01, *, verified: bool = True, tokens: int = 100) -> CostEstimate:
    return CostEstimate(
        provider="openrouter",
        model="deepseek-v4.1-flash",
        tier="reasoning",
        task_class="research_synthesis",
        prompt_tokens=tokens,
        completion_tokens=tokens,
        input_per_1k=0.001,
        output_per_1k=0.003,
        estimated_cost_usd=cost,
        price_verified=verified,
    )


class FakeProvider:
    """A provider that records calls and can be told how to fail."""

    name = "openrouter"

    def __init__(self, *, cost: float = 0.001, fail_with: Exception | None = None,
                 delay: float = 0.0) -> None:
        self.calls: list[LLMRequest] = []
        self.cost = cost
        self.fail_with = fail_with
        self.delay = delay
        self.in_flight = 0
        self.peak_in_flight = 0

    def is_configured(self) -> bool:
        return True

    async def structured(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail_with is not None:
                raise self.fail_with
            return LLMResponse(
                provider=self.name,
                model=request.metadata.get("model", "unknown"),
                data={"ok": True},
                prompt_tokens=100,
                completion_tokens=40,
                cost_usd=self.cost,
                latency_ms=1,
            )
        finally:
            self.in_flight -= 1


def gateway(provider: FakeProvider, settings_obj: Settings | None = None) -> LLMGateway:
    return LLMGateway(
        session=None,
        settings=settings_obj or settings(),
        providers={provider.name: provider},
    )


def request(max_tokens: int = 200) -> LLMRequest:
    return LLMRequest(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=USER_PROMPT,
        schema_name="test_schema",
        json_schema={"type": "object"},
        max_tokens=max_tokens,
    )


# --------------------------------------------------------------------------- #
# 1. zero budget blocks all paid requests
# --------------------------------------------------------------------------- #
def test_zero_budget_blocks_check_and_reserve():
    guard = BudgetGuard(max_cost_usd=0, label="zero")
    with pytest.raises(BudgetExceeded) as check_exc:
        guard.check(estimate())
    assert "cost budget is zero" in str(check_exc.value)

    with pytest.raises(BudgetExceeded) as reserve_exc:
        guard.reserve(estimate())
    assert "cost budget is zero" in str(reserve_exc.value)
    assert guard.remaining_usd == 0.0
    assert guard.open_reservations() == []


async def test_zero_budget_never_reaches_the_provider():
    provider = FakeProvider()
    guard = BudgetGuard(max_cost_usd=0.0, label="zero")
    with pytest.raises(BudgetExceeded):
        await gateway(provider).structured(
            agent_name="strategy", request=request(), task_class="strategic_decision",
            budget=guard,
        )
    assert provider.calls == [], "a zero budget must not spend, not even one call"


# --------------------------------------------------------------------------- #
# 2. missing / invalid configuration
# --------------------------------------------------------------------------- #
def test_missing_budget_blocks_rather_than_meaning_unlimited():
    """``None`` used to coerce to 0.0 and then mean "uncapped". It now blocks."""
    guard = BudgetGuard()  # no max_cost_usd at all
    assert guard.max_cost_usd == 0.0
    assert guard.unlimited is False
    assert guard.remaining_usd == 0.0
    with pytest.raises(BudgetExceeded):
        guard.check(estimate(cost=0.001))


@pytest.mark.parametrize(
    "bad_value",
    [-1, -0.0001, float("nan"), float("inf"), float("-inf"), True, [0.1], "lots"],
)
def test_invalid_budget_values_are_rejected_at_construction(bad_value):
    with pytest.raises(BudgetConfigurationError):
        BudgetGuard(max_cost_usd=bad_value)


def test_negative_budget_error_is_a_value_error():
    """Callers written before Phase 2.6 catch ValueError; keep that working."""
    with pytest.raises(ValueError):
        BudgetGuard(max_cost_usd=-3)


def test_unlimited_requires_an_explicit_opt_in():
    with pytest.raises(BudgetConfigurationError):
        BudgetGuard(max_cost_usd=math.inf)

    guard = BudgetGuard(max_cost_usd=math.inf, allow_unlimited=True)
    assert guard.unlimited is True
    assert guard.remaining_usd == math.inf
    assert guard.snapshot()["unlimited"] is True
    # Even an explicitly unlimited guard still never blocks on price — but it
    # must be obvious in the snapshot, never implied.
    assert guard.snapshot()["max_cost_usd"] is None


def test_invalid_token_and_tool_call_limits_are_rejected():
    for field in ("max_tokens", "max_tool_calls"):
        with pytest.raises(BudgetConfigurationError):
            BudgetGuard(max_cost_usd=1.0, **{field: -1})


# --------------------------------------------------------------------------- #
# 3. positive budget permits eligible requests, within the envelope
# --------------------------------------------------------------------------- #
async def test_positive_budget_permits_eligible_request():
    provider = FakeProvider(cost=0.002)
    guard = BudgetGuard(max_cost_usd=0.10, max_tokens=10_000, label="ok")
    response = await gateway(provider).structured(
        agent_name="research", request=request(), task_class="research_synthesis",
        budget=guard,
    )
    assert response.total_tokens == 140
    assert len(provider.calls) == 1
    assert guard.spent_usd == pytest.approx(0.002)
    assert guard.tokens_used == 140
    assert guard.open_reservations() == [], "a settled reservation must be released"


async def test_request_above_remaining_budget_is_refused_before_the_call():
    provider = FakeProvider()
    guard = BudgetGuard(max_cost_usd=1e-9, label="tiny")
    with pytest.raises(BudgetExceeded) as exc:
        await gateway(provider).structured(
            agent_name="research", request=request(), task_class="research_synthesis",
            budget=guard,
        )
    assert "cost budget exhausted" in str(exc.value)
    assert provider.calls == []


# --------------------------------------------------------------------------- #
# 4. pricing safety
# --------------------------------------------------------------------------- #
async def test_unverified_pricing_blocks_paid_execution():
    """A reservation against an invented price is not a budget guarantee."""
    provider = FakeProvider()
    guard = BudgetGuard(max_cost_usd=1.0, label="strict")
    unpriced = settings(llm_pricing_json={})
    with pytest.raises(BudgetExceeded) as exc:
        await gateway(provider, unpriced).structured(
            agent_name="strategy", request=request(), task_class="strategic_decision",
            budget=guard,
        )
    assert "untrusted pricing" in str(exc.value)
    assert provider.calls == []


async def test_verified_pricing_allows_the_same_request():
    provider = FakeProvider()
    guard = BudgetGuard(max_cost_usd=1.0, label="strict")
    await gateway(provider, settings()).structured(
        agent_name="strategy", request=request(), task_class="strategic_decision",
        budget=guard,
    )
    assert len(provider.calls) == 1
    assert guard.pricing_trusted is True


async def test_operator_can_opt_into_placeholder_pricing_explicitly():
    """Never implicit: the caller must say so, and the flag is in the snapshot."""
    provider = FakeProvider()
    guard = BudgetGuard(max_cost_usd=1.0, allow_unknown_pricing=True, label="opt-in")
    await gateway(provider, settings(llm_pricing_json={})).structured(
        agent_name="strategy", request=request(), task_class="strategic_decision",
        budget=guard,
    )
    assert len(provider.calls) == 1
    snapshot = guard.snapshot()
    assert snapshot["pricing_trusted"] is False
    assert snapshot["rejections"] == []


def test_rejection_ledger_records_reason_provider_model_and_request_id():
    guard = BudgetGuard(max_cost_usd=0.01, label="ledger")
    guard.set_request_id("req-42")
    with pytest.raises(BudgetExceeded):
        guard.reserve(estimate(cost=5.0))
    entries = guard.snapshot()["rejections"]
    assert entries, "a rejection must be observable, not silent"
    last = entries[-1]
    assert last["kind"] == "cost_budget"
    assert last["provider"] == "openrouter"
    assert last["model"] == "deepseek-v4.1-flash"
    assert last["request_id"] == "req-42"
    assert "exhausted" in last["reason"]


# --------------------------------------------------------------------------- #
# 5. atomic reservations — concurrency
# --------------------------------------------------------------------------- #
async def test_concurrent_requests_cannot_overspend_the_same_budget():
    """Ten callers, room for three. Exactly three may run.

    Without reservations each caller reads ``remaining_usd`` before any of them
    spends, so all ten see headroom and all ten proceed. The reservation makes the
    claim atomic, so the envelope holds no matter how the tasks interleave.
    """
    per_call = call_cost()
    provider = FakeProvider(cost=per_call, delay=0.01)
    guard = BudgetGuard(max_cost_usd=3 * per_call, label="concurrent")  # room for exactly 3
    gw = gateway(provider)

    results = await asyncio.gather(
        *[
            gw.structured(
                agent_name="research", request=request(), task_class="research_synthesis",
                budget=guard,
            )
            for _ in range(10)
        ],
        return_exceptions=True,
    )
    ok = [r for r in results if not isinstance(r, Exception)]
    refused = [r for r in results if isinstance(r, BudgetExceeded)]

    assert len(ok) + len(refused) == 10, "every caller must be accounted for"
    assert len(provider.calls) == len(ok)
    assert len(ok) == 3, f"expected exactly 3 grants, got {len(ok)}"
    assert guard.spent_usd <= 3 * per_call + 1e-12, f"overspent: {guard.spent_usd}"
    assert guard.spent_usd == pytest.approx(per_call * len(ok))
    assert guard.remaining_usd >= 0.0
    assert guard.open_reservations() == []


def test_reservations_are_atomic_across_threads():
    """Same guarantee outside the event loop (workers, scripts, test drivers)."""
    import threading

    guard = BudgetGuard(max_cost_usd=0.05, label="threads")
    per = 0.01
    granted: list[str] = []
    denied: list[int] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            granted.append(guard.reserve(estimate(cost=per)))
        except BudgetExceeded:
            denied.append(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(granted) == 5, f"expected 5 grants, got {len(granted)}"
    assert len(denied) == 3
    assert guard.used_usd == pytest.approx(0.05)
    assert guard.remaining_usd == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# 6. failure handling
# --------------------------------------------------------------------------- #
async def test_failed_request_releases_its_reservation():
    provider = FakeProvider(fail_with=ProviderUnavailable("openrouter", "connection reset"))
    guard = BudgetGuard(max_cost_usd=0.10, label="fails")
    before = guard.remaining_usd

    with pytest.raises(ProviderUnavailable):
        await gateway(provider).structured(
            agent_name="research", request=request(), task_class="research_synthesis",
            budget=guard,
        )
    assert guard.remaining_usd == pytest.approx(before), "a safe failure must not consume budget"
    assert guard.spent_usd == 0.0
    assert guard.open_reservations() == []


async def test_unexpected_exception_releases_its_reservation():
    provider = FakeProvider(fail_with=RuntimeError("boom"))
    guard = BudgetGuard(max_cost_usd=0.10, label="fails")
    with pytest.raises(RuntimeError):
        await gateway(provider).structured(
            agent_name="research", request=request(), task_class="research_synthesis",
            budget=guard,
        )
    assert guard.spent_usd == 0.0
    assert guard.remaining_usd == pytest.approx(0.10)


async def test_timeout_retains_the_reservation_as_ambiguous():
    """A timeout may have been processed and billed. We must not assume it was free."""
    provider = FakeProvider(fail_with=ProviderUnavailable("openrouter", "Read timeout"))
    guard = BudgetGuard(max_cost_usd=0.10, label="timeout")
    before = guard.remaining_usd

    with pytest.raises(ProviderUnavailable):
        await gateway(provider).structured(
            agent_name="research", request=request(), task_class="research_synthesis",
            budget=guard,
        )

    assert guard.spent_usd > 0.0, "an ambiguous timeout must consume budget"
    assert guard.remaining_usd < before
    assert guard.open_reservations() == [], "the claim is settled, not left dangling"


def test_ambiguous_reconcile_keeps_the_whole_reservation():
    guard = BudgetGuard(max_cost_usd=1.0, label="ambiguous")
    claim = guard.reserve(estimate(cost=0.25))
    assert guard.remaining_usd == pytest.approx(0.75)

    entry = guard.reconcile(claim, actual_cost_usd=0.0, ambiguous=True)
    assert entry["state"] == "ambiguous"
    assert entry["released_usd"] == 0.0
    assert guard.spent_usd == pytest.approx(0.25), "an unknown outcome is charged at the estimate"
    assert guard.remaining_usd == pytest.approx(0.75)


def test_reconcile_releases_only_the_unused_part():
    guard = BudgetGuard(max_cost_usd=1.0, label="reconcile")
    claim = guard.reserve(estimate(cost=0.10))
    assert guard.remaining_usd == pytest.approx(0.90)

    entry = guard.reconcile(claim, actual_cost_usd=0.04, tokens=140)
    assert entry["state"] == "settled"
    assert entry["reserved_usd"] == pytest.approx(0.10)
    assert entry["actual_usd"] == pytest.approx(0.04)
    assert entry["released_usd"] == pytest.approx(0.06)
    assert guard.spent_usd == pytest.approx(0.04)
    assert guard.remaining_usd == pytest.approx(0.96)
    assert guard.tokens_used == 140


def test_actual_cost_above_the_estimate_exhausts_the_guard():
    """Estimates are estimates: observed spend is authoritative and can overspend."""
    guard = BudgetGuard(max_cost_usd=0.05, label="overspend")
    claim = guard.reserve(estimate(cost=0.01))
    guard.reconcile(claim, actual_cost_usd=0.30)
    assert guard.spent_usd == pytest.approx(0.30)
    assert guard.remaining_usd == 0.0
    assert guard.exhausted is True
    with pytest.raises(BudgetExceeded):
        guard.reserve(estimate(cost=0.001))


def test_monthly_cap_reservation_is_released_when_the_cap_refuses():
    """The gateway reserves, then the monthly cap refuses: nothing was spent."""
    guard = BudgetGuard(max_cost_usd=1.0, label="monthly")
    claim = guard.reserve(estimate(cost=0.02))
    guard.reconcile(claim, actual_cost_usd=0.0)  # gateway's refusal path
    assert guard.remaining_usd == pytest.approx(1.0)
    assert guard.spent_usd == 0.0


# --------------------------------------------------------------------------- #
# 7. shared scope: primary and auxiliary model calls
# --------------------------------------------------------------------------- #
async def test_primary_and_auxiliary_share_one_budget_scope():
    per_call = call_cost()
    provider = FakeProvider(cost=per_call)
    guard = BudgetGuard(max_cost_usd=2 * per_call, label="shared")
    gw = gateway(provider)

    # Room for exactly two calls of $0.01 at the estimate, whichever tier asks.
    await gw.structured(
        agent_name="research", request=request(), task_class="research_synthesis",
        budget=guard,
    )
    assert guard.spent_usd == pytest.approx(per_call)

    await gw.structured(
        agent_name="research", request=request(), task_class="classification",
        budget=guard,
    )
    assert guard.spent_usd == pytest.approx(2 * per_call)

    with pytest.raises(BudgetExceeded):
        await gw.structured(
            agent_name="research", request=request(), task_class="research_synthesis",
            budget=guard,
        )
    assert len(provider.calls) == 2, "the third call must not reach the provider"


def test_execution_guard_adopts_a_shared_budget_instead_of_copying_it():
    from app.agents.limits import ExecutionGuard, ExecutionLimits

    shared = BudgetGuard(max_cost_usd=0.05, label="director:research")
    guard = ExecutionGuard(limits=ExecutionLimits.for_research(settings()), shared_budget=shared)
    assert guard.budget is shared, "a private copy would double-count the envelope"

    guard.record_spend if hasattr(guard, "record_spend") else None
    shared.record(cost_usd=0.05, tokens=10)
    assert guard.budget.exhausted is True


def test_execution_limits_carry_the_pricing_policy_from_settings():
    from app.agents.limits import ExecutionGuard, ExecutionLimits

    strict = ExecutionGuard(
        limits=ExecutionLimits.for_research(settings(llm_allow_unverified_pricing=False))
    )
    assert strict.budget.allow_unknown_pricing is False

    permissive = ExecutionGuard(
        limits=ExecutionLimits.for_research(settings(llm_allow_unverified_pricing=True))
    )
    assert permissive.budget.allow_unknown_pricing is True


# --------------------------------------------------------------------------- #
# 8. observability
# --------------------------------------------------------------------------- #
async def test_snapshot_exposes_the_full_cost_story():
    provider = FakeProvider(cost=0.002)
    guard = BudgetGuard(max_cost_usd=0.10, label="observe")
    await gateway(provider).structured(
        agent_name="research", request=request(), task_class="research_synthesis",
        budget=guard,
    )
    snapshot = guard.snapshot()
    for key in (
        "max_cost_usd", "reserved_usd", "spent_usd", "committed_usd", "remaining_usd",
        "tokens_used", "max_tokens", "pricing_trusted", "exhausted", "open_reservations",
        "rejections", "unlimited", "label", "stopped_reason",
    ):
        assert key in snapshot, f"snapshot is missing {key}"
    assert snapshot["max_cost_usd"] == pytest.approx(0.10)
    assert snapshot["spent_usd"] == pytest.approx(0.002)
    assert snapshot["reserved_usd"] == 0.0
    assert snapshot["tokens_used"] == 140
    assert snapshot["unlimited"] is False


def test_snapshot_never_reports_an_infinite_limit_as_a_number():
    guard = BudgetGuard(max_cost_usd=math.inf, allow_unlimited=True)
    snapshot = guard.snapshot()
    assert snapshot["max_cost_usd"] is None
    assert snapshot["remaining_usd"] is None
    assert snapshot["unlimited"] is True


async def test_rejection_does_not_leak_credentials_into_the_ledger():
    """Reasons name the provider and model, never a key."""
    provider = FakeProvider()
    guard = BudgetGuard(max_cost_usd=0.0, label="redaction")
    with pytest.raises(BudgetExceeded):
        await gateway(provider).structured(
            agent_name="strategy", request=request(), task_class="strategic_decision",
            budget=guard,
        )
    blob = repr(guard.snapshot()).lower()
    for secret in ("api_key", "sk-", "authorization", "bearer", "secret"):
        assert secret not in blob


# --------------------------------------------------------------------------- #
# 9. known limitation, stated as a test rather than a footnote
# --------------------------------------------------------------------------- #
async def test_estimate_error_is_bounded_to_one_request():
    """A reservation can only be as accurate as the estimate.

    If the provider bills more than estimated, the overspend is bounded to the
    requests already in flight, and the guard hard-stops immediately afterwards.
    This is deliberately pinned: the guarantee is "never start a call that cannot
    fit an honest estimate", not "never exceed the ceiling under any circumstances".
    """
    honest = call_cost()
    provider = FakeProvider(cost=honest * 8)  # the provider bills 8x the estimate
    guard = BudgetGuard(max_cost_usd=honest, label="underestimate")
    gw = gateway(provider)

    await gw.structured(
        agent_name="research", request=request(), task_class="research_synthesis",
        budget=guard,
    )
    assert guard.spent_usd == pytest.approx(honest * 8)
    assert guard.exhausted is True

    with pytest.raises(BudgetExceeded):
        await gw.structured(
            agent_name="research", request=request(), task_class="research_synthesis",
            budget=guard,
        )
    assert len(provider.calls) == 1, "the guard must stop immediately after an overspend"


# --------------------------------------------------------------------------- #
# 10. Director handoff: research -> strategy must never silently uncap
# --------------------------------------------------------------------------- #
def _director(session=None):
    from app.director.strategy_nodes import StrategyNodes

    return StrategyNodes(session)


def test_director_remaining_budget_is_zero_when_research_spent_it_all():
    """The handoff that used to mean "unlimited" now means "blocked"."""
    nodes = _director()
    state = {
        "budget": {"max_cost_usd": 0.12},
        "cost_actual": {"total_usd": 0.12, "research_usd": 0.12},
    }
    remaining = nodes._remaining_budget(state)
    assert remaining == 0.0
    # 0.0 must translate into a guard that refuses, not one that allows everything.
    guard = BudgetGuard(max_cost_usd=remaining, label="director:strategy")
    with pytest.raises(BudgetExceeded):
        guard.check(estimate(cost=0.0005))


def test_director_node_guard_uses_the_remaining_budget():
    nodes = _director()
    state = {
        "budget": {"max_cost_usd": 0.12},
        "cost_actual": {"total_usd": 0.05, "research_usd": 0.05},
    }
    ctx = nodes._ctx(state, None, "strategy")
    guard = ctx.extra["budget_guard"]
    assert guard.max_cost_usd == pytest.approx(0.07)
    assert guard.snapshot()["unlimited"] is False


def test_director_remaining_budget_never_goes_negative():
    nodes = _director()
    state = {
        "budget": {"max_cost_usd": 0.10},
        "cost_actual": {"total_usd": 0.30, "research_usd": 0.30},
    }
    assert nodes._remaining_budget(state) == 0.0


# --------------------------------------------------------------------------- #
# 11. provider isolation still intact
# --------------------------------------------------------------------------- #
async def test_budget_does_not_bypass_provider_configuration_checks():
    """Fail-closed budget must not become a route around a missing credential."""
    class UnconfiguredProvider(FakeProvider):
        def is_configured(self) -> bool:
            return False

    provider = UnconfiguredProvider()
    guard = BudgetGuard(max_cost_usd=10.0, label="isolation")
    with pytest.raises(ProviderUnavailable):
        await gateway(provider).structured(
            agent_name="strategy", request=request(), task_class="strategic_decision",
            budget=guard,
        )
    assert provider.calls == []
    assert guard.spent_usd == 0.0
    assert guard.open_reservations() == [] or guard.remaining_usd == pytest.approx(10.0)


# --------------------------------------------------------------------------- #
# 12. the caller-supplied ceiling ("preflight") is not a market price
# --------------------------------------------------------------------------- #
def test_caller_supplied_ceiling_is_not_subject_to_pricing_trust():
    """``ExecutionGuard.check_budget_before_call`` quotes no provider price.

    Its estimate is the caller's own ceiling, so it must not be blocked by the
    pricing-trust rule — otherwise a correctly budgeted run would refuse to start
    simply because no model price was involved.
    """
    from app.agents.limits import ExecutionGuard, ExecutionLimits

    guard = ExecutionGuard(limits=ExecutionLimits(max_cost_usd=0.5, label="preflight"))
    guard.check_budget_before_call(prompt_tokens=100, estimated_cost_usd=0.05)
    assert guard.budget.spent_usd == 0.0, "the pre-call gate must not record spend"

    # The zero-budget rule still applies to it: no exception is granted for that.
    from app.core.errors import BudgetExhaustedError

    zero = ExecutionGuard(limits=ExecutionLimits(max_cost_usd=0.0, label="preflight"))
    with pytest.raises(BudgetExhaustedError):
        zero.check_budget_before_call(prompt_tokens=100, estimated_cost_usd=0.05)
