"""Phase 3C.2.3.4 — strict correlation, pending-future cleanup and cancel races.

Three findings from the independent review of Phase 3C.2.3.3:

1. correlation was a *mismatch* check (``if result.job_id and ...``), so a result
   that merely omitted the job id — or the request hash, which did not exist — was
   still treated as correlated, could set ``NOT_DISPATCHED`` and could authorize a
   retry of an attempt that may already have been billable;
2. ``await_result`` left the ``pending`` entry behind on timeout, cancellation and
   any other failure, so the record outlived the command it described;
3. ``cancel()`` took no lock and ``_deliver`` entered the transport with no
   transition, so a cancel could be overwritten by a send it never saw (or a send
   could happen after a cancel had already won).

Everything here is synthetic: in-memory transports, a fake socket, a fake result
stream. No browser, no Google account, no Flow endpoint, no paid call.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.flow import protocol as proto
from app.flow.bridge import (
    BridgeConfig,
    BridgeTransport,
    ExtensionSession,
    FlowBridge,
)
from app.flow.security import generate_pairing_id, generate_pairing_secret
from app.flow.coordinator import (
    CancellationAmbiguous,
    CommandTransport,
    CoordinatorError,
    GenerationCoordinator,
    UnknownSubmissionRemains,
    classify_error_evidence,
    correlate_result,
)
from app.flow.delivery import DeliveryOutcome, DispatchFailed, ProviderDispatchOutcome
from pydantic import ValidationError

from app.schemas.flow import CommandResultIn
from app.services.flow_budget import FlowBudgetLedger

from tests.test_phase3c231_security_hardening import _request

IMAGE_ESTIMATE = 0.04


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
class Scripted(CommandTransport):
    """Answers from a script, without inventing any correlation of its own."""

    def __init__(self, results: list[Any] | None = None) -> None:
        self.sent: list[proto.CommandEnvelope] = []
        self._results = list(results or [])

    async def send(self, envelope: proto.CommandEnvelope) -> DeliveryOutcome:
        self.sent.append(envelope)
        return DeliveryOutcome.SENT

    async def await_result(self, command_id: str, timeout: float) -> Any:
        outcome = self._results.pop(0) if self._results else asyncio.TimeoutError()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _coordinator(transport: CommandTransport, ledger: Any = None, **kwargs: Any):
    return GenerationCoordinator(
        transport,
        live_generation_enabled=True,
        budget_ledger=ledger,
        command_timeout=kwargs.pop("command_timeout", 0.5),
        **kwargs,
    )


def _frame(
    envelope: proto.CommandEnvelope | None = None,
    *,
    command_id: str | None = None,
    job_id: str | None = None,
    request_hash: str | None = None,
    ok: bool = False,
    error: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> proto.ResultEnvelope:
    """Build an answer, defaulting every correlation field to the real value."""

    if envelope is not None:
        command_id = envelope.id if command_id is None else command_id
        job_id = envelope.job_id if job_id is None else job_id
        request_hash = envelope.request_hash if request_hash is None else request_hash
    return proto.ResultEnvelope(
        type="result",
        id=command_id or "",
        ok=ok,
        job_id=job_id or "",
        result=result or {},
        error=error,
        request_hash=request_hash or "",
    )


def _refusal(dispatched: Any = False, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": "quota",
        "message": "no quota left",
        "dispatched": dispatched,
    }
    payload.update(extra)
    return payload


async def _submit_with(transport: Scripted, ledger: Any = None, **kwargs: Any):
    """Submit once and return ``(job, envelope)``."""

    coordinator = _coordinator(transport, ledger, **kwargs)
    job = await coordinator.submit("image", _request("correlation"), approval_binding="a" * 64)
    return coordinator, job, transport.sent[0]


def _assert_settled_not_refunded(ledger: FlowBudgetLedger) -> None:
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE), "an ambiguous attempt keeps the spend"
    assert ledger.reservations == 1


def _assert_refunded(ledger: FlowBudgetLedger) -> None:
    assert ledger.spent_usd == 0.0, "provable non-delivery must refund exactly once"
    assert ledger.reservations == 0


# --------------------------------------------------------------------------- #
# 1. correlation verdict, in isolation
# --------------------------------------------------------------------------- #
def test_the_verdict_accepts_a_fully_correlated_answer():
    envelope = proto.CommandEnvelope(
        id="cmd-1",
        command="generate_image",
        job_id="job-1",
        payload={"prompt": "x"},
        issued_at=proto.iso(),
        expires_at=proto.iso(),
        request_hash="h" * 64,
    )
    verdict = correlate_result(
        _frame(envelope), command_id=envelope.id, job_id=envelope.job_id,
        request_hash=envelope.request_hash,
    )
    assert verdict.ok, verdict.reason


@pytest.mark.parametrize(
    "mutate, field",
    [
        ({"command_id": ""}, "command_id"),
        ({"command_id": "someone-else"}, "command_id"),
        ({"job_id": ""}, "job_id"),
        ({"job_id": "someone-else"}, "job_id"),
        ({"request_hash": ""}, "request_hash"),
        ({"request_hash": "f" * 64}, "request_hash"),
    ],
)
def test_the_verdict_names_the_field_that_failed(mutate, field):
    envelope = proto.CommandEnvelope(
        id="cmd-1",
        command="generate_image",
        job_id="job-1",
        payload={"prompt": "x"},
        issued_at=proto.iso(),
        expires_at=proto.iso(),
        request_hash="h" * 64,
    )
    verdict = correlate_result(
        _frame(envelope, **mutate),
        command_id=envelope.id,
        job_id=envelope.job_id,
        request_hash=envelope.request_hash,
    )
    assert not verdict.ok
    assert verdict.field == field, verdict.reason
    assert verdict.as_details()["correlation"] == "rejected"


def test_a_frame_that_is_not_a_result_is_rejected():
    envelope = proto.CommandEnvelope(
        id="cmd-1", command="generate_image", job_id="job-1", payload={},
        issued_at=proto.iso(), expires_at=proto.iso(), request_hash="h" * 64,
    )
    forged = proto.ResultEnvelope.model_construct(
        type="notice", id="cmd-1", ok=True, job_id="job-1", result={}, error=None,
        request_hash="h" * 64,
    )
    verdict = correlate_result(
        forged, command_id=envelope.id, job_id=envelope.job_id,
        request_hash=envelope.request_hash,
    )
    assert not verdict.ok and verdict.field == "type"


# --------------------------------------------------------------------------- #
# 2. correlation at the coordinator boundary
# --------------------------------------------------------------------------- #
async def test_a_fully_correlated_refusal_releases_the_reservation():
    ledger = FlowBudgetLedger(cap_usd=5.0)

    class Refusing(Scripted):
        async def await_result(self, command_id: str, timeout: float) -> Any:
            return _frame(self.sent[0], error=_refusal(False))

    transport = Refusing()
    coordinator = _coordinator(transport, ledger)
    job = await coordinator.submit("image", _request("refusal"), approval_binding="a" * 64)
    applied = job
    assert applied.provider_dispatch is ProviderDispatchOutcome.NOT_DISPATCHED
    assert applied.delivery is DeliveryOutcome.SENT
    assert applied.state is proto.SubmissionState.FAILED
    assert applied.dispatched is True
    _assert_refunded(ledger)


@pytest.mark.parametrize(
    "case, mutate",
    [
        ("missing command id", {"command_id": ""}),
        ("missing job id", {"job_id": ""}),
        ("missing request hash", {"request_hash": ""}),
        ("wrong command id", {"command_id": "a-different-command"}),
        ("wrong job id", {"job_id": "someone-else"}),
        ("wrong request hash", {"request_hash": "f" * 64}),
    ],
)
async def test_every_incomplete_correlation_case_is_unknown_and_not_retryable(case, mutate):
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = Scripted([])

    class Answering(Scripted):
        async def await_result(self, command_id: str, timeout: float) -> Any:
            return _frame(transport.sent[0], error=_refusal(False), **mutate)

    answering = Answering()
    coordinator = _coordinator(answering, ledger)
    job = await coordinator.submit("image", _request(case), approval_binding="a" * 64)

    assert job.delivery is DeliveryOutcome.SENT, "the frame was delivered"
    assert job.provider_dispatch is ProviderDispatchOutcome.UNKNOWN, case
    assert job.dispatched is True
    assert job.state is proto.SubmissionState.UNKNOWN_SUBMISSION, case
    assert job.error is not None and job.error.retryable is False
    _assert_settled_not_refunded(ledger)

    with pytest.raises(UnknownSubmissionRemains):
        await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert len(answering.sent) == 1, "an uncorrelated answer may never unlock a retry"


async def test_a_malformed_error_payload_is_not_evidence():
    ledger = FlowBudgetLedger(cap_usd=5.0)

    class Answering(Scripted):
        async def await_result(self, command_id: str, timeout: float) -> Any:
            return _frame(self.sent[0], error=None)

    transport = Answering()
    coordinator = _coordinator(transport, ledger)
    job = await coordinator.submit("image", _request("malformed"), approval_binding="a" * 64)
    assert job.provider_dispatch is ProviderDispatchOutcome.UNKNOWN
    assert job.state is proto.SubmissionState.UNKNOWN_SUBMISSION
    _assert_settled_not_refunded(ledger)


@pytest.mark.parametrize(
    "extra",
    [
        {"transport": "definitely_not_sent"},
        {"transport": "may_have_been_sent"},
        {"provider_dispatch": "dispatched"},
        {"provider_dispatch": "unknown"},
    ],
)
async def test_a_contradictory_refusal_is_not_evidence(extra):
    ledger = FlowBudgetLedger(cap_usd=5.0)

    class Answering(Scripted):
        async def await_result(self, command_id: str, timeout: float) -> Any:
            return _frame(self.sent[0], error=_refusal(False, **extra))

    transport = Answering()
    coordinator = _coordinator(transport, ledger)
    job = await coordinator.submit("image", _request("contradiction"), approval_binding="a" * 64)
    assert job.provider_dispatch is ProviderDispatchOutcome.UNKNOWN, extra
    assert job.state is proto.SubmissionState.UNKNOWN_SUBMISSION
    _assert_settled_not_refunded(ledger)


async def test_an_uncorrelated_success_does_not_prove_a_submission():
    ledger = FlowBudgetLedger(cap_usd=5.0)

    class Answering(Scripted):
        async def await_result(self, command_id: str, timeout: float) -> Any:
            return _frame(self.sent[0], ok=True, job_id="", result={"media_id": "media-9"})

    transport = Answering()
    coordinator = _coordinator(transport, ledger)
    job = await coordinator.submit("image", _request("success"), approval_binding="a" * 64)
    assert job.provider_dispatch is ProviderDispatchOutcome.UNKNOWN, "no inferred submission"
    assert job.state is proto.SubmissionState.UNKNOWN_SUBMISSION
    assert job.media_id == "", "uncorrelated media must not be claimed"
    _assert_settled_not_refunded(ledger)


async def test_a_foreign_success_response_is_not_attached():
    ledger = FlowBudgetLedger(cap_usd=5.0)

    class Foreign(Scripted):
        async def await_result(self, command_id: str, timeout: float) -> Any:
            return _frame(self.sent[0], ok=True, job_id="job-other",
                          result={"media_id": "media-9"})

    transport = Foreign()
    coordinator = _coordinator(transport, ledger)
    job = await coordinator.submit("image", _request("foreign"), approval_binding="a" * 64)
    assert job.state is proto.SubmissionState.UNKNOWN_SUBMISSION
    assert job.media_id == ""
    assert coordinator._media_owner == {}


async def test_an_uncorrelated_answer_cannot_move_a_settled_job():
    """A duplicate or late answer is discarded, not applied twice."""
    ledger = FlowBudgetLedger(cap_usd=5.0)

    class Slow(Scripted):
        async def await_result(self, command_id: str, timeout: float) -> Any:
            raise asyncio.TimeoutError

    transport = Slow()
    coordinator = _coordinator(transport, ledger)
    job = await coordinator.submit("image", _request("late"), approval_binding="a" * 64)
    assert job.state is proto.SubmissionState.UNKNOWN_SUBMISSION
    envelope = transport.sent[0]

    # A perfectly correlated answer, arriving after the job was settled.
    for frame in (
        _frame(envelope, error=_refusal(False)),
        _frame(envelope, ok=True, result={"media_id": "media-1"}),
    ):
        applied = await coordinator._apply_submission_result(job, frame, envelope=envelope)
        assert applied.state is proto.SubmissionState.UNKNOWN_SUBMISSION
        assert applied.provider_dispatch is ProviderDispatchOutcome.UNKNOWN
    _assert_settled_not_refunded(ledger)


async def test_an_envelope_bound_to_another_request_is_refused():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = Scripted([])
    coordinator = _coordinator(transport, ledger)
    job = await coordinator.submit("image", _request("bound"), approval_binding="a" * 64)
    forged = transport.sent[0].model_copy(update={"request_hash": "f" * 64})
    with pytest.raises(CoordinatorError):
        await coordinator._deliver(job, forged)
    assert len(transport.sent) == 1, "nothing may be sent for an unbound envelope"


def test_the_evidence_classifier_requires_a_real_boolean():
    job = type("Job", (), {"request_hash": "h" * 64})()
    for value, expected in (
        (False, ProviderDispatchOutcome.NOT_DISPATCHED),
        (True, ProviderDispatchOutcome.DISPATCHED),
        ("false", ProviderDispatchOutcome.UNKNOWN),
        (0, ProviderDispatchOutcome.UNKNOWN),
        (None, ProviderDispatchOutcome.UNKNOWN),
    ):
        outcome, _reason = classify_error_evidence({"dispatched": value}, job)
        assert outcome is expected, (value, outcome)


# --------------------------------------------------------------------------- #
# 3. the companion result contract
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "missing",
    ["command_id", "job_id", "request_hash"],
)
def test_the_schema_rejects_an_uncorrelated_refusal(missing):
    payload: dict[str, Any] = {
        "command_id": "c1",
        "job_id": "j1",
        "request_hash": "h" * 64,
        "transport": "sent",
        "provider_dispatch": "not_dispatched",
        "error": {"code": "x", "message": "y", "dispatched": False},
    }
    payload[missing] = ""
    with pytest.raises(Exception):
        CommandResultIn(**payload)


def test_the_schema_rejects_a_refusal_without_the_boolean():
    with pytest.raises(Exception):
        CommandResultIn(
            command_id="c1",
            job_id="j1",
            request_hash="h" * 64,
            transport="sent",
            provider_dispatch="not_dispatched",
            error={"code": "x", "message": "y"},
        )


_CORRELATED_REFUSAL: dict[str, Any] = {
    "command_id": "c1",
    "job_id": "j1",
    "request_hash": "h" * 64,
    "transport": "sent",
    "provider_dispatch": "not_dispatched",
}


@pytest.mark.parametrize(
    "case, error",
    [
        ("error is None", None),
        ("error.dispatched is missing", {"code": "x", "message": "y"}),
        ("error.dispatched is true", {"code": "x", "message": "y", "dispatched": True}),
        ("error.dispatched is the string 'false'", {"dispatched": "false"}),
        ("error.dispatched is the number 0", {"dispatched": 0}),
        ("error.dispatched is null", {"dispatched": None}),
    ],
)
def test_the_schema_requires_explicit_refusal_evidence(case, error):
    """Phase 3C.2.3.4.1: `not_dispatched` needs the boolean, not its absence."""

    with pytest.raises(ValidationError):
        CommandResultIn(**_CORRELATED_REFUSAL, error=error)


def test_the_schema_rejects_a_refusal_whose_error_is_not_an_object():
    with pytest.raises(ValidationError):
        CommandResultIn(**_CORRELATED_REFUSAL, error=["dispatched", False])


def test_the_schema_accepts_a_correlated_refusal_with_the_boolean():
    result = CommandResultIn(
        **_CORRELATED_REFUSAL,
        error={"code": "quota", "message": "no quota left", "dispatched": False},
    )
    assert result.provider_dispatch == "not_dispatched"
    assert result.error is not None and result.error["dispatched"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"transport": "sent", "provider_dispatch": "dispatched"},
        {"transport": "definitely_not_sent", "provider_dispatch": "not_dispatched",
         "error": {"dispatched": False}},
        {"transport": "may_have_been_sent", "provider_dispatch": "unknown"},
        {"transport": "", "provider_dispatch": ""},
        {"transport": "sent", "provider_dispatch": ""},
    ],
)
def test_the_schema_still_accepts_valid_transport_and_provider_combinations(payload):
    """The refusal invariant must not narrow the honest, non-refusal cases."""

    allowed = dict(payload)
    if allowed.get("provider_dispatch") == "not_dispatched":
        allowed.update(job_id="j1", request_hash="h" * 64)
    result = CommandResultIn(command_id="c1", **allowed)
    assert result.command_id == "c1"


@pytest.mark.parametrize(
    "payload",
    [
        {"transport": "may_have_been_sent", "provider_dispatch": "not_dispatched",
         "job_id": "j1", "request_hash": "h" * 64, "error": {"dispatched": False}},
        {"transport": "definitely_not_sent", "provider_dispatch": "dispatched"},
        {"transport": "may_have_been_sent",
         "error": {"code": "x", "message": "y", "dispatched": False}},
    ],
)
def test_the_schema_still_rejects_contradictory_combinations(payload):
    with pytest.raises(ValidationError):
        CommandResultIn(command_id="c1", **payload)


def test_the_schema_accepts_a_fully_correlated_refusal():
    result = CommandResultIn(
        command_id="c1",
        job_id="j1",
        request_hash="h" * 64,
        transport="sent",
        provider_dispatch="not_dispatched",
        error={"code": "x", "message": "y", "dispatched": False},
    )
    assert result.provider_dispatch == "not_dispatched"
    assert CommandResultIn(command_id="c1").request_hash == ""


# --------------------------------------------------------------------------- #
# 4. the bridge's own correlation (dispatch)
# --------------------------------------------------------------------------- #
class _Socket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: list[tuple[int, str]] = []

    async def send(self, frame: str) -> None:
        self.sent.append(frame)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))


def _bridge() -> FlowBridge:
    return FlowBridge(
        BridgeConfig(
            pairing_id=generate_pairing_id(),
            pairing_secret=generate_pairing_secret(),
            http_port=0,
            ws_port=0,
        )
    )


def _authed_bridge() -> tuple[FlowBridge, ExtensionSession, _Socket]:
    bridge = _bridge()
    socket = _Socket()
    session = ExtensionSession(session_id="s1", websocket=socket)
    session.authenticated = True
    bridge.session = session
    return bridge, session, socket


async def test_dispatch_reports_an_uncorrelated_refusal_as_unknown():
    bridge, _session, _socket = _authed_bridge()

    async def answer(command_id: str, timeout: float):
        return proto.ResultEnvelope(
            type="result", id=command_id, ok=False, job_id="", error=_refusal(False)
        )

    bridge.await_result = answer  # type: ignore[method-assign]
    with pytest.raises(DispatchFailed) as excinfo:
        await bridge.dispatch("generate_image", {"prompt": "x"}, job_id="job-1")
    assert excinfo.value.code == "flow_result_uncorrelated"
    assert excinfo.value.outcome is DeliveryOutcome.SENT
    assert excinfo.value.provider is ProviderDispatchOutcome.UNKNOWN


async def test_dispatch_refuses_a_refusal_for_a_command_without_a_job():
    bridge, _session, _socket = _authed_bridge()

    async def answer(command_id: str, timeout: float):
        return proto.ResultEnvelope(
            type="result", id=command_id, ok=False, job_id="", error=_refusal(False)
        )

    bridge.await_result = answer  # type: ignore[method-assign]
    with pytest.raises(DispatchFailed) as excinfo:
        await bridge.dispatch("generate_image", {"prompt": "x"})
    assert excinfo.value.code == "flow_result_uncorrelated"
    assert excinfo.value.provider is ProviderDispatchOutcome.UNKNOWN


async def test_dispatch_accepts_a_fully_correlated_refusal():
    bridge, _session, _socket = _authed_bridge()

    async def answer(command_id: str, timeout: float):
        return proto.ResultEnvelope(
            type="result", id=command_id, ok=False, job_id="job-1",
            request_hash="h" * 64, error=_refusal(False),
        )

    bridge.await_result = answer  # type: ignore[method-assign]
    with pytest.raises(DispatchFailed) as excinfo:
        await bridge.dispatch(
            "generate_image", {"prompt": "x"}, job_id="job-1", request_hash="h" * 64
        )
    assert excinfo.value.provider is ProviderDispatchOutcome.NOT_DISPATCHED
    assert excinfo.value.outcome is DeliveryOutcome.SENT


async def test_dispatch_rejects_a_foreign_request_hash():
    bridge, _session, _socket = _authed_bridge()

    async def answer(command_id: str, timeout: float):
        return proto.ResultEnvelope(
            type="result", id=command_id, ok=False, job_id="job-1",
            request_hash="f" * 64, error=_refusal(False),
        )

    bridge.await_result = answer  # type: ignore[method-assign]
    with pytest.raises(DispatchFailed) as excinfo:
        await bridge.dispatch(
            "generate_image", {"prompt": "x"}, job_id="job-1", request_hash="h" * 64
        )
    assert excinfo.value.provider is ProviderDispatchOutcome.UNKNOWN


async def test_every_attempt_gets_a_fresh_command_id():
    """Command ids are never reused, so a late answer cannot reach a newer attempt."""
    ledger = FlowBudgetLedger(cap_usd=5.0)

    class Twice(Scripted):
        async def await_result(self, command_id: str, timeout: float) -> Any:
            if len(self.sent) == 1:
                return _frame(self.sent[0], error=_refusal(False))
            return _frame(self.sent[0], ok=True, result={"media_id": "media-1"})

    transport = Twice()
    coordinator = _coordinator(transport, ledger)
    job = await coordinator.submit("image", _request("ids"), approval_binding="a" * 64)
    await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert len(transport.sent) == 2
    assert transport.sent[0].id != transport.sent[1].id
    assert transport.sent[0].job_id == transport.sent[1].job_id == job.job_id


async def test_a_stale_answer_for_the_previous_attempt_is_not_applied():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = Scripted([])

    class Stale(Scripted):
        async def await_result(self, command_id: str, timeout: float) -> Any:
            if len(self.sent) == 1:
                return _frame(self.sent[0], error=_refusal(False))
            # Answers the *first* command while the second attempt is waiting.
            return _frame(self.sent[0], ok=True, result={"media_id": "media-1"})

    stale = Stale()
    coordinator = _coordinator(stale, ledger)
    job = await coordinator.submit("image", _request("stale"), approval_binding="a" * 64)
    retried = await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert retried.state is proto.SubmissionState.UNKNOWN_SUBMISSION
    assert retried.media_id == "", "an answer to another command proves nothing"


# --------------------------------------------------------------------------- #
# 5. pending-future lifecycle
# --------------------------------------------------------------------------- #
def _command(job_id: str = "job-1") -> proto.CommandEnvelope:
    return proto.CommandEnvelope(
        id=proto.new_id(),
        command="generate_image",
        job_id=job_id,
        payload={"prompt": "x", "count": 1},
        issued_at=proto.iso(),
        expires_at=proto.iso(),
        request_hash="h" * 64,
    )


class FrameStream:
    """A websocket that yields a fixed list of frames and then ends."""

    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self.frames = list(frames)
        self.sent: list[str] = []
        self.closed: list[tuple[int, str]] = []

    def __aiter__(self) -> "FrameStream":
        return self

    async def __anext__(self) -> str:
        if not self.frames:
            raise StopAsyncIteration
        return json.dumps(self.frames.pop(0))

    async def send(self, frame: str) -> None:
        self.sent.append(frame)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))


async def test_a_successful_result_retires_the_pending_entry():
    bridge, session, _socket = _authed_bridge()
    envelope = _command()
    await bridge.send_command(envelope)
    assert envelope.id in session.pending
    waiter = asyncio.create_task(bridge.await_result(envelope.id, 2.0))
    await asyncio.sleep(0)

    session.pending[envelope.id].set_result(_frame(envelope, ok=True))
    result = await waiter
    assert result.ok is True
    assert session.pending == {}


async def test_a_timeout_retires_the_pending_entry():
    bridge, session, _socket = _authed_bridge()
    envelope = _command()
    session.pending[envelope.id] = asyncio.get_running_loop().create_future()
    with pytest.raises(asyncio.TimeoutError):
        await bridge.await_result(envelope.id, 0.01)
    assert session.pending == {}, "a timed-out command leaves no record behind"


async def test_a_cancellation_retires_the_pending_entry():
    bridge, session, _socket = _authed_bridge()
    envelope = _command()
    session.pending[envelope.id] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(bridge.await_result(envelope.id, 5.0))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.pending == {}


async def test_a_disconnect_retires_every_pending_entry():
    bridge, session, _socket = _authed_bridge()
    loop = asyncio.get_running_loop()
    first, second = _command(), _command("job-2")
    session.pending[first.id] = loop.create_future()
    session.pending[second.id] = loop.create_future()
    retired = bridge.retire_pending(session)
    assert retired == 2
    assert session.pending == {}


async def test_a_malformed_result_frame_retires_the_pending_entry():
    bridge, session, _socket = _authed_bridge()
    envelope = _command()
    future = asyncio.get_running_loop().create_future()
    session.pending[envelope.id] = future
    session.websocket = FrameStream([{"type": "result", "id": envelope.id, "ok": "not-a-bool"}])
    await bridge._ws_loop(session)
    assert session.pending == {}
    assert future.done(), "a malformed frame must not leave the waiter hanging"
    with pytest.raises(proto.FlowProtocolError):
        future.result()


async def test_a_late_result_cannot_complete_a_settled_command():
    bridge, session, _socket = _authed_bridge()
    envelope = _command()
    future = asyncio.get_running_loop().create_future()
    session.pending[envelope.id] = future
    with pytest.raises(asyncio.TimeoutError):
        await bridge.await_result(envelope.id, 0.01)
    assert session.pending == {}

    session.websocket = FrameStream(
        [json.loads(_frame(envelope, ok=True).model_dump_json())]
    )
    await bridge._ws_loop(session)
    assert future.cancelled() or not future.done()
    assert session.pending == {}


async def test_a_duplicate_result_cannot_satisfy_another_command():
    bridge, session, _socket = _authed_bridge()
    first, second = _command(), _command("job-2")
    loop = asyncio.get_running_loop()
    first_future, second_future = loop.create_future(), loop.create_future()
    session.pending[first.id] = first_future
    session.pending[second.id] = second_future
    session.websocket = FrameStream(
        [
            json.loads(_frame(first, ok=True).model_dump_json()),
            json.loads(_frame(first, ok=True).model_dump_json()),
        ]
    )
    await bridge._ws_loop(session)
    assert first_future.done() and first_future.result().id == first.id
    assert not second_future.done(), "a duplicate frame may not answer a different command"
    assert list(session.pending) == [second.id]
    second_future.cancel()


async def test_a_replacement_future_is_not_removed_by_a_stale_cleanup():
    bridge, session, _socket = _authed_bridge()
    envelope = _command()
    stale = asyncio.get_running_loop().create_future()
    session.pending[envelope.id] = stale
    waiter = asyncio.create_task(bridge.await_result(envelope.id, 0.01))
    await asyncio.sleep(0)

    replacement = asyncio.get_running_loop().create_future()
    session.pending[envelope.id] = replacement  # the caller re-registered the id
    with pytest.raises(asyncio.TimeoutError):
        await waiter
    assert session.pending.get(envelope.id) is replacement, "the replacement must survive"
    replacement.cancel()
    session.pending.clear()


async def test_a_delivered_but_cancelled_attempt_retires_its_pending_record():
    """Bridge-level: the frame is out, a cancel won, nobody will await the answer."""
    ledger = FlowBudgetLedger(cap_usd=5.0)
    bridge, session, _socket = _authed_bridge()
    transport = BridgeTransport(bridge)
    coordinator = _coordinator(transport, ledger)

    pause, go = asyncio.Event(), asyncio.Event()
    original = coordinator._record_send_success

    async def paused(job, command_id):
        pause.set()
        await go.wait()
        return await original(job, command_id)

    coordinator._record_send_success = paused  # type: ignore[method-assign]
    task = asyncio.create_task(
        coordinator.submit("image", _request("discard"), approval_binding="a" * 64)
    )
    await pause.wait()
    job = coordinator.jobs[0]
    assert len(session.pending) == 1, "the send registered a record"

    with pytest.raises(CancellationAmbiguous):
        await coordinator.cancel(job.job_id)
    go.set()
    finished = await task
    assert finished.delivery is DeliveryOutcome.SENT
    assert finished.state is proto.SubmissionState.UNKNOWN_SUBMISSION
    assert session.pending == {}, "the abandoned record is retired"
    _assert_settled_not_refunded(ledger)


async def test_discard_leaves_a_replacement_future_alone():
    bridge, session, _socket = _authed_bridge()
    replacement = asyncio.get_running_loop().create_future()
    session.pending["cmd-x"] = replacement
    assert bridge.discard("cmd-missing") is False
    assert bridge.discard("cmd-x") is True
    assert replacement.cancelled()
    replacement.cancel()
    assert session.pending == {}


async def test_send_command_refuses_a_duplicate_command_id():
    bridge, session, _socket = _authed_bridge()
    envelope = _command()
    session.pending[envelope.id] = asyncio.get_running_loop().create_future()
    from app.flow.delivery import DefiniteNonDelivery

    with pytest.raises(DefiniteNonDelivery):
        await bridge.send_command(envelope)
    session.pending.clear()


async def test_send_command_refuses_a_command_without_an_id():
    bridge, _session, _socket = _authed_bridge()
    envelope = _command().model_copy(update={"id": ""})
    from app.flow.delivery import DefiniteNonDelivery

    with pytest.raises(DefiniteNonDelivery):
        await bridge.send_command(envelope)


# --------------------------------------------------------------------------- #
# 6. cancel races
# --------------------------------------------------------------------------- #
class GatedSend(CommandTransport):
    """A transport whose send enters a barrier and waits for permission."""

    def __init__(self) -> None:
        self.sent: list[proto.CommandEnvelope] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.results: list[Any] = []

    async def send(self, envelope: proto.CommandEnvelope) -> DeliveryOutcome:
        self.entered.set()
        await self.release.wait()
        self.sent.append(envelope)
        return DeliveryOutcome.SENT

    async def await_result(self, command_id: str, timeout: float) -> Any:
        outcome = self.results.pop(0) if self.results else asyncio.TimeoutError()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


async def test_a_cancel_that_wins_before_the_send_prevents_it():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = GatedSend()
    coordinator = _coordinator(transport, ledger)

    before_send = asyncio.Event()
    allow_send = asyncio.Event()
    original = coordinator._deliver

    async def gated(job, envelope):
        before_send.set()
        await allow_send.wait()
        return await original(job, envelope)

    coordinator._deliver = gated  # type: ignore[method-assign]
    task = asyncio.create_task(
        coordinator.submit("image", _request("cancel wins"), approval_binding="a" * 64)
    )
    await before_send.wait()
    job = coordinator.jobs[0]
    assert ledger.reservations == 1, "the reservation exists before the transport entry"

    cancelled = await coordinator.cancel(job.job_id)
    assert cancelled.state is proto.SubmissionState.CANCELLED
    assert cancelled.dispatched is False
    _assert_refunded(ledger)

    allow_send.set()
    finished = await task
    assert finished.state is proto.SubmissionState.CANCELLED
    assert transport.sent == [], "no frame may be written after a cancel has won"
    assert transport.entered.is_set() is False


async def test_a_cancel_after_the_transport_entry_is_ambiguous():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = GatedSend()
    transport.release.clear()
    coordinator = _coordinator(transport, ledger)
    task = asyncio.create_task(
        coordinator.submit("image", _request("cancel mid-send"), approval_binding="a" * 64)
    )
    await transport.entered.wait()
    job = coordinator.jobs[0]

    with pytest.raises(CancellationAmbiguous):
        await coordinator.cancel(job.job_id)
    assert job.state is proto.SubmissionState.UNKNOWN_SUBMISSION
    assert job.provider_dispatch is ProviderDispatchOutcome.UNKNOWN
    _assert_settled_not_refunded(ledger)

    transport.release.set()
    finished = await task
    assert finished.state is proto.SubmissionState.UNKNOWN_SUBMISSION, "state kept"
    assert finished.delivery is DeliveryOutcome.SENT, "the frame did leave"
    assert len(transport.sent) == 1, "exactly one attempt"
    _assert_settled_not_refunded(ledger)


async def test_a_cancel_waiting_for_the_lock_still_sees_a_consistent_state():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = GatedSend()
    transport.release.clear()
    coordinator = _coordinator(transport, ledger)
    task = asyncio.create_task(
        coordinator.submit("image", _request("lock"), approval_binding="a" * 64)
    )
    await transport.entered.wait()
    job = coordinator.jobs[0]

    await coordinator._lock.acquire()
    cancel_task = asyncio.create_task(coordinator.cancel(job.job_id))
    await asyncio.sleep(0)
    assert not cancel_task.done(), "the cancel waits for the lock"
    coordinator._lock.release()

    with pytest.raises(CancellationAmbiguous):
        await cancel_task
    transport.release.set()
    await task
    _assert_settled_not_refunded(ledger)


async def test_a_cancel_while_awaiting_the_result_is_ambiguous():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    answered = asyncio.Event()

    class Waiting(CommandTransport):
        def __init__(self) -> None:
            self.sent: list[proto.CommandEnvelope] = []

        async def send(self, envelope: proto.CommandEnvelope) -> DeliveryOutcome:
            self.sent.append(envelope)
            return DeliveryOutcome.SENT

        async def await_result(self, command_id: str, timeout: float) -> Any:
            await answered.wait()
            return _frame(self.sent[0], ok=True, result={"media_id": "media-1"})

    transport = Waiting()
    coordinator = _coordinator(transport, ledger)
    task = asyncio.create_task(
        coordinator.submit("image", _request("awaiting"), approval_binding="a" * 64)
    )
    for _ in range(50):
        if coordinator.jobs and coordinator.jobs[0].state is proto.SubmissionState.DISPATCHED:
            break
        await asyncio.sleep(0.01)
    job = coordinator.jobs[0]

    with pytest.raises(CancellationAmbiguous):
        await coordinator.cancel(job.job_id)
    answered.set()
    finished = await task
    assert finished.state is proto.SubmissionState.UNKNOWN_SUBMISSION
    assert finished.media_id == "", "an answer after the cancel cannot claim media"
    _assert_settled_not_refunded(ledger)


async def test_a_cancel_during_a_retry_releases_only_the_new_reservation():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = Scripted([])

    class RefusingThenGated(Scripted):
        async def send(self, envelope: proto.CommandEnvelope) -> DeliveryOutcome:
            self.sent.append(envelope)
            if len(self.sent) == 1:
                from app.flow.delivery import DefiniteNonDelivery

                raise DefiniteNonDelivery("the session was never authenticated")
            self.entered.set()
            await self.allow.wait()
            return DeliveryOutcome.SENT

    retrying = RefusingThenGated()
    retrying.entered = asyncio.Event()
    retrying.allow = asyncio.Event()
    coordinator = _coordinator(retrying, ledger)
    job = await coordinator.submit("image", _request("retry cancel"), approval_binding="a" * 64)
    assert job.state is proto.SubmissionState.FAILED
    _assert_refunded(ledger)

    retry_task = asyncio.create_task(
        coordinator.retry(job.job_id, approval_binding="b" * 64)
    )
    await retrying.entered.wait()
    assert ledger.reservations == 1

    with pytest.raises(CancellationAmbiguous):
        await coordinator.cancel(job.job_id)
    retrying.allow.set()
    finished = await retry_task
    assert finished.state is proto.SubmissionState.UNKNOWN_SUBMISSION
    assert finished.attempts == 2
    _assert_settled_not_refunded(ledger)


async def test_a_cancel_before_a_retry_sends_refunds_the_retry():
    ledger = FlowBudgetLedger(cap_usd=5.0)

    class NonDelivering(CommandTransport):
        def __init__(self) -> None:
            self.sent: list[proto.CommandEnvelope] = []

        async def send(self, envelope: proto.CommandEnvelope) -> DeliveryOutcome:
            self.sent.append(envelope)
            from app.flow.delivery import DefiniteNonDelivery

            raise DefiniteNonDelivery("the session was never authenticated")

        async def await_result(self, command_id: str, timeout: float) -> Any:  # pragma: no cover
            raise AssertionError("await_result must not be reached")

    transport = NonDelivering()
    coordinator = _coordinator(transport, ledger)
    job = await coordinator.submit("image", _request("retry then cancel"), approval_binding="a" * 64)
    assert job.state is proto.SubmissionState.FAILED
    _assert_refunded(ledger)

    before_send = asyncio.Event()
    allow = asyncio.Event()
    original = coordinator._deliver

    async def gated(job_, envelope):
        before_send.set()
        await allow.wait()
        return await original(job_, envelope)

    coordinator._deliver = gated  # type: ignore[method-assign]
    task = asyncio.create_task(coordinator.retry(job.job_id, approval_binding="b" * 64))
    await before_send.wait()
    assert ledger.reservations == 1, "the retry reserved first"

    cancelled = await coordinator.cancel(job.job_id)
    assert cancelled.state is proto.SubmissionState.CANCELLED
    _assert_refunded(ledger)
    allow.set()
    finished = await task
    assert finished.state is proto.SubmissionState.CANCELLED
    assert len(transport.sent) == 1, "the retry never reached the socket"


async def test_repeated_cancellation_of_a_terminal_job_is_rejected():
    ledger = FlowBudgetLedger(cap_usd=5.0)

    class NonDelivering(Scripted):
        async def send(self, envelope: proto.CommandEnvelope) -> DeliveryOutcome:
            from app.flow.delivery import DefiniteNonDelivery

            self.sent.append(envelope)
            raise DefiniteNonDelivery("refused before the socket")

    transport = NonDelivering()
    coordinator = _coordinator(transport, ledger)
    job = await coordinator.submit("image", _request("terminal"), approval_binding="a" * 64)
    assert job.state is proto.SubmissionState.FAILED
    with pytest.raises(CoordinatorError):
        await coordinator.cancel(job.job_id)
    with pytest.raises(CoordinatorError):
        await coordinator.cancel(job.job_id)


async def test_cancelling_an_unknown_job_is_rejected():
    coordinator = _coordinator(Scripted([]))
    with pytest.raises(CoordinatorError):
        await coordinator.cancel("no-such-job")


async def test_a_cancelled_job_can_never_be_retried():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = GatedSend()
    coordinator = _coordinator(transport, ledger)
    before_send = asyncio.Event()
    allow = asyncio.Event()
    original = coordinator._deliver

    async def gated(job_, envelope):
        before_send.set()
        await allow.wait()
        return await original(job_, envelope)

    coordinator._deliver = gated  # type: ignore[method-assign]
    task = asyncio.create_task(
        coordinator.submit("image", _request("never retried"), approval_binding="a" * 64)
    )
    await before_send.wait()
    job = coordinator.jobs[0]
    await coordinator.cancel(job.job_id)
    allow.set()
    await task
    with pytest.raises(CoordinatorError):
        await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert transport.sent == []


async def test_a_bridge_cancelled_during_the_socket_send_stays_ambiguous():
    """The whole path: bridge send cancelled → coordinator ambiguity, no refund."""
    ledger = FlowBudgetLedger(cap_usd=5.0)

    class BlockingSocket:
        def __init__(self) -> None:
            self.entered = asyncio.Event()

        async def send(self, frame: str) -> None:
            self.entered.set()
            await asyncio.Event().wait()

        async def close(self, code: int = 1000, reason: str = "") -> None:  # pragma: no cover
            return None

    bridge, session, _socket = _authed_bridge()
    session.websocket = BlockingSocket()
    transport = BridgeTransport(bridge)
    coordinator = _coordinator(transport, ledger)
    task = asyncio.create_task(
        coordinator.submit("image", _request("socket cancel"), approval_binding="a" * 64)
    )
    await session.websocket.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    job = coordinator.jobs[0]
    assert job.delivery is DeliveryOutcome.MAY_HAVE_BEEN_SENT
    assert job.provider_dispatch is ProviderDispatchOutcome.UNKNOWN
    assert job.state is proto.SubmissionState.UNKNOWN_SUBMISSION
    assert session.pending == {}, "the cancelled send left no record"
    _assert_settled_not_refunded(ledger)
