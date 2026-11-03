"""Bounded execution: hard stops for research and strategy runs.

Every Phase 2 agent run is wrapped in an :class:`ExecutionGuard`. The guard is
the single place that answers "should this run continue?", and it answers with a
typed error rather than letting a loop spin:

* ``max_iterations``     — research/evaluate cycles
* ``max_tool_calls``     — provider searches + model calls
* ``max_wall_seconds``   — wall clock
* ``max_cost_usd``       — delegated to :class:`BudgetGuard`
* ``max_tokens``         — delegated to :class:`BudgetGuard`
* loop detection         — the same logical step repeated too often is a bug,
                           not progress

The guard never raises a bare exception: callers can distinguish a budget stop
from a loop from a cancellation from a timeout, which is what makes structured
failure handling possible.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import (
    BudgetExhaustedError,
    CancelledProblem,
    LoopDetectedError,
)
from app.llm.base import BudgetExceeded
from app.llm.routing import BudgetGuard, CancellationToken


class ExecutionLimitExceeded(RuntimeError):
    """A non-budget execution limit (iterations, tool calls, wall clock) tripped."""

    code = "execution_limit_exceeded"

    def __init__(self, message: str, *, limit: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.limit = limit
        self.details = details or {}


@dataclass
class ExecutionLimits:
    max_iterations: int = 6
    max_tool_calls: int = 24
    max_wall_seconds: int = 240
    max_cost_usd: float = 0.12
    max_tokens: int = 20_000
    max_repeats: int = 3
    label: str = "run"
    #: Phase 2.6 — whether an unverified (placeholder) model price may be spent
    #: against. Defaults to False so a misconfigured price cannot silently
    #: under-reserve; the agents copy this from settings.
    allow_unknown_pricing: bool = False

    @classmethod
    def for_research(cls, settings: Any) -> "ExecutionLimits":
        return cls(
            max_iterations=settings.research_max_iterations,
            max_tool_calls=settings.research_max_tool_calls,
            max_wall_seconds=settings.research_max_wall_seconds,
            max_cost_usd=settings.research_budget_usd,
            max_tokens=getattr(settings, "agent_max_tokens_per_run", 20_000),
            label="research",
            allow_unknown_pricing=bool(
                getattr(settings, "llm_allow_unverified_pricing", False)
            ),
        )

    @classmethod
    def for_content(cls, settings: Any) -> "ExecutionLimits":
        """Bounds for one weekly content plan (Phase 3A)."""
        return cls(
            max_iterations=int(getattr(settings, "content_max_slots", 12)),
            max_tool_calls=int(getattr(settings, "content_max_tool_calls", 40)),
            max_wall_seconds=int(getattr(settings, "content_max_wall_seconds", 300)),
            max_cost_usd=float(getattr(settings, "content_budget_usd", 0.06)),
            max_tokens=int(getattr(settings, "content_max_tokens", 40_000)),
            label="content",
            allow_unknown_pricing=bool(
                getattr(settings, "llm_allow_unverified_pricing", False)
            ),
        )

    @classmethod
    def for_strategy(cls, settings: Any) -> "ExecutionLimits":
        return cls(
            max_iterations=settings.strategy_max_iterations,
            max_tool_calls=settings.strategy_max_tool_calls,
            max_wall_seconds=settings.strategy_max_wall_seconds,
            max_cost_usd=settings.strategy_budget_usd,
            max_tokens=getattr(settings, "agent_max_tokens_per_run", 20_000),
            label="strategy",
            allow_unknown_pricing=bool(
                getattr(settings, "llm_allow_unverified_pricing", False)
            ),
        )


@dataclass
class ExecutionGuard:
    """Tracks and enforces every bound for one run."""

    limits: ExecutionLimits
    #: Optional externally-owned budget. When supplied (the Director passes one
    #: per node so research/strategy share one scope) the guard adopts it instead
    #: of creating a private one, so primary and auxiliary model calls draw from
    #: the same reservation ledger.
    shared_budget: BudgetGuard | None = None
    budget: BudgetGuard = field(init=False)
    cancel: CancellationToken = field(default_factory=CancellationToken)
    started_at: float = field(default_factory=time.monotonic)
    iterations: int = 0
    tool_calls: int = 0
    stop_reason: str = ""
    _signatures: dict[str, int] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.shared_budget is not None:
            # Adopt, don't recreate: a second guard would give the same run two
            # independent spend counters and defeat the reservation guarantee.
            self.budget = self.shared_budget
            self.budget.allow_unknown_pricing = self.limits.allow_unknown_pricing
            return
        self.budget = BudgetGuard(
            max_cost_usd=self.limits.max_cost_usd,
            max_tokens=self.limits.max_tokens,
            max_tool_calls=self.limits.max_tool_calls,
            label=self.limits.label,
            allow_unknown_pricing=self.limits.allow_unknown_pricing,
        )

    # ------------------------------------------------------------------ checks
    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def exhausted(self) -> bool:
        return bool(self.stop_reason)

    def _log(self, event: str, **fields: Any) -> None:
        self.events.append({"event": event, "at": round(self.elapsed_seconds, 3), **fields})

    def check_time(self) -> None:
        if self.elapsed_seconds > self.limits.max_wall_seconds:
            self.stop_reason = "wall_clock_exhausted"
            self._log("stop", reason=self.stop_reason)
            raise ExecutionLimitExceeded(
                f"{self.limits.label} exceeded its {self.limits.max_wall_seconds}s wall-clock "
                "budget.",
                limit="wall_clock",
                details=self.snapshot(),
            )

    def check_cancelled(self) -> None:
        self.cancel.check()

    # -------------------------------------------------------------- iteration
    def begin_iteration(self) -> int:
        """Start one research/evaluation cycle. Raises when the bound is hit."""
        self.check_cancelled()
        self.check_time()
        if self.iterations >= self.limits.max_iterations:
            self.stop_reason = "max_iterations"
            self._log("stop", reason=self.stop_reason)
            raise ExecutionLimitExceeded(
                f"{self.limits.label} reached its iteration limit "
                f"({self.limits.max_iterations}).",
                limit="max_iterations",
                details=self.snapshot(),
            )
        self.iterations += 1
        self._log("iteration", n=self.iterations)
        return self.iterations

    # --------------------------------------------------------------- tool use
    def note_tool_call(self, name: str, *, cost_usd: float = 0.0, tokens: int = 0) -> None:
        self.check_time()
        if self.tool_calls >= self.limits.max_tool_calls:
            self.stop_reason = "max_tool_calls"
            self._log("stop", reason=self.stop_reason)
            raise ExecutionLimitExceeded(
                f"{self.limits.label} reached its tool-call limit "
                f"({self.limits.max_tool_calls}).",
                limit="max_tool_calls",
                details=self.snapshot(),
            )
        self.tool_calls += 1
        if cost_usd or tokens:
            self.budget.record(cost_usd=cost_usd, tokens=tokens)
        self._log("tool_call", name=name, n=self.tool_calls)

    def note_cost(self, *, cost_usd: float = 0.0, tokens: int = 0) -> None:
        self.budget.record(cost_usd=cost_usd, tokens=tokens)

    def note_usage(self, **kwargs: Any) -> None:
        """Record provider spend that arrived without a tool-call increment."""
        self.budget.record(**kwargs)

    # ----------------------------------------------------------- loop control
    def note_signature(self, signature: str) -> None:
        """Detect a repeated logical step.

        Repeating the *same* query is normal research refinement up to a point;
        beyond ``max_repeats`` it means the loop is not converging and must stop
        rather than burn budget.
        """
        count = self._signatures.get(signature, 0) + 1
        self._signatures[signature] = count
        if count > self.limits.max_repeats:
            self.stop_reason = "loop_detected"
            self._log("stop", reason=self.stop_reason, signature=signature[:120])
            raise LoopDetectedError(
                f"{self.limits.label} repeated the same step {count} times without "
                "progress; stopping instead of looping.",
                details={"signature": signature[:200], **self.snapshot()},
            )

    # ---------------------------------------------------------------- results
    def check_budget_before_call(
        self,
        *,
        prompt_tokens: int = 1,
        estimated_cost_usd: float = 0.0,
        completion_tokens: int = 0,
    ) -> None:
        """Delegates to the budget guard; raises before the paid call."""
        from app.llm.routing import CostEstimate

        estimate = CostEstimate(
            provider="preflight",
            model=None,
            tier="default",
            task_class="",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            input_per_1k=0.0,
            output_per_1k=0.0,
            estimated_cost_usd=estimated_cost_usd,
            price_verified=False,
        )
        try:
            self.budget.check(estimate)
        except BudgetExceeded as exc:
            self.stop_reason = "budget_exhausted"
            self._log("stop", reason=self.stop_reason)
            raise BudgetExhaustedError(
                str(exc),
                details={**self.snapshot(), "budget": self.budget.snapshot()},
            ) from exc

    def snapshot(self) -> dict[str, Any]:
        budget = self.budget.snapshot()
        return {
            "label": self.limits.label,
            "iterations": self.iterations,
            "max_iterations": self.limits.max_iterations,
            "tool_calls": self.tool_calls,
            "max_tool_calls": self.limits.max_tool_calls,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "max_wall_seconds": self.limits.max_wall_seconds,
            "spent_usd": budget["spent_usd"],
            "max_cost_usd": budget["max_cost_usd"],
            "remaining_usd": budget["remaining_usd"],
            "tokens_used": budget["tokens_used"],
            "max_tokens": budget["max_tokens"],
            "exhausted": self.exhausted or budget["exhausted"],
            "stop_reason": self.stop_reason or budget.get("stopped_reason", ""),
        }


def classify_failure(exc: BaseException) -> tuple[str, str, str]:
    """Map an exception to ``(status, error_code, user_message)``.

    Structured failure handling: the API and the UI never have to guess what
    went wrong, and a budget stop is distinguishable from a provider outage.
    """
    from app.agents.base import AgentBudgetExceeded, AgentTimeout
    from app.core.errors import ResearchUnavailableError

    if isinstance(exc, (BudgetExhaustedError, AgentBudgetExceeded, BudgetExceeded)):
        return ("budget_exhausted", "budget_exhausted", str(exc))
    if isinstance(exc, CancelledProblem):
        return ("cancelled", "cancelled", "The run was cancelled by the founder.")
    if isinstance(exc, LoopDetectedError):
        return ("failed", "loop_detected", str(exc))
    if isinstance(exc, ExecutionLimitExceeded):
        return (
            "partial",
            exc.limit,
            f"Stopped at the {exc.limit} limit; results so far are saved.",
        )
    if isinstance(exc, ResearchUnavailableError):
        return ("unavailable", "research_unavailable", str(exc))
    if isinstance(exc, AgentTimeout):
        return ("failed", "timeout", str(exc))
    return ("failed", "internal_error", f"{type(exc).__name__}: {exc}"[:500])


__all__ = [
    "ExecutionGuard",
    "ExecutionLimitExceeded",
    "ExecutionLimits",
    "classify_failure",
]
