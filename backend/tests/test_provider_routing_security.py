"""Phase 2.5 — provider routing and credential-isolation regression tests.

Every test here uses ``httpx.MockTransport``. No test opens a socket, so the
suite cannot spend money or depend on a vendor being up. The mock records the
exact URL and headers each request would have used, which is what lets us assert
on credential routing — the property this phase exists to guarantee.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.llm.base import LLMRequest, ProviderUnavailable
from app.llm.openai_compatible import (
    OpenAICompatibleProvider,
    ProviderConfigError,
    RetryPolicy,
    validate_base_url,
)
from app.llm.providers import (
    OpenRouterProvider,
    VelonaProvider,
    build_providers,
)

# Distinct, obviously-fake credentials so a mixup is unmistakable in assertions.
OPENROUTER_KEY = "or-test-key-not-a-real-credential"
VELONA_KEY = "velona-test-key-not-a-real-credential"
VELONA_URL = "https://api.velona.example/v1"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def make_request(model: str = "deepseek-v4.1-flash", max_tokens: int = 100) -> LLMRequest:
    return LLMRequest(
        system_prompt="You are a test harness.",
        user_prompt='Return {"status": "ok"}.',
        schema_name="probe",
        json_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
        },
        max_tokens=max_tokens,
        temperature=0.0,
        purpose="test",
        metadata={"model": model},
    )


def ok_body(text: str = '{"status": "ok"}', usage: dict[str, int] | None = None) -> dict:
    return {
        "choices": [{"message": {"content": text}}],
        "usage": usage or {"prompt_tokens": 120, "completion_tokens": 30},
    }


class Recorder:
    """Captures what the transport was asked to do."""

    def __init__(self, responses: list[httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses or [])
        self.default = httpx.Response(200, json=ok_body())

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._responses:
            return self._responses.pop(0)
        return self.default

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    @property
    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]

    @property
    def auth_headers(self) -> list[str]:
        return [r.headers.get("authorization", "") for r in self.requests]


def settings(**kwargs: Any) -> Settings:
    base = dict(
        openrouter_api_key=None,
        velona_api_key=None,
        velona_base_url=None,
        llm_reasoning_provider="velona",
        llm_auxiliary_provider="velona",
        llm_allow_http_localhost=True,
    )
    base.update(kwargs)
    return Settings(**base)


# --------------------------------------------------------------------------- #
# 1. correct provider selection
# --------------------------------------------------------------------------- #
async def test_tier_routing_selects_the_configured_provider():
    from app.llm.gateway import LLMGateway

    gateway = LLMGateway(
        settings=settings(
            velona_api_key=VELONA_KEY,
            velona_base_url=VELONA_URL,
        )
    )
    route = gateway.router.resolve("strategy", "strategic_decision")
    assert route.provider == "velona"
    assert route.model == "deepseek-v4.1-flash"

    aux = gateway.router.resolve("research", "classification")
    assert aux.provider == "velona"
    assert aux.model == "qwen-3.7-flash"


async def test_cheap_and_reasoning_tasks_route_to_their_tiers():
    from app.llm.gateway import LLMGateway

    gateway = LLMGateway(
        settings=settings(
            velona_api_key=VELONA_KEY,
            velona_base_url=VELONA_URL,
            llm_auxiliary_model="qwen-3.7-flash",
            llm_reasoning_model="deepseek-v4.1-flash",
        )
    )
    assert gateway.router.resolve("x", "summarisation").model == "qwen-3.7-flash"
    assert gateway.router.resolve("x", "competitor_analysis").model == "deepseek-v4.1-flash"
    assert gateway.router.tier_models()["reasoning"][1] == "deepseek-v4.1-flash"


# --------------------------------------------------------------------------- #
# 2. correct endpoint selection
# --------------------------------------------------------------------------- #
async def test_velona_posts_to_its_configured_endpoint():
    recorder = Recorder()
    provider = VelonaProvider(
        settings(velona_api_key=VELONA_KEY, velona_base_url=VELONA_URL),
        transport=recorder.transport(),
    )
    await provider.structured(make_request())

    assert recorder.urls == [f"{VELONA_URL}/chat/completions"]
    assert "openrouter.ai" not in recorder.urls[0]


async def test_openrouter_posts_to_its_configured_endpoint():
    recorder = Recorder()
    provider = OpenRouterProvider(
        settings(openrouter_api_key=OPENROUTER_KEY),
        transport=recorder.transport(),
    )
    await provider.structured(
        make_request(model="openai/gpt-4o-mini")
    )
    assert recorder.urls == ["https://openrouter.ai/api/v1/chat/completions"]


async def test_openrouter_endpoint_is_configurable_not_hardcoded():
    recorder = Recorder()
    provider = OpenRouterProvider(
        settings(
            openrouter_api_key=OPENROUTER_KEY,
            openrouter_base_url="https://proxy.internal.example/v1",
        ),
        transport=recorder.transport(),
    )
    await provider.structured(make_request(model="openai/gpt-4o-mini"))
    assert recorder.urls == ["https://proxy.internal.example/v1/chat/completions"]


async def test_trailing_slash_in_base_url_does_not_double_up():
    recorder = Recorder()
    provider = VelonaProvider(
        settings(velona_api_key=VELONA_KEY, velona_base_url=VELONA_URL + "/"),
        transport=recorder.transport(),
    )
    await provider.structured(make_request())
    assert recorder.urls == [f"{VELONA_URL}/chat/completions"]


# --------------------------------------------------------------------------- #
# 3. correct credential isolation  (the core of Phase 2.5)
# --------------------------------------------------------------------------- #
async def test_velona_sends_only_the_velona_key():
    recorder = Recorder()
    provider = VelonaProvider(
        settings(velona_api_key=VELONA_KEY, velona_base_url=VELONA_URL),
        transport=recorder.transport(),
    )
    await provider.structured(make_request())

    assert recorder.auth_headers == [f"Bearer {VELONA_KEY}"]
    assert OPENROUTER_KEY not in str(recorder.auth_headers)


async def test_openrouter_sends_only_the_openrouter_key():
    recorder = Recorder()
    provider = OpenRouterProvider(
        settings(openrouter_api_key=OPENROUTER_KEY, velona_api_key=VELONA_KEY),
        transport=recorder.transport(),
    )
    await provider.structured(make_request(model="openai/gpt-4o-mini"))

    assert recorder.auth_headers == [f"Bearer {OPENROUTER_KEY}"]
    assert VELONA_KEY not in str(recorder.auth_headers)


async def test_openrouter_credential_is_never_sent_to_velonas_endpoint():
    """The Phase 2 defect, asserted against directly."""
    recorder = Recorder()
    velona = VelonaProvider(
        settings(
            velona_api_key=None,  # no velona credential at all
            velona_base_url=VELONA_URL,
            openrouter_api_key=OPENROUTER_KEY,  # only an openrouter credential exists
        ),
        transport=recorder.transport(),
    )
    with pytest.raises(ProviderUnavailable) as exc:
        await velona.structured(make_request())

    assert recorder.requests == [], "no request may leave without Velona's own key"
    assert "VELONA_API_KEY" in str(exc.value)
    assert OPENROUTER_KEY not in str(exc.value)


async def test_velona_credential_is_never_sent_to_openrouter():
    recorder = Recorder()
    openrouter = OpenRouterProvider(
        settings(openrouter_api_key=None, velona_api_key=VELONA_KEY),
        transport=recorder.transport(),
    )
    with pytest.raises(ProviderUnavailable) as exc:
        await openrouter.structured(make_request(model="openai/gpt-4o-mini"))

    assert recorder.requests == [], "no request may leave without OpenRouter's own key"
    assert "OPENROUTER_API_KEY" in str(exc.value)
    assert VELONA_KEY not in str(exc.value)


def test_each_provider_is_bound_to_exactly_one_credential_variable():
    built = build_providers(settings())
    assert built["velona"]._impl.env_var == "VELONA_API_KEY"
    assert built["openrouter"]._impl.env_var == "OPENROUTER_API_KEY"
    # nothing reads a generic shared key
    assert built["velona"]._impl.env_var != built["openrouter"]._impl.env_var


def test_providers_receive_values_not_the_settings_object():
    """A provider cannot go looking for another vendor's key."""
    built = build_providers(settings())
    for name in ("openrouter", "velona"):
        impl = built[name]._impl
        assert not hasattr(impl, "_settings"), f"{name} holds a Settings reference"


# --------------------------------------------------------------------------- #
# 4. incorrect endpoint rejection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad_url",
    [
        "ftp://api.velona.example/v1",
        "javascript:alert(1)",
        "not-a-url",
        "https://",
        "",
    ],
)
def test_malformed_endpoints_are_rejected(bad_url):
    with pytest.raises(ProviderConfigError):
        validate_base_url(bad_url)


def test_endpoint_with_embedded_credentials_is_rejected():
    with pytest.raises(ProviderConfigError) as exc:
        validate_base_url("https://user:pass@api.velona.example/v1")
    assert "credentials" in str(exc.value)


def test_endpoint_with_query_string_is_rejected():
    with pytest.raises(ProviderConfigError):
        validate_base_url("https://api.velona.example/v1?key=abc")


def test_velona_provider_refuses_a_bad_url_at_construction():
    with pytest.raises(ProviderConfigError):
        VelonaProvider(
            settings(velona_api_key=VELONA_KEY, velona_base_url="http://api.velona.example/v1")
        )


# --------------------------------------------------------------------------- #
# 5. missing credentials fail safely
# --------------------------------------------------------------------------- #
async def test_unconfigured_velona_fails_closed_without_a_request():
    recorder = Recorder()
    provider = VelonaProvider(settings(velona_base_url=VELONA_URL), transport=recorder.transport())
    assert provider.is_configured() is False
    with pytest.raises(ProviderUnavailable):
        await provider.structured(make_request())
    assert recorder.requests == []


async def test_key_without_url_fails_closed():
    """A credential with no verified endpoint must not be sent anywhere."""
    recorder = Recorder()
    provider = VelonaProvider(settings(velona_api_key=VELONA_KEY), transport=recorder.transport())
    assert provider.is_configured() is False
    with pytest.raises(ProviderConfigError) as exc:
        await provider.structured(make_request())
    assert "VELONA_BASE_URL" in str(exc.value)
    assert recorder.requests == []


async def test_gateway_names_the_missing_variable_instead_of_substituting():
    """An unconfigured provider must not silently fall back to another vendor."""
    from app.llm.gateway import LLMGateway

    recorder = Recorder()
    gateway = LLMGateway(
        settings=settings(openrouter_api_key=OPENROUTER_KEY),  # velona absent
        transport=recorder.transport(),
    )
    with pytest.raises(ProviderUnavailable) as exc:
        await gateway.structured(
            agent_name="strategy",
            request=make_request(),
            task_class="strategic_decision",
        )
    assert "VELONA" in str(exc.value)
    assert recorder.requests == [], "must not fall back to OpenRouter"


async def test_unknown_provider_is_refused_not_substituted():
    from app.llm.gateway import LLMGateway

    gateway = LLMGateway(
        settings=settings(llm_reasoning_provider="acme-ai", velona_api_key=VELONA_KEY)
    )
    with pytest.raises(ProviderUnavailable) as exc:
        await gateway.structured(
            agent_name="x", request=make_request(), task_class="strategic_decision"
        )
    assert "not registered" in str(exc.value)


# --------------------------------------------------------------------------- #
# 6. HTTPS enforcement
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    ["http://api.velona.example/v1", "http://evil.example/v1", "http://10.0.0.5/v1"],
)
def test_plain_http_is_refused_for_remote_hosts(url):
    with pytest.raises(ProviderConfigError) as exc:
        validate_base_url(url)
    assert "https" in str(exc.value).lower()


@pytest.mark.parametrize(
    "url",
    ["http://localhost:8080/v1", "http://127.0.0.1:8080/v1", "http://[::1]:8080/v1"],
)
def test_plain_http_loopback_is_allowed_by_default(url):
    assert validate_base_url(url)


@pytest.mark.parametrize("url", ["http://localhost:8080/v1", "http://127.0.0.1:8080/v1"])
def test_loopback_http_can_be_forbidden_explicitly(url):
    with pytest.raises(ProviderConfigError):
        validate_base_url(url, allow_http_localhost=False)


def test_https_is_accepted():
    assert validate_base_url("https://api.velona.example/v1") == "https://api.velona.example/v1"


def test_public_ip_is_not_treated_as_loopback():
    with pytest.raises(ProviderConfigError):
        validate_base_url("http://93.184.216.34/v1")


# --------------------------------------------------------------------------- #
# 7. redirect safety
# --------------------------------------------------------------------------- #
async def test_redirect_is_refused_rather_than_followed():
    """A 3xx must never cause an Authorization header to be forwarded."""
    recorder = Recorder(
        responses=[
            httpx.Response(307, headers={"location": "https://evil.example/steal"}),
        ]
    )
    provider = VelonaProvider(
        settings(velona_api_key=VELONA_KEY, velona_base_url=VELONA_URL),
        transport=recorder.transport(),
    )
    with pytest.raises(ProviderUnavailable) as exc:
        await provider.structured(make_request())

    assert "redirect" in str(exc.value).lower()
    # exactly one request: the redirect target was never contacted
    assert len(recorder.requests) == 1
    assert "evil.example" not in recorder.urls[0]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_all_redirect_codes_are_refused(status):
    policy = RetryPolicy()
    assert policy.should_retry(status_code=status, transport_error=False) is False


async def test_client_is_constructed_without_following_redirects():
    provider = VelonaProvider(
        settings(velona_api_key=VELONA_KEY, velona_base_url=VELONA_URL),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=ok_body())),
    )
    client = provider._impl._get_client()
    assert client.follow_redirects is False
    await provider._impl.aclose()


# --------------------------------------------------------------------------- #
# 8. retry limits
# --------------------------------------------------------------------------- #
async def test_no_retry_on_client_error():
    recorder = Recorder(responses=[httpx.Response(401), httpx.Response(200, json=ok_body())])
    provider = VelonaProvider(
        settings(velona_api_key=VELONA_KEY, velona_base_url=VELONA_URL),
        transport=recorder.transport(),
    )
    with pytest.raises(ProviderUnavailable):
        await provider.structured(make_request())
    assert len(recorder.requests) == 1, "a 401 must not be retried"


async def test_retry_on_429_then_succeed():
    recorder = Recorder(responses=[httpx.Response(429), httpx.Response(200, json=ok_body())])
    provider = VelonaProvider(
        settings(velona_api_key=VELONA_KEY, velona_base_url=VELONA_URL),
        transport=recorder.transport(),
    )
    response = await provider.structured(make_request())
    assert response.data == {"status": "ok"}
    assert len(recorder.requests) == 2


async def test_retries_are_bounded():
    recorder = Recorder(responses=[httpx.Response(503) for _ in range(10)])
    provider = VelonaProvider(
        settings(velona_api_key=VELONA_KEY, velona_base_url=VELONA_URL),
        transport=recorder.transport(),
    )
    with pytest.raises(ProviderUnavailable):
        await provider.structured(make_request())
    assert len(recorder.requests) == 2, "max_attempts=2 means two requests, never more"


def test_retry_policy_rejects_a_zero_attempt_configuration():
    with pytest.raises(ProviderConfigError):
        RetryPolicy(max_attempts=0)


# --------------------------------------------------------------------------- #
# 9. usage attribution
# --------------------------------------------------------------------------- #
async def test_token_usage_is_extracted_from_the_response():
    recorder = Recorder(
        responses=[httpx.Response(200, json=ok_body(usage={"prompt_tokens": 321, "completion_tokens": 45}))]
    )
    provider = VelonaProvider(
        settings(velona_api_key=VELONA_KEY, velona_base_url=VELONA_URL),
        transport=recorder.transport(),
    )
    response = await provider.structured(make_request())
    assert response.prompt_tokens == 321
    assert response.completion_tokens == 45
    assert response.total_tokens == 366
    assert response.provider == "velona"
    assert response.model == "deepseek-v4.1-flash"


async def test_missing_usage_block_records_zero_rather_than_guessing():
    body = ok_body()
    body.pop("usage")
    recorder = Recorder(responses=[httpx.Response(200, json=body)])
    provider = VelonaProvider(
        settings(velona_api_key=VELONA_KEY, velona_base_url=VELONA_URL),
        transport=recorder.transport(),
    )
    response = await provider.structured(make_request())
    assert response.prompt_tokens == 0
    assert response.completion_tokens == 0
    assert response.cost_usd == 0.0


async def test_gateway_records_usage_against_the_resolved_tier(session):
    """Usage attribution survives the Phase 2.5 refactor."""
    from sqlalchemy import select

    from app.llm.gateway import LLMGateway
    from app.models.agent import ModelUsage

    recorder = Recorder()
    gateway = LLMGateway(
        session=session,
        settings=settings(velona_api_key=VELONA_KEY, velona_base_url=VELONA_URL),
        transport=recorder.transport(),
    )
    await gateway.structured(
        agent_name="strategy",
        request=make_request(),
        task_class="strategic_decision",
    )

    rows = (await session.execute(select(ModelUsage))).scalars().all()
    matching = [r for r in rows if r.provider == "velona"]
    assert matching, "usage was not recorded"
    row = matching[-1]
    assert row.tier == "reasoning"
    assert row.task_class == "strategic_decision"
    assert row.model == "deepseek-v4.1-flash"
    assert row.total_tokens == 150


async def test_auxiliary_spend_is_recorded_separately_from_reasoning(session):
    from sqlalchemy import select

    from app.llm.gateway import LLMGateway
    from app.models.agent import ModelUsage

    recorder = Recorder()
    gateway = LLMGateway(
        session=session,
        settings=settings(velona_api_key=VELONA_KEY, velona_base_url=VELONA_URL),
        transport=recorder.transport(),
    )
    await gateway.structured(
        agent_name="research", request=make_request("qwen-3.7-flash"), task_class="summarisation"
    )
    await gateway.structured(
        agent_name="strategy", request=make_request(), task_class="positioning"
    )

    rows = [
        r for r in (await session.execute(select(ModelUsage))).scalars().all() if r.provider == "velona"
    ]
    tiers = {r.tier for r in rows}
    assert tiers == {"auxiliary", "reasoning"}


# --------------------------------------------------------------------------- #
# 10. existing cost tracking and budgets remain intact
# --------------------------------------------------------------------------- #
async def test_cost_tracking_still_works(session):
    from sqlalchemy import select

    from app.llm.gateway import LLMGateway
    from app.models.agent import ModelUsage

    recorder = Recorder()
    gateway = LLMGateway(
        session=session,
        settings=settings(velona_api_key=VELONA_KEY, velona_base_url=VELONA_URL),
        transport=recorder.transport(),
    )
    response = await gateway.structured(
        agent_name="strategy", request=make_request(), task_class="strategic_decision"
    )
    assert response.cost_usd > 0

    rows = [
        r for r in (await session.execute(select(ModelUsage))).scalars().all() if r.provider == "velona"
    ]
    assert rows[-1].cost_usd == pytest.approx(response.cost_usd)


async def test_budget_guard_still_blocks_before_spending():
    from app.llm.base import BudgetExceeded
    from app.llm.gateway import LLMGateway
    from app.llm.routing import BudgetGuard

    recorder = Recorder()
    gateway = LLMGateway(
        settings=settings(velona_api_key=VELONA_KEY, velona_base_url=VELONA_URL),
        transport=recorder.transport(),
    )
    # NB: max_cost_usd=0 means "uncapped" in BudgetGuard (Phase 2 semantics), not
    # "block everything" — see test_zero_cost_budget_means_uncapped below. Use a
    # budget too small for any call so the guard must refuse.
    guard = BudgetGuard(max_cost_usd=1e-9, label="test")
    with pytest.raises(BudgetExceeded):
        await gateway.structured(
            agent_name="strategy",
            request=make_request(),
            task_class="strategic_decision",
            budget=guard,
        )
    assert recorder.requests == [], "an exhausted budget must prevent the request entirely"


def test_zero_cost_budget_means_uncapped_and_that_is_a_known_trap():
    """Documents a fail-open semantic inherited from Phase 2.

    ``BudgetGuard(max_cost_usd=0)`` disables the cost check instead of forbidding
    all spend. An operator who sets 0 expecting "spend nothing" gets "spend
    anything". This test pins the current behaviour so the Phase 2.5 report can
    flag it as a residual risk rather than have it discovered in production.
    """
    from app.llm.routing import BudgetGuard, CostEstimate

    permissive = BudgetGuard(max_cost_usd=0.0)
    estimate = CostEstimate(
        provider="velona",
        model="deepseek-v4.1-flash",
        tier="reasoning",
        task_class="strategic_decision",
        prompt_tokens=100,
        completion_tokens=100,
        input_per_1k=0.004,
        output_per_1k=0.016,
        estimated_cost_usd=0.5,
        price_verified=False,
    )
    permissive.check(estimate)  # no raise: 0 == uncapped

    strict = BudgetGuard(max_cost_usd=1e-9)
    with pytest.raises(Exception):
        strict.check(estimate)


async def test_estimated_cost_is_available_before_the_call():
    from app.llm.gateway import LLMGateway

    gateway = LLMGateway(
        settings=settings(velona_api_key=VELONA_KEY, velona_base_url=VELONA_URL)
    )
    estimate = gateway.router.estimate(
        agent_name="strategy",
        task_class="positioning",
        system_prompt="a" * 100,
        user_prompt="b" * 100,
        max_output_tokens=100,
    )
    assert estimate["estimated_cost_usd"] > 0
    assert estimate["provider"] == "velona"


# --------------------------------------------------------------------------- #
# 11. no paid inference in automated tests
# --------------------------------------------------------------------------- #
def test_no_live_transport_is_constructed_in_the_test_environment(monkeypatch):
    """Even a mistaken construction must not reach the network under APP_ENV=test."""
    from app.llm.openai_compatible import assert_no_live_network_in_tests

    monkeypatch.setenv("APP_ENV", "test")
    with pytest.raises(ProviderConfigError):
        assert_no_live_network_in_tests(None)


def test_provider_module_has_no_hardcoded_vendor_endpoints():
    """Both OpenAI-compatible endpoints must come from configuration."""
    from pathlib import Path

    source = Path("app/llm/providers.py").read_text()
    assert "openrouter.ai/api/v1/chat/completions" not in source
    assert "velona.in" not in source
    assert "api.velona" not in source


# --------------------------------------------------------------------------- #
# 12. key hygiene
# --------------------------------------------------------------------------- #
def test_describe_never_contains_the_key_or_its_variable_name():
    built = build_providers(settings(velona_api_key=VELONA_KEY, velona_base_url=VELONA_URL))
    for name in ("openrouter", "velona"):
        described = json.dumps(built[name].describe())
        assert VELONA_KEY not in described
        assert OPENROUTER_KEY not in described
        assert "API_KEY" not in described


async def test_error_messages_never_contain_the_key():
    recorder = Recorder(responses=[httpx.Response(401)])
    provider = VelonaProvider(
        settings(velona_api_key=VELONA_KEY, velona_base_url=VELONA_URL),
        transport=recorder.transport(),
    )
    with pytest.raises(ProviderUnavailable) as exc:
        await provider.structured(make_request())
    assert VELONA_KEY not in str(exc.value)


async def test_provider_status_endpoint_does_not_leak_credential_identifiers(client, auth):
    """The HTTP status surface must not name credential variables."""
    resp = await client.get("/api/v1/system/status", headers=auth("founder"))
    assert resp.status_code == 200
    text = resp.text
    for forbidden in ("API_KEY", "VELONA_API_KEY", "OPENROUTER_API_KEY", VELONA_KEY, OPENROUTER_KEY):
        assert forbidden not in text
