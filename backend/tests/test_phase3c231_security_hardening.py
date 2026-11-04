"""Phase 3C.2.3.1 — security hardening and trust-boundary corrections.

Five findings from the independent review of Phase 3C.2.3, each with tests that fail
if the correction is reverted:

1. the budget ledger refunded nothing and could be satisfied by an infinite cap;
2. traversal resolved paths before opening them, so a parent directory could be
   swapped between resolution and read;
3. a **validly signed but incomplete** v2 manifest was live-capable, so "signed"
   stood in for "identified";
4. ``FLOW_REQUIRED_COMPONENTS`` was documented as additive but enforced as a
   replacement list;
5. the bridge read the source-package path from ``os.environ`` while the rest of the
   application read Pydantic settings.

Everything here is synthetic: temporary trees, self-generated Ed25519 keys, in-process
settings objects. No DeepX source, no browser, no Google account, no paid call.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import app.flow.installed_artifact as artifact_module
from app.core.config import Settings
from app.flow import protocol as proto
from app.flow.bridge import (
    BridgeConfig,
    BridgeConfigError,
    ComponentInstallSpec,
    ExtensionSession,
    FlowBridge,
)
from app.flow.coordinator import (
    CommandTransport,
    GenerationCoordinator,
    IdempotencyConflict,
    JobInProgress,
    LiveGenerationDisabled,
)
from app.flow.installed_artifact import (
    LAYOUT_TREE,
    InstalledArtifactVerifier,
    InstallationSpec,
    MeasurementError,
    MeasurementPolicy,
)
from app.flow.provenance import (
    COMPONENT_COMPANION,
    COMPONENT_EXTENSION,
    COMPONENT_MCP,
    ProvenanceRejected,
    TrustPolicy,
    resolve_trust_anchor,
    verify_release_manifest,
)
from app.flow.security import generate_pairing_id, generate_pairing_secret
from app.flow.source_gate import STATUS_AUTHORIZED, STATUS_LICENSE_EVIDENCE_PRESENT
from app.flow.trust import (
    COMPONENT_VERIFIED,
    UNVERIFIED,
    VERIFIED,
    AttestationRejected,
    ChallengeStore,
    ComponentReport,
    SessionAttestation,
    SpendingControls,
    TrustEvidence,
    UntrustedArtifact,
    VerificationSubmission,
    aggregate_trust,
)
from app.services.flow_budget import (
    BudgetUnavailable,
    FlowBudgetLedger,
    InvalidBudgetCap,
    flow_ledger_from_settings,
)

from tests.flow_correlation import correlated
from tests.test_phase3c21_trust_boundaries import _attestation, _package, _review
from tests.test_phase3c22_installed_artifact import (
    BUILD_ID,
    EXT_ID,
    VERSION,
    _extension_dir,
    _keypair,
    _manifest_document,
    _sign,
    _write_manifest,
)

BACKEND = Path(__file__).resolve().parents[1]
HEX_ID = "a" * 32
COMPANION_ID = "companion-3c231"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _mcp_tree(base: Path, name: str = "mcp-server") -> Path:
    root = base / name
    (root / "node_modules" / "sdk").mkdir(parents=True, exist_ok=True)
    (root / "server.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    (root / "node_modules" / "sdk" / "index.js").write_text(
        "module.exports = 1;\n", encoding="utf-8"
    )
    return root


def _release(
    tmp_path: Path,
    *,
    components: tuple[str, ...] = (COMPONENT_EXTENSION, COMPONENT_MCP),
    mutate: Any = None,
) -> dict[str, Any]:
    """A signed two-component release; ``mutate`` edits the document before signing."""
    extension_root = _extension_dir(tmp_path)
    mcp_root = _mcp_tree(tmp_path)
    extension_report = InstalledArtifactVerifier(COMPONENT_EXTENSION).measure(
        InstallationSpec(
            root=extension_root, expected_extension_id=EXT_ID, expected_version=VERSION
        )
    )
    mcp_report = InstalledArtifactVerifier(COMPONENT_MCP).measure(
        InstallationSpec(root=mcp_root, layout=LAYOUT_TREE)
    )
    private, public = _keypair()
    entries = [
        {
            "name": COMPONENT_EXTENSION,
            "version": VERSION,
            "sha256": extension_report.digest,
            "extension_id": EXT_ID,
            "protocol": proto.PROTOCOL_VERSION,
            "build_id": BUILD_ID,
            "digest_algorithm": extension_report.digest_algorithm,
            "required": True,
        },
        {
            "name": COMPONENT_MCP,
            "version": VERSION,
            "build_id": BUILD_ID,
            "sha256": mcp_report.digest,
            "digest_algorithm": extension_report.digest_algorithm,
            "required": True,
        },
    ]
    if COMPONENT_COMPANION in components:
        companion_root = _mcp_tree(tmp_path, "companion")
        companion_report = InstalledArtifactVerifier(COMPONENT_COMPANION).measure(
            InstallationSpec(root=companion_root, layout=LAYOUT_TREE)
        )
        entries.append(
            {
                "name": COMPONENT_COMPANION,
                "version": VERSION,
                "build_id": BUILD_ID,
                "sha256": companion_report.digest,
                "required": True,
            }
        )
    else:
        companion_root = None

    document = {
        **_manifest_document(extension_report.digest, components=entries),
        "digest_algorithm": extension_report.digest_algorithm,
    }
    if mutate is not None:
        document = mutate(document)
    manifest_path = _write_manifest(tmp_path / "release-3c231.json", document, private)
    return {
        "extension_root": extension_root,
        "mcp_root": mcp_root,
        "companion_root": companion_root,
        "extension_digest": extension_report.digest,
        "mcp_digest": mcp_report.digest,
        "private": private,
        "public": public,
        "manifest_path": manifest_path,
        "anchor": verify_release_manifest(manifest_path, public),
    }


@pytest.fixture(autouse=True)
def authorized_source_gate(request, monkeypatch):
    """Substitute an authorized upstream gate for this module only.

    On this machine the real gate is ``SOURCE_ABSENT`` — there is no DeepX source and
    no licence — which blocks ``VERIFIED`` before any measurement is consulted. These
    tests exercise the measurement and budget machinery, so they substitute the gate
    explicitly. The gate's own behaviour is tested directly (``test_source_gate_*``),
    and production never patches it.
    """
    if request.node.name.startswith(
        (
            "test_settings",
            "test_the_environment_",
            "test_missing_settings",
            "test_the_configured_attestation",
            "test_pydantic_settings",
        )
    ):
        yield
        return
    monkeypatch.setattr(
        FlowBridge, "_source_gate_status", lambda self: "AUTHORIZED", raising=True
    )
    yield


@pytest.fixture()
def release(tmp_path: Path) -> dict[str, Any]:
    return _release(tmp_path)


class _PendingTransport(CommandTransport):
    """Records what was sent and never completes the submission."""

    def __init__(self, *, fail_send: bool = False) -> None:
        self.sent: list[proto.CommandEnvelope] = []
        self.by_id: dict[str, proto.CommandEnvelope] = {}
        self.fail_send = fail_send

    async def send(self, envelope: proto.CommandEnvelope) -> None:
        if self.fail_send:
            raise ConnectionError("socket closed before the frame left")
        self.sent.append(envelope)
        self.by_id[envelope.id] = envelope

    def correlated(self, command_id: str, result: Any) -> Any:
        """Answer *result* as an extension would: echoing the command it answers."""
        return correlated(self.by_id.get(command_id), result)

    async def await_result(self, command_id: str, timeout: float) -> Any:
        raise asyncio.TimeoutError


class _Settings:
    """A stand-in for Pydantic settings, built per test."""

    def __init__(self, **values: Any) -> None:
        defaults: dict[str, Any] = {
            "flow_generation_budget_usd": 0.0,
            "flow_budget_enforcement": "none",
            "flow_source_package_path": "",
            "flow_authorization_attestation_path": "",
            "flow_required_components": "",
            "flow_mcp_install_root": "",
        }
        defaults.update(values)
        for key, value in defaults.items():
            setattr(self, key, value)


def _request(prompt: str = "A charcoal frame") -> proto.GenerationCommand:
    return proto.GenerationCommand(prompt=prompt, model="imagen-3", count=1)


def _coordinator(transport: CommandTransport, ledger: Any, **kwargs: Any) -> GenerationCoordinator:
    return GenerationCoordinator(
        transport,
        live_generation_enabled=True,
        budget_ledger=ledger,
        max_live_jobs=kwargs.pop("max_live_jobs", 4),
        **kwargs,
    )


def _verified_decision(release: dict[str, Any]) -> Any:
    moment = time.time()
    evidence = TrustEvidence(
        supported_protocol=True,
        claims_well_formed=True,
        components_consistent=True,
        release_anchor_valid=True,
        installed_artifact_measured=True,
        installed_artifact_digest_matches_anchor=True,
        runtime_identity_correlated=True,
        measurement_fresh=True,
        measurement_id=HEX_ID,
        measured_at=moment - 1,
        measurement_expires_at=moment + 300,
        measurement_source="local_companion",
        component_states={
            COMPONENT_EXTENSION: COMPONENT_VERIFIED,
            COMPONENT_MCP: COMPONENT_VERIFIED,
        },
        required_components=(COMPONENT_EXTENSION, COMPONENT_MCP),
    )
    return aggregate_trust(
        source_gate_status="AUTHORIZED",
        anchor=release["anchor"],
        provenance=None,
        evidence=evidence,
        session_id="session-1",
        now=moment,
    )


def _report(name: str, digest: str, **overrides: Any) -> ComponentReport:
    payload: dict[str, Any] = {
        "name": name,
        "digest": digest,
        "extension_id": EXT_ID if name == COMPONENT_EXTENSION else "",
        "version": VERSION if name == COMPONENT_EXTENSION else "",
        "measurement_id": HEX_ID,
        "measured_at": overrides.pop("measured_at", time.time()),
        "file_count": 3,
        "digest_algorithm": "sha256-tree-runtime-v1",
    }
    payload.update(overrides)
    return ComponentReport(**payload)


# --------------------------------------------------------------------------- #
# 1. budget reservation lifecycle
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "cap",
    [float("nan"), float("inf"), float("-inf"), 0, 0.0, -1, -0.0001],
)
def test_an_unusable_cap_never_becomes_an_enforceable_ledger(cap, tmp_path):
    with pytest.raises(InvalidBudgetCap):
        FlowBudgetLedger(cap_usd=cap)
    settings = _Settings(flow_budget_enforcement="flow_budget_ledger", flow_generation_budget_usd=cap)
    assert flow_ledger_from_settings(settings) is None
    assert SpendingControls(enforcement="flow_budget_ledger", cap_usd=cap, ledger_wired=True).active is False


def test_a_finite_positive_cap_does_build_a_ledger():
    ledger = flow_ledger_from_settings(
        _Settings(flow_budget_enforcement="flow_budget_ledger", flow_generation_budget_usd=2.5)
    )
    assert ledger is not None and ledger.enforceable is True
    assert ledger.cap_usd == 2.5


async def test_an_identical_replay_reserves_nothing_extra():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = _PendingTransport()
    coordinator = _coordinator(transport, ledger)

    first = await coordinator.submit(
        "image", _request("same"), approval_binding="a" * 64, idempotency_key="k1"
    )
    spent_after_first = ledger.spent_usd
    assert spent_after_first == pytest.approx(0.04)
    assert len(transport.sent) == 1

    second = await coordinator.submit(
        "image", _request("same"), approval_binding="a" * 64, idempotency_key="k1"
    )
    assert second.job_id == first.job_id
    assert ledger.spent_usd == spent_after_first
    assert ledger.reservations == 1
    assert len(transport.sent) == 1

    # A replay identified by request hash alone (no key) is also free.
    third = await coordinator.submit("image", _request("same"), approval_binding="a" * 64)
    assert third.job_id == first.job_id
    assert ledger.spent_usd == spent_after_first


async def test_a_conflicting_idempotency_key_reserves_nothing():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    coordinator = _coordinator(_PendingTransport(), ledger)
    await coordinator.submit(
        "image", _request("first"), approval_binding="a" * 64, idempotency_key="k1"
    )
    spent = ledger.spent_usd
    with pytest.raises(IdempotencyConflict):
        await coordinator.submit(
            "image", _request("different"), approval_binding="a" * 64, idempotency_key="k1"
        )
    assert ledger.spent_usd == spent
    assert ledger.reservations == 1


async def test_an_existing_live_job_reserves_nothing():
    class Hanging(_PendingTransport):
        async def await_result(self, command_id, timeout):
            await asyncio.Event().wait()  # never resolves: the job stays live

    ledger = FlowBudgetLedger(cap_usd=5.0)
    coordinator = _coordinator(Hanging(), ledger, max_live_jobs=1)
    first = asyncio.create_task(
        coordinator.submit("image", _request("one"), approval_binding="a" * 64)
    )
    for _ in range(50):
        if coordinator.live_jobs:
            break
        await asyncio.sleep(0.01)
    assert coordinator.live_jobs, "the first submission never became live"
    spent = ledger.spent_usd
    with pytest.raises(JobInProgress):
        await coordinator.submit("image", _request("two"), approval_binding="a" * 64)
    assert ledger.spent_usd == spent
    assert ledger.reservations == 1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first


async def test_a_proven_pre_dispatch_failure_releases_the_reservation():
    """Phase 3C.2.3.2: only *proven* non-delivery refunds.

    This test used to raise a bare ``ConnectionError`` from ``send()`` and expect a
    refund. That was the defect: once a socket send is entered, an exception does not
    prove the frame stayed home. A transport now has to say so explicitly, with
    :class:`~app.flow.delivery.DefiniteNonDelivery`.
    """
    from app.flow.delivery import DefiniteNonDelivery

    class NotDelivered(_PendingTransport):
        async def send(self, envelope):
            raise DefiniteNonDelivery("the session was never paired")

    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = NotDelivered()
    coordinator = _coordinator(transport, ledger)
    job = await coordinator.submit("image", _request("x"), approval_binding="a" * 64)
    assert job.state.value == "failed"
    assert job.dispatched is False
    assert job.delivery.value == "definitely_not_sent"
    assert transport.sent == []
    assert ledger.spent_usd == 0.0
    assert ledger.reservations == 0
    assert coordinator._reservations == {}
    # Proven non-delivery is retryable.
    assert job.error is not None and job.error.retryable is True


async def test_an_ambiguous_result_keeps_the_reservation():
    ledger = FlowBudgetLedger(cap_usd=5.0)
    coordinator = _coordinator(_PendingTransport(), ledger)
    job = await coordinator.submit("image", _request("x"), approval_binding="a" * 64)
    assert job.state.value == "unknown_submission"
    assert ledger.spent_usd == pytest.approx(0.04), "an ambiguous dispatch may already be billed"
    assert ledger.reservations == 1


async def test_a_confirmed_dispatch_keeps_the_reservation():
    ledger = FlowBudgetLedger(cap_usd=5.0)

    class Answered(_PendingTransport):
        async def await_result(self, command_id, timeout):
            return self.correlated(
                command_id,
                proto.ResultEnvelope(
                    type="result", id=command_id, ok=True, job_id="",
                    result={"media_id": "media-0001"},
                ),
            )

    coordinator = _coordinator(Answered(), ledger)
    job = await coordinator.submit("image", _request("x"), approval_binding="a" * 64)
    assert job.state.value == "submitted"
    assert ledger.spent_usd == pytest.approx(0.04)


async def test_a_provider_reported_non_dispatch_releases_the_reservation():
    ledger = FlowBudgetLedger(cap_usd=5.0)

    class NotDispatched(_PendingTransport):
        async def await_result(self, command_id, timeout):
            return self.correlated(
                command_id,
                proto.ResultEnvelope(
                    type="result",
                    id=command_id,
                    ok=False,
                    job_id="",
                    error={"code": "quota", "message": "nope", "dispatched": False},
                ),
            )

    coordinator = _coordinator(NotDispatched(), ledger)
    job = await coordinator.submit("image", _request("x"), approval_binding="a" * 64)
    # Phase 3C.2.3.3 correction: this test used to assert `dispatched is False` here,
    # which conflated two events. The frame WAS delivered (transport SENT); what the
    # provider answer establishes is that no generation was submitted. The budget is
    # still released, because nothing was billable.
    assert job.delivery.value == "sent"
    assert job.provider_dispatch.value == "not_dispatched"
    assert job.dispatched is True, "the command reached the extension"
    assert ledger.spent_usd == 0.0
    assert ledger.reservations == 0


async def test_rejected_requests_never_reserve():
    from app.flow.coordinator import ApprovalRequired

    ledger = FlowBudgetLedger(cap_usd=5.0)
    transport = _PendingTransport()

    disabled = GenerationCoordinator(transport, live_generation_enabled=False, budget_ledger=ledger)
    with pytest.raises(LiveGenerationDisabled):
        await disabled.submit("image", _request(), approval_binding="a" * 64)

    untrusted = _coordinator(transport, ledger, trust_provider=lambda: None)
    with pytest.raises(UntrustedArtifact):
        await untrusted.submit("image", _request(), approval_binding="a" * 64)

    no_approval = _coordinator(transport, ledger)
    with pytest.raises(ApprovalRequired):
        await no_approval.submit("image", _request(), approval_binding="")

    assert ledger.spent_usd == 0.0
    assert ledger.reservations == 0
    assert transport.sent == []


def test_the_ledger_is_atomic_under_concurrent_reservations():
    """Two reservations cannot both fit when only one does."""
    ledger = FlowBudgetLedger(cap_usd=0.05)  # exactly one image estimate
    results: list[str] = []
    barrier = threading.Barrier(8)
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        try:
            ledger.reserve(kind="image")
            outcome = "reserved"
        except BudgetUnavailable:
            outcome = "refused"
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count("reserved") == 1, results
    assert results.count("refused") == 7, results
    assert ledger.spent_usd == pytest.approx(0.04)
    assert ledger.reservations == 1


async def test_concurrent_submissions_cannot_overspend():
    ledger = FlowBudgetLedger(cap_usd=0.05)
    coordinator = _coordinator(_PendingTransport(), ledger, max_live_jobs=8)

    outcomes = await asyncio.gather(
        *[
            coordinator.submit("image", _request(f"prompt-{index}"), approval_binding="a" * 64)
            for index in range(6)
        ],
        return_exceptions=True,
    )
    dispatched = [item for item in outcomes if not isinstance(item, Exception)]
    assert len(dispatched) == 1, outcomes
    assert ledger.spent_usd == pytest.approx(0.04)
    assert ledger.reservations == 1


def test_the_guard_reports_a_nan_cap_as_no_control():
    controls = SpendingControls(enforcement="flow_budget_ledger", cap_usd=float("nan"), ledger_wired=True)
    assert controls.active is False


# --------------------------------------------------------------------------- #
# 2. traversal races
# --------------------------------------------------------------------------- #
def _spec(root: Path, **kwargs: Any) -> InstallationSpec:
    return InstallationSpec(
        root=root,
        layout=kwargs.pop("layout", LAYOUT_TREE),
        policy=MeasurementPolicy(**kwargs),
    )


def test_a_symlinked_parent_directory_is_refused(tmp_path):
    real = _mcp_tree(tmp_path, "real")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(link))
    assert excinfo.value.code in {"symlink_in_package", "path_outside_root"}


def test_a_symlinked_ancestor_of_the_root_is_refused(tmp_path):
    real = _mcp_tree(tmp_path, "real")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    nested = link / "node_modules"
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(nested))
    assert excinfo.value.code == "symlink_in_package"


def test_a_directory_replaced_during_traversal_fails_closed(tmp_path, monkeypatch):
    """The name can be repointed at a new directory; the descriptor cannot."""
    root = _mcp_tree(tmp_path)
    real_open_child = artifact_module._open_child_dir
    swapped = {"done": False}

    def open_then_swap(parent_fd: int, name: str, relative: str, **kwargs: Any) -> int:
        child = real_open_child(parent_fd, name, relative, **kwargs)
        # Only the measured tree's own directory is swapped; the root acquisition walk
        # below "/" must be left alone (it now goes through this same helper).
        if not swapped["done"] and name == "node_modules":
            swapped["done"] = True
            # The descriptor keeps measuring the directory that was opened, while the
            # name now points somewhere else. The parent's re-listing refuses it.
            os.rename(name, f"{name}-old", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.mkdir(name, dir_fd=parent_fd)
        return child

    monkeypatch.setattr(artifact_module, "_open_child_dir", open_then_swap)
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(root))
    assert excinfo.value.code in {
        "directory_replaced_during_measurement",
        "file_added_during_measurement",
        "file_replaced_during_measurement",
    }
    assert swapped["done"] is True


def test_a_file_replaced_during_the_scan_fails_closed(tmp_path, monkeypatch):
    root = _mcp_tree(tmp_path)
    real_read = artifact_module._read_file_at
    replaced = {"done": False}

    def read_then_replace(dir_fd, name, relative, *, max_bytes):
        data, info = real_read(dir_fd, name, relative, max_bytes=max_bytes)
        if relative == "server.py" and not replaced["done"]:
            replaced["done"] = True
            os.unlink(name, dir_fd=dir_fd)
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=dir_fd)
            try:
                os.write(fd, b"def main():\n    return 1\n")
            finally:
                os.close(fd)
        return data, info

    monkeypatch.setattr(artifact_module, "_read_file_at", read_then_replace)
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(root))
    # Either code is correct here: the file is a different file (inode) *and* its
    # bytes changed. Both are refusals.
    assert excinfo.value.code in {
        "file_replaced_during_measurement",
        "file_mutated_during_measurement",
    }
    assert replaced["done"] is True


def test_a_file_mutated_during_the_scan_fails_closed(tmp_path, monkeypatch):
    """Same inode, new bytes: an inode check alone would accept this."""
    root = _mcp_tree(tmp_path)
    real_read = artifact_module._read_file_at
    mutated = {"done": False}

    def read_then_rewrite(dir_fd, name, relative, *, max_bytes):
        data, info = real_read(dir_fd, name, relative, max_bytes=max_bytes)
        if relative == "server.py" and not mutated["done"]:
            mutated["done"] = True
            fd = os.open(name, os.O_WRONLY, dir_fd=dir_fd)
            try:
                os.write(fd, b"def main():\n    return 9\n")
            finally:
                os.close(fd)
        return data, info

    monkeypatch.setattr(artifact_module, "_read_file_at", read_then_rewrite)
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(root))
    assert excinfo.value.code == "file_mutated_during_measurement"
    assert mutated["done"] is True


def test_a_file_removed_during_the_scan_fails_closed(tmp_path, monkeypatch):
    root = _mcp_tree(tmp_path)
    real_read = artifact_module._read_file_at
    removed = {"done": False}

    def read_then_unlink(dir_fd, name, relative, *, max_bytes):
        data, info = real_read(dir_fd, name, relative, max_bytes=max_bytes)
        if relative == "server.py" and not removed["done"]:
            removed["done"] = True
            os.unlink(name, dir_fd=dir_fd)
        return data, info

    monkeypatch.setattr(artifact_module, "_read_file_at", read_then_unlink)
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(root))
    assert excinfo.value.code in {
        "file_replaced_during_measurement",
        "file_added_during_measurement",
    }


def test_enumeration_errors_fail_closed(tmp_path, monkeypatch):
    root = _mcp_tree(tmp_path)
    real_scandir = os.scandir

    def failing_scandir(path=None, *args, **kwargs):
        if isinstance(path, int):
            raise PermissionError(13, "Permission denied")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", failing_scandir)
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(root))
    assert excinfo.value.code == "enumeration_failed"


def test_an_unsupported_platform_refuses_rather_than_downgrading(tmp_path, monkeypatch):
    root = _mcp_tree(tmp_path)
    monkeypatch.setattr(artifact_module, "DIR_FD_SUPPORTED", False)
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(root))
    assert excinfo.value.code == "unsupported_platform"


def test_the_measurement_does_not_claim_an_atomic_snapshot(tmp_path):
    report = InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(_mcp_tree(tmp_path)))
    assert any(item.startswith("no_atomic_snapshot") for item in report.limitations)
    assert "descriptor_relative_traversal" in report.checks


def test_bounds_still_apply(tmp_path):
    root = _mcp_tree(tmp_path)
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(root, max_files=1))
    assert excinfo.value.code == "too_many_files"

    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(root, max_file_bytes=2))
    assert excinfo.value.code == "file_too_large"

    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier(COMPONENT_MCP).measure(_spec(root, max_total_bytes=4))
    assert excinfo.value.code == "package_too_large"


# --------------------------------------------------------------------------- #
# 3. signed v2 release identity
# --------------------------------------------------------------------------- #
def _drop(section: str, key: str):
    def mutate(document: dict[str, Any]) -> dict[str, Any]:
        document[section].pop(key, None)
        return document

    return mutate


def _set(section: str, key: str, value: Any):
    def mutate(document: dict[str, Any]) -> dict[str, Any]:
        document[section][key] = value
        return document

    return mutate


def _set_top(key: str, value: Any):
    def mutate(document: dict[str, Any]) -> dict[str, Any]:
        document[key] = value
        return document

    return mutate


def _drop_top(key: str):
    def mutate(document: dict[str, Any]) -> dict[str, Any]:
        document.pop(key, None)
        return document

    return mutate


_MISSING = object()


def _component_set(index: int, key: str, value: Any):
    def mutate(document: dict[str, Any]) -> dict[str, Any]:
        document["components"][index][key] = value
        return document

    return mutate


def _component_and_top_drop(index: int, key: str, section: str, top_key: str | None = None):
    """Drop the component-level declaration *and* its top-level fallback."""

    def mutate(document: dict[str, Any]) -> dict[str, Any]:
        document["components"][index].pop(key, None)
        document[section].pop(top_key or key, None)
        return document

    return mutate


def _component_and_top_set(index: int, key: str, section: str, value: Any):
    def mutate(document: dict[str, Any]) -> dict[str, Any]:
        document["components"][index][key] = value
        document[section][key] = value
        return document

    return mutate


def _algorithm(value: Any):
    """Strip every per-component algorithm declaration, then set (or drop) the top one."""

    def mutate(document: dict[str, Any]) -> dict[str, Any]:
        for component in document["components"]:
            component.pop("digest_algorithm", None)
        if value is _MISSING:
            document.pop("digest_algorithm", None)
        else:
            document["digest_algorithm"] = value
        return document

    return mutate


def _component_drop(index: int, key: str):
    def mutate(document: dict[str, Any]) -> dict[str, Any]:
        document["components"][index].pop(key, None)
        return document

    return mutate


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_drop("release", "version"), id="no-release-version"),
        pytest.param(_set("release", "version", ""), id="empty-release-version"),
        pytest.param(_set("release", "version", "latest"), id="version-not-semver"),
        pytest.param(_set("release", "version", "1.2"), id="version-partial"),
        pytest.param(_drop("release", "commit"), id="no-commit"),
        pytest.param(_set("release", "commit", "not-a-sha"), id="commit-not-hex"),
        pytest.param(
            _component_and_top_drop(0, "build_id", "release"), id="no-build-id"
        ),
        pytest.param(
            _component_and_top_set(0, "build_id", "release", "b1"),
            id="build-id-too-short",
        ),
        pytest.param(_component_set(0, "extension_id", "not-a-chrome-id"), id="bad-extension-id"),
        pytest.param(
            _component_and_top_drop(0, "extension_id", "extension", "id"),
            id="no-extension-id",
        ),
        pytest.param(
            _component_and_top_drop(0, "protocol", "extension"), id="no-protocol"
        ),
        pytest.param(_component_set(0, "protocol", "flow.bridge.v0"), id="unsupported-protocol"),
        pytest.param(_algorithm(_MISSING), id="no-digest-algorithm"),
        pytest.param(_algorithm(""), id="empty-digest-algorithm"),
    ],
)
def test_a_signed_but_incomplete_manifest_is_not_live_capable(tmp_path, mutate):
    """A valid signature over an incomplete document proves authenticity, not identity."""
    built = _release(tmp_path, mutate=mutate)
    anchor = built["anchor"]
    assert anchor.signature_verified is True
    assert anchor.present is True
    assert anchor.live_capable is False, anchor.as_dict()


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_component_set(1, "sha256", "not-a-digest"), id="component-digest-malformed"),
        pytest.param(_component_drop(1, "sha256"), id="component-digest-missing"),
        pytest.param(_algorithm("sha1-tree"), id="unsupported-algorithm"),
        pytest.param(_drop_top("digest_algorithm"), id="algorithm-undeclared-but-asserted"),
    ],
)
def test_a_malformed_manifest_is_refused_outright(tmp_path, mutate):
    with pytest.raises(ProvenanceRejected):
        _release(tmp_path, mutate=mutate)


def test_a_duplicate_component_declaration_is_refused(tmp_path):
    def duplicate(document):
        document["components"].append(dict(document["components"][0]))
        return document

    with pytest.raises(ProvenanceRejected) as excinfo:
        _release(tmp_path, mutate=duplicate)
    assert "more than once" in str(excinfo.value)


def test_an_unknown_component_declaration_is_refused(tmp_path):
    def unknown(document):
        document["components"].append({"name": "evil_sidecar", "sha256": "a" * 64})
        return document

    with pytest.raises(ProvenanceRejected) as excinfo:
        _release(tmp_path, mutate=unknown)
    assert "unknown component" in str(excinfo.value).lower()


def test_a_contradictory_extension_digest_is_refused(tmp_path):
    def contradict(document):
        document["extension"]["sha256"] = "b" * 64
        return document

    with pytest.raises(ProvenanceRejected):
        _release(tmp_path, mutate=contradict)


def test_a_signed_but_incomplete_anchor_cannot_reach_verified(release):
    """Perfect measurement evidence is not enough if the anchor cannot identify it."""
    anchor = resolve_trust_anchor(
        TrustPolicy(
            release_manifest_path=str(release["manifest_path"]),
            release_manifest_public_key=release["public"],
        )
    )
    incomplete = type(anchor)(
        **{
            **{field: getattr(anchor, field) for field in anchor.__dataclass_fields__},
            "build_id": "",
        }
    )
    assert incomplete.live_capable is False
    evidence = TrustEvidence(
        supported_protocol=True,
        claims_well_formed=True,
        components_consistent=True,
        release_anchor_valid=True,
        installed_artifact_measured=True,
        installed_artifact_digest_matches_anchor=True,
        runtime_identity_correlated=True,
        measurement_fresh=True,
        measurement_expires_at=time.time() + 300,
        component_states={
            COMPONENT_EXTENSION: COMPONENT_VERIFIED,
            COMPONENT_MCP: COMPONENT_VERIFIED,
        },
        required_components=(COMPONENT_EXTENSION, COMPONENT_MCP),
    )
    decision = aggregate_trust(
        source_gate_status="AUTHORIZED",
        anchor=incomplete,
        provenance=None,
        evidence=evidence,
        now=time.time(),
    )
    assert decision.state == UNVERIFIED
    assert "anchor_not_live_capable" in decision.reasons


def test_a_complete_signed_manifest_is_live_capable(release):
    assert release["anchor"].live_capable is True
    assert _verified_decision(release).state == VERIFIED


# --------------------------------------------------------------------------- #
# 4. additive component configuration
# --------------------------------------------------------------------------- #
def _attestation_accept(anchor, *, required, reports, session="s1"):
    attestation = SessionAttestation(
        store=ChallengeStore(), anchor=anchor, required_components=tuple(required)
    )
    challenge = attestation.issue(
        session_id=session, companion_id=COMPANION_ID, extension_id=EXT_ID
    )
    submission = VerificationSubmission(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        components=tuple(reports),
    )
    return attestation.accept(
        submission,
        session_id=session,
        companion_id=COMPANION_ID,
        hello_extension_id=EXT_ID,
        hello_version=VERSION,
        hello_digest="",
    )


def test_adding_a_component_does_not_drop_a_manifest_requirement(release):
    """``local_companion`` must not require repeating ``mcp_server``."""
    attestation = SessionAttestation(
        store=ChallengeStore(),
        anchor=release["anchor"],
        required_components=(COMPONENT_COMPANION,),
    )
    # The configured list is additive at its source, so the manifest's requirement is
    # still there before any submission is considered.
    assert attestation.effective_required_components() == (
        COMPONENT_EXTENSION,
        COMPONENT_MCP,
        COMPONENT_COMPANION,
    )

    # This release does not describe the companion, so an addition that names it is
    # refused rather than accepted with an unverifiable requirement.
    with pytest.raises(AttestationRejected) as excinfo:
        _attestation_accept(
            release["anchor"],
            required=(COMPONENT_COMPANION,),  # additive: does not repeat mcp_server
            reports=(
                _report(COMPONENT_EXTENSION, release["extension_digest"]),
                _report(COMPONENT_MCP, release["mcp_digest"]),
            ),
        )
    assert excinfo.value.code == "component_addition_not_in_release"


def test_an_empty_addition_list_keeps_the_manifest_set(release):
    evidence = _attestation_accept(
        release["anchor"],
        required=(),
        reports=(
            _report(COMPONENT_EXTENSION, release["extension_digest"]),
            _report(COMPONENT_MCP, release["mcp_digest"]),
        ),
    )
    assert evidence.failures == ()
    assert evidence.required_components == (COMPONENT_EXTENSION, COMPONENT_MCP)


def test_an_addition_present_in_the_release_is_measured(tmp_path):
    built = _release(tmp_path, components=(COMPONENT_EXTENSION, COMPONENT_MCP, COMPONENT_COMPANION))
    evidence = _attestation_accept(
        built["anchor"],
        required=(COMPONENT_COMPANION,),
        reports=(
            _report(COMPONENT_EXTENSION, built["extension_digest"]),
            _report(COMPONENT_MCP, built["mcp_digest"]),
            _report(
                COMPONENT_COMPANION,
                built["anchor"].digest_for(COMPONENT_COMPANION),
            ),
        ),
    )
    assert evidence.failures == ()
    assert evidence.component_states[COMPONENT_COMPANION] == COMPONENT_VERIFIED


def test_an_unknown_addition_is_refused(release):
    with pytest.raises(AttestationRejected) as excinfo:
        _attestation_accept(
            release["anchor"],
            required=("evil_sidecar",),
            reports=(_report(COMPONENT_EXTENSION, release["extension_digest"]),),
        )
    assert excinfo.value.code == "unknown_component_addition"


def test_an_unknown_addition_is_refused_at_config_time():
    with pytest.raises(BridgeConfigError):
        BridgeConfig(
            pairing_id=generate_pairing_id(),
            pairing_secret=generate_pairing_secret(),
            required_components=("evil_sidecar",),
        ).validate()


def test_no_configuration_can_shrink_the_manifest_set(release):
    for additions in ((), (COMPONENT_COMPANION,), (COMPONENT_MCP,)):
        attestation = SessionAttestation(
            store=ChallengeStore(),
            anchor=release["anchor"],
            required_components=additions,
        )
        effective = set(attestation.effective_required_components())
        assert set(release["anchor"].required_components) <= effective
        assert COMPONENT_EXTENSION in effective


async def test_an_addition_without_a_root_fails_the_measurement(release):
    bridge = FlowBridge(
        BridgeConfig(
            pairing_id=generate_pairing_id(),
            pairing_secret=generate_pairing_secret(),
            http_port=0,
            ws_port=0,
            extension_ids=(EXT_ID,),
            companion_id=COMPANION_ID,
            release_manifest_path=str(release["manifest_path"]),
            release_manifest_public_key=release["public"],
            require_installed_measurement=True,
            required_components=(COMPONENT_COMPANION,),
            install_components=(
                ComponentInstallSpec(component=COMPONENT_EXTENSION, root=str(release["extension_root"])),
                ComponentInstallSpec(component=COMPONENT_MCP, root=str(release["mcp_root"]), layout=LAYOUT_TREE),
            ),
        ),
        settings=_Settings(),
    )
    session = ExtensionSession(session_id="session-add", websocket=None)
    session.extension_id = EXT_ID
    session.extension_version = VERSION
    anchor = resolve_trust_anchor(bridge.config.trust_policy)
    with pytest.raises(MeasurementError) as excinfo:
        bridge._measure_locally(session, anchor)
    assert excinfo.value.code == "component_not_configured"


# --------------------------------------------------------------------------- #
# 5. source authorization from injected settings
# --------------------------------------------------------------------------- #
def test_settings_paths_reach_the_source_gate_without_environment_variables(tmp_path, monkeypatch):
    root = _package(tmp_path, licence="MIT License\n")
    attestation_path = _review(tmp_path, _attestation(root))
    for name in ("FLOW_SOURCE_PACKAGE_PATH", "FLOW_AUTHORIZATION_ATTESTATION_PATH"):
        monkeypatch.delenv(name, raising=False)

    bridge = FlowBridge(
        BridgeConfig(
            pairing_id=generate_pairing_id(),
            pairing_secret=generate_pairing_secret(),
            http_port=0,
            ws_port=0,
            extension_ids=(EXT_ID,),
        ),
        settings=_Settings(
            flow_source_package_path=str(root),
            flow_authorization_attestation_path=str(attestation_path),
        ),
    )
    assert bridge._source_gate_status() == STATUS_AUTHORIZED


def test_the_environment_cannot_override_the_configured_path(tmp_path, monkeypatch):
    root = _package(tmp_path, licence="MIT License\n")
    attestation_path = _review(tmp_path, _attestation(root))
    monkeypatch.setenv("FLOW_SOURCE_PACKAGE_PATH", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("FLOW_AUTHORIZATION_ATTESTATION_PATH", "")

    bridge = FlowBridge(
        BridgeConfig(
            pairing_id=generate_pairing_id(),
            pairing_secret=generate_pairing_secret(),
            http_port=0,
            ws_port=0,
            extension_ids=(EXT_ID,),
        ),
        settings=_Settings(
            flow_source_package_path=str(root),
            flow_authorization_attestation_path=str(attestation_path),
        ),
    )
    assert bridge._source_gate_status() == STATUS_AUTHORIZED


def test_missing_settings_fail_closed():
    bridge = FlowBridge(
        BridgeConfig(
            pairing_id=generate_pairing_id(),
            pairing_secret=generate_pairing_secret(),
            http_port=0,
            ws_port=0,
            extension_ids=(EXT_ID,),
        )
    )
    bridge.settings = None
    assert bridge._source_gate_status() == "LICENSING_BLOCKED"


def test_the_configured_attestation_path_is_the_one_evaluated(tmp_path):
    root = _package(tmp_path, licence="MIT License\n")
    other = _package(tmp_path / "other", licence="MIT License\n")
    attestation_path = _review(tmp_path, _attestation(root))

    configured = FlowBridge(
        BridgeConfig(
            pairing_id=generate_pairing_id(),
            pairing_secret=generate_pairing_secret(),
            http_port=0,
            ws_port=0,
            extension_ids=(EXT_ID,),
        ),
        settings=_Settings(
            flow_source_package_path=str(root),
            flow_authorization_attestation_path=str(attestation_path),
        ),
    )
    assert configured._source_gate_status() == STATUS_AUTHORIZED

    missing_attestation = FlowBridge(
        BridgeConfig(
            pairing_id=generate_pairing_id(),
            pairing_secret=generate_pairing_secret(),
            http_port=0,
            ws_port=0,
            extension_ids=(EXT_ID,),
        ),
        settings=_Settings(flow_source_package_path=str(other)),
    )
    assert missing_attestation._source_gate_status() == STATUS_LICENSE_EVIDENCE_PRESENT


def test_pydantic_settings_carry_the_fields_the_bridge_reads(tmp_path):
    """The injected object must be the real settings class, not a bespoke dict."""
    settings = Settings(
        flow_source_package_path=str(tmp_path),
        flow_authorization_attestation_path=str(tmp_path / "attestation.json"),
    )
    assert str(getattr(settings, "flow_source_package_path")) == str(tmp_path)
    assert getattr(settings, "flow_authorization_attestation_path").endswith("attestation.json")


@pytest.mark.parametrize(
    "status",
    ["SOURCE_ABSENT", "LICENSING_BLOCKED", "LICENSE_EVIDENCE_PRESENT", "AUTHORIZATION_REVIEW_REQUIRED"],
)
def test_only_authorized_can_produce_a_trusted_decision(release, status):
    decision = aggregate_trust(
        source_gate_status=status,
        anchor=release["anchor"],
        provenance=None,
        evidence=TrustEvidence(
            supported_protocol=True,
            claims_well_formed=True,
            components_consistent=True,
            release_anchor_valid=True,
            installed_artifact_measured=True,
            installed_artifact_digest_matches_anchor=True,
            runtime_identity_correlated=True,
            measurement_fresh=True,
            measurement_expires_at=time.time() + 300,
            component_states={COMPONENT_EXTENSION: COMPONENT_VERIFIED},
            required_components=(COMPONENT_EXTENSION,),
        ),
        now=time.time(),
    )
    assert decision.trusted is False
    assert decision.state != VERIFIED


# --------------------------------------------------------------------------- #
# 6. cross-component regression
# --------------------------------------------------------------------------- #
async def test_a_blocked_generation_writes_nothing_to_the_transport(release):
    bridge = FlowBridge(
        BridgeConfig(
            pairing_id=generate_pairing_id(),
            pairing_secret=generate_pairing_secret(),
            http_port=0,
            ws_port=0,
            extension_ids=(EXT_ID,),
        ),
        settings=_Settings(),
    )
    # A decision that was trusted once and is now past its validity window.
    bridge.last_trust = _verified_decision(release).expired_copy()
    with pytest.raises(UntrustedArtifact):
        await bridge.submit_generation("image", _request(), approval_binding="a" * 64)
    assert bridge.stats["commands_sent"] == 0
    assert bridge.denied_commands == []
    assert bridge.coordinator.budget_ledger is None


async def test_an_approval_binding_cannot_bypass_a_missing_budget(release):
    from app.flow.trust import evaluate_live_generation_guard

    decision = _verified_decision(release)
    guard = evaluate_live_generation_guard(
        live_generation_flag=True,
        source_gate_status="AUTHORIZED",
        decision=decision,
        approval_binding="a" * 64,
        budget_controls=SpendingControls(),
        idempotency_enforced=True,
    )
    assert guard.allowed is False
    assert "budget_controls_missing" in guard.blockers


async def test_a_blocked_trust_state_cannot_be_bypassed_by_an_approval(release):
    from app.flow.trust import evaluate_live_generation_guard

    guard = evaluate_live_generation_guard(
        live_generation_flag=True,
        source_gate_status="AUTHORIZED",
        decision=None,
        approval_binding="a" * 64,
        budget_controls=SpendingControls(
            enforcement="flow_budget_ledger", cap_usd=5.0, ledger_wired=True
        ),
        idempotency_enforced=True,
    )
    assert guard.allowed is False


def test_the_coordinator_still_reports_real_idempotency_enforcement():
    coordinator = _coordinator(_PendingTransport(), FlowBudgetLedger(cap_usd=5.0))
    assert coordinator.idempotency_enforced is True
