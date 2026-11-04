"""Phase 3C.2.3.3 — cancellation and end-to-end delivery semantics.

Two defects from the independent review of Phase 3C.2.3.2:

1. a cancellation that landed inside a socket send left the job ``PENDING``, its
   reservation tracked, and its pending future dangling — and the code could not
   see it, because ``except Exception`` never catches ``asyncio.CancelledError``;
2. transport delivery and provider dispatch were collapsed into one field, so an
   extension answering ``dispatched: false`` was recorded as transport
   ``DEFINITELY_NOT_SENT`` — rewriting the history of a frame that had, in fact,
   been delivered to the extension.

Everything is synthetic: in-memory transports, a fake socket, an httpx mock for the
companion. No DeepX source, no browser, no Google account, no paid call.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.flow import protocol as proto
from app.flow.bridge import BridgeConfig, ExtensionSession, FlowBridge
from app.flow.companion import LocalFlowCompanion
from app.flow.coordinator import (
    ApprovalRequired,
    CommandTransport,
    GenerationCoordinator,
    UnknownSubmissionRemains,
)
from app.flow.delivery import (
    DefiniteNonDelivery,
    DeliveryOutcome,
    DispatchFailed,
    ProviderDispatchOutcome,
    classify_provider_dispatch,
    classify_send_exception,
)
from app.flow.security import generate_pairing_id, generate_pairing_secret
from app.schemas.flow import CommandResultIn
from app.services.flow_budget import FlowBudgetLedger

from tests.test_phase3c231_security_hardening import _request, _release, _verified_decision

IMAGE_ESTIMATE = 0.04


@pytest.fixture()
def release(tmp_path: Path) -> dict[str, Any]:
    return _release(tmp_path)


# --------------------------------------------------------------------------- #
# transports
# --------------------------------------------------------------------------- #
class Scripted(CommandTransport):
    """Answers from a script; stamps the awaited command id onto its answers."""

    def __init__(self, results: list[Any] | None = None, *, delay: float = 0.0) -> None:
        self.sent: list[Any] = []
        self._results = list(results or [])
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self.delay = delay

    async def send(self, envelope: Any) -> DeliveryOutcome:
        self.sent.append(envelope)
        return DeliveryOutcome.SENT

    async def await_result(self, command_id: str, timeout: float) -> Any:
        if self.delay:
            await asyncio.sleep(self.delay)
        outcome = self._results.pop(0) if self._results else asyncio.TimeoutError()
        if isinstance(outcome, BaseException):
            raise outcome
        if getattr(outcome, "id", command_id) != command_id:
            outcome = outcome.model_copy(update={"id": command_id})
        return outcome


class Hanging(CommandTransport):
    """Never answers: the coordinator stays inside ``await_result``."""

    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.entered = asyncio.Event()

    async def send(self, envelope: Any) -> DeliveryOutcome:
        self.sent.append(envelope)
        return DeliveryOutcome.SENT

    async def await_result(self, command_id: str, timeout: float) -> Any:
        self.entered.set()
        await asyncio.Event().wait()


class CancelOnSend(CommandTransport):
    """Raises ``CancelledError`` from inside the send."""

    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, envelope: Any) -> DeliveryOutcome:
        self.attempts += 1
        raise asyncio.CancelledError()

    async def await_result(self, command_id: str, timeout: float) -> Any:  # pragma: no cover
        raise AssertionError("await_result must not be reached")


class NotDelivered(CommandTransport):
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, envelope: Any) -> DeliveryOutcome:
        raise DefiniteNonDelivery("the session was never authenticated")

    async def await_result(self, command_id: str, timeout: float) -> Any:  # pragma: no cover
        raise AssertionError("await_result must not be reached")


class Exploding(CommandTransport):
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def send(self, envelope: Any) -> DeliveryOutcome:
        raise self.error

    async def await_result(self, command_id: str, timeout: float) -> Any:  # pragma: no cover
        raise AssertionError("await_result must not be reached")


def _ok(media_id: str = "media-0001"):
    return proto.ResultEnvelope(
        type="result", id="cmd", ok=True, job_id="", result={"media_id": media_id}
    )


def _error(dispatched: Any = None, *, code: str = "command_failed", include: bool = True, **extra):
    payload: dict[str, Any] = {"code": code, "message": "refused", **extra}
    if include:
        payload["dispatched"] = dispatched
    return proto.ResultEnvelope(type="result", id="cmd", ok=False, job_id="", error=payload)


def _coordinator(transport: CommandTransport, ledger: Any = None, **kwargs: Any):
    return GenerationCoordinator(
        transport, live_generation_enabled=True, budget_ledger=ledger, **kwargs
    )


async def _drive_to_failed(coordinator, transport, prompt: str = "p"):
    """Submit once with a provider ``dispatched:false`` answer: FAILED, retryable."""
    job = await coordinator.submit("image", _request(prompt), approval_binding="a" * 64)
    assert job.state.value == "failed", job.state
    return job


# --------------------------------------------------------------------------- #
# 1. cancellation
# --------------------------------------------------------------------------- #
async def test_cancellation_before_the_transport_runs_is_not_delivery_evidence():
    """A cancel that lands before any send leaves no job and no reservation.

    The coordinator lock is held by the test, so the submission is provably parked
    before it can reserve or reach a transport. Cancelling there is the only case the
    implementation can call safe: no transport interaction happened at all.
    """

    class NeverRuns(CommandTransport):
        async def send(self, envelope):
            raise AssertionError("not reached")

        async def await_result(self, command_id, timeout):  # pragma: no cover
            raise AssertionError("not reached")

    ledger = FlowBudgetLedger(cap_usd=5.0)
    coordinator = _coordinator(NeverRuns(), ledger)
    await coordinator._lock.acquire()
    try:
        task = asyncio.create_task(
            coordinator.submit("image", _request(), approval_binding="a" * 64)
        )
        await asyncio.sleep(0.02)  # it is parked on the lock
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        coordinator._lock.release()
    assert coordinator.jobs == []
    assert ledger.spent_usd == 0.0
    assert ledger.reservations == 0


async def test_cancellation_inside_the_send_is_ambiguous_and_retained():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = CancelOnSend()
    coordinator = _coordinator(transport, ledger)

    with pytest.raises(asyncio.CancelledError):
        await coordinator.submit("image", _request(), approval_binding="a" * 64)

    job = coordinator.jobs[0]
    assert job.state.value == "unknown_submission"
    assert job.delivery is DeliveryOutcome.MAY_HAVE_BEEN_SENT
    assert job.provider_dispatch is ProviderDispatchOutcome.UNKNOWN
    assert job.dispatched is True, "'not dispatched' would be a false claim here"
    assert job.error is not None and job.error.retryable is False
    assert "cancel" in job.error.details.get("reason", "").lower()
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE), "the send may already be billed"
    assert ledger.reservations == 1
    assert coordinator._reservations == {}


async def test_a_cancelled_send_can_never_be_retried():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    coordinator = _coordinator(CancelOnSend(), ledger)
    with pytest.raises(asyncio.CancelledError):
        await coordinator.submit("image", _request(), approval_binding="a" * 64)
    job = coordinator.jobs[0]
    with pytest.raises(UnknownSubmissionRemains):
        await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)
    assert ledger.reservations == 1


async def test_cancellation_while_waiting_for_the_answer_is_ambiguous():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = Hanging()
    coordinator = _coordinator(transport, ledger)

    task = asyncio.create_task(
        coordinator.submit("image", _request(), approval_binding="a" * 64)
    )
    await asyncio.wait_for(transport.entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    job = coordinator.jobs[0]
    assert job.state.value == "unknown_submission"
    assert job.delivery is DeliveryOutcome.SENT, "the frame did leave"
    assert job.provider_dispatch is ProviderDispatchOutcome.UNKNOWN
    assert job.dispatched is True
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)
    assert coordinator._reservations == {}


async def test_the_job_survives_cancellation_and_stays_inspectable():
    coordinator = _coordinator(CancelOnSend(), FlowBudgetLedger(cap_usd=5.0))
    with pytest.raises(asyncio.CancelledError):
        await coordinator.submit("image", _request(), approval_binding="a" * 64)
    job_id = coordinator.jobs[0].job_id
    fetched = coordinator.get(job_id)
    assert fetched is not None and fetched.state.value == "unknown_submission"
    snapshot = coordinator.snapshot()
    assert snapshot["jobs"][0]["state"] == "unknown_submission"
    assert snapshot["jobs"][0]["provider_dispatch"] == "unknown"
    assert snapshot["budget"]["reservations"] == 1


async def test_cancellation_is_never_swallowed_into_a_job():
    coordinator = _coordinator(CancelOnSend(), FlowBudgetLedger(cap_usd=5.0))
    result: dict[str, Any] = {}

    async def run() -> None:
        result["job"] = await coordinator.submit(
            "image", _request(), approval_binding="a" * 64
        )

    with pytest.raises(asyncio.CancelledError):
        await run()
    assert "job" not in result, "a cancelled submit must not return a success value"


async def test_a_definite_pre_send_refusal_still_releases_its_reservation():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    coordinator = _coordinator(NotDelivered(), ledger)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.state.value == "failed"
    assert job.delivery is DeliveryOutcome.DEFINITELY_NOT_SENT
    assert job.provider_dispatch is ProviderDispatchOutcome.NOT_DISPATCHED
    assert job.dispatched is False
    assert ledger.spent_usd == 0.0
    assert ledger.reservations == 0


async def test_cancellation_during_a_retry_is_ambiguous_and_cannot_duplicate():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = Scripted([_error(False)])
    coordinator = _coordinator(transport, ledger)
    job = await _drive_to_failed(coordinator, transport)

    # The retry's send is cancelled: the attempt may have reached the extension.
    transport2 = CancelOnSend()
    coordinator.transport = transport2
    with pytest.raises(asyncio.CancelledError):
        await coordinator.retry(job.job_id, approval_binding="b" * 64)

    assert job.attempts == 2
    assert job.state.value == "unknown_submission"
    assert job.delivery is DeliveryOutcome.MAY_HAVE_BEEN_SENT
    assert job.provider_dispatch is ProviderDispatchOutcome.UNKNOWN
    # The first attempt was refunded (provider never submitted); only the cancelled
    # retry is charged, and it is charged once.
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)
    with pytest.raises(UnknownSubmissionRemains):
        await coordinator.retry(job.job_id, approval_binding="c" * 64)
    assert transport2.attempts == 1, "no duplicate attempt was made"


# --------------------------------------------------------------------------- #
# 2. the state mapping
# --------------------------------------------------------------------------- #
async def test_local_refusal_before_the_socket_is_not_delivered_and_not_dispatched():
    coordinator = _coordinator(NotDelivered(), FlowBudgetLedger(cap_usd=5.0))
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert (job.delivery, job.provider_dispatch) == (
        DeliveryOutcome.DEFINITELY_NOT_SENT,
        ProviderDispatchOutcome.NOT_DISPATCHED,
    )


async def test_a_completed_send_is_sent_with_unknown_provider_state():
    coordinator = _coordinator(Hanging(), FlowBudgetLedger(cap_usd=5.0))
    task = asyncio.create_task(
        coordinator.submit("image", _request(), approval_binding="a" * 64)
    )
    await asyncio.sleep(0.05)
    job = coordinator.jobs[0]
    assert job.delivery is DeliveryOutcome.SENT
    assert job.provider_dispatch is ProviderDispatchOutcome.UNKNOWN
    assert job.state.value == "dispatched"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_raised_send_is_ambiguous_with_unknown_provider_state():
    coordinator = _coordinator(
        Exploding(ConnectionResetError("reset")), FlowBudgetLedger(cap_usd=5.0)
    )
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert (job.delivery, job.provider_dispatch) == (
        DeliveryOutcome.MAY_HAVE_BEEN_SENT,
        ProviderDispatchOutcome.UNKNOWN,
    )


async def test_a_correlated_provider_non_dispatch_keeps_transport_sent():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = Scripted([_error(False)])
    coordinator = _coordinator(transport, ledger)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.delivery is DeliveryOutcome.SENT, "delivery history is not rewritten"
    assert job.provider_dispatch is ProviderDispatchOutcome.NOT_DISPATCHED
    assert job.dispatched is True
    assert job.state.value == "failed"
    assert ledger.spent_usd == 0.0, "nothing was billable"
    assert job.error is not None and job.error.retryable is True


async def test_a_correlated_provider_dispatch_is_recorded_as_dispatched():
    coordinator = _coordinator(Scripted([_ok()]), FlowBudgetLedger(cap_usd=5.0))
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.delivery is DeliveryOutcome.SENT
    assert job.provider_dispatch is ProviderDispatchOutcome.DISPATCHED
    assert job.state.value == "submitted"


async def test_a_timeout_after_a_successful_send_leaves_the_provider_unknown():
    coordinator = _coordinator(
        Scripted([asyncio.TimeoutError()]), FlowBudgetLedger(cap_usd=5.0), command_timeout=0.05
    )
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.delivery is DeliveryOutcome.SENT
    assert job.provider_dispatch is ProviderDispatchOutcome.UNKNOWN
    assert job.state.value == "unknown_submission"


async def test_a_missing_dispatch_field_is_unknown_not_non_dispatch():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = Scripted([_error(include=False)])
    coordinator = _coordinator(transport, ledger)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.provider_dispatch is ProviderDispatchOutcome.UNKNOWN
    assert job.state.value == "unknown_submission"
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE), "no refund without evidence"


@pytest.mark.parametrize("value", ["false", 0, "", [], {}, None])
async def test_malformed_dispatch_evidence_is_unknown(value):
    ledger = FlowBudgetLedger(cap_usd=5.0)
    coordinator = _coordinator(Scripted([_error(value)]), ledger)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.provider_dispatch is ProviderDispatchOutcome.UNKNOWN
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)


async def test_a_foreign_job_id_cannot_establish_non_dispatch():
    ledger = FlowBudgetLedger(cap_usd=5.0)

    class Foreign(CommandTransport):
        async def send(self, envelope):
            return DeliveryOutcome.SENT

        async def await_result(self, command_id, timeout):
            return proto.ResultEnvelope(
                type="result",
                id=command_id,
                ok=False,
                job_id="someone-else",
                error={"code": "quota", "message": "nope", "dispatched": False},
            )

    coordinator = _coordinator(Foreign(), ledger)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.provider_dispatch is ProviderDispatchOutcome.UNKNOWN
    assert job.state.value == "unknown_submission"
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)


async def test_a_foreign_command_id_cannot_establish_non_dispatch():
    ledger = FlowBudgetLedger(cap_usd=5.0)

    class WrongCommand(CommandTransport):
        async def send(self, envelope):
            return DeliveryOutcome.SENT

        async def await_result(self, command_id, timeout):
            return proto.ResultEnvelope(
                type="result",
                id="a-different-command",
                ok=False,
                job_id="",
                error={"code": "quota", "message": "nope", "dispatched": False},
            )

    coordinator = _coordinator(WrongCommand(), ledger)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.provider_dispatch is ProviderDispatchOutcome.UNKNOWN
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)


async def test_a_contradictory_request_hash_cannot_establish_non_dispatch():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    coordinator = _coordinator(
        Scripted([_error(False, request_hash="0" * 64)]), ledger
    )
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.provider_dispatch is ProviderDispatchOutcome.UNKNOWN
    assert job.state.value == "unknown_submission"
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)


async def test_a_matching_request_hash_is_accepted_as_evidence():
    request = _request()
    ledger = FlowBudgetLedger(cap_usd=5.0)
    coordinator = _coordinator(Scripted([_error(False, request_hash=request.request_hash())]), ledger)
    job = await coordinator.submit("image", request, approval_binding="a" * 64)
    assert job.provider_dispatch is ProviderDispatchOutcome.NOT_DISPATCHED


def test_the_classification_helpers_are_strict():
    assert classify_provider_dispatch(False) is ProviderDispatchOutcome.NOT_DISPATCHED
    assert classify_provider_dispatch(True) is ProviderDispatchOutcome.DISPATCHED
    for value in (None, "true", 1, 0, {}, []):
        assert classify_provider_dispatch(value) is ProviderDispatchOutcome.UNKNOWN


# --------------------------------------------------------------------------- #
# 3. retry eligibility on the separated states
# --------------------------------------------------------------------------- #
async def test_a_correlated_provider_non_dispatch_permits_a_newly_reserved_retry():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = Scripted([_error(False), _ok()])
    coordinator = _coordinator(transport, ledger)
    job = await _drive_to_failed(coordinator, transport)
    assert ledger.spent_usd == 0.0

    retried = await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert retried.attempts == 2
    assert retried.state.value == "submitted"
    assert retried.provider_dispatch is ProviderDispatchOutcome.DISPATCHED
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)
    assert ledger.reservations == 1


async def test_an_ambiguous_provider_state_is_never_retryable():
    coordinator = _coordinator(Scripted([_error(include=False)]), FlowBudgetLedger(cap_usd=5.0))
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.state.value == "unknown_submission"
    with pytest.raises(UnknownSubmissionRemains):
        await coordinator.retry(job.job_id, approval_binding="b" * 64)


async def test_an_ambiguous_transport_state_is_never_retryable():
    coordinator = _coordinator(
        Exploding(ConnectionError("mid-frame")), FlowBudgetLedger(cap_usd=5.0)
    )
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.delivery is DeliveryOutcome.MAY_HAVE_BEEN_SENT
    with pytest.raises(UnknownSubmissionRemains):
        await coordinator.retry(job.job_id, approval_binding="b" * 64)


async def test_a_failed_job_with_a_foreign_answer_is_not_retryable():
    ledger = FlowBudgetLedger(cap_usd=5.0)

    class Foreign(CommandTransport):
        async def send(self, envelope):
            return DeliveryOutcome.SENT

        async def await_result(self, command_id, timeout):
            return proto.ResultEnvelope(
                type="result",
                id=command_id,
                ok=False,
                job_id="other-job",
                error={"code": "quota", "message": "nope", "dispatched": False},
            )

    coordinator = _coordinator(Foreign(), ledger)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    with pytest.raises(UnknownSubmissionRemains):
        await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)


async def test_retry_still_requires_trust_and_approval(release):
    ledger = FlowBudgetLedger(cap_usd=5.0)
    fresh = _verified_decision(release)
    transport = Scripted([_error(False)])
    coordinator = _coordinator(transport, ledger, trust_provider=lambda: fresh)
    job = await _drive_to_failed(coordinator, transport)

    with pytest.raises(ApprovalRequired):
        await coordinator.retry(job.job_id, approval_binding="")

    coordinator.trust_provider = lambda: fresh.expired_copy()
    with pytest.raises(Exception) as excinfo:
        await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert getattr(excinfo.value, "code", "") in {"flow_untrusted_artifact", "untrusted_artifact"} or (
        "trust" in str(excinfo.value).lower()
    )
    assert ledger.spent_usd == 0.0


async def test_the_retry_reservation_is_settled_exactly_once():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = Scripted([_error(False), _ok()])
    coordinator = _coordinator(transport, ledger)
    job = await _drive_to_failed(coordinator, transport)
    await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)
    assert ledger.reservations == 1
    assert coordinator._reservations == {}
    # Settling again must not refund, and releasing a stale id must not either.
    coordinator._settle_reservation(job.job_id)
    coordinator._release_reservation(job.job_id)
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)


# --------------------------------------------------------------------------- #
# 4. the bridge: cancellation and the two states
# --------------------------------------------------------------------------- #
def _bridge() -> FlowBridge:
    return FlowBridge(
        BridgeConfig(
            pairing_id=generate_pairing_id(),
            pairing_secret=generate_pairing_secret(),
            http_port=0,
            ws_port=0,
        )
    )


def _envelope() -> proto.CommandEnvelope:
    return proto.CommandEnvelope(
        id=proto.new_id(),
        command="generate_image",
        job_id="job-1",
        payload={"prompt": "x", "count": 1},
        issued_at=proto.iso(),
        expires_at=proto.iso(),
    )


async def test_a_cancelled_socket_send_cleans_up_and_re_raises():
    class BlockingSocket:
        def __init__(self) -> None:
            self.entered = asyncio.Event()

        async def send(self, frame: str) -> None:
            self.entered.set()
            await asyncio.Event().wait()

    socket = BlockingSocket()
    bridge = _bridge()
    session = ExtensionSession(session_id="s1", websocket=socket)
    session.authenticated = True
    bridge.session = session
    envelope = _envelope()

    task = asyncio.create_task(bridge.send_command(envelope))
    await asyncio.wait_for(socket.entered.wait(), timeout=5)
    assert envelope.id in session.pending, "the future exists while the send is open"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.pending == {}, "the pending future is removed exactly once"


async def test_a_cancelled_socket_send_does_not_claim_non_delivery():
    class BlockingSocket:
        async def send(self, frame: str) -> None:
            await asyncio.Event().wait()

    bridge = _bridge()
    session = ExtensionSession(session_id="s1", websocket=BlockingSocket())
    session.authenticated = True
    bridge.session = session

    task = asyncio.create_task(bridge.send_command(_envelope()))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # No DeliveryUncertain or DefiniteNonDelivery was produced: cancellation is its
    # own signal and the caller must classify it as ambiguous.
    assert session.pending == {}


async def test_a_socket_exception_is_reported_as_uncertain_not_undefined():
    class ExplodingSocket:
        async def send(self, frame: str) -> None:
            raise ConnectionResetError("reset")

    bridge = _bridge()
    session = ExtensionSession(session_id="s1", websocket=ExplodingSocket())
    session.authenticated = True
    bridge.session = session

    from app.flow.delivery import DeliveryUncertain

    with pytest.raises(DeliveryUncertain):
        await bridge.send_command(_envelope())
    assert session.pending == {}


async def test_dispatch_reports_both_states_for_a_provider_refusal():
    class Socket:
        async def send(self, frame: str) -> None:
            return None

    bridge = _bridge()
    session = ExtensionSession(session_id="s1", websocket=Socket())
    session.authenticated = True
    bridge.session = session

    async def answer_without_submitting(command_id: str, timeout: float):
        return proto.ResultEnvelope(
            type="result",
            id=command_id,
            ok=False,
            job_id="",
            error={"code": "quota", "message": "no quota", "dispatched": False},
        )

    bridge.await_result = answer_without_submitting  # type: ignore[method-assign]
    with pytest.raises(DispatchFailed) as excinfo:
        await bridge.dispatch("generate_image", {"prompt": "x"}, job_id="job-1")
    assert excinfo.value.details.get("upstream_code") in ("quota", "")
    assert excinfo.value.outcome is DeliveryOutcome.SENT
    assert excinfo.value.provider is ProviderDispatchOutcome.NOT_DISPATCHED


async def test_dispatch_reports_uncertainty_after_a_timeout():
    class Socket:
        async def send(self, frame: str) -> None:
            return None

    bridge = _bridge()
    session = ExtensionSession(session_id="s1", websocket=Socket())
    session.authenticated = True
    bridge.session = session

    async def never_answers(command_id: str, timeout: float):
        raise asyncio.TimeoutError()

    bridge.await_result = never_answers  # type: ignore[method-assign]
    with pytest.raises(DispatchFailed) as excinfo:
        await bridge.dispatch("generate_image", {"prompt": "x"}, job_id="job-1")
    assert excinfo.value.outcome is DeliveryOutcome.SENT
    assert excinfo.value.provider is ProviderDispatchOutcome.UNKNOWN
    assert excinfo.value.code == "flow_command_timeout"


async def test_dispatch_refuses_before_the_socket_with_a_provable_classification():
    bridge = _bridge()
    assert bridge.session is None
    from app.flow.coordinator import NotPaired

    with pytest.raises(NotPaired):
        await bridge.dispatch("generate_image", {"prompt": "x"}, job_id="job-1")
    assert classify_send_exception(NotPaired()) is DeliveryOutcome.DEFINITELY_NOT_SENT


# --------------------------------------------------------------------------- #
# 5. the companion relay
# --------------------------------------------------------------------------- #
class _Bridge:
    """Stands in for FlowBridge so the companion loop can be driven."""

    def __init__(self, *, paired: bool = True, error: BaseException | None = None) -> None:
        self.paired = paired
        self.error = error
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self.started = False
        self.http_port = 8793
        self.ws_port = 8794

    def snapshot(self) -> dict[str, Any]:
        return {
            "bridge_version": "1.0-3c233",
            "session": {"extension_id": "", "extension_version": "", "flow_tab_present": False},
        }

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def dispatch(self, command: str, payload: dict[str, Any], *, job_id: str = "") -> Any:
        self.calls.append((command, payload, job_id))
        if self.error is not None:
            raise self.error
        return {"ready": True}


def _companion(handler, bridge: _Bridge) -> LocalFlowCompanion:
    from app.flow.companion import CompanionConfig

    config = CompanionConfig(
        backend_url="https://crusor.example",
        companion_id="companion-test",
        companion_secret="c" * 43,
        heartbeat_seconds=0.0,
        request_timeout=5.0,
    )
    client = httpx.AsyncClient(base_url=config.backend_url, transport=httpx.MockTransport(handler))
    return LocalFlowCompanion(
        config=config,
        bridge_config=BridgeConfig(
            pairing_id="pid", pairing_secret="s" * 43, http_port=0, ws_port=0
        ),
        bridge=bridge,  # type: ignore[arg-type]
        client=client,
    )


def _handler(results: list[dict[str, Any]], *, command: str = "status"):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commands"):
            return httpx.Response(
                200,
                json={
                    "commands": [
                        {"id": "cmd-1", "command": command, "payload": {}, "job_id": "job-1"}
                    ]
                },
            )
        if request.url.path.endswith("/results"):
            results.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    return handler


async def test_the_companion_relays_a_pre_send_refusal_as_definite_non_delivery():
    from app.flow.coordinator import NotPaired

    results: list[dict[str, Any]] = []
    companion = _companion(_handler(results), _Bridge(error=NotPaired()))
    await companion.run_forever(max_iterations=2)
    body = results[0]
    assert body["transport"] == "definitely_not_sent"
    assert body["provider_dispatch"] == "not_dispatched"
    assert body["error"]["dispatched"] is False


async def test_the_companion_relays_a_timeout_as_uncertainty():
    error = DispatchFailed(
        "delivered, no answer",
        code="flow_command_timeout",
        delivery=DeliveryOutcome.SENT,
        provider=ProviderDispatchOutcome.UNKNOWN,
    )
    results: list[dict[str, Any]] = []
    companion = _companion(_handler(results), _Bridge(error=error))
    await companion.run_forever(max_iterations=2)
    body = results[0]
    assert body["transport"] == "sent"
    assert body["provider_dispatch"] == "unknown"
    assert "dispatched" not in body["error"], "an ambiguous send may not claim non-delivery"
    # And the payload the backend accepts must validate.
    CommandResultIn(**body)


async def test_the_companion_relays_a_socket_failure_as_uncertainty():
    results: list[dict[str, Any]] = []
    companion = _companion(_handler(results), _Bridge(error=ConnectionResetError("boom")))
    await companion.run_forever(max_iterations=2)
    body = results[0]
    assert body["transport"] == "may_have_been_sent"
    assert body["provider_dispatch"] == "unknown"
    assert "dispatched" not in body["error"]
    CommandResultIn(**body)


async def test_the_companion_preserves_a_correlated_provider_refusal():
    error = DispatchFailed(
        "no quota",
        code="quota",
        delivery=DeliveryOutcome.SENT,
        provider=ProviderDispatchOutcome.NOT_DISPATCHED,
    )
    results: list[dict[str, Any]] = []
    companion = _companion(_handler(results), _Bridge(error=error))
    await companion.run_forever(max_iterations=2)
    body = results[0]
    assert body["transport"] == "sent"
    assert body["provider_dispatch"] == "not_dispatched"
    assert "dispatched" not in body["error"], "transport delivery stays a fact, not a guess"


async def test_a_cancelled_companion_command_posts_no_false_result():
    results: list[dict[str, Any]] = []

    class Cancelling(_Bridge):
        async def dispatch(self, command, payload, *, job_id=""):
            raise asyncio.CancelledError()

    companion = _companion(_handler(results), Cancelling())
    with pytest.raises(asyncio.CancelledError):
        await companion.run_forever(max_iterations=2)
    assert results == [], "silence is honest; a fabricated dispatched:false is not"


def test_the_result_schema_rejects_contradictory_states():
    with pytest.raises(Exception):
        CommandResultIn(
            command_id="c1", transport="may_have_been_sent", provider_dispatch="not_dispatched"
        )
    with pytest.raises(Exception):
        CommandResultIn(
            command_id="c1", transport="definitely_not_sent", provider_dispatch="dispatched"
        )
    with pytest.raises(Exception):
        CommandResultIn(
            command_id="c1",
            transport="may_have_been_sent",
            error={"code": "x", "message": "y", "dispatched": False},
        )
    # The honest combinations are accepted, and silence means "not stated".
    CommandResultIn(command_id="c1", transport="sent", provider_dispatch="not_dispatched")
    CommandResultIn(command_id="c1")
    assert CommandResultIn(command_id="c1").provider_dispatch == ""


# --------------------------------------------------------------------------- #
# 6. lifecycle edge cases
# --------------------------------------------------------------------------- #
async def test_a_late_result_after_a_timeout_cannot_re_arm_a_retry():
    """The coordinator settled the job as unknown; a later answer changes nothing."""
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = Scripted([asyncio.TimeoutError()])
    coordinator = _coordinator(transport, ledger, command_timeout=0.05)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.state.value == "unknown_submission"

    late = _error(False)
    late.id = "cmd"
    coordinator._apply_submission_result(job, late, command_id="whatever")
    assert job.state.value == "unknown_submission", "a late answer cannot make it retryable"
    with pytest.raises(UnknownSubmissionRemains):
        await coordinator.retry(job.job_id, approval_binding="b" * 64)


async def test_cancelling_a_dispatched_job_keeps_the_spend_and_unknown_provider():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = Hanging()
    coordinator = _coordinator(transport, ledger)
    task = asyncio.create_task(
        coordinator.submit("image", _request(), approval_binding="a" * 64)
    )
    await asyncio.wait_for(transport.entered.wait(), timeout=5)
    job = coordinator.jobs[0]

    from app.flow.coordinator import CancellationAmbiguous

    with pytest.raises(CancellationAmbiguous):
        await coordinator.cancel(job.job_id)
    assert job.state.value == "unknown_submission"
    assert job.provider_dispatch is ProviderDispatchOutcome.UNKNOWN
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)
    assert coordinator._reservations == {}
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_one_attempt_only_survives_the_new_classification():
    transport = Scripted([_ok()])
    coordinator = _coordinator(transport)
    await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert len(transport.sent) == 1
    assert transport.sent[0].payload["one_attempt_only"] is True
    assert transport.sent[0].payload["count"] == 1


def test_the_job_exposes_both_states():
    from app.flow.coordinator import FlowJob

    job = FlowJob(job_id="j", kind="image", request=_request(), approval_binding="a" * 64)
    data = job.as_dict()
    assert data["delivery"] is None
    assert data["provider_dispatch"] == "unknown"
    assert data["dispatched"] is False
