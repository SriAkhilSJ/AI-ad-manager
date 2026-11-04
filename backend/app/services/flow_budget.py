"""Flow generation spend controls (Phase 3C.2.3).

Phase 3C.2.2's live-generation guard took ``budget_configured=True`` from its caller.
That recorded an assumption. This module supplies the thing the guard should have
been asking: an actual, enforceable cap for Flow generation, with a reservation the
dispatch path consults before a command exists.

Two rules carried over from the rest of the product:

* a zero or missing cap is not "unlimited" — it blocks every billable request;
* the check happens *before* a request is built, so a blocked generation cannot
  leave a half-dispatch behind.

Nothing here contacts a provider, and the ledger starts empty on every process
start: a reservation is only meaningful for the process that would spend it.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any

#: Conservative per-request estimates, in USD. Deliberately pessimistic: the guard
#: is a safety limit, not an accounting system.
FLOW_IMAGE_ESTIMATE_USD = 0.04
FLOW_VIDEO_ESTIMATE_USD = 0.35


class BudgetUnavailable(Exception):
    """No enforceable budget exists, so the request must not proceed."""

    code = "flow_budget_unavailable"


class InvalidBudgetCap(ValueError):
    """The configured cap is not a number a spend limit can be built from.

    Phase 3C.2.3.1: ``cap_usd > 0`` accepted ``float("inf")``, and an infinite cap
    is not a limit. NaN, both infinities, zero and negative values are refusals, not
    "unlimited".
    """

    code = "flow_budget_cap_invalid"


def validate_cap(value: Any) -> float:
    """Return a finite, positive cap or raise. Never coerces a bad value to zero."""
    try:
        cap = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidBudgetCap(f"{value!r} is not a number.") from exc
    if not math.isfinite(cap):
        raise InvalidBudgetCap(f"{cap!r} is not finite; a spend limit must be.")
    if cap <= 0:
        raise InvalidBudgetCap(f"{cap!r} is not positive; zero is never 'unlimited'.")
    return cap


@dataclass
class FlowBudgetLedger:
    """A cap plus the reservations made against it.

    Thread-safe: ``reserve`` and ``release`` hold a lock, so two concurrent
    submissions cannot both observe the same remaining balance and overspend it.
    """

    cap_usd: float = 0.0
    spent_usd: float = 0.0
    reservations: int = 0
    opened_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.cap_usd = validate_cap(self.cap_usd)

    @property
    def enforceable(self) -> bool:
        return math.isfinite(self.cap_usd) and self.cap_usd > 0

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.spent_usd)

    def estimate(self, kind: str) -> float:
        return FLOW_VIDEO_ESTIMATE_USD if kind == "video" else FLOW_IMAGE_ESTIMATE_USD

    def can_spend(self, estimate_usd: float) -> bool:
        if not self.enforceable or estimate_usd <= 0:
            return False
        return estimate_usd <= self.remaining_usd

    def reserve(self, *, kind: str) -> float:
        """Reserve one attempt atomically. Raises when the cap cannot cover it."""
        estimate = self.estimate(kind)
        with self._lock:
            if not self.can_spend(estimate):
                raise BudgetUnavailable(
                    f"The Flow budget cannot cover this {kind} request "
                    f"(${estimate:.4f} against ${self.remaining_usd:.4f} remaining)."
                )
            self.spent_usd += estimate
            self.reservations += 1
        return estimate

    def release(self, estimate_usd: float) -> None:
        """Return a reservation when the attempt provably never left the machine.

        Callers must not use this after a dispatch: once a request may have reached
        the provider, the money may already be spent and the reservation must stay.
        """
        amount = float(estimate_usd)
        if not math.isfinite(amount) or amount <= 0:
            return
        with self._lock:
            self.spent_usd = max(0.0, self.spent_usd - amount)
            self.reservations = max(0, self.reservations - 1)

    def snapshot(self) -> dict[str, Any]:
        return {
            "cap_usd": round(self.cap_usd, 4),
            "spent_usd": round(self.spent_usd, 4),
            "remaining_usd": round(self.remaining_usd, 4),
            "reservations": self.reservations,
            "enforceable": self.enforceable,
        }


def flow_ledger_from_settings(settings: Any) -> FlowBudgetLedger | None:
    """Build the ledger the deployment actually configured, or ``None``.

    ``FLOW_GENERATION_BUDGET_USD`` defaults to 0 and ``FLOW_BUDGET_ENFORCEMENT``
    defaults to ``none``: with no ledger configured, the live-generation guard
    reports ``budget_controls_missing`` and generation stays blocked.
    """
    enforcement = str(getattr(settings, "flow_budget_enforcement", "none") or "none").strip().lower()
    if enforcement != "flow_budget_ledger":
        return None
    try:
        # A cap that is NaN, infinite, zero or negative is not a control. It is
        # reported as "no budget" rather than as a limit that can never be reached.
        cap = validate_cap(getattr(settings, "flow_generation_budget_usd", 0.0))
    except InvalidBudgetCap:
        return None
    return FlowBudgetLedger(cap_usd=cap)
