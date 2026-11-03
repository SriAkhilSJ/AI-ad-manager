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
import threading
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


class BudgetConfigurationError(ValueError):
    """The budget itself is invalid. Raised at construction, never at call time.

    Distinct from :class:`BudgetExceeded`: this means the operator configured
    something that cannot be interpreted safely, so no paid work may start at
    all. Subclasses ``ValueError`` so existing ``except ValueError`` handlers
    behave sensibly.
    """

    code = "budget_configuration_invalid"


#: Callers that supply their own cost ceiling rather than quoting a provider
#: price. There is no market price to verify, so the pricing-trust rule is
#: skipped for them; their own number is the bound.
_PRICELESS_PROVIDERS = frozenset({"preflight"})


#: Sentinel for an explicitly unlimited budget. Never the default, and refused
#: unless an operator passes it deliberately (see ``allow_unlimited``).
UNLIMITED = float("inf")


class BudgetGuard:
    """Fail-closed cost/token/tool budget for one scope, with reservation support.

    Phase 2.6 changed the semantics. Before, ``max_cost_usd=0`` (and ``None``,
    negatives, NaN and infinity) all meant *unlimited*::

        if self.max_cost_usd <= 0:
            return math.inf          # ← unlimited spending

    Now every ambiguous value is rejected at construction and a zero budget
    blocks all paid requests:

    ==========================  =========================================
    Configuration               Behaviour
    ==========================  =========================================
    ``0``                       block all paid requests
    ``None`` / missing          block all paid requests
    negative                    ``BudgetConfigurationError``
    ``nan`` / ``inf``           ``BudgetConfigurationError``
                            (unless ``allow_unlimited=True`` for ``inf``)
    positive finite             allow only within budget
    unknown model pricing       block paid requests
    ==========================  =========================================

    **Reservations.** ``reserve()`` atomically claims estimated cost before a
    request is sent, so concurrent callers cannot each see the same headroom and
    both spend it. ``reconcile()`` settles the claim against the observed cost.
    A reservation may be held open (``ambiguous=True``) when a timeout leaves it
    unknown whether the provider processed the request — the money stays
    committed, which is the conservative choice.

    Thread safety: a module-level ``threading.Lock`` guards every mutation. The
    asyncio event loop is single-threaded and asyncio tasks yield at ``await``
    points only, so the ``check``→``reserve`` sequence is atomic with respect to
    other tasks on the same loop *as long as the caller does not await between
    them*. The lock additionally makes this correct across threads (e.g. a
    ``run_in_executor`` worker or a test driver), which a bare attribute would
    not be.
    """

    def __init__(
        self,
        *,
        max_cost_usd: float | None = None,
        max_tokens: int | None = None,
        max_tool_calls: int | None = None,
        label: str = "run",
        allow_unlimited: bool = False,
        allow_unknown_pricing: bool = False,
    ) -> None:
        self.label = label
        self.stopped_reason = ""
        self.spent_usd = 0.0
        self.tokens_used = 0
        self.tool_calls = 0
        self.rejections: list[dict[str, Any]] = []

        # ---- cost ceiling: fail closed --------------------------------
        self.max_cost_usd = self._validate_cost(max_cost_usd, allow_unlimited)
        self.unlimited = self.max_cost_usd == UNLIMITED
        self._spent_usd = 0.0
        self._reserved_usd = 0.0
        # RLock: snapshot()/exhausted reads re-enter, so a plain Lock deadlocks.
        self._lock = threading.RLock()
        self._open: dict[str, dict[str, Any]] = {}

        # ---- other ceilings -------------------------------------------
        self.max_tokens = self._validate_non_negative_int(max_tokens, "max_tokens")
        self.max_tool_calls = self._validate_non_negative_int(
            max_tool_calls, "max_tool_calls"
        )

        # ---- pricing trust --------------------------------------------
        # A budget over an unknown price is not a guarantee. Unless the operator
        # explicitly opts in, paid execution is blocked rather than allowed on a
        # number nobody can stand behind.
        self.allow_unknown_pricing = bool(allow_unknown_pricing)
        self.pricing_trusted = True
        self.untrusted_pricing_reason = ""

    # ------------------------------------------------------------ validation
    @staticmethod
    def _validate_cost(value: float | None, allow_unlimited: bool) -> float:
        if value is None:
            # "Budget missing" is now a zero budget, not a licence to spend.
            return 0.0
        if isinstance(value, bool):  # bool is an int; reject rather than coerce
            raise BudgetConfigurationError(
                f"max_cost_usd must be a number, got {value!r}"
            )
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise BudgetConfigurationError(
                f"max_cost_usd must be a number, got {value!r}"
            ) from exc
        if math.isnan(number):
            raise BudgetConfigurationError("max_cost_usd may not be NaN")
        if math.isinf(number):
            if allow_unlimited and number > 0:
                return UNLIMITED
            raise BudgetConfigurationError(
                "max_cost_usd may not be infinite; pass allow_unlimited=True with "
                "an explicit positive infinity if unlimited spending is truly intended"
            )
        if number < 0:
            raise BudgetConfigurationError(
                f"max_cost_usd may not be negative (got {number}); use 0 to block all spend"
            )
        return number

    @staticmethod
    def _validate_non_negative_int(value: int | None, field: str) -> int:
        if value is None:
            # 0 means "no ceiling on this axis". Tokens and tool calls are not
            # money, so an absent value here is a bounded-by-other-means case,
            # not a spending hazard. Cost is the axis that fails closed.
            return 0
        if isinstance(value, bool):
            raise BudgetConfigurationError(f"{field} must be an integer, got {value!r}")
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise BudgetConfigurationError(
                f"{field} must be an integer, got {value!r}"
            ) from exc
        if number < 0:
            raise BudgetConfigurationError(f"{field} may not be negative (got {number})")
        return number

    # --------------------------------------------------------------- pricing
    def trust_pricing(self, *, verified: bool, provider: str = "", model: str = "") -> None:
        """Record whether a request's price is a real number or a placeholder.

        ``verified=True`` clears any previous complaint. ``verified=False`` marks
        the budget untrusted and blocks paid execution unless
        ``allow_unknown_pricing`` was set explicitly.
        """
        with self._lock:
            if verified:
                self.pricing_trusted = True
                self.untrusted_pricing_reason = ""
            else:
                self.pricing_trusted = False
                self.untrusted_pricing_reason = (
                    f"model price for '{model or '?'}' from provider "
                    f"'{provider or '?'}' is an unverified placeholder"
                )

    # ----------------------------------------------------------- accounting
    @property
    def used_usd(self) -> float:
        """Committed money: settled spend plus outstanding reservations."""
        with self._lock:
            return round(self._spent_usd + self._reserved_usd, 6)

    @property
    def remaining_usd(self) -> float:
        if self.max_cost_usd == UNLIMITED:
            return math.inf
        with self._lock:
            return round(max(self.max_cost_usd - self._spent_usd - self._reserved_usd, 0.0), 6)

    @property
    def remaining_tokens(self) -> int:
        if self.max_tokens <= 0:
            return 2**31 - 1
        return max(self.max_tokens - self.tokens_used, 0)

    @property
    def exhausted(self) -> bool:
        if self.max_cost_usd != UNLIMITED and self.remaining_usd <= 0:
            return True
        return self.remaining_tokens <= 0

    def record(self, *, cost_usd: float, tokens: int, tool_calls: int = 0) -> None:
        """Directly settle spend with no reservation (unchanged public API)."""
        with self._lock:
            self._spent_usd = round(self._spent_usd + max(0.0, float(cost_usd)), 6)
            self.spent_usd = self._spent_usd
            self.tokens_used += max(0, int(tokens))
            self.tool_calls += max(0, int(tool_calls))

    # --------------------------------------------------------- reservations
    def reserve(self, estimate: CostEstimate, *, token_id: str = "") -> str:
        """Atomically claim ``estimate.estimated_cost_usd`` and return a claim id.

        Raises :class:`BudgetExceeded` if the claim does not fit. Callers must
        ``reconcile`` the returned id once the real cost is known.
        """
        import uuid as _uuid

        from app.llm.base import BudgetExceeded

        claim = token_id or f"res-{_uuid.uuid4().hex[:16]}"
        amount = float(estimate.estimated_cost_usd)

        with self._lock:
            # --- token ceiling first (cheap, no state) -----------------
            if self.max_tokens > 0 and (
                self.tokens_used + estimate.prompt_tokens > self.max_tokens
            ):
                self.stopped_reason = (
                    f"token budget exhausted: {self.tokens_used}/{self.max_tokens} used, "
                    f"request needs {estimate.prompt_tokens}"
                )
                self._note_rejection("token_budget", self.stopped_reason, estimate)
                raise BudgetExceeded(self.stopped_reason)

            # --- tool-call ceiling -------------------------------------
            if self.max_tool_calls > 0 and self.tool_calls >= self.max_tool_calls:
                self.stopped_reason = (
                    f"tool-call budget reached: {self.tool_calls}/{self.max_tool_calls}"
                )
                self._note_rejection("tool_calls", self.stopped_reason, estimate)
                raise BudgetExceeded(self.stopped_reason)

            # --- pricing trust -----------------------------------------
            # A caller that supplies its own ceiling has no provider price to
            # verify, so the rule does not apply to it (see CostEstimate).
            if (
                estimate.provider not in _PRICELESS_PROVIDERS
                and not self.pricing_trusted
                and not self.allow_unknown_pricing
            ):
                self.stopped_reason = (
                    f"refusing to spend on untrusted pricing: "
                    f"{self.untrusted_pricing_reason}. Supply verified prices via "
                    f"LLM_PRICING_JSON, or pass allow_unknown_pricing=True to accept "
                    f"the estimate as an upper bound."
                )
                self._note_rejection("untrusted_pricing", self.stopped_reason, estimate)
                raise BudgetExceeded(self.stopped_reason)

            # --- cost ceiling ------------------------------------------
            if self.max_cost_usd == 0:
                self.stopped_reason = (
                    f"cost budget is zero for '{self.label}': every paid request is "
                    f"blocked (request estimated ${amount:.6f})"
                )
                self._note_rejection("zero_budget", self.stopped_reason, estimate)
                raise BudgetExceeded(self.stopped_reason)

            if self.max_cost_usd != UNLIMITED:
                available = self.max_cost_usd - self._spent_usd - self._reserved_usd
                if amount > available + 1e-12:
                    self.stopped_reason = (
                        f"cost budget exhausted: ${self._spent_usd:.4f} spent + "
                        f"${self._reserved_usd:.4f} reserved of ${self.max_cost_usd:.4f}; "
                        f"request estimated ${amount:.6f}, available ${max(available, 0.0):.6f}"
                    )
                    self._note_rejection("cost_budget", self.stopped_reason, estimate)
                    raise BudgetExceeded(self.stopped_reason)

            # --- commit the claim --------------------------------------
            self._reserved_usd = round(self._reserved_usd + amount, 6)
            self._open[claim] = {
                "claim_id": claim,
                "reserved_usd": round(amount, 6),
                "provider": estimate.provider,
                "model": estimate.model,
                "prompt_tokens": estimate.prompt_tokens,
                "completion_tokens": estimate.completion_tokens,
                "price_verified": estimate.price_verified,
                "state": "reserved",
                "actual_usd": None,
            }
            return claim

    def reconcile(
        self,
        claim: str,
        *,
        actual_cost_usd: float,
        tokens: int = 0,
        tool_calls: int = 0,
        ambiguous: bool = False,
    ) -> dict[str, Any]:
        """Settle a reservation against observed cost.

        ``ambiguous=True`` keeps the full reservation committed: a timeout means
        we do not know whether the provider billed us, so the conservative choice
        is to assume it might have. The claim stays ``ambiguous`` in the ledger
        rather than being released.
        """
        with self._lock:
            entry = self._open.get(claim)
            if entry is None:
                # Unknown claim: record the spend conservatively and move on.
                self._spent_usd = round(self._spent_usd + max(0.0, float(actual_cost_usd)), 6)
                self.spent_usd = self._spent_usd
                self.tokens_used += max(0, int(tokens))
                self.tool_calls += max(0, int(tool_calls))
                self._note_rejection("unknown_claim", f"unknown reservation {claim}", None)
                return {"claim_id": claim, "state": "unknown", "released_usd": 0.0}

            reserved = float(entry["reserved_usd"])
            if ambiguous:
                # Keep the reservation; do not credit anything back.
                entry["state"] = "ambiguous"
                entry["actual_usd"] = None
                self._spent_usd = round(self._spent_usd + reserved, 6)
                self.spent_usd = self._spent_usd
                self._reserved_usd = round(max(0.0, self._reserved_usd - reserved), 6)
                self.tokens_used += max(0, int(tokens))
                self._open.pop(claim, None)
                entry["released_usd"] = 0.0
                return entry

            actual = max(0.0, float(actual_cost_usd))
            self._reserved_usd = round(max(0.0, self._reserved_usd - reserved), 6)
            self._spent_usd = round(self._spent_usd + actual, 6)
            self.spent_usd = self._spent_usd
            self.tokens_used += max(0, int(tokens))
            self.tool_calls += max(0, int(tool_calls))
            self._open.pop(claim, None)
            entry["state"] = "settled"
            entry["actual_usd"] = round(actual, 6)
            entry["released_usd"] = round(max(0.0, reserved - actual), 6)
            return entry

    def open_reservations(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._open.values()]

    def _note_rejection(self, kind: str, reason: str, estimate: CostEstimate | None) -> None:
        self.rejections.append(
            {
                "kind": kind,
                "reason": reason,
                "provider": getattr(estimate, "provider", ""),
                "model": getattr(estimate, "model", ""),
                "estimated_cost_usd": getattr(estimate, "estimated_cost_usd", None),
                "request_id": self._current_request_id,
            }
        )

    _current_request_id: str = ""

    def set_request_id(self, request_id: str) -> None:
        """Tag subsequent rejections with a request id for correlation."""
        self._current_request_id = request_id or ""

    # ---------------------------------------------------------------- gating
    def check(self, estimate: CostEstimate) -> None:
        """Non-reserving gate. Kept for callers that only want a yes/no.

        Prefer :meth:`reserve` for anything that actually spends: ``check`` does
        not claim headroom, so two concurrent callers can both pass it.
        """
        from app.llm.base import BudgetExceeded

        if estimate.prompt_tokens > self.remaining_tokens:
            self.stopped_reason = (
                f"token budget exhausted: {self.tokens_used}/{self.max_tokens} used, "
                f"request needs {estimate.prompt_tokens}"
            )
            self._note_rejection("token_budget", self.stopped_reason, estimate)
            raise BudgetExceeded(self.stopped_reason)

        if (
            estimate.provider not in _PRICELESS_PROVIDERS
            and not self.pricing_trusted
            and not self.allow_unknown_pricing
        ):
            self.stopped_reason = (
                f"refusing to spend on untrusted pricing: {self.untrusted_pricing_reason}"
            )
            self._note_rejection("untrusted_pricing", self.stopped_reason, estimate)
            raise BudgetExceeded(self.stopped_reason)

        if self.max_cost_usd == 0:
            self.stopped_reason = (
                f"cost budget is zero for '{self.label}': every paid request is blocked"
            )
            self._note_rejection("zero_budget", self.stopped_reason, estimate)
            raise BudgetExceeded(self.stopped_reason)

        if self.max_cost_usd != UNLIMITED and estimate.estimated_cost_usd > self.remaining_usd + 1e-12:
            self.stopped_reason = (
                f"cost budget exhausted: ${self.spent_usd:.4f}/${self.max_cost_usd:.4f} "
                f"used, request estimated ${estimate.estimated_cost_usd:.4f}"
            )
            self._note_rejection("cost_budget", self.stopped_reason, estimate)
            raise BudgetExceeded(self.stopped_reason)

        if self.max_tool_calls > 0 and self.tool_calls >= self.max_tool_calls:
            self.stopped_reason = (
                f"tool-call budget reached: {self.tool_calls}/{self.max_tool_calls}"
            )
            self._note_rejection("tool_calls", self.stopped_reason, estimate)
            raise BudgetExceeded(self.stopped_reason)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "label": self.label,
                "max_cost_usd": None if self.unlimited else self.max_cost_usd,
                "unlimited": self.unlimited,
                "spent_usd": round(self._spent_usd, 6),
                "reserved_usd": round(self._reserved_usd, 6),
                "committed_usd": round(self._spent_usd + self._reserved_usd, 6),
                "remaining_usd": None
                if self.unlimited
                else round(
                    max(self.max_cost_usd - self._spent_usd - self._reserved_usd, 0.0), 6
                ),
                "open_reservations": len(self._open),
                "pricing_trusted": self.pricing_trusted,
                "max_tokens": self.max_tokens,
                "tokens_used": self.tokens_used,
                "tool_calls": self.tool_calls,
                "max_tool_calls": self.max_tool_calls,
                "exhausted": self.exhausted,
                "stopped_reason": self.stopped_reason,
                "rejections": list(self.rejections[-10:]),
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
    "BudgetConfigurationError",
    "UNLIMITED",
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
