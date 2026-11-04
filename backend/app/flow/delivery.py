"""Explicit transport-delivery classification (Phase 3C.2.3.2).

Before this module existed the coordinator treated *any* exception from
``transport.send()`` as proof that nothing was delivered, and refunded the budget
reservation accordingly. That is false for the production transport: once
``websocket.send()`` has been entered, an exception can mean a partial write, a
socket that closed after the frame moved, or a send whose acknowledgement never
arrived. Refunding an ambiguous send can pay for the same generation twice.

The classification below is deliberately three-valued, and the default for an
unclassified failure is the *unsafe-by-assumption* one — "may have been sent" —
so a transport that has not thought about delivery is never credited with a
refund it cannot justify.

* :attr:`DeliveryOutcome.DEFINITELY_NOT_SENT` — the transport can establish that
  the command was never handed to the socket (no frame bytes were written).
* :attr:`DeliveryOutcome.MAY_HAVE_BEEN_SENT` — the send operation was entered and
  delivery cannot be established either way.
* :attr:`DeliveryOutcome.SENT` — the send completed without error.

Nothing here proves what the *provider* did with a frame that did leave; that is
the coordinator's ``UNKNOWN_SUBMISSION`` concern, not this module's.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class DeliveryOutcome(str, Enum):
    """What a transport can honestly say about one send attempt."""

    DEFINITELY_NOT_SENT = "definitely_not_sent"
    MAY_HAVE_BEEN_SENT = "may_have_been_sent"
    SENT = "sent"


class DeliveryFailure(Exception):
    """A send that did not complete, carrying the classification it can support.

    The base class defaults to :attr:`DeliveryOutcome.MAY_HAVE_BEEN_SENT`: an
    exception raised by a transport is not, by itself, evidence that no bytes
    moved.
    """

    outcome: DeliveryOutcome = DeliveryOutcome.MAY_HAVE_BEEN_SENT

    def __init__(
        self,
        message: str,
        *,
        outcome: DeliveryOutcome | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if outcome is not None:
            self.outcome = outcome
        self.details = details or {}


class DefiniteNonDelivery(DeliveryFailure):
    """Raised only when the transport can *prove* the frame was never sent.

    This is the only failure that may release a budget reservation.
    """

    outcome = DeliveryOutcome.DEFINITELY_NOT_SENT


class DeliveryUncertain(DeliveryFailure):
    """Raised when the send was entered but delivery cannot be established."""

    outcome = DeliveryOutcome.MAY_HAVE_BEEN_SENT


def classify_send_result(result: Any) -> DeliveryOutcome:
    """Interpret what ``transport.send()`` returned.

    ``None`` is the historical contract — the coroutine completed, which means the
    send completed — and is still accepted so transports written before this module
    keep working. A transport may return a :class:`DeliveryOutcome` explicitly.
    """

    if result is None:
        return DeliveryOutcome.SENT
    if isinstance(result, DeliveryOutcome):
        return result
    # The call returned without raising: whatever it returned, the send it performed
    # completed. Anything else would be a transport that cannot state its own
    # contract, and "returned normally" is the only defensible reading.
    return DeliveryOutcome.SENT


def classify_send_exception(exc: BaseException) -> DeliveryOutcome:
    """Classify a raised send failure, defaulting to *ambiguous*.

    A typed :class:`DeliveryFailure` states its own outcome. Pre-send refusals in
    this codebase (``NotPaired``, ``CommandDenied``) declare the class attribute
    ``delivery``, because they are raised before any socket operation. Everything
    else — ``ConnectionError``, ``OSError``, ``RuntimeError``, a bare ``Exception``
    — is ambiguous by construction and must not release a reservation.
    """

    if isinstance(exc, DeliveryFailure):
        return exc.outcome
    declared = getattr(exc, "delivery", None)
    if isinstance(declared, DeliveryOutcome):
        return declared
    return DeliveryOutcome.MAY_HAVE_BEEN_SENT


class ProviderDispatchOutcome(str, Enum):
    """Whether the *extension* submitted a generation to the external provider.

    Phase 3C.2.3.3. This is a different event from :class:`DeliveryOutcome`, and the
    two were previously collapsed into one field: a provider ``dispatched:false``
    used to be recorded as transport ``DEFINITELY_NOT_SENT``, which rewrote history —
    the WebSocket frame *had* been delivered, and the extension may well have read it.

    ``UNKNOWN`` is the only safe default. A missing field, an absent response, a
    timeout, a cancellation or an uncorrelated result all leave it ``UNKNOWN``.
    """

    NOT_DISPATCHED = "not_dispatched"
    DISPATCHED = "dispatched"
    UNKNOWN = "unknown"


def classify_provider_dispatch(value: Any) -> ProviderDispatchOutcome:
    """Map an upstream ``dispatched`` flag to a provider-dispatch outcome.

    Only the two Python booleans carry meaning. ``None``, a string, a number or a
    missing key are all ``UNKNOWN`` — never ``NOT_DISPATCHED``, because an absent
    field is not evidence that nothing was submitted.
    """

    if value is True:
        return ProviderDispatchOutcome.DISPATCHED
    if value is False:
        return ProviderDispatchOutcome.NOT_DISPATCHED
    return ProviderDispatchOutcome.UNKNOWN


class DispatchFailed(DeliveryFailure):
    """A ``FlowBridge.dispatch()`` that produced no result, carrying both states.

    ``delivery`` describes Crusor → extension; ``provider`` describes extension →
    provider. They are independent, and a failure after a successful transport send
    must never be reported as ``DEFINITELY_NOT_SENT``.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "flow_command_failed",
        delivery: DeliveryOutcome = DeliveryOutcome.MAY_HAVE_BEEN_SENT,
        provider: ProviderDispatchOutcome = ProviderDispatchOutcome.UNKNOWN,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, outcome=delivery, details=details)
        self.code = code
        self.provider = provider
