"""Configurable model routing, budget enforcement and usage accounting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.llm.base import (
    BudgetExceeded,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderUnavailable,
)
from app.llm.providers import build_providers
from app.models.agent import ModelUsage
from app.services.usage import UsageService


from app.llm.routing import (
    Route,
    configured_models,
    estimate_cost,
    estimate_prompt_tokens,
    price_for_model,
    tier_for_task,
    unverified_models,
)

__all__ = [
    "LLMGateway",
    "ModelRouter",
    "Route",
    "gateway_status",
    "reset_gateway_status",
]


class ModelRouter:
    """Resolves (agent, task_class) → (provider, model) from configuration.

    Resolution precedence, most specific first:

    1. ``LLM_TASK_ROUTING_JSON[task_class]`` — explicit per-work-class override.
    2. ``LLM_ROUTING_JSON[agent_name]``      — Phase 1 behaviour, unchanged.
    3. tier default                          — auxiliary or reasoning model.
    4. ``LLM_DEFAULT_PROVIDER/MODEL``        — global fallback.
    5. ``default`` entry in ``LLM_ROUTING_JSON``.

    Nothing is guessed: if no provider can be resolved a typed
    :class:`ProviderUnavailable` is raised and the caller degrades honestly.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._routing: dict[str, Any] = settings.llm_routing_json or {}
        self._task_routing: dict[str, Any] = getattr(settings, "llm_task_routing_json", None) or {}

    # ------------------------------------------------------------------ tiers
    def tier_models(self) -> dict[str, tuple[str, str | None]]:
        return {
            "reasoning": (
                self.settings.llm_reasoning_provider,
                self.settings.llm_reasoning_model,
            ),
            "auxiliary": (
                self.settings.llm_auxiliary_provider,
                self.settings.llm_auxiliary_model,
            ),
        }

    def resolve(self, agent_name: str, task_class: str = "") -> Route:
        tier = tier_for_task(task_class)

        # 1. explicit task-class override
        if task_class:
            entry = self._task_routing.get(task_class) or {}
            if entry.get("provider"):
                return Route(
                    provider=entry["provider"],
                    model=entry.get("model"),
                    tier=tier,
                    task_class=task_class,
                    source="task_routing",
                )

        # 2. Phase 1 agent routing (unchanged semantics)
        entry = self._routing.get(agent_name) or {}
        if entry.get("provider"):
            return Route(
                provider=entry["provider"],
                model=entry.get("model"),
                tier=tier,
                task_class=task_class,
                source="agent_routing",
            )

        # 3. tier default
        if tier in ("reasoning", "auxiliary"):
            provider, model = self.tier_models()[tier]
            if provider:
                return Route(
                    provider=provider,
                    model=model,
                    tier=tier,
                    task_class=task_class,
                    source=f"{tier}_default",
                )

        # 4. global default
        if self.settings.llm_default_provider:
            return Route(
                provider=self.settings.llm_default_provider,
                model=self.settings.llm_default_model,
                tier=tier,
                task_class=task_class,
                source="global_default",
            )

        # 5. routing table default entry
        entry = self._routing.get("default") or {}
        if entry.get("provider"):
            return Route(
                provider=entry["provider"],
                model=entry.get("model"),
                tier=tier,
                task_class=task_class,
                source="routing_default",
            )

        # Nothing configured: raise a typed error, never guess a vendor.
        raise ProviderUnavailable(
            "none",
            "no LLM provider configured (set LLM_REASONING_PROVIDER, "
            "LLM_DEFAULT_PROVIDER or LLM_ROUTING_JSON)",
        )

    # ------------------------------------------------------------ estimation
    def estimate(
        self,
        *,
        agent_name: str,
        task_class: str,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        """Pre-flight cost estimate; the UI shows this before a run starts."""
        route = self.resolve(agent_name, task_class)
        estimate = estimate_cost(
            provider=route.provider,
            model=route.model,
            prompt_tokens=estimate_prompt_tokens(system_prompt, user_prompt),
            completion_tokens=max_output_tokens,
            tier=route.tier,
            task_class=task_class,
            settings=self.settings,
        )
        payload = estimate.as_dict()
        payload["route"] = route.as_dict()
        return payload

    def describe(self) -> dict[str, Any]:
        models = configured_models(self.settings)
        return {
            "default": {
                "provider": self.settings.llm_default_provider or None,
                "model": self.settings.llm_default_model or None,
            },
            "tiers": models,
            "task_routing": self._task_routing,
            "routing": self._routing,
            "unverified_models": list(unverified_models(self.settings)),
            "configured": bool(
                self.settings.llm_default_provider
                or self.settings.llm_reasoning_provider
                or self._routing
                or self._task_routing
            ),
        }


class LLMGateway:
    def __init__(
        self,
        session: AsyncSession | None = None,
        settings: Settings | None = None,
        providers: dict[str, LLMProvider] | None = None,
        transport: Any = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.session = session
        self.router = ModelRouter(self.settings)
        # An injected transport (httpx.MockTransport in tests) is threaded into
        # every provider so automated tests can never open a socket.
        self.providers = providers or build_providers(self.settings, transport=transport)
        self.usage = UsageService(session) if session is not None else None

    # ------------------------------------------------------------------ status
    def availability(self) -> dict[str, Any]:
        return {
            "providers": {
                name: (
                    p.describe()
                    if hasattr(p, "describe")
                    else {"configured": p.is_configured()}
                )
                for name, p in self.providers.items()
            },
            "routing": self.router.describe(),
            "monthly_budget_usd": self.settings.llm_monthly_budget_usd,
            "strategy_budget_usd": self.settings.strategy_budget_usd,
            "research_budget_usd": self.settings.research_budget_usd,
            "available": any(p.is_configured() for p in self.providers.values()),
        }

    # ------------------------------------------------------------------- call
    async def structured(
        self,
        *,
        agent_name: str,
        request: LLMRequest,
        agent_run_id: Any = None,
        attempt: int = 1,
        model_budget_tokens: int | None = None,
        task_class: str = "",
        budget: Any = None,
        dedupe: Any = None,
        dedupe_key: str = "",
        cancel: Any = None,
    ) -> LLMResponse:
        """Run one structured call under every configured budget.

        Order matters and is deliberate:

        1. cancellation is honoured *before* anything is spent;
        2. the route is resolved (a missing provider fails before a budget check,
           so the error explains the real cause);
        3. the response cache is consulted (a duplicate prompt costs nothing);
        4. the estimate is checked against the run budget *and* the monthly cap;
        5. only then is the provider called.
        """
        if cancel is not None:
            cancel.check()

        route = self.router.resolve(agent_name, task_class)
        provider = self.providers.get(route.provider)
        if provider is None:
            # No silent substitution: an unknown provider is a hard failure, not a
            # cue to try a different vendor with a different credential.
            raise ProviderUnavailable(
                route.provider, "provider is not registered; refusing to substitute another"
            )
        if not provider.is_configured():
            # Name precisely which configuration is missing, without values.
            missing = getattr(provider, "env_var", None) or "credentials"
            detail = f"{missing} is not set"
            if getattr(provider, "name", "") == "velona" and not provider.is_configured():
                detail = "VELONA_API_KEY and/or VELONA_BASE_URL is not set"
            raise ProviderUnavailable(route.provider, detail)

        if model_budget_tokens is not None and request.max_tokens > model_budget_tokens:
            raise BudgetExceeded(
                f"requested {request.max_tokens} tokens exceeds per-run budget "
                f"{model_budget_tokens}"
            )

        cache_key = ""
        if dedupe is not None:
            cache_key = dedupe.key_for(
                dedupe_key or request.schema_name, request.user_prompt
            )
            cached = dedupe.get(cache_key)
            if cached is not None:
                return cached

        estimate = estimate_cost(
            provider=route.provider,
            model=route.model,
            prompt_tokens=estimate_prompt_tokens(
                request.system_prompt, request.user_prompt
            ),
            completion_tokens=request.max_tokens,
            tier=route.tier,
            task_class=task_class,
            settings=self.settings,
        )

        if budget is not None:
            budget.check(estimate)

        if self.usage is not None:
            spent = await self.usage.month_spend()
            if spent + estimate.estimated_cost_usd > self.settings.llm_monthly_budget_usd:
                raise BudgetExceeded(
                    f"monthly LLM budget of ${self.settings.llm_monthly_budget_usd:.2f} "
                    f"would be exceeded (spent ${spent:.2f}, "
                    f"call estimated ${estimate.estimated_cost_usd:.4f})"
                )

        if route.model:
            request.metadata["model"] = route.model

        response = await provider.structured(request)

        if budget is not None:
            budget.record(cost_usd=response.cost_usd, tokens=response.total_tokens)

        if dedupe is not None and cache_key:
            dedupe.put(cache_key, response)

        if self.usage is not None:
            await self._record_usage(
                agent_run_id=agent_run_id,
                agent_name=agent_name,
                response=response,
                attempt=attempt,
                operation=request.purpose,
                tier=route.tier,
                task_class=task_class,
            )
        return response

    async def _record_usage(
        self,
        *,
        agent_run_id: Any,
        agent_name: str,
        response: LLMResponse,
        attempt: int,
        operation: str,
        tier: str = "default",
        task_class: str = "",
    ) -> None:
        """Idempotent write: one row per (run, attempt).

        Runs without an ``agent_run_id`` (e.g. a research tool call made outside
        an agent) are still recorded — ``agent_run_id`` is nullable precisely so
        auxiliary research spend is not invisible to cost reporting.
        """
        assert self.usage is not None
        if agent_run_id is not None:
            existing = await self.session.execute(  # type: ignore[union-attr]
                select(ModelUsage).where(
                    ModelUsage.agent_run_id == agent_run_id,
                    ModelUsage.attempt == attempt,
                )
            )
            if existing.scalar_one_or_none() is not None:
                return
        await self.usage.record(
            agent_run_id=agent_run_id,
            agent_name=agent_name,
            provider=response.provider,
            model=response.model,
            attempt=attempt,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            cost_usd=response.cost_usd,
            latency_ms=response.latency_ms,
            operation=operation,
            tier=tier,
            task_class=task_class,
            metadata={"tier": tier, "task_class": task_class},
        )


_gateway_status_cache: dict[str, Any] | None = None


def gateway_status(settings: Settings | None = None) -> dict[str, Any]:
    """Cheap status probe for ``/system/status`` (no DB session required)."""
    global _gateway_status_cache
    if _gateway_status_cache is None:
        gateway = LLMGateway(session=None, settings=settings or get_settings())
        _gateway_status_cache = gateway.availability()
    return _gateway_status_cache


def reset_gateway_status() -> None:
    global _gateway_status_cache
    _gateway_status_cache = None
