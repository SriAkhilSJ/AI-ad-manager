"""Provider-independent LLM contracts.

Agents never import a vendor SDK. They call :class:`LLMGateway`, which:

1. resolves a provider/model from configurable routing,
2. enforces a per-run token budget and a monthly USD budget,
3. requests a **structured** (schema-constrained) response,
4. records tokens and cost against the agent run.

If no provider is configured the gateway raises :class:`ProviderUnavailable`.
It never returns fabricated text or invented token counts — callers fall back to
their deterministic implementation, which is a real code path, not a mock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel


class ProviderUnavailable(RuntimeError):
    code = "provider_unavailable"

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(f"LLM provider '{provider}' unavailable: {reason}")
        self.provider = provider
        self.reason = reason


class BudgetExceeded(RuntimeError):
    code = "budget_exceeded"


class LLMRequest(BaseModel):
    """A structured generation request."""

    system_prompt: str
    user_prompt: str
    schema_name: str
    json_schema: dict[str, Any]
    max_tokens: int = 1200
    temperature: float = 0.4
    purpose: str = "structured_call"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    provider: str
    model: str
    data: dict[str, Any]
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: int
    raw_text: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMProvider(Protocol):
    name: str

    def is_configured(self) -> bool: ...

    async def structured(self, request: LLMRequest) -> LLMResponse: ...


@dataclass(frozen=True)
class ModelPrice:
    provider: str
    model: str
    input_per_1k: float
    output_per_1k: float

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return round(
            (prompt_tokens / 1000.0) * self.input_per_1k
            + (completion_tokens / 1000.0) * self.output_per_1k,
            6,
        )


# Indicative list prices (USD / 1k tokens) used for cost attribution. These are
# configuration data, not billing truth; override via LLM_ROUTING_JSON pricing
# when provider rates change.
DEFAULT_PRICES: list[ModelPrice] = [
    ModelPrice("openai", "gpt-4o-mini", 0.00015, 0.0006),
    ModelPrice("openai", "gpt-4o", 0.0025, 0.01),
    ModelPrice("anthropic", "claude-3-5-haiku", 0.0008, 0.004),
    ModelPrice("anthropic", "claude-sonnet-4", 0.003, 0.015),
    ModelPrice("openrouter", "auto", 0.001, 0.003),
]


def price_for(provider: str, model: str) -> ModelPrice:
    for price in DEFAULT_PRICES:
        if price.provider == provider and price.model == model:
            return price
    for price in DEFAULT_PRICES:
        if price.provider == provider:
            return price
    return ModelPrice(provider, model, 0.001, 0.003)
