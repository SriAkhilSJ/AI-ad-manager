"""Task-class model routing, cost estimation and budget enforcement.

Phase 1 routed by ``agent_name`` alone. Phase 2 needs to route by *what kind of
work* a call is, because the cost of a strategy run is dominated by two very
different activities:

* **cheap / mechanical** — classification, extraction, summarisation, formatting;
* **expensive / reasoning** — competitor analysis, positioning, strategic
  decisions, research synthesis.

A single routing table cannot express "use the auxiliary model for extraction
even when the content agent is the caller". So resolution is layered:

    explicit task_class  →  agent name  →  tier default  →  global default

Design constraints honoured here:

* **Models are configuration, not code.** ``deepseek-v4.1-flash`` and
  ``qwen-3.7-flash`` are defaults in settings; nothing in agent code names a
  model. If a configured id is not in the price table it resolves to a
  conservative (expensive) price and is flagged ``verified: false`` in
  ``/system/status`` — we never pretend to know a price we do not.
* **Budgets are checked before the call**, not after, so a run cannot overspend
  and then report the damage.
* **Estimates are labelled estimates.** They are computed from prompt size and a
  configured output allowance; the actual cost comes from the provider's
  reported usage.

Nothing here imports an agent or a service, so it is unit-testable in isolation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from app.core.config import Settings, get_settings
from app.llm.base import DEFAULT_PRICES, ModelPrice, ProviderUnavailable

Tier = Literal["reasoning", "auxiliary", "default"]

# Work classes that should run on the cheap model whenever possible.
CHEAP_TASK_CLASSES: frozenset[str] = frozenset(
    {
        "classification",
        "extraction",
        "summarisation",
        "summarization",
        "formatting",
        "deduplication",
        "tagging",
    }
)

# Work classes that justify the stronger reasoning model.
REASONING_TASK_CLASSES: frozenset[str] = frozenset(
    {
        "competitor_analysis",
        "positioning",
        "strategic_decision",
        "strategy",
        "research_synthesis",
        "brand_consistency",
        "evaluation",
    }
)

# Conservative placeholder price (USD / 1k tokens) used for models we have no
# verified list price for. Deliberately high: an unknown model should fail a
# budget check before it silently spends money.
UNKNOWN_MODEL_INPUT_PER_1K = 0.004
UNKNOWN_MODEL_OUTPUT_PER_1K = 0.016

# Rough character→token ratio for estimation only. Real usage is always taken
# from the provider response; this exists so a budget can be checked *before*
# money is spent.
CHARS_PER_TOKEN = 3.6


def tier_for_task(task_class: str) -> Tier:
    """Map a work class to the tier that should serve it."""
    key = (task_class or "").strip().lower()
    if key in CHEAP_TASK_CLASSES:
        return "auxiliary"
    if key in REASONING_TASK_CLASSES:
        return "reasoning"
    return "default"


@dataclass(frozen=True)
class Route:
    """A resolved routing decision."""

    provider: str
    model: str | None
    tier: Tier
    task_class: str
    source: str = "default"  # which rule matched: used by /system/status + tests

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model or 'provider-default'}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "tier": self.tier,
            "task_class": self.task_class,
            "source": self.source,
        }


def estimate_prompt_tokens(*parts: str) -> int:
    """Cheap, deliberately rounded-up token estimate for pre-flight budgets."""
    total = sum(len(p or "") for p in parts)
    return max(1, math.ceil(total / CHARS_PER_TOKEN))


@dataclass
class CostEstimate:
    """A pre-flight cost estimate that the caller must be able to show a human."""

    provider: str
    model: str | None
    tier: str
    task_class: str
    prompt_tokens: int
    completion_tokens: int
    input_per_1k: float
    output_per_1k: float
    estimated_cost_usd: float
    price_verified: bool
    currency_note: str = "estimated from list price; actual cost is recorded from provider usage"

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "tier": self.tier,
            "task_class": self.task_class,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "input_per_1k": self.input_per_1k,
            "output_per_1k": self.output_per_1k,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "price_verified": self.price_verified,
            "currency_note": self.currency_note,
        }


def price_for_model(
    provider: str, model: str | None, settings: Settings | None = None
) -> tuple[ModelPrice, bool]:
    """Return ``(price, verified)``.

    ``verified`` is False when we are falling back to a generic price because the
    configured model id is not in the price table. That flag is surfaced rather
    than hidden, so a finance-critical number is never silently invented.
    """
    settings = settings or get_settings()
    overrides: dict[str, Any] = getattr(settings, "llm_pricing_json", None) or {}
    model = model or ""

    # 1. operator-supplied pricing wins and counts as verified.
    for key in (f"{provider}/{model}", model):
        entry = overrides.get(key)
        if isinstance(entry, dict) and "input_per_1k" in entry:
            return (
                ModelPrice(
                    provider,
                    model,
                    float(entry.get("input_per_1k", 0.0)),
                    float(entry.get("output_per_1k", 0.0)),
                ),
                True,
            )

    # 2. exact match in the built-in table
    for price in DEFAULT_PRICES:
        if price.provider == provider and price.model == model:
            return price, True

    # 3. same provider, different model: use it only as a *shape* fallback for
    #    the same tier and mark it unverified.
    for price in DEFAULT_PRICES:
        if price.provider == provider:
            return (
                ModelPrice(
                    provider,
                    model,
                    UNKNOWN_MODEL_INPUT_PER_1K,
                    UNKNOWN_MODEL_OUTPUT_PER_1K,
                ),
                False,
            )

    return ModelPrice(
        provider, model, UNKNOWN_MODEL_INPUT_PER_1K, UNKNOWN_MODEL_OUTPUT_PER_1K
    ), False


def estimate_cost(
    *,
    provider: str,
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    tier: str = "default",
    task_class: str = "",
    settings: Settings | None = None,
) -> CostEstimate:
    price, verified = price_for_model(provider, model, settings)
    cost = price.cost(prompt_tokens, completion_tokens)
    return CostEstimate(
        provider=provider,
        model=model,
        tier=tier,
        task_class=task_class,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        input_per_1k=price.input_per_1k,
        output_per_1k=price.output_per_1k,
        estimated_cost_usd=cost,
        price_verified=verified,
    )


class BudgetGuard:
    """Tracks a single run's spend and refuses requests that would exceed the cap.

    Two limits are enforced together:

    * ``max_cost_usd`` — the hard ceiling for this run (e.g. ₹5 ≈ configured USD);
    * ``max_tokens``   — the token ceiling for this run.

    Both are checked *before* a paid request. ``remaining_usd`` is exposed so the
    Director can stop early and report why, rather than fail mid-call.
    """

    def __init__(
        self,
        *,
        max_cost_usd: float | None = None,
        max_tokens: int | None = None,
        max_tool_calls: int | None = None,
        label: str = "run",
    ) -> None:
        self.max_cost_usd = float(max_cost_usd or 0.0)
        self.max_tokens = int(max_tokens or 0)
        self.max_tool_calls = int(max_tool_calls or 0)
        self.spent_usd = 0.0
        self.tokens_used = 0
        self.tool_calls = 0
        self.label = label
        self.stopped_reason = ""

    # ------------------------------------------------------------- accounting
    @property
    def remaining_usd(self) -> float:
        if self.max_cost_usd <= 0:
            return math.inf
        return max(self.max_cost_usd - self.spent_usd, 0.0)

    @property
    def remaining_tokens(self) -> int:
        if self.max_tokens <= 0:
            return 2**31 - 1
        return max(self.max_tokens - self.tokens_used, 0)

    @property
    def exhausted(self) -> bool:
        return self.remaining_usd <= 0 or self.remaining_tokens <= 0

    def record(
        self, *, cost_usd: float, tokens: int, tool_calls: int = 0
    ) -> None:
        self.spent_usd = round(self.spent_usd + float(cost_usd), 6)
        self.tokens_used += max(0, int(tokens))
        self.tool_calls += max(0, int(tool_calls))

    # ---------------------------------------------------------------- gating
    def check(self, estimate: CostEstimate) -> None:
        """Raise :class:`BudgetExceeded` if a call may not proceed."""
        from app.llm.base import BudgetExceeded

        if estimate.prompt_tokens > self.remaining_tokens:
            self.stopped_reason = (
                f"token budget exhausted: {self.tokens_used}/{self.max_tokens} used, "
                f"request needs {estimate.prompt_tokens}"
            )
            raise BudgetExceeded(self.stopped_reason)

        if self.max_cost_usd > 0 and estimate.estimated_cost_usd > self.remaining_usd:
            self.stopped_reason = (
                f"cost budget exhausted: ${self.spent_usd:.4f}/${self.max_cost_usd:.4f} "
                f"used, request estimated ${estimate.estimated_cost_usd:.4f}"
            )
            raise BudgetExceeded(self.stopped_reason)

        if self.max_tool_calls > 0 and self.tool_calls >= self.max_tool_calls:
            self.stopped_reason = (
                f"tool-call budget reached: {self.tool_calls}/{self.max_tool_calls}"
            )
            raise BudgetExceeded(self.stopped_reason)

    def snapshot(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "max_cost_usd": self.max_cost_usd,
            "spent_usd": round(self.spent_usd, 6),
            "remaining_usd": self.remaining_usd
            if math.isfinite(self.remaining_usd)
            else None,
            "max_tokens": self.max_tokens,
            "tokens_used": self.tokens_used,
            "tool_calls": self.tool_calls,
            "max_tool_calls": self.max_tool_calls,
            "exhausted": self.exhausted,
            "stopped_reason": self.stopped_reason,
        }


@dataclass
class CancellationToken:
    """Cooperative cancellation.

    The Director polls this between steps; a long provider call cannot be aborted
    mid-flight by the HTTP layer, so cancellation is checked at step boundaries
    and the run stops before starting new paid work.
    """

    cancelled: bool = False
    reason: str = ""
    _listeners: list[Any] = field(default_factory=list)

    def cancel(self, reason: str = "cancelled by operator") -> None:
        self.cancelled = True
        self.reason = reason

    def check(self) -> None:
        if self.cancelled:
            from app.core.errors import CancelledProblem

            raise CancelledProblem(self.reason or "Run cancelled.")


class RequestDeduplicator:
    """Prevents paying twice for an identical request within one run.

    Keyed on a caller-supplied logical key (e.g. ``research:competitor_analysis``)
    plus a hash of the prompt, so a retry loop cannot re-bill the same prompt.
    """

    def __init__(self) -> None:
        self._seen: dict[str, Any] = {}
        self.hits = 0
        self.misses = 0

    def key_for(self, logical_key: str, prompt: str) -> str:
        import hashlib

        digest = hashlib.sha256(f"{logical_key}\x00{prompt}".encode()).hexdigest()[:32]
        return f"{logical_key}:{digest}"

    def get(self, key: str) -> Any | None:
        value = self._seen.get(key)
        if value is not None:
            self.hits += 1
        else:
            self.misses += 1
        return value

    def put(self, key: str, value: Any) -> None:
        self._seen[key] = value

    def stats(self) -> dict[str, int]:
        return {"cache_hits": self.hits, "cache_misses": self.misses, "entries": len(self._seen)}


# --------------------------------------------------------------------------- #
# configuration-derived helpers
# --------------------------------------------------------------------------- #
def configured_models(settings: Settings | None = None) -> dict[str, Any]:
    """Describe the configured model tiering for ``/system/status``."""
    settings = settings or get_settings()
    primary = settings.llm_reasoning_model or ""
    auxiliary = settings.llm_auxiliary_model or ""
    primary_price, primary_verified = price_for_model(
        settings.llm_reasoning_provider or "openrouter", primary, settings
    )
    aux_price, aux_verified = price_for_model(
        settings.llm_auxiliary_provider or "openrouter", auxiliary, settings
    )
    return {
        "primary": {
            "provider": settings.llm_reasoning_provider,
            "model": primary,
            "input_per_1k_usd": primary_price.input_per_1k,
            "output_per_1k_usd": primary_price.output_per_1k,
            "price_verified": primary_verified,
        },
        "auxiliary": {
            "provider": settings.llm_auxiliary_provider,
            "model": auxiliary,
            "input_per_1k_usd": aux_price.input_per_1k,
            "output_per_1k_usd": aux_price.output_per_1k,
            "price_verified": aux_verified,
        },
        "cheap_task_classes": sorted(CHEAP_TASK_CLASSES),
        "reasoning_task_classes": sorted(REASONING_TASK_CLASSES),
        "default_strategy_budget_usd": settings.strategy_budget_usd,
        "default_research_budget_usd": settings.research_budget_usd,
        "note": (
            "Model ids and prices are configuration. Unverified prices use a "
            "conservative placeholder so a budget check fails before money is spent."
        ),
    }


def unverified_models(settings: Settings | None = None) -> Iterable[str]:
    """Model ids whose price we cannot verify — surfaced, never hidden."""
    settings = settings or get_settings()
    out = []
    for provider, model in (
        (settings.llm_reasoning_provider, settings.llm_reasoning_model),
        (settings.llm_auxiliary_provider, settings.llm_auxiliary_model),
    ):
        if not model:
            continue
        _price, verified = price_for_model(provider or "openrouter", model, settings)
        if not verified:
            out.append(model)
    return out


class RouterFailure(ProviderUnavailable):
    """Raised when no provider can serve a request."""


__all__ = [
    "BudgetGuard",
    "CHEAP_TASK_CLASSES",
    "CancellationToken",
    "CostEstimate",
    "REASONING_TASK_CLASSES",
    "RequestDeduplicator",
    "Route",
    "Tier",
    "configured_models",
    "estimate_cost",
    "estimate_prompt_tokens",
    "price_for_model",
    "tier_for_task",
    "unverified_models",
]
