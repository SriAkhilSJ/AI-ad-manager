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


@dataclass(frozen=True)
class Route:
    provider: str
    model: str | None = None


class ModelRouter:
    """Resolves (agent/purpose) → (provider, model) from environment config.

    ``LLM_ROUTING_JSON`` example::

        {"strategy": {"provider": "anthropic", "model": "claude-sonnet-4"},
         "content":  {"provider": "openai",    "model": "gpt-4o-mini"},
         "default":  {"provider": "openai",    "model": "gpt-4o-mini"}}
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._routing: dict[str, Any] = settings.llm_routing_json or {}

    def resolve(self, agent_name: str) -> Route:
        entry = self._routing.get(agent_name) or self._routing.get("default") or {}
        provider = entry.get("provider") or self.settings.llm_default_provider
        model = entry.get("model") or self.settings.llm_default_model
        if not provider:
            # Nothing configured: raise a typed error, never guess a vendor.
            raise ProviderUnavailable(
                "none",
                "no LLM provider configured (set LLM_DEFAULT_PROVIDER or LLM_ROUTING_JSON)",
            )
        return Route(provider=provider, model=model)

    def describe(self) -> dict[str, Any]:
        return {
            "default": {
                "provider": self.settings.llm_default_provider or None,
                "model": self.settings.llm_default_model or None,
            },
            "routing": self._routing,
            "configured": bool(self.settings.llm_default_provider or self._routing),
        }


class LLMGateway:
    def __init__(
        self,
        session: AsyncSession | None = None,
        settings: Settings | None = None,
        providers: dict[str, LLMProvider] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.session = session
        self.router = ModelRouter(self.settings)
        self.providers = providers or build_providers(self.settings)
        self.usage = UsageService(session) if session is not None else None

    # ------------------------------------------------------------------ status
    def availability(self) -> dict[str, Any]:
        return {
            "providers": {
                name: {"configured": p.is_configured()}
                for name, p in self.providers.items()
            },
            "routing": self.router.describe(),
            "monthly_budget_usd": self.settings.llm_monthly_budget_usd,
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
    ) -> LLMResponse:
        route = self.router.resolve(agent_name)
        provider = self.providers.get(route.provider)
        if provider is None or not provider.is_configured():
            raise ProviderUnavailable(route.provider, "provider not configured")

        if model_budget_tokens is not None and request.max_tokens > model_budget_tokens:
            raise BudgetExceeded(
                f"requested {request.max_tokens} tokens exceeds per-run budget "
                f"{model_budget_tokens}"
            )

        if self.usage is not None:
            spent = await self.usage.month_spend()
            if spent >= self.settings.llm_monthly_budget_usd:
                raise BudgetExceeded(
                    f"monthly LLM budget of ${self.settings.llm_monthly_budget_usd:.2f} "
                    f"exhausted (spent ${spent:.2f})"
                )

        if route.model:
            request.metadata["model"] = route.model

        response = await provider.structured(request)

        if self.usage is not None and agent_run_id is not None:
            await self._record_usage(
                agent_run_id=agent_run_id,
                agent_name=agent_name,
                response=response,
                attempt=attempt,
                operation=request.purpose,
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
    ) -> None:
        """Idempotent write: one row per (run, attempt)."""
        assert self.usage is not None
        existing = await self.session.execute(  # type: ignore[union-attr]
            select(ModelUsage).where(
                ModelUsage.agent_run_id == agent_run_id, ModelUsage.attempt == attempt
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
