"""Phase 3C.2.3.2 — retry, delivery and filesystem security closure.

Three defects from the independent review of Phase 3C.2.3.1, each with tests that fail
if the correction is reverted:

1. ``GenerationCoordinator.retry()`` reserved nothing and re-checked nothing, so a
   failed job could be retried without limit and beyond the spend cap.
2. Any exception from ``transport.send()`` was read as proof of non-delivery, so an
   ambiguous socket failure refunded budget that may already have been spent.
3. The filesystem traversal opened the root by path and never revalidated a nested
   directory's identity in its parent after the recursive scan.

Everything here is synthetic: temporary trees, in-memory transports, in-process
settings. No DeepX source, no browser, no Google account, no paid call.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import pytest

import app.flow.installed_artifact as artifact_module
from app.flow.bridge import BridgeConfig, ExtensionSession, FlowBridge
from app.flow.coordinator import (
    ApprovalRequired,
    CommandTransport,
    GenerationCoordinator,
    JobInProgress,
    LiveGenerationDisabled,
    UnknownSubmissionRemains,
)
from app.flow.delivery import (
    DefiniteNonDelivery,
    DeliveryOutcome,
    DeliveryUncertain,
    classify_send_exception,
    classify_send_result,
)
from app.flow.installed_artifact import (
    LAYOUT_TREE,
    InstalledArtifactVerifier,
    InstallationSpec,
    MeasurementError,
    MeasurementPolicy,
)
from app.flow.provenance import COMPONENT_MCP, TrustPolicy, resolve_trust_anchor
from app.flow.security import generate_pairing_id, generate_pairing_secret
from app.flow.trust import (
    COMPONENT_VERIFIED,
    UNVERIFIED,
    ChallengeStore,
    SessionAttestation,
    SpendingControls,
    TrustEvidence,
    UntrustedArtifact,
    aggregate_trust,
    evaluate_live_generation_guard,
)
from app.services.flow_budget import (
    BudgetUnavailable,
    FlowBudgetLedger,
    InvalidBudgetCap,
)

from tests.test_phase3c231_security_hardening import (
    _mcp_tree,
    _release,
    _request,
    _spec,
    _verified_decision,
)

IMAGE_ESTIMATE = 0.04


@pytest.fixture()
def release(tmp_path: Path) -> dict[str, Any]:
    return _release(tmp_path)


# --------------------------------------------------------------------------- #
# transports
# --------------------------------------------------------------------------- #
class RecordingTransport(CommandTransport):
    """Records every frame and answers from a scripted queue."""

    def __init__(self, results: list[Any] | None = None) -> None:
        self.sent: list[Any] = []
        self._results = list(results or [])
        self._pending: dict[str, asyncio.Future[Any]] = {}

    async def send(self, envelope) -> DeliveryOutcome:
        self.sent.append(envelope)
        outcome = self._results.pop(0) if self._results else asyncio.TimeoutError()
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        if isinstance(outcome, BaseException):
            future.set_exception(outcome)
        else:
            future.set_result(outcome)
        self._pending[envelope.id] = future
        return DeliveryOutcome.SENT

    async def await_result(self, command_id: str, timeout: float) -> Any:
        return await asyncio.wait_for(self._pending[command_id], timeout=timeout)


def _ok(media_id: str = "media-0001"):
    from app.flow import protocol as proto

    return proto.ResultEnvelope(
        type="result", id="cmd", ok=True, job_id="", result={"media_id": media_id}
    )


def _provider_non_dispatch(code: str = "command_failed"):
    from app.flow import protocol as proto

    return proto.ResultEnvelope(
        type="result",
        id="cmd",
        ok=False,
        job_id="",
        error={"code": code, "message": "refused before submitting", "dispatched": False},
    )


class FailingSendTransport(RecordingTransport):
    """``send`` raises the exception it is given; nothing is recorded as sent."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    async def send(self, envelope):
        raise self.error


class BlockingTransport(RecordingTransport):
    """Holds the first submission open so a second operation can race it."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def await_result(self, command_id: str, timeout: float) -> Any:
        self.entered.set()
        await asyncio.Event().wait()


def _coordinator(transport: CommandTransport, ledger: Any = None, **kwargs: Any):
    return GenerationCoordinator(
        transport,
        live_generation_enabled=True,
        budget_ledger=ledger,
        **kwargs,
    )


async def _failed_not_dispatched(coordinator, *, prompt: str = "retry me"):
    """A job in FAILED with *proven* non-delivery, ready to be retried."""
    job = await coordinator.submit("image", _request(prompt), approval_binding="a" * 64)
    assert job.state.value == "failed", job.state
    assert job.delivery is DeliveryOutcome.DEFINITELY_NOT_SENT
    return job


# --------------------------------------------------------------------------- #
# 1. defect A — retry reserves, re-checks trust and cannot race itself
# --------------------------------------------------------------------------- #
async def test_a_retry_reserves_a_fresh_estimate():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = RecordingTransport([_provider_non_dispatch(), _ok()])
    coordinator = _coordinator(transport, ledger)
    job = await _failed_not_dispatched(coordinator)
    assert ledger.spent_usd == 0.0, "the first attempt was refunded"

    retried = await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert retried.attempts == 2
    assert retried.state.value == "submitted"
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)
    assert ledger.reservations == 1


async def test_a_retry_cannot_proceed_with_an_expired_trust_decision(release):
    ledger = FlowBudgetLedger(cap_usd=5.0)
    decision = _verified_decision(release).expired_copy()
    transport = RecordingTransport([_provider_non_dispatch()])
    coordinator = _coordinator(transport, ledger, trust_provider=lambda: decision)
    # The first submission is refused by the expired decision, so build the FAILED
    # job with a fresh decision and only *then* expire it.
    fresh = _verified_decision(release)
    coordinator.trust_provider = lambda: fresh
    job = await _failed_not_dispatched(coordinator)
    coordinator.trust_provider = lambda: decision
    sent_before = len(transport.sent)

    with pytest.raises(UntrustedArtifact):
        await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert len(transport.sent) == sent_before
    assert ledger.spent_usd == 0.0
    assert ledger.reservations == 0


async def test_a_retry_cannot_proceed_with_an_untrusted_decision(release):
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = RecordingTransport([_provider_non_dispatch()])
    fresh = _verified_decision(release)
    coordinator = _coordinator(transport, ledger, trust_provider=lambda: fresh)
    job = await _failed_not_dispatched(coordinator)
    coordinator.trust_provider = lambda: None
    with pytest.raises(UntrustedArtifact):
        await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert ledger.spent_usd == 0.0


async def test_a_retry_requires_an_approval_binding():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = RecordingTransport([_provider_non_dispatch()])
    coordinator = _coordinator(transport, ledger)
    job = await _failed_not_dispatched(coordinator)
    with pytest.raises(ApprovalRequired):
        await coordinator.retry(job.job_id, approval_binding="")
    assert ledger.spent_usd == 0.0
    assert ledger.reservations == 0


async def test_a_successful_retry_remains_charged():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = RecordingTransport([_provider_non_dispatch(), _ok()])
    coordinator = _coordinator(transport, ledger)
    job = await _failed_not_dispatched(coordinator)
    await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)
    assert ledger.reservations == 1
    assert coordinator._reservations == {}


async def test_an_ambiguous_retry_remains_charged():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = RecordingTransport([_provider_non_dispatch(), asyncio.TimeoutError()])
    coordinator = _coordinator(transport, ledger, command_timeout=0.05)
    job = await _failed_not_dispatched(coordinator)
    retried = await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert retried.state.value == "unknown_submission"
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)
    assert ledger.reservations == 1


async def test_a_proven_non_delivery_retry_releases_only_its_own_reservation():
    ledger = FlowBudgetLedger(cap_usd=5.0)

    class SecondSendNotDelivered(RecordingTransport):
        def __init__(self) -> None:
            super().__init__([_provider_non_dispatch()])
            self.sends = 0

        async def send(self, envelope):
            self.sends += 1
            if self.sends == 2:
                raise DefiniteNonDelivery("the socket was closed before the frame")
            return await super().send(envelope)

    transport = SecondSendNotDelivered()
    coordinator = _coordinator(transport, ledger)
    job = await _failed_not_dispatched(coordinator)
    retried = await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert retried.state.value == "failed"
    assert retried.delivery is DeliveryOutcome.DEFINITELY_NOT_SENT
    assert retried.attempts == 2
    assert ledger.spent_usd == 0.0, "both attempts were provably not delivered"
    assert ledger.reservations == 0
    assert coordinator._reservations == {}


async def test_an_insufficient_budget_blocks_the_retry_before_transport_dispatch():
    ledger = FlowBudgetLedger(cap_usd=0.05)
    transport = RecordingTransport([_provider_non_dispatch(), _ok()])
    coordinator = _coordinator(transport, ledger)
    job = await _failed_not_dispatched(coordinator)
    sent_before = len(transport.sent)

    # Spend the cap on another attempt, then try to retry inside the same cap.
    ledger.reserve(kind="image")
    with pytest.raises(BudgetUnavailable):
        await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert len(transport.sent) == sent_before, "no command may reach the transport"
    assert job.attempts == 1, "a refused retry must not consume an attempt number"
    assert job.state.value == "failed"
    assert ledger.reservations == 1


async def test_concurrent_retries_cannot_both_dispatch():
    """The state transition is the dispatch token; a second retry cannot claim it."""

    class BlockingRetry(RecordingTransport):
        def __init__(self) -> None:
            super().__init__([_provider_non_dispatch()])
            self.calls = 0
            self.blocked = asyncio.Event()

        async def await_result(self, command_id, timeout):
            self.calls += 1
            if self.calls == 1:
                return await super().await_result(command_id, timeout)
            self.blocked.set()
            await asyncio.Event().wait()  # the retry stays in flight

    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = BlockingRetry()
    coordinator = _coordinator(transport, ledger)
    job = await _failed_not_dispatched(coordinator)
    sends_before = len(transport.sent)

    first = asyncio.create_task(coordinator.retry(job.job_id, approval_binding="b" * 64))
    await asyncio.wait_for(transport.blocked.wait(), timeout=5)

    from app.flow.coordinator import CoordinatorError

    with pytest.raises(CoordinatorError) as excinfo:
        await coordinator.retry(job.job_id, approval_binding="c" * 64)
    assert excinfo.value.details.get("state") == "dispatched"
    assert len(transport.sent) == sends_before + 1, "exactly one retry reached the wire"
    assert ledger.reservations == 1, "only one retry reservation exists"
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first


async def test_repeated_retries_cannot_bypass_the_cap():
    """A retry is a new paid attempt; the ledger, not the caller, decides."""
    # One 0.04 attempt fits; two do not. 0.045 leaves 0.005 after a paid attempt,
    # which cannot cover a retry.
    ledger = FlowBudgetLedger(cap_usd=0.045)
    transport = RecordingTransport([_provider_non_dispatch(), _ok()])
    coordinator = _coordinator(transport, ledger, max_live_jobs=4)

    job = await coordinator.submit("image", _request("free"), approval_binding="a" * 64)
    assert job.state.value == "failed" and job.delivery is DeliveryOutcome.DEFINITELY_NOT_SENT

    charged = await coordinator.submit("image", _request("paid"), approval_binding="a" * 64)
    assert charged.state.value == "submitted"
    # Settle it so nothing is live; the spend stays.
    coordinator.reconcile(charged.job_id, status="succeeded", media_id="media-0001")
    assert charged.state.value == "completed"

    sends_before = len(transport.sent)
    for _ in range(5):  # every attempt must be refused before the wire
        with pytest.raises(BudgetUnavailable):
            await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert len(transport.sent) == sends_before, "no retry may reach the transport"
    assert job.attempts == 1, "a refused retry never consumes an attempt number"
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)


async def test_an_unknown_submission_is_never_retryable():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = RecordingTransport([asyncio.TimeoutError()])
    coordinator = _coordinator(transport, ledger, command_timeout=0.05)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.state.value == "unknown_submission"
    sends_before = len(transport.sent)
    with pytest.raises(UnknownSubmissionRemains):
        await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert len(transport.sent) == sends_before
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)


async def test_a_failed_state_without_proven_non_delivery_is_not_retryable():
    """``FAILED`` alone is not permission: the delivery record must agree."""
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = RecordingTransport([_ok()])
    coordinator = _coordinator(transport, ledger)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    # Force the state a naive implementation would treat as retryable.
    job.state = __import__("app.flow.protocol", fromlist=["SubmissionState"]).SubmissionState.FAILED
    job.delivery = None
    job.dispatched = False
    sends_before = len(transport.sent)
    with pytest.raises(UnknownSubmissionRemains):
        await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert len(transport.sent) == sends_before
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)


async def test_a_retry_is_refused_while_live_generation_is_disabled():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = RecordingTransport([_provider_non_dispatch()])
    coordinator = _coordinator(transport, ledger)
    job = await _failed_not_dispatched(coordinator)
    coordinator.live_generation_enabled = False
    with pytest.raises(LiveGenerationDisabled):
        await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert ledger.spent_usd == 0.0


# --------------------------------------------------------------------------- #
# 2. defect B — delivery classification
# --------------------------------------------------------------------------- #
def test_the_classification_of_a_returned_send_is_sent():
    assert classify_send_result(None) is DeliveryOutcome.SENT
    assert classify_send_result(DeliveryOutcome.SENT) is DeliveryOutcome.SENT


def test_the_classification_of_an_untyped_exception_is_ambiguous():
    for error in (ConnectionError("socket closed"), OSError(32, "broken pipe"), RuntimeError("x")):
        assert classify_send_exception(error) is DeliveryOutcome.MAY_HAVE_BEEN_SENT


def test_typed_delivery_failures_classify_themselves():
    assert classify_send_exception(DefiniteNonDelivery("never sent")) is (
        DeliveryOutcome.DEFINITELY_NOT_SENT
    )
    assert classify_send_exception(DeliveryUncertain("unknown")) is (
        DeliveryOutcome.MAY_HAVE_BEEN_SENT
    )


def test_a_pre_send_refusal_declares_itself_as_definite_non_delivery():
    from app.flow.coordinator import NotPaired
    from app.flow.protocol import CommandDenied

    assert classify_send_exception(NotPaired()) is DeliveryOutcome.DEFINITELY_NOT_SENT
    assert (
        classify_send_exception(CommandDenied("nope", details={}))
        is DeliveryOutcome.DEFINITELY_NOT_SENT
    )


async def test_a_generic_send_exception_retains_budget_and_blocks_retry():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = FailingSendTransport(ConnectionError("socket closed mid-frame"))
    coordinator = _coordinator(transport, ledger)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)

    assert job.state.value == "unknown_submission"
    assert job.dispatched is True, "an ambiguous frame must never look 'not dispatched'"
    assert job.delivery is DeliveryOutcome.MAY_HAVE_BEEN_SENT
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)
    assert ledger.reservations == 1
    assert coordinator._reservations == {}

    with pytest.raises(UnknownSubmissionRemains):
        await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE), "still no refund"


async def test_a_proven_pre_send_failure_releases_exactly_once():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = FailingSendTransport(DefiniteNonDelivery("no session"))
    coordinator = _coordinator(transport, ledger)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)

    assert job.state.value == "failed"
    assert job.delivery is DeliveryOutcome.DEFINITELY_NOT_SENT
    assert ledger.spent_usd == 0.0
    assert ledger.reservations == 0
    # Releasing again must not invent a negative spend.
    ledger.release(0.10)
    assert ledger.spent_usd == 0.0
    assert ledger.reservations == 0


async def test_a_confirmed_send_retains_the_reservation_until_the_result():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = RecordingTransport([asyncio.TimeoutError()])
    coordinator = _coordinator(transport, ledger, command_timeout=0.05)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.delivery is DeliveryOutcome.SENT
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)
    assert job.state.value == "unknown_submission"


async def test_a_transport_that_reports_not_sent_without_raising_is_honoured():
    ledger = FlowBudgetLedger(cap_usd=5.0)

    class Shy(RecordingTransport):
        async def send(self, envelope):
            return DeliveryOutcome.DEFINITELY_NOT_SENT

    coordinator = _coordinator(Shy(), ledger)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.state.value == "failed"
    assert job.delivery is DeliveryOutcome.DEFINITELY_NOT_SENT
    assert ledger.spent_usd == 0.0


async def test_a_transport_that_reports_uncertainty_keeps_the_spend():
    ledger = FlowBudgetLedger(cap_usd=5.0)

    class Unsure(RecordingTransport):
        async def send(self, envelope):
            return DeliveryOutcome.MAY_HAVE_BEEN_SENT

    coordinator = _coordinator(Unsure(), ledger)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.state.value == "unknown_submission"
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)


async def test_the_bridge_classifies_a_missing_session_as_definite_non_delivery():
    bridge = FlowBridge(
        BridgeConfig(
            pairing_id=generate_pairing_id(),
            pairing_secret=generate_pairing_secret(),
            http_port=0,
            ws_port=0,
        )
    )
    assert bridge.session is None
    ledger = FlowBudgetLedger(cap_usd=5.0)
    from app.flow.bridge import BridgeTransport

    coordinator = _coordinator(BridgeTransport(bridge), ledger)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.state.value == "failed"
    assert job.delivery is DeliveryOutcome.DEFINITELY_NOT_SENT
    assert ledger.spent_usd == 0.0
    assert bridge.stats["commands_sent"] == 0


async def test_the_bridge_classifies_a_socket_exception_as_ambiguous():
    class ExplodingSocket:
        async def send(self, frame):
            raise ConnectionResetError("socket reset during send")

    bridge = FlowBridge(
        BridgeConfig(
            pairing_id=generate_pairing_id(),
            pairing_secret=generate_pairing_secret(),
            http_port=0,
            ws_port=0,
        )
    )
    session = ExtensionSession(session_id="s1", websocket=ExplodingSocket())
    session.authenticated = True
    bridge.session = session

    ledger = FlowBudgetLedger(cap_usd=5.0)
    from app.flow.bridge import BridgeTransport

    coordinator = _coordinator(BridgeTransport(bridge), ledger)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)

    assert job.state.value == "unknown_submission"
    assert job.delivery is DeliveryOutcome.MAY_HAVE_BEEN_SENT
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)
    assert session.pending == {}, "the pending future is cleaned up"


async def test_the_bridge_removes_the_pending_future_without_claiming_failure():
    class ExplodingSocket:
        async def send(self, frame):
            raise ConnectionResetError("boom")

    bridge = FlowBridge(
        BridgeConfig(
            pairing_id=generate_pairing_id(),
            pairing_secret=generate_pairing_secret(),
            http_port=0,
            ws_port=0,
        )
    )
    session = ExtensionSession(session_id="s1", websocket=ExplodingSocket())
    session.authenticated = True
    bridge.session = session
    from app.flow import protocol as proto

    envelope = proto.CommandEnvelope(
        id=proto.new_id(),
        command="generate_image",
        job_id="job-1",
        payload={"prompt": "x", "count": 1},
        issued_at=proto.iso(),
        expires_at=proto.iso(),
    )
    with pytest.raises(DeliveryUncertain):
        await bridge.send_command(envelope)
    assert envelope.id not in session.pending


async def test_a_blocked_generation_still_writes_nothing(release):
    bridge = FlowBridge(
        BridgeConfig(
            pairing_id=generate_pairing_id(),
            pairing_secret=generate_pairing_secret(),
            http_port=0,
            ws_port=0,
        )
    )
    assert bridge.coordinator.budget_ledger is None
    with pytest.raises(UntrustedArtifact):
        await bridge.submit_generation("image", _request(), approval_binding="a" * 64)
    assert bridge.stats["commands_sent"] == 0


async def test_one_attempt_only_survives_the_classification_change():
    transport = RecordingTransport([_ok()])
    coordinator = _coordinator(transport)
    await coordinator.submit("image", _request(), approval_binding="a" * 64)
    envelope = transport.sent[0]
    assert envelope.payload["count"] == 1
    assert envelope.payload["one_attempt_only"] is True
    assert len(transport.sent) == 1


# --------------------------------------------------------------------------- #
# 3. defect C — filesystem root and nested-directory identity
# --------------------------------------------------------------------------- #
def test_a_symlinked_root_ancestor_is_refused(tmp_path):
    real = _mcp_tree(tmp_path, "real")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(link / "node_modules"))
    assert excinfo.value.code in {"symlink_in_package", "path_outside_root"}


def test_a_symlinked_component_inside_the_path_is_refused(tmp_path):
    real = _mcp_tree(tmp_path, "real")
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / "hop").symlink_to(real, target_is_directory=True)
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(outer / "hop"))
    assert excinfo.value.code in {"symlink_in_package", "path_outside_root"}


def test_the_root_is_acquired_by_descriptor_walk_not_by_path(tmp_path, monkeypatch):
    root = _mcp_tree(tmp_path)
    opened: list[str] = []
    real_open_child = artifact_module._open_child_dir

    def tracking(parent_fd: int, name: str, relative: str, **kwargs: Any) -> int:
        opened.append(relative)
        return real_open_child(parent_fd, name, relative, **kwargs)

    monkeypatch.setattr(artifact_module, "_open_child_dir", tracking)
    report = InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(root))
    assert report.digest
    expected: list[str] = []
    for part in root.absolute().parts:
        if part == "/":
            continue
        expected.append((expected[-1] if expected else "") + "/" + part)
    assert opened[: len(expected)] == expected, "every path component is opened one at a time"


def test_a_root_replaced_during_measurement_fails_closed(tmp_path, monkeypatch):
    root = _mcp_tree(tmp_path)
    real_read = artifact_module._read_file_at
    swapped = {"done": False}

    def swap_root(dir_fd, name, relative, *, max_bytes):
        data, info = real_read(dir_fd, name, relative, max_bytes=max_bytes)
        if not swapped["done"] and relative == "server.py":
            swapped["done"] = True
            os.rename(root, root.with_name("measured-old"))
            os.mkdir(root)
            (root / "server.py").write_text("def main():\n    return 0\n", encoding="utf-8")
        return data, info

    monkeypatch.setattr(artifact_module, "_read_file_at", swap_root)
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(root))
    assert excinfo.value.code in {
        "root_replaced_during_measurement",
        "enumeration_failed",
        "file_added_during_measurement",
    }
    assert swapped["done"] is True


def test_a_nested_directory_replaced_after_it_was_scanned_fails_closed(tmp_path, monkeypatch):
    """Same name, same listing, different inode: only an identity check sees it."""
    root = _mcp_tree(tmp_path)
    real_scan = artifact_module._scan_directory
    swapped = {"done": False}

    def scan_then_replace(fd, *, prefix, exclusions, policy, state):
        real_scan(fd, prefix=prefix, exclusions=exclusions, policy=policy, state=state)
        if not swapped["done"] and prefix == "node_modules/":
            swapped["done"] = True
            # Replace the directory in the parent *after* its contents were read.
            moved = tmp_path / "node_modules-old"
            os.rename(root / "node_modules", moved)
            replacement = root / "node_modules"
            replacement.mkdir()
            (replacement / "sdk").mkdir()
            (replacement / "sdk" / "index.js").write_text(
                "module.exports = 1;\n", encoding="utf-8"
            )

    monkeypatch.setattr(artifact_module, "_scan_directory", scan_then_replace)
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(root))
    assert excinfo.value.code in {
        "directory_replaced_during_measurement",
        "file_added_during_measurement",
    }
    assert swapped["done"] is True


def test_a_nested_directory_replaced_during_its_own_scan_fails_closed(tmp_path, monkeypatch):
    root = _mcp_tree(tmp_path)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    real_read = artifact_module._read_file_at
    swapped = {"done": False}

    def read_then_swap(dir_fd, name, relative, *, max_bytes):
        data, info = real_read(dir_fd, name, relative, max_bytes=max_bytes)
        if not swapped["done"] and relative == "node_modules/sdk/index.js":
            swapped["done"] = True
            os.rename("node_modules", "node_modules-old", src_dir_fd=root_fd, dst_dir_fd=root_fd)
            os.mkdir("node_modules", dir_fd=root_fd)
        return data, info

    monkeypatch.setattr(artifact_module, "_read_file_at", read_then_swap)
    try:
        with pytest.raises(MeasurementError) as excinfo:
            InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(root))
    finally:
        os.close(root_fd)
    assert excinfo.value.code in {
        "directory_replaced_during_measurement",
        "file_added_during_measurement",
    }
    assert swapped["done"] is True


def test_a_directory_entry_added_during_the_scan_fails_closed(tmp_path, monkeypatch):
    root = _mcp_tree(tmp_path)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    real_read = artifact_module._read_file_at
    added = {"done": False}

    def read_then_add(dir_fd, name, relative, *, max_bytes):
        data, info = real_read(dir_fd, name, relative, max_bytes=max_bytes)
        if not added["done"] and relative == "node_modules/sdk/index.js":
            added["done"] = True
            fd = os.open(
                "extra.js", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=root_fd
            )
            os.write(fd, b"1;\n")
            os.close(fd)
        return data, info

    monkeypatch.setattr(artifact_module, "_read_file_at", read_then_add)
    try:
        with pytest.raises(MeasurementError) as excinfo:
            InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(root))
    finally:
        os.close(root_fd)
    assert excinfo.value.code == "file_added_during_measurement"


def test_a_directory_entry_removed_during_the_scan_fails_closed(tmp_path, monkeypatch):
    root = _mcp_tree(tmp_path)
    real_read = artifact_module._read_file_at
    removed = {"done": False}

    def read_then_remove(dir_fd, name, relative, *, max_bytes):
        data, info = real_read(dir_fd, name, relative, max_bytes=max_bytes)
        if not removed["done"] and relative == "node_modules/sdk/index.js":
            removed["done"] = True
            os.unlink(name, dir_fd=dir_fd)
        return data, info

    monkeypatch.setattr(artifact_module, "_read_file_at", read_then_remove)
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(root))
    assert excinfo.value.code in {
        "file_replaced_during_measurement",
        "file_added_during_measurement",
    }


def test_a_platform_without_o_nofollow_cannot_claim_secure_traversal(monkeypatch):
    assert artifact_module._descriptor_traversal_available() is True
    monkeypatch.setattr(os, "O_NOFOLLOW", 0)
    assert artifact_module._descriptor_traversal_available() is False

    root = _mcp_tree(Path(os.environ.get("TMPDIR", "/tmp")) / "no-follow-probe")
    monkeypatch.setattr(artifact_module, "DIR_FD_SUPPORTED", False)
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(root))
    assert excinfo.value.code == "unsupported_platform"


def test_a_platform_without_scandir_on_a_descriptor_is_refused(monkeypatch):
    monkeypatch.setattr(os, "scandir", os.scandir)
    monkeypatch.setattr(
        artifact_module.os, "supports_fd", type(os.supports_fd)()  # empty set
    )
    assert artifact_module._descriptor_traversal_available() is False


def test_the_measurement_still_states_its_snapshot_limits(tmp_path):
    report = InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(_mcp_tree(tmp_path)))
    assert any(item.startswith("no_atomic_snapshot") for item in report.limitations)
    assert "descriptor_relative_traversal" in report.checks


def test_a_stable_installation_measures_to_the_same_digest(tmp_path):
    root = _mcp_tree(tmp_path)
    first = InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(root))
    second = InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(root))
    assert first.digest == second.digest
    assert first.file_count == second.file_count == 2


# --------------------------------------------------------------------------- #
# 4. cross-component regression
# --------------------------------------------------------------------------- #
def test_the_guard_and_the_coordinator_share_one_ledger():
    ledger = FlowBudgetLedger(cap_usd=2.0)
    coordinator = _coordinator(RecordingTransport(), ledger)
    controls = SpendingControls(
        enforcement="flow_budget_ledger", cap_usd=ledger.cap_usd, ledger_wired=True
    )
    assert controls.active is True
    assert controls.cap_usd == coordinator.budget_ledger.cap_usd
    assert coordinator.budget_ledger is ledger


async def test_submission_and_retry_have_identical_reservation_semantics():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = RecordingTransport([_provider_non_dispatch(), _ok()])
    coordinator = _coordinator(transport, ledger)
    job = await _failed_not_dispatched(coordinator)
    after_submit = (ledger.spent_usd, ledger.reservations)
    await coordinator.retry(job.job_id, approval_binding="b" * 64)
    after_retry = (ledger.spent_usd, ledger.reservations)
    assert after_submit == (0.0, 0)
    assert after_retry == (pytest.approx(IMAGE_ESTIMATE), 1)


async def test_ambiguous_delivery_never_releases_a_reservation():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    coordinator = _coordinator(FailingSendTransport(RuntimeError("unknown")), ledger)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.state.value == "unknown_submission"
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)
    with pytest.raises(UnknownSubmissionRemains):
        await coordinator.retry(job.job_id, approval_binding="b" * 64)
    assert ledger.spent_usd == pytest.approx(IMAGE_ESTIMATE)


def test_an_approval_cannot_bypass_trust_or_budget(release):
    blocked = evaluate_live_generation_guard(
        live_generation_flag=True,
        source_gate_status="AUTHORIZED",
        decision=None,
        approval_binding="a" * 64,
        budget_controls=SpendingControls(
            enforcement="flow_budget_ledger", cap_usd=5.0, ledger_wired=True
        ),
        idempotency_enforced=True,
    )
    assert blocked.allowed is False

    unbudgeted = evaluate_live_generation_guard(
        live_generation_flag=True,
        source_gate_status="AUTHORIZED",
        decision=_verified_decision(release),
        approval_binding="a" * 64,
        budget_controls=SpendingControls(),
        idempotency_enforced=True,
    )
    assert unbudgeted.allowed is False
    assert "budget_controls_missing" in unbudgeted.blockers


def test_an_unusable_cap_never_becomes_a_control():
    for cap in (0.0, float("nan"), float("inf")):
        with pytest.raises(InvalidBudgetCap):
            FlowBudgetLedger(cap_usd=cap)
        assert SpendingControls(
            enforcement="flow_budget_ledger", cap_usd=cap, ledger_wired=True
        ).active is False


def test_filesystem_instability_never_reaches_verified(release):
    """A measurement that failed cannot produce evidence at all."""
    evidence = TrustEvidence(
        supported_protocol=True,
        claims_well_formed=True,
        components_consistent=True,
        release_anchor_valid=True,
        installed_artifact_measured=False,
        installed_artifact_digest_matches_anchor=False,
        runtime_identity_correlated=False,
        measurement_fresh=False,
        measurement_id="",
        component_states={},
        required_components=(),
    )
    decision = aggregate_trust(
        source_gate_status="AUTHORIZED",
        anchor=release["anchor"],
        provenance=None,
        evidence=evidence,
        session_id="session-1",
        now=time.time(),
    )
    assert decision.trusted is False
    assert decision.state != "VERIFIED"


def test_a_peer_supplied_digest_is_not_a_measurement(release):
    """The HELLO digest is a claim; ``accept`` never treats it as evidence.

    A peer that echoes the release digest in its HELLO, but supplies no locally
    measured component reports, is refused — the same digest *inside* a real
    measurement is the only thing that counts.
    """
    from app.flow.trust import AttestationRejected, VerificationSubmission

    attestation = SessionAttestation(
        store=ChallengeStore(), anchor=release["anchor"], required_components=()
    )
    challenge = attestation.issue(
        session_id="session-1", companion_id="companion-1", extension_id="a" * 32
    )
    submission = VerificationSubmission(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        components=(),
    )
    with pytest.raises(AttestationRejected) as excinfo:
        attestation.accept(
            submission,
            session_id="session-1",
            companion_id="companion-1",
            hello_extension_id="a" * 32,
            hello_version="1.3.0",
            hello_digest=release["extension_digest"],
        )
    assert excinfo.value.code == "component_missing"
    assert UNVERIFIED != COMPONENT_VERIFIED
