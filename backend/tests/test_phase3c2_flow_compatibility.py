"""Phase 3C.2 — DeepX remediation: Crusor-side compatibility and enforcement.

The upstream half of Phase 3C.2 is blocked: there is no DeepX package on this
machine and no licence to modify one. So this suite tests the half Crusor *can*
own — the rules an authorised extension build will have to satisfy:

* **provenance** (§10): protocol support and per-component version agreement,
  proven from a HELLO rather than asserted by it;
* **lifecycle** (§8): the eight submission states, one attempt per request,
  idempotency keys, ambiguity that never re-arms a dispatch, reconciliation that
  cannot manufacture a success or a retry;
* **chunked export** (§9): bounded, checksummed, strictly ordered chunks that only
  become an asset when the whole set verifies;
* **source gate** (§2) and the read-only audit scanner (§13).

No Google account, no Flow tab, no generation, no paid call. Every media byte is a
PNG built in-process.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets

from app.core.config import get_settings
from app.creative.media import encode_png
from app.flow import protocol as proto
from app.flow.bridge import BridgeConfig, FlowBridge
from app.flow.coordinator import (
    CancellationAmbiguous,
    CommandTransport,
    FlowJob,
    GenerationCoordinator,
    IdempotencyConflict,
    LiveGenerationDisabled,
    ReconciliationRejected,
)
from app.flow.provenance import (
    ExtensionProvenance,
    ProvenanceRejected,
    TrustPolicy,
    UnsupportedProtocol,
    provenance_from_hello,
    verify_provenance,
)
from app.flow.security import generate_pairing_id, generate_pairing_secret
from app.flow.source_gate import (
    STATUS_AUTHORIZED,
    STATUS_LICENSE_EVIDENCE_PRESENT,
    STATUS_LICENSING_BLOCKED,
    STATUS_SOURCE_ABSENT,
    evaluate_source_gate,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXT_ID = "abcdefghijklmnopabcdefghijklmnop"
#: Phase 3C.2.1: trust needs an anchor Crusor already holds, so the fixtures pin a
#: digest the HELLO must match. A digest the extension reports about itself is
#: never used as its own anchor.
HELLO_DIGEST = "a" * 64
FLOW_API = "/api/v1/flow"
CHUNK = 8 * 1024 * 1024


def _png(width: int = 24, height: int = 30, shade: int = 40) -> bytes:
    return encode_png(width, height, bytes([shade, shade, shade]) * (width * height))


def _hello(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "hello",
        "protocol": proto.PROTOCOL_VERSION,
        "pairing_id": "pair-id",
        "pairing_secret": "p" * 43,
        "extension_id": EXT_ID,
        "extension_version": "1.3.0",
        "service_worker_version": "1.3.0",
        "mcp_version": "1.3.0",
        "source_commit": "0343884",
        "build_id": "build-2026-11-03",
        "package_sha256": HELLO_DIGEST,
        "flow_tab_present": True,
    }
    payload.update(overrides)
    return payload


def _provenance(**overrides: Any) -> ExtensionProvenance:
    base = {
        "protocol": proto.PROTOCOL_VERSION,
        "extension_id": EXT_ID,
        "extension_version": "1.3.0",
        "service_worker_version": "1.3.0",
        "mcp_version": "1.3.0",
        "source_commit": "0343884",
        "build_id": "build-2026-11-03",
        "package_sha256": HELLO_DIGEST,
    }
    base.update(overrides)
    return ExtensionProvenance(**base)


# --------------------------------------------------------------------------- A
# A. provenance and protocol negotiation (§10)
# --------------------------------------------------------------------------- #
def test_supported_protocol_is_exactly_one_and_declared():
    assert proto.SUPPORTED_PROTOCOLS == (proto.PROTOCOL_VERSION,)


def test_a_consistent_package_is_well_formed_but_not_trusted():
    """Phase 3C.2.1: syntax and self-consistency are not trust.

    Nothing here has been compared against an expectation Crusor holds, so the
    report must say UNVERIFIED even though every field is well-formed.
    """
    report = verify_provenance(_provenance())
    assert report.claims_well_formed is True
    assert report.components_consistent is True
    assert report.artifact_integrity_verified is False
    assert report.trusted is False
    assert report.trust_state == "UNVERIFIED"
    assert any("artifact_integrity_unverified" in warning for warning in report.warnings)
    assert report.provenance.release_line == "1.3"
    assert "extension_matches_service_worker" in report.checks
    assert len(report.provenance.fingerprint()) == 64


def test_a_pinned_digest_makes_the_same_package_trusted():
    report = verify_provenance(
        _provenance(),
        policy=TrustPolicy(trusted_artifact_sha256=HELLO_DIGEST, trusted_release="1.3.0"),
    )
    assert report.artifact_integrity_verified is True
    assert report.trusted is True
    assert report.trust_state == "VERIFIED"
    assert "artifact_digest_matches_trusted_anchor" in report.checks


def test_an_unsupported_protocol_is_refused():
    with pytest.raises(UnsupportedProtocol):
        verify_provenance(_provenance(protocol="flow.bridge.v0"))
    with pytest.raises(UnsupportedProtocol):
        verify_provenance(_provenance(protocol=""))


def test_the_upstream_version_split_is_refused():
    """Manifest 1.2.1 with an MCP server at 1.0.0 is exactly what the audit found."""
    with pytest.raises(ProvenanceRejected) as excinfo:
        verify_provenance(
            _provenance(
                extension_version="1.2.1",
                service_worker_version="1.2.0",
                mcp_version="1.0.0",
            )
        )
    assert excinfo.value.code == "flow_provenance_rejected"


def test_service_worker_must_match_the_extension():
    with pytest.raises(ProvenanceRejected) as excinfo:
        verify_provenance(_provenance(service_worker_version="1.2.0"))
    assert "different versions" in str(excinfo.value)


def test_mcp_release_line_drift_is_refused_unless_explicitly_allowed():
    drifted = _provenance(mcp_version="1.0.0")
    with pytest.raises(ProvenanceRejected):
        verify_provenance(drifted)
    report = verify_provenance(drifted, allow_component_drift=True)
    assert report.trusted is False
    assert any("release_line_mismatch" in warning for warning in report.warnings)


@pytest.mark.parametrize(
    "overrides",
    [
        {"extension_version": ""},
        {"extension_version": "1.2"},
        {"service_worker_version": "v1.2.1"},
        {"mcp_version": ""},
        {"source_commit": ""},
        {"source_commit": "not-a-commit"},
        {"build_id": ""},
        {"build_id": "x"},
        {"package_sha256": "abc123"},
        {"extension_id": ""},
    ],
)
def test_incomplete_provenance_is_refused(overrides: dict[str, Any]):
    with pytest.raises(ProvenanceRejected):
        verify_provenance(_provenance(**overrides))


def test_provenance_is_extracted_from_the_hello_message_not_trusted_blindly():
    hello = proto.HelloMessage.model_validate(_hello())
    provenance = provenance_from_hello(hello)
    assert provenance.source_commit == "0343884"
    # Self-reported fields are extracted, and extract them is all they are: with no
    # independent anchor the report stays UNVERIFIED.
    assert verify_provenance(provenance).trusted is False

    with pytest.raises(Exception):
        proto.HelloMessage.model_validate(_hello(protocol="flow.bridge.v9"))


# --------------------------------------------------------------------------- B
# B. the bridge refuses unproven handshakes
# --------------------------------------------------------------------------- #
@pytest.fixture
def pairing() -> dict[str, str]:
    return {"id": generate_pairing_id(), "secret": generate_pairing_secret()}


@pytest.fixture
async def bridge(pairing: dict[str, str]):  # type: ignore[no-untyped-def]
    instance = FlowBridge(
        BridgeConfig(
            pairing_id=pairing["id"],
            pairing_secret=pairing["secret"],
            http_port=0,
            ws_port=0,
            extension_ids=(EXT_ID,),
            handshake_timeout=2.0,
            request_timeout=5.0,
            command_timeout=3.0,
            trusted_artifact_sha256=HELLO_DIGEST,
            trusted_release="1.3.0",
        )
    )
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


def _pairing_hello(pairing: dict[str, str], **overrides: Any) -> str:
    payload = _hello(pairing_id=pairing["id"], pairing_secret=pairing["secret"], **overrides)
    return json.dumps(payload)


async def _expect_close(bridge: FlowBridge, frame: str, *, timeout: float = 3.0):  # type: ignore[no-untyped-def]
    ws = await websockets.connect(f"ws://127.0.0.1:{bridge.ws_port}/ws")
    try:
        await ws.send(frame)
        with pytest.raises(websockets.ConnectionClosed) as excinfo:
            await asyncio.wait_for(ws.recv(), timeout)
        return excinfo.value.rcvd.code
    finally:
        await ws.close()


async def test_a_proven_handshake_is_accepted_and_reported(bridge: FlowBridge, pairing):
    ws = await websockets.connect(f"ws://127.0.0.1:{bridge.ws_port}/ws")
    try:
        await ws.send(_pairing_hello(pairing))
        ack = json.loads(await asyncio.wait_for(ws.recv(), 3))
        assert ack["type"] == "hello_ack"
        # Phase 3C.2.3: a HELLO digest is a claim, so the ack reports the claim and
        # never a session-level "trusted". Session trust needs a local measurement.
        assert ack["provenance_trusted"] is False
        assert ack["trust_state"] == "UNVERIFIED"
        assert ack["hello_digest_claim_verified"] is True
        # This fixture configures no install roots, so no measurement is attempted.
        assert ack["measurement_required"] is False
        assert ack["provenance_state"] == "VERIFIED"
        assert ack["trust_anchor"] == "pinned_digest"
        # Three independent facts, each reported as itself: the ack must not imply
        # an integrity check by reusing a broader boolean for a narrower name.
        assert ack["claims_well_formed"] is True
        assert ack["components_consistent"] is True
        assert ack["artifact_integrity_verified"] is True
        assert ack["provenance"]["extension_version"] == "1.3.0"
        assert ack["provenance"]["service_worker_version"] == "1.3.0"
        assert ack["provenance"]["source_commit"] == "0343884"
        snapshot = bridge.snapshot()
        assert snapshot["provenance"]["build_id"] == "build-2026-11-03"
        assert snapshot["hello_digest_claim_verified"] is True
        assert snapshot["provenance_trusted"] is False
        assert snapshot["supported_protocols"] == list(proto.SUPPORTED_PROTOCOLS)
    finally:
        await ws.close()


@pytest.mark.parametrize(
    "overrides,expected_code",
    [
        ({"protocol": "flow.bridge.v0"}, proto.WS_CLOSE_PROTOCOL),
        ({"extension_version": "1.2.1", "service_worker_version": "1.2.0"}, proto.WS_CLOSE_PROVENANCE),
        ({"mcp_version": "1.0.0"}, proto.WS_CLOSE_PROVENANCE),
        ({"source_commit": ""}, proto.WS_CLOSE_PROVENANCE),
        ({"build_id": ""}, proto.WS_CLOSE_PROVENANCE),
    ],
)
async def test_unproven_handshakes_are_closed(bridge: FlowBridge, pairing, overrides, expected_code):
    code = await _expect_close(bridge, _pairing_hello(pairing, **overrides))
    assert code == expected_code
    assert bridge.paired is False
    assert bridge.session is None
    stats = bridge.snapshot()["stats"]
    assert stats["ws_rejected_provenance"] + stats["ws_rejected_protocol"] >= 1


async def test_a_phase_3c1_style_hello_without_provenance_is_now_refused(bridge: FlowBridge, pairing):
    """The old minimal HELLO claimed a version but proved nothing."""
    frame = json.dumps(
        {
            "type": "hello",
            "protocol": proto.PROTOCOL_VERSION,
            "pairing_id": pairing["id"],
            "pairing_secret": pairing["secret"],
            "extension_id": EXT_ID,
            "extension_version": "1.2.1",
            "flow_tab_present": True,
        }
    )
    assert await _expect_close(bridge, frame) == proto.WS_CLOSE_PROVENANCE


async def test_component_drift_can_be_opened_by_an_explicit_operator_choice(pairing):
    instance = FlowBridge(
        BridgeConfig(
            pairing_id=pairing["id"],
            pairing_secret=pairing["secret"],
            http_port=0,
            ws_port=0,
            extension_ids=(EXT_ID,),
            handshake_timeout=2.0,
            allow_component_drift=True,
        )
    )
    await instance.start()
    try:
        ws = await websockets.connect(f"ws://127.0.0.1:{instance.ws_port}/ws")
        try:
            await ws.send(_pairing_hello(pairing, mcp_version="1.0.0"))
            ack = json.loads(await asyncio.wait_for(ws.recv(), 3))
            assert ack["provenance_trusted"] is False
            assert instance.snapshot()["provenance_trusted"] is False
        finally:
            await ws.close()
    finally:
        await instance.stop()


async def test_the_companion_reports_the_version_that_actually_connected(bridge: FlowBridge, pairing):
    from app.flow.companion import LocalFlowCompanion

    ws = await websockets.connect(f"ws://127.0.0.1:{bridge.ws_port}/ws")
    try:
        await ws.send(_pairing_hello(pairing))
        await asyncio.wait_for(ws.recv(), 3)
        companion = LocalFlowCompanion(
            config=_companion_config(),
            bridge_config=BridgeConfig(
                pairing_id=pairing["id"], pairing_secret=pairing["secret"], http_port=0, ws_port=0
            ),
            bridge=bridge,
        )
        payload = companion.session_payload()
        assert payload["extension_version"] == "1.3.0"
        assert payload["bridge_paired"] is True
        capabilities = payload["capabilities"]
        assert capabilities["live_generation"] is False
        assert capabilities["generation_commands_executable"] is False
    finally:
        await ws.close()


# --------------------------------------------------------------------------- C
# C. lifecycle, idempotency, cancellation, reconciliation (§8)
# --------------------------------------------------------------------------- #
def test_the_eight_states_are_exactly_what_the_spec_requires():
    assert [state.value for state in proto.FlowLifecycle] == [
        "NOT_SUBMITTED",
        "SUBMITTING",
        "SUBMITTED",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "UNKNOWN_SUBMISSION",
        "CANCELLED",
    ]


def test_unknown_submission_can_never_be_re_armed_for_dispatch():
    assert proto.FlowLifecycle.NOT_SUBMITTED not in proto.LEGAL_TRANSITIONS[
        proto.FlowLifecycle.UNKNOWN_SUBMISSION
    ]
    with pytest.raises(proto.IllegalTransition):
        proto.transition(proto.FlowLifecycle.UNKNOWN_SUBMISSION, proto.FlowLifecycle.NOT_SUBMITTED)


def test_terminal_states_have_no_exits():
    for terminal in (
        proto.FlowLifecycle.SUCCEEDED,
        proto.FlowLifecycle.CANCELLED,
    ):
        assert proto.LEGAL_TRANSITIONS[terminal] == frozenset()
    assert proto.LEGAL_TRANSITIONS[proto.FlowLifecycle.FAILED] == frozenset(
        {proto.FlowLifecycle.NOT_SUBMITTED}
    )


def test_every_internal_state_maps_to_one_canonical_lifecycle_state():
    for state in proto.SubmissionState:
        assert isinstance(proto.lifecycle_for(state), proto.FlowLifecycle)


class ScriptedTransport(CommandTransport):
    def __init__(self, results: list[Any] | None = None) -> None:
        self.sent: list[proto.CommandEnvelope] = []
        self._results = list(results or [])
        self._pending: dict[str, asyncio.Future[Any]] = {}

    async def send(self, envelope: proto.CommandEnvelope) -> None:
        self.sent.append(envelope)
        outcome = self._results.pop(0) if self._results else None
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        if isinstance(outcome, Exception):
            future.set_exception(outcome)
        elif outcome is not None:
            future.set_result(outcome)
        self._pending[envelope.id] = future

    async def await_result(self, command_id: str, timeout: float) -> Any:
        return await asyncio.wait_for(self._pending[command_id], timeout)


def _request(prompt: str = "A charcoal frame", **overrides: Any) -> proto.GenerationCommand:
    payload = {
        "prompt": prompt,
        "aspect_ratio": "4:5",
        "model": "imagen-3",
        "count": 1,
        "idempotency_key": overrides.pop("idempotency_key", ""),
    }
    payload.update(overrides)
    return proto.GenerationCommand(**payload)


def _ok(media_id: str = "media-0001") -> proto.ResultEnvelope:
    return proto.ResultEnvelope(
        type="result", id="cmd", ok=True, job_id="", result={"media_id": media_id}
    )


def _coordinator(transport: ScriptedTransport, **kwargs: Any) -> GenerationCoordinator:
    return GenerationCoordinator(transport, live_generation_enabled=True, **kwargs)


async def test_generation_is_still_impossible_while_disabled():
    transport = ScriptedTransport()
    coordinator = GenerationCoordinator(transport, live_generation_enabled=False)
    with pytest.raises(LiveGenerationDisabled):
        await coordinator.submit(
            "image", _request(), approval_binding="a" * 64, idempotency_key="k1"
        )
    assert transport.sent == []


async def test_one_idempotency_key_means_one_generation_attempt():
    transport = ScriptedTransport([_ok()])
    coordinator = _coordinator(transport)
    first = await coordinator.submit(
        "image", _request(), approval_binding="a" * 64, idempotency_key="key-1"
    )
    replay = await coordinator.submit(
        "image", _request(), approval_binding="a" * 64, idempotency_key="key-1"
    )
    assert replay.job_id == first.job_id
    assert len(transport.sent) == 1, "a duplicate key must not produce a second attempt"
    assert first.lifecycle is proto.FlowLifecycle.SUBMITTED


async def test_a_reused_key_with_a_different_prompt_is_a_conflict():
    transport = ScriptedTransport([_ok()])
    coordinator = _coordinator(transport)
    await coordinator.submit(
        "image", _request("first"), approval_binding="a" * 64, idempotency_key="key-1"
    )
    with pytest.raises(IdempotencyConflict) as excinfo:
        await coordinator.submit(
            "image", _request("second"), approval_binding="a" * 64, idempotency_key="key-1"
        )
    assert excinfo.value.code == "flow_idempotency_conflict"
    assert len(transport.sent) == 1


async def test_keyless_replays_are_still_deduplicated_by_request_hash():
    transport = ScriptedTransport([_ok()])
    coordinator = _coordinator(transport)
    first = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    second = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert second.job_id == first.job_id
    assert len(transport.sent) == 1


async def test_lifecycle_counts_are_reported():
    transport = ScriptedTransport([_ok()])
    coordinator = _coordinator(transport)
    await coordinator.submit("image", _request(), approval_binding="a" * 64)
    counts = coordinator.snapshot()["lifecycle_counts"]
    assert counts["SUBMITTED"] == 1
    assert sum(counts.values()) == 1


async def test_cancelling_before_dispatch_is_a_fact():
    coordinator = GenerationCoordinator(ScriptedTransport(), live_generation_enabled=False)
    job = FlowJob(job_id="job-1", kind="image", request=_request(), approval_binding="a" * 64)
    coordinator._jobs[job.job_id] = job
    cancelled = await coordinator.cancel(job.job_id)
    assert cancelled.state is proto.SubmissionState.CANCELLED
    assert cancelled.lifecycle is proto.FlowLifecycle.CANCELLED
    assert cancelled.error is None


async def test_cancelling_an_in_flight_submit_is_ambiguous_not_cancelled():
    coordinator = GenerationCoordinator(ScriptedTransport(), live_generation_enabled=False)
    job = FlowJob(
        job_id="job-2",
        kind="image",
        request=_request(),
        approval_binding="a" * 64,
        state=proto.SubmissionState.DISPATCHED,
        dispatched=True,
    )
    coordinator._jobs[job.job_id] = job
    with pytest.raises(CancellationAmbiguous):
        await coordinator.cancel(job.job_id)
    assert job.state is proto.SubmissionState.UNKNOWN_SUBMISSION
    assert job.error is not None and job.error.retryable is False


async def test_cancelling_after_submission_does_not_claim_the_provider_stopped():
    transport = ScriptedTransport([_ok()])
    coordinator = _coordinator(transport)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    cancelled = await coordinator.cancel(job.job_id)
    assert cancelled.state is proto.SubmissionState.CANCELLED
    assert cancelled.error is not None
    assert cancelled.error.details["provider_may_still_complete"] is True
    assert cancelled.error.retryable is False


async def test_reconciliation_cannot_claim_success_without_a_media_id():
    transport = ScriptedTransport([asyncio.TimeoutError()])
    coordinator = _coordinator(transport, command_timeout=0.05)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert job.state is proto.SubmissionState.UNKNOWN_SUBMISSION

    with pytest.raises(ReconciliationRejected):
        coordinator.reconcile(job.job_id, status="succeeded")
    assert job.state is proto.SubmissionState.UNKNOWN_SUBMISSION


async def test_reconciliation_accepts_a_correlated_media_id():
    transport = ScriptedTransport([asyncio.TimeoutError()])
    coordinator = _coordinator(transport, command_timeout=0.05)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    reconciled = coordinator.reconcile(
        job.job_id, status="succeeded", media_id="media-0001", evidence_source="flow_tab"
    )
    assert reconciled.state is proto.SubmissionState.COMPLETED
    assert reconciled.lifecycle is proto.FlowLifecycle.SUCCEEDED
    assert reconciled.result["reconciled"] is True


async def test_reconciliation_rejects_a_foreign_media_id_or_wrong_request():
    transport = ScriptedTransport([_ok("media-0001")])
    coordinator = _coordinator(transport)
    first = await coordinator.submit("image", _request("first"), approval_binding="a" * 64)
    # A different job already owns media-0002's sibling id; claim one and then try
    # to reconcile another job with it.
    other = FlowJob(
        job_id="job-other",
        kind="image",
        request=_request("other"),
        approval_binding="b" * 64,
        state=proto.SubmissionState.UNKNOWN_SUBMISSION,
    )
    coordinator._jobs[other.job_id] = other
    coordinator._claim_media(other, "media-9999")

    with pytest.raises(ReconciliationRejected):
        coordinator.reconcile(first.job_id, status="succeeded", media_id="media-9999")
    with pytest.raises(ReconciliationRejected):
        coordinator.reconcile(first.job_id, request_hash="0" * 64)
    with pytest.raises(ReconciliationRejected):
        coordinator.reconcile(first.job_id, status="succeeded", media_id="bad id!")


async def test_inconclusive_reconciliation_leaves_the_job_unknown():
    transport = ScriptedTransport([asyncio.TimeoutError()])
    coordinator = _coordinator(transport, command_timeout=0.05)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    result = coordinator.reconcile(job.job_id, status="not_found")
    assert result.state is proto.SubmissionState.UNKNOWN_SUBMISSION
    assert result.result["reconciliation"] == "inconclusive"


async def test_a_reconciled_success_never_rearms_a_retry():
    transport = ScriptedTransport([asyncio.TimeoutError()])
    coordinator = _coordinator(transport, command_timeout=0.05)
    job = await coordinator.submit("image", _request(), approval_binding="a" * 64)
    coordinator.reconcile(job.job_id, status="succeeded", media_id="media-0001")
    with pytest.raises(Exception):
        await coordinator.retry(job.job_id, approval_binding="a" * 64)
    assert len(transport.sent) == 1


# --------------------------------------------------------------------------- D
# D. chunked asset export (§9)
# --------------------------------------------------------------------------- #
@pytest.fixture
def companion_settings(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLOW_COMPANION_ID", "companion-test")
    monkeypatch.setenv("FLOW_COMPANION_SECRET", "c" * 43)
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path / "storage"))
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def _companion_config(**overrides: Any):  # type: ignore[no-untyped-def]
    from app.flow.companion import CompanionConfig

    return CompanionConfig(
        backend_url="https://crusor.example",
        companion_id="companion-test",
        companion_secret="c" * 43,
        heartbeat_seconds=0.0,
        **overrides,
    )


def _split(data: bytes, size: int) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


async def test_chunked_export_creates_the_asset_only_when_complete(
    session, tmp_path, companion_settings
):
    from app.services.flow_gateway import FlowAssetExportService
    from app.storage.base import LocalFilesystemStorage

    storage = LocalFilesystemStorage(tmp_path / "assets")
    service = FlowAssetExportService(session, companion_settings, storage)
    media = _png(32, 40)
    checksum = hashlib.sha256(media).hexdigest()
    ticket, token = await service.create_ticket(
        companion_id="companion-test",
        expected_sha256=checksum,
        content_type="image/png",
        filename="flow-video-poster.png",
    )

    chunks = _split(media, 400)
    for index, chunk in enumerate(chunks):
        ticket, asset = await service.accept_chunk(
            token=token,
            index=index,
            total=len(chunks),
            chunk_sha256=hashlib.sha256(chunk).hexdigest(),
            data=chunk,
            companion_id="companion-test",
        )
        if index < len(chunks) - 1:
            assert asset is None
            assert ticket.status == "pending"
            assert ticket.chunks_received == index + 1

    assert asset is not None
    assert ticket.status == "consumed"
    assert ticket.upload_mode == "chunked"
    assert ticket.chunk_count == len(chunks)
    assert ticket.chunks_received == len(chunks)
    assert ticket.checksum == checksum
    assert ticket.received_bytes == len(media)
    assert asset.checksum == checksum
    assert await storage.get(asset.storage_key) == media
    assert asset.metadata_json["transfer"] == "chunked"

    # The staged chunks are gone, and the manifest keeps only provenance (no keys).
    assert not await storage.exists(f"flow/staging/{ticket.id}/000000.part")
    assert all("key" not in entry for entry in ticket.chunk_manifest)
    assert [entry["index"] for entry in ticket.chunk_manifest] == list(range(len(chunks)))


async def test_chunks_must_arrive_in_order(session, tmp_path, companion_settings):
    from app.services.flow_gateway import FlowAssetExportService
    from app.storage.base import LocalFilesystemStorage

    service = FlowAssetExportService(session, companion_settings, LocalFilesystemStorage(tmp_path / "a"))
    media = _png()
    ticket, token = await service.create_ticket(companion_id="companion-test", filename="x.png")
    chunks = _split(media, max(1, len(media) // 3))

    with pytest.raises(Exception) as excinfo:
        await service.accept_chunk(
            token=token,
            index=1,
            total=len(chunks),
            chunk_sha256=hashlib.sha256(chunks[1]).hexdigest(),
            data=chunks[1],
            companion_id="companion-test",
        )
    assert "in order" in str(excinfo.value)
    assert ticket.chunks_received == 0
    assert ticket.status == "pending"
    assert ticket.asset_id is None


async def test_a_duplicate_chunk_index_is_refused(session, tmp_path, companion_settings):
    from app.services.flow_gateway import FlowAssetExportService
    from app.storage.base import LocalFilesystemStorage

    service = FlowAssetExportService(session, companion_settings, LocalFilesystemStorage(tmp_path / "b"))
    media = _png()
    chunks = _split(media, max(1, len(media) // 2))
    ticket, token = await service.create_ticket(companion_id="companion-test", filename="x.png")

    await service.accept_chunk(
        token=token,
        index=0,
        total=len(chunks),
        chunk_sha256=hashlib.sha256(chunks[0]).hexdigest(),
        data=chunks[0],
        companion_id="companion-test",
    )
    with pytest.raises(Exception) as excinfo:
        await service.accept_chunk(
            token=token,
            index=0,
            total=len(chunks),
            chunk_sha256=hashlib.sha256(chunks[0]).hexdigest(),
            data=chunks[0],
            companion_id="companion-test",
        )
    assert "duplicate" in str(excinfo.value).lower()
    assert ticket.chunks_received == 1


async def test_a_missing_chunk_cannot_produce_an_asset(session, tmp_path, companion_settings):
    from sqlalchemy import select

    from app.models.creative import CreativeAsset
    from app.services.flow_gateway import FlowAssetExportService
    from app.storage.base import LocalFilesystemStorage

    service = FlowAssetExportService(session, companion_settings, LocalFilesystemStorage(tmp_path / "c"))
    media = _png()
    chunks = _split(media, max(1, len(media) // 3))
    ticket, token = await service.create_ticket(companion_id="companion-test", filename="x.png")

    ticket, asset = await service.accept_chunk(
        token=token,
        index=0,
        total=len(chunks),
        chunk_sha256=hashlib.sha256(chunks[0]).hexdigest(),
        data=chunks[0],
        companion_id="companion-test",
    )
    assert asset is None
    assert ticket.status == "pending"
    assert ticket.chunks_received < ticket.chunk_count
    assets = list((await session.execute(select(CreativeAsset))).scalars().all())
    assert assets == []


async def test_a_corrupted_chunk_fails_the_ticket(session, tmp_path, companion_settings):
    from sqlalchemy import select

    from app.models.creative import CreativeAsset
    from app.services.flow_gateway import FlowAssetExportService
    from app.storage.base import LocalFilesystemStorage

    service = FlowAssetExportService(session, companion_settings, LocalFilesystemStorage(tmp_path / "d"))
    media = _png()
    chunks = _split(media, max(1, len(media) // 2))
    ticket, token = await service.create_ticket(companion_id="companion-test", filename="x.png")

    with pytest.raises(Exception) as excinfo:
        await service.accept_chunk(
            token=token,
            index=0,
            total=len(chunks),
            chunk_sha256=hashlib.sha256(b"not the chunk").hexdigest(),
            data=chunks[0],
            companion_id="companion-test",
        )
    assert "checksum" in str(excinfo.value).lower()
    assert ticket.status == "failed"
    assert ticket.error_code == "chunk_checksum_mismatch"
    assert ticket.chunks_received == 0
    assets = list((await session.execute(select(CreativeAsset))).scalars().all())
    assert assets == []


async def test_an_oversized_chunk_or_assembled_asset_is_refused(session, tmp_path, companion_settings):
    from app.services.flow_gateway import FlowAssetExportService
    from app.storage.base import LocalFilesystemStorage

    service = FlowAssetExportService(session, companion_settings, LocalFilesystemStorage(tmp_path / "e"))
    ticket, token = await service.create_ticket(
        companion_id="companion-test", filename="x.png", max_bytes=100
    )
    with pytest.raises(Exception) as excinfo:
        await service.accept_chunk(
            token=token,
            index=0,
            total=2,
            chunk_sha256=hashlib.sha256(b"x" * 60).hexdigest(),
            data=b"x" * 60,
            companion_id="companion-test",
            max_chunk_bytes=32,
        )
    assert "exceed" in str(excinfo.value).lower()

    # Each chunk fits under the per-chunk ceiling but the assembled asset does not
    # fit the ticket, so the second chunk fails the ticket.
    await service.accept_chunk(
        token=token,
        index=0,
        total=2,
        chunk_sha256=hashlib.sha256(b"y" * 60).hexdigest(),
        data=b"y" * 60,
        companion_id="companion-test",
        max_chunk_bytes=64,
    )
    with pytest.raises(Exception):
        await service.accept_chunk(
            token=token,
            index=1,
            total=2,
            chunk_sha256=hashlib.sha256(b"z" * 60).hexdigest(),
            data=b"z" * 60,
            companion_id="companion-test",
            max_chunk_bytes=64,
        )
    assert ticket.status == "failed"
    assert ticket.error_code == "export_too_large"


async def test_a_chunk_on_an_expired_or_foreign_ticket_is_refused(
    session, tmp_path, companion_settings
):
    from app.services.flow_gateway import FlowAssetExportService
    from app.storage.base import LocalFilesystemStorage

    service = FlowAssetExportService(session, companion_settings, LocalFilesystemStorage(tmp_path / "f"))
    ticket, token = await service.create_ticket(companion_id="companion-test", filename="x.png")
    payload = b"chunk"

    with pytest.raises(Exception) as excinfo:
        await service.accept_chunk(
            token=token,
            index=0,
            total=2,
            chunk_sha256=hashlib.sha256(payload).hexdigest(),
            data=payload,
            companion_id="someone-else",
        )
    assert "different companion" in str(excinfo.value).lower()

    ticket.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await session.flush()
    with pytest.raises(Exception) as excinfo:
        await service.accept_chunk(
            token=token,
            index=0,
            total=2,
            chunk_sha256=hashlib.sha256(payload).hexdigest(),
            data=payload,
            companion_id="companion-test",
        )
    assert "expired" in str(excinfo.value).lower()


async def test_single_shot_and_chunked_transfers_cannot_be_mixed(
    session, tmp_path, companion_settings
):
    from app.services.flow_gateway import FlowAssetExportService
    from app.storage.base import LocalFilesystemStorage

    service = FlowAssetExportService(session, companion_settings, LocalFilesystemStorage(tmp_path / "g"))
    media = _png()
    chunks = _split(media, max(1, len(media) // 2))
    ticket, token = await service.create_ticket(companion_id="companion-test", filename="x.png")

    await service.accept_chunk(
        token=token,
        index=0,
        total=len(chunks),
        chunk_sha256=hashlib.sha256(chunks[0]).hexdigest(),
        data=chunks[0],
        companion_id="companion-test",
    )
    with pytest.raises(Exception) as excinfo:
        await service.accept_upload(
            token=token, data=media, companion_id="companion-test"
        )
    assert "chunked" in str(excinfo.value).lower()


async def test_an_assembled_asset_that_does_not_match_the_ticket_checksum_is_refused(
    session, tmp_path, companion_settings
):
    from app.services.flow_gateway import FlowAssetExportService
    from app.storage.base import LocalFilesystemStorage

    service = FlowAssetExportService(session, companion_settings, LocalFilesystemStorage(tmp_path / "h"))
    media = _png()
    chunks = _split(media, max(1, len(media) // 2))
    ticket, token = await service.create_ticket(
        companion_id="companion-test",
        filename="x.png",
        expected_sha256=hashlib.sha256(b"a different asset").hexdigest(),
    )
    for index, chunk in enumerate(chunks):
        try:
            await service.accept_chunk(
                token=token,
                index=index,
                total=len(chunks),
                chunk_sha256=hashlib.sha256(chunk).hexdigest(),
                data=chunk,
                companion_id="companion-test",
            )
        except Exception:
            break
    assert ticket.status == "failed"
    assert ticket.error_code == "export_checksum_mismatch"
    assert ticket.asset_id is None


# --------------------------------------------------------------------------- E
# E. the source and licensing gate (§2)
# --------------------------------------------------------------------------- #
def test_the_default_gate_is_blocked_and_says_why():
    gate = evaluate_source_gate()
    assert gate.status == STATUS_SOURCE_ABSENT
    assert gate.present is False
    assert gate.authorized is False
    payload = gate.as_dict()
    assert payload["can_vendor_source"] is False
    assert payload["upstream_remediation_allowed"] is False
    assert [item["blocker"] for item in payload["remediation_requirements"]] == [
        f"U{i}" for i in range(1, 8)
    ]
    assert "not supplied" in payload["reason"]


def test_a_package_without_a_licence_is_blocked_even_though_it_exists(tmp_path):
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    gate = evaluate_source_gate(tmp_path)
    assert gate.status == STATUS_LICENSING_BLOCKED
    assert gate.present is True
    assert gate.authorized is False
    assert "no licence" in gate.reason.lower()


def test_licence_evidence_moves_the_gate_but_never_authorizes_it(tmp_path):
    """Phase 3C.2.1: a file called LICENSE is evidence for a reviewer, not consent."""
    (tmp_path / "LICENSE").write_text(
        "MIT License\n\nPermission is hereby granted, free of charge, to any person.",
        encoding="utf-8",
    )
    gate = evaluate_source_gate(tmp_path)
    assert gate.status == STATUS_LICENSE_EVIDENCE_PRESENT
    assert gate.authorized is False
    assert gate.as_dict()["authorized_to_modify"] is False
    assert gate.as_dict()["can_vendor_source"] is False
    assert gate.as_dict()["evidence_alone_authorizes"] is False
    assert "LICENSE" in gate.license_files


def test_nothing_from_the_upstream_package_is_vendored_here():
    """The gate is only honest if the repository actually holds no upstream file."""
    markers = {
        "content-script-main.js",
        "content-script-isolated.js",
        "service-worker.js",
        "ws-bridge.js",
    }
    found: list[str] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        parts = set(path.parts)
        if parts & {".git", "node_modules", ".venv", "__pycache__", ".data"}:
            continue
        if path.name in markers:
            found.append(str(path.relative_to(REPO_ROOT)))
    assert found == [], f"upstream-looking files are vendored: {found}"


# --------------------------------------------------------------------------- F
# F. the read-only audit scanner (§13)
# --------------------------------------------------------------------------- #
def _run_audit(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "flow_source_audit.py"), *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
    )


def test_the_scanner_refuses_to_run_without_authorised_source():
    completed = _run_audit()
    assert completed.returncode == 3, completed.stderr[-800:]
    report = json.loads(completed.stdout)
    assert report["status"] == STATUS_SOURCE_ABSENT
    assert report["authorized"] is False
    assert report["scans_run"] == []
    assert report["findings"] == []


def test_the_scanner_refuses_an_unlicensed_package(tmp_path):
    (tmp_path / "manifest.json").write_text('{"version": "1.2.1"}', encoding="utf-8")
    completed = _run_audit("--package", str(tmp_path))
    assert completed.returncode == 3, completed.stderr[-800:]
    report = json.loads(completed.stdout)
    assert report["status"] == STATUS_LICENSING_BLOCKED
    assert report["scans_run"] == []


def test_the_scanner_finds_the_audited_defects_without_implying_authorization(tmp_path):
    package = tmp_path / "ext"
    (package / "mcp-server" / "src").mkdir(parents=True)
    (package / "LICENSE").write_text(
        "MIT License\n\nPermission is hereby granted, free of charge, to any person "
        "obtaining a copy of this software.",
        encoding="utf-8",
    )
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": 3,
                "version": "1.2.1",
                "permissions": ["debugger", "tabs"],
                "host_permissions": ["http://*/*", "https://*/*"],
            }
        ),
        encoding="utf-8",
    )
    (package / "service-worker.js").write_text(
        "\n".join(
            [
                'const HELLO = {type: "hello", version: "1.2.0"};',
                'window.postMessage({type: "DEEPX_AUTH_TOKEN_CAPTURED"}, "*");',
                'if (msg.type === "get_token") return token;',
                'if (msg.type === "get_flow_context") return ctx;',
                "api_post(url, body, token);",
                "batch_generate(); rapid_batch_dispatch();",
                "autonomous_storyboard_pipeline(); rename_project(); open_new_project();",
                "setTimeout(() => retry(1), 500);",
                'return {downloadId: id, status: "download_started"};',
            ]
        ),
        encoding="utf-8",
    )
    (package / "content-script-main.js").write_text('eval("1+1");\n', encoding="utf-8")
    (package / "content-script-isolated.js").write_text("// relay\n", encoding="utf-8")
    (package / "mcp-server" / "src" / "ws-bridge.js").write_text(
        "listen(8792)\n", encoding="utf-8"
    )
    (package / "mcp-server" / "src" / "server.js").write_text(
        "registerTool(eval_script)\n", encoding="utf-8"
    )
    (package / "mcp-server" / "package.json").write_text(
        '{"name": "mcp", "version": "1.0.0"}', encoding="utf-8"
    )
    (package / "README.md").write_text("DeepX Flow Connector v1.2.0\n", encoding="utf-8")

    completed = _run_audit("--package", str(package))
    # Exit 2 says exactly what is true: scanned, and still not authorized to modify.
    assert completed.returncode == 2, completed.stderr[-800:]
    report = json.loads(completed.stdout)
    assert report["status"] == STATUS_LICENSE_EVIDENCE_PRESENT
    assert report["authorized"] is False
    scans = {finding["scan"] for finding in report["findings"]}
    assert {"S1", "S2", "S3", "S4", "S6", "S8", "S9", "S10", "S11", "S12", "S13", "S14"} <= scans
    assert report["versions"]["consistent"] is False
    assert report["host_permissions"] == ["http://*/*", "https://*/*"]
    assert all(finding["needs_behavioural_test"] for finding in report["findings"])
    # Findings never carry credentials.
    serialised = json.dumps(report)
    assert "Bearer ey" not in serialised


# --------------------------------------------------------------------------- G
# G. the API surface reports the new contract
# --------------------------------------------------------------------------- #
COMPANION_HEADERS = {"X-Companion-Id": "companion-test", "X-Companion-Secret": "c" * 43}


async def test_status_exposes_the_source_gate_and_protocol_contract(client, auth, companion_settings):
    response = await client.get(f"{FLOW_API}/status", headers=auth())
    assert response.status_code == 200, response.text
    body = response.json()

    gate = body["source_gate"]
    assert gate["status"] == STATUS_SOURCE_ABSENT
    assert gate["authorized_to_modify"] is False
    assert len(gate["remediation_requirements"]) == 7

    protocol = body["protocol"]
    assert protocol["supported"] == list(proto.SUPPORTED_PROTOCOLS)
    assert protocol["requires_provenance"] is True
    assert protocol["allow_component_drift"] is False

    lifecycle = body["lifecycle"]
    assert lifecycle["states"][0] == "NOT_SUBMITTED"
    assert len(lifecycle["states"]) == 8
    assert lifecycle["auto_resubmit_after_unknown"] is False

    export = body["asset_export"]
    assert export["chunked"]["enabled"] is True
    assert export["chunked"]["ordering"] == "strict"
    assert body["live_generation_enabled"] is False


async def test_chunked_upload_over_http_creates_a_verified_asset(
    client, auth, session, users, brand_row, companion_settings
):
    media = _png(28, 34)
    checksum = hashlib.sha256(media).hexdigest()
    created = await client.post(
        f"{FLOW_API}/exports",
        json={
            "companion_id": "companion-test",
            "expected_sha256": checksum,
            "content_type": "image/png",
            "filename": "flow-chunked.png",
        },
        headers=auth("marketer"),
    )
    assert created.status_code == 201, created.text
    ticket = created.json()
    assert ticket["upload_mode"] == "single"
    assert ticket["max_chunks"] >= 1

    chunks = _split(media, 300)
    for index, chunk in enumerate(chunks):
        response = await client.post(
            f"{FLOW_API}/exports/{ticket['id']}/chunks",
            content=chunk,
            headers={
                **COMPANION_HEADERS,
                "X-Export-Token": ticket["upload_token"],
                "X-Chunk-Index": str(index),
                "X-Chunk-Count": str(len(chunks)),
                "X-Chunk-Sha256": hashlib.sha256(chunk).hexdigest(),
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["chunks_received"] == index + 1
        if index < len(chunks) - 1:
            assert body["asset_id"] is None
            assert body["complete"] is False

    assert body["status"] == "consumed"
    assert body["complete"] is True
    assert body["checksum"] == checksum
    assert body["asset_id"]
    assert body["upload_mode"] == "chunked"

    stored = await client.get(f"{FLOW_API}/exports/{ticket['id']}", headers=auth())
    assert stored.json()["chunks_received"] == len(chunks)


async def test_a_chunk_out_of_order_is_refused_over_http(client, auth, companion_settings):
    media = _png()
    created = await client.post(
        f"{FLOW_API}/exports",
        json={"companion_id": "companion-test", "filename": "flow-order.png"},
        headers=auth("marketer"),
    )
    ticket = created.json()
    chunks = _split(media, max(1, len(media) // 2))
    response = await client.post(
        f"{FLOW_API}/exports/{ticket['id']}/chunks",
        content=chunks[1],
        headers={
            **COMPANION_HEADERS,
            "X-Export-Token": ticket["upload_token"],
            "X-Chunk-Index": "1",
            "X-Chunk-Count": str(len(chunks)),
            "X-Chunk-Sha256": hashlib.sha256(chunks[1]).hexdigest(),
        },
    )
    assert response.status_code == 422, response.text
    assert "in order" in response.json()["error"]["message"]


async def test_generation_decisions_echo_the_lifecycle_and_idempotency_key(
    client, auth, session, users, brand_row
):
    from app.core.config import Settings
    from app.models.approval import ApprovalRecord
    from app.models.content import ContentItem, ContentVersion
    from app.services.creative_briefs import CreativeBriefService
    from app.services.flow_approval import FlowGenerationBinding

    item = ContentItem(
        brand_profile_id=brand_row.id,
        title="3C.2 gated post",
        platform="instagram",
        format="social_post",
        status="DRAFT",
    )
    session.add(item)
    await session.flush()
    version = ContentVersion(
        content_item_id=item.id,
        version_number=1,
        headline="Ship",
        body="Body",
        content_hash="c-3c2-1",
    )
    session.add(version)
    await session.flush()
    item.current_version_id = version.id
    brief, _created = await CreativeBriefService(
        session, Settings(environment="test")
    ).create(content_version_id=version.id, asset_type="image")
    await session.flush()

    command = proto.GenerationCommand(prompt="A charcoal frame", model="imagen-3", count=1)
    binding = FlowGenerationBinding.from_brief(
        brief, request=command, provider="flow", model="imagen-3"
    )
    session.add(
        ApprovalRecord(
            content_item_id=item.id,
            content_version_id=version.id,
            action="approve",
            actor_role="founder",
            flow_binding_hash=binding.hash(),
            flow_prompt_hash=binding.prompt_hash,
            flow_provider="flow",
            flow_model=binding.model,
            flow_aspect_ratio=binding.aspect_ratio,
            flow_duration_seconds=binding.duration_seconds,
            flow_output_count=binding.output_count,
        )
    )
    await session.commit()

    response = await client.post(
        f"{FLOW_API}/generations",
        json={
            "brief_id": str(brief.id),
            "prompt": "A charcoal frame",
            "model": "imagen-3",
            "aspect_ratio": "4:5",
            "idempotency_key": "gen-key-1",
            "dry_run": True,
        },
        headers=auth("marketer"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["authorized"] is True
    assert body["dispatched"] is False
    assert body["lifecycle"] == "NOT_SUBMITTED"
    assert body["idempotency_key"] == "gen-key-1"
    assert body["live_generation_enabled"] is False


async def test_the_flow_surface_still_carries_no_dangerous_route(client):
    schema = (await client.get("/openapi.json")).json()
    paths = " ".join(schema["paths"])
    for token in ("eval", "api_post", "get_token", "native_", "storyboard", "script"):
        assert token not in paths
    assert "chunks" in paths, "the chunked export route should exist"
