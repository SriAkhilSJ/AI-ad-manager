"""Phase 3C.2.3 — trust pipeline integration and hardening.

The fifteen adversarial cases from §10, each written to fail if the pipeline goes
back to trusting something it did not measure. Two rules run through the whole
file:

* a **claim** is never evidence — not a HELLO digest, not a version string, not a
  signature over a document, not a peer-supplied "report";
* a **measurement** is never permanent — it describes a moment, and the moment has
  an expiry that the dispatch path re-checks.

Every fixture is synthetic: an extension tree built in a temporary directory, a
directory tree standing in for the MCP server, and an Ed25519 key pair generated
inside the test. No DeepX source is present or used, no browser is involved, and
no generation is attempted. A synthetic release that passes these tests proves the
machinery works; it says nothing about the real DeepX integration, which remains
unverifiable while the upstream source is absent and unlicensed.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from pathlib import Path
from typing import Any

import pytest
import websockets

from app.flow import protocol as proto
from app.flow.bridge import (
    BridgeConfig,
    BridgeConfigError,
    ComponentInstallSpec,
    FlowBridge,
    _install_components_from_settings,
)
from app.flow.coordinator import CommandTransport, GenerationCoordinator, LiveGenerationDisabled
from app.flow.installed_artifact import (
    LAYOUT_TREE,
    InstalledArtifactVerifier,
    InstallationSpec,
    MeasurementPolicy,
)
from app.flow.provenance import (
    COMPONENT_COMPANION,
    COMPONENT_EXTENSION,
    COMPONENT_MCP,
    TrustPolicy,
    resolve_trust_anchor,
    verify_release_manifest,
)
from app.flow.security import generate_pairing_id, generate_pairing_secret
from app.flow.trust import (
    ARTIFACT_DIGEST_MISMATCH,
    COMPONENT_VERIFIED,
    INSTALLED_ARTIFACT_NOT_FOUND,
    MEASUREMENT_STALE,
    SOURCE_ABSENT,
    UNVERIFIED,
    VERIFIED,
    AttestationRejected,
    ChallengeStore,
    ComponentReport,
    SessionAttestation,
    SpendingControls,
    TrustEvidence,
    TrustRegistry,
    UntrustedArtifact,
    VerificationSubmission,
    aggregate_trust,
    evaluate_live_generation_guard,
    spending_controls_from_settings,
)
from app.services.flow_budget import (
    BudgetUnavailable,
    FlowBudgetLedger,
    InvalidBudgetCap,
    flow_ledger_from_settings,
)

from tests.test_phase3c22_installed_artifact import (
    BUILD_ID,
    COMMIT,
    EXT_ID,
    OTHER_EXT_ID,
    VERSION,
    _extension_dir,
    _keypair,
    _manifest_document,
    _sign,
    _write_manifest,
)

BACKEND = Path(__file__).resolve().parents[1]

COMPANION_ID = "companion-3c23-local-process"
HEX_ID = "a" * 32


# --------------------------------------------------------------------------- #
# fixtures: a signed two-component release (extension + MCP server)
# --------------------------------------------------------------------------- #
def _mcp_tree(base: Path, *, dependency_body: str = "module.exports = 1;\n") -> Path:
    """A plain directory standing in for the MCP server, with a real dependency."""
    root = base / "mcp-server"
    (root / "node_modules" / "flow-sdk").mkdir(parents=True, exist_ok=True)
    (root / "server.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    (root / "node_modules" / "flow-sdk" / "index.js").write_text(dependency_body, encoding="utf-8")
    (root / "__pycache__").mkdir(exist_ok=True)
    (root / "__pycache__" / "server.pyc").write_bytes(b"\x00\x01\x02")
    return root


def _two_component_release(tmp_path: Path) -> dict[str, Any]:
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
    components = [
        {
            "name": COMPONENT_EXTENSION,
            "version": VERSION,
            "sha256": extension_report.digest,
            "extension_id": EXT_ID,
            "protocol": proto.PROTOCOL_VERSION,
            "build_id": BUILD_ID,
            "required": True,
        },
        {
            "name": COMPONENT_MCP,
            "version": VERSION,
            "sha256": mcp_report.digest,
            "required": True,
        },
    ]
    document = {
        **_manifest_document(extension_report.digest, components=components),
        "digest_algorithm": extension_report.digest_algorithm,
    }
    manifest_path = _write_manifest(tmp_path / "release-3c23.json", document, private)
    return {
        "extension_root": extension_root,
        "mcp_root": mcp_root,
        "extension_digest": extension_report.digest,
        "mcp_digest": mcp_report.digest,
        "private": private,
        "public": public,
        "manifest_path": manifest_path,
        "anchor": verify_release_manifest(manifest_path, public),
        "specs": (
            ComponentInstallSpec(
                component=COMPONENT_EXTENSION, root=str(extension_root), layout="versioned"
            ),
            ComponentInstallSpec(component=COMPONENT_MCP, root=str(mcp_root), layout=LAYOUT_TREE),
        ),
    }


@pytest.fixture(autouse=True)
def authorized_source_gate(monkeypatch):
    """Pretend the upstream gate is AUTHORIZED for this module only.

    On this machine it is ``SOURCE_ABSENT`` — there is no DeepX source and no
    licence — which blocks ``VERIFIED`` before any measurement is consulted. The
    machinery tests below need to get past that gate to exercise the measurement
    paths, so they substitute an authorized gate explicitly. Nothing in this suite
    asserts that the real gate is authorized; ``test_an_unauthorized_source_gate_*``
    covers the real states, and the bridge's own gate is never patched in production.
    """
    monkeypatch.setattr(
        FlowBridge, "_source_gate_status", lambda self: "AUTHORIZED", raising=True
    )


@pytest.fixture()
def release(tmp_path: Path) -> dict[str, Any]:
    return _two_component_release(tmp_path)


def _hello(pairing_id: str, pairing_secret: str, **overrides: Any) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "type": "hello",
        "protocol": proto.PROTOCOL_VERSION,
        "pairing_id": pairing_id,
        "pairing_secret": pairing_secret,
        "extension_id": EXT_ID,
        "extension_version": VERSION,
        "service_worker_version": VERSION,
        "mcp_version": VERSION,
        "source_commit": COMMIT,
        "build_id": BUILD_ID,
        "package_sha256": "",
        "flow_tab_present": True,
    }
    frame.update(overrides)
    return frame


def _bridge(release: dict[str, Any], *, required=(), specs=None, measure=True, **overrides: Any):
    pairing_id = generate_pairing_id()
    secret = generate_pairing_secret()
    config = BridgeConfig(
        pairing_id=pairing_id,
        pairing_secret=secret,
        http_port=0,
        ws_port=0,
        extension_ids=(EXT_ID,),
        handshake_timeout=2.0,
        companion_id=COMPANION_ID,
        release_manifest_path=str(release["manifest_path"]),
        release_manifest_public_key=release["public"],
        require_installed_measurement=measure,
        required_components=tuple(required),
        install_components=release["specs"] if specs is None else specs,
        **overrides,
    )
    return FlowBridge(config), pairing_id, secret


async def _connect_and_measure(bridge: FlowBridge, pairing_id: str, secret: str, **hello) -> dict:
    """One authenticated session, then wait for the local measurement to settle."""
    hello.setdefault(
        "package_sha256",
        resolve_trust_anchor(bridge.config.trust_policy).digest_for(COMPONENT_EXTENSION),
    )
    async with websockets.connect(f"ws://127.0.0.1:{bridge.ws_port}/ws") as ws:
        await ws.send(json.dumps(_hello(pairing_id, secret, **hello)))
        ack = json.loads(await asyncio.wait_for(ws.recv(), 3))
        assert ack["type"] == "hello_ack"
        session = bridge.session
        if session is not None and session.measurement_task is not None:
            await asyncio.wait_for(session.measurement_task, 30)
        return bridge.snapshot()


class _RecordingTransport(CommandTransport):
    """Counts what would have been written to the extension socket."""

    def __init__(self) -> None:
        self.sent: list[proto.CommandEnvelope] = []

    async def send(self, envelope: proto.CommandEnvelope) -> None:
        self.sent.append(envelope)

    async def await_result(self, command_id: str, timeout: float) -> Any:  # pragma: no cover
        raise AssertionError("a blocked generation must never await a result")


def _request() -> proto.GenerationCommand:
    return proto.GenerationCommand(prompt="A charcoal frame", model="imagen-3", count=1)


def _verified_decision(release: dict[str, Any], *, now: float | None = None, expires_in: float = 300.0):
    """A decision that is genuinely VERIFIED *for the given moment*."""
    moment = time.time() if now is None else now
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
        measured_at=moment - 1.0,
        measurement_expires_at=moment + expires_in,
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
        expected_extension_id=EXT_ID,
        expected_version=VERSION,
        measured_extension_id=EXT_ID,
        measured_version=VERSION,
        measured_digest=release["extension_digest"],
        now=moment,
    )


GOOD_SPENDING = SpendingControls(
    enforcement="flow_budget_ledger", cap_usd=5.0, ledger_wired=True
)
NO_SPENDING = SpendingControls()


# --------------------------------------------------------------------------- #
# 1 — the production companion path invokes the verifier
# --------------------------------------------------------------------------- #
async def test_the_companion_measures_the_configured_roots_itself(release, monkeypatch):
    """`FlowBridge` (what `LocalFlowCompanion` runs) is the caller of the verifier.

    Nothing in this test hands a digest to the bridge: the HELLO carries an empty
    ``package_sha256``, and the only source of the recorded digests is the local
    filesystem walk that this assertion intercepts.
    """
    import app.flow.bridge as bridge_module

    calls: list[dict[str, Any]] = []
    real_measure = bridge_module.measure_components

    def spy(specs):
        calls.append(dict(specs))
        return real_measure(specs)

    monkeypatch.setattr(bridge_module, "measure_components", spy)
    bridge, pairing_id, secret = _bridge(release)
    await bridge.start()
    try:
        snapshot = await _connect_and_measure(bridge, pairing_id, secret)
    finally:
        await bridge.stop()

    assert calls, "the production bridge never called the verifier"
    measured = calls[0]
    assert set(measured) == {COMPONENT_EXTENSION, COMPONENT_MCP}
    assert str(measured[COMPONENT_EXTENSION].root) == str(release["extension_root"])
    assert str(measured[COMPONENT_MCP].root) == str(release["mcp_root"])

    trust = snapshot["trust"]
    assert trust["state"] == VERIFIED, trust["reasons"]
    assert trust["measured"]["source"] == "local_companion"
    assert trust["measured"]["artifact_digest"] == release["extension_digest"]
    assert trust["components"][COMPONENT_MCP]["state"] == COMPONENT_VERIFIED
    # The HELLO's digest claim matched the release; that is a claim, and the trust
    # above came from the local measurement instead.
    assert snapshot["hello_digest_claim_verified"] is True


# --------------------------------------------------------------------------- #
# 2 — the verifier reads the filesystem, not the peer's claim
# --------------------------------------------------------------------------- #
async def test_a_hello_digest_cannot_stand_in_for_the_disk(release):
    """HELLO declares the release digest; the files on disk are not that release."""
    (release["extension_root"] / "content.js").write_text(
        "// tampered after signing\n", encoding="utf-8"
    )
    bridge, pairing_id, secret = _bridge(release)
    await bridge.start()
    try:
        snapshot = await _connect_and_measure(
            bridge, pairing_id, secret, package_sha256=release["extension_digest"]
        )
    finally:
        await bridge.stop()

    trust = snapshot["trust"]
    assert trust["state"] == ARTIFACT_DIGEST_MISMATCH, trust
    assert trust["trusted"] is False
    assert trust["measured"]["artifact_digest"] != release["extension_digest"]


# --------------------------------------------------------------------------- #
# 3 — a fabricated report over the extension socket cannot reach VERIFIED
# --------------------------------------------------------------------------- #
async def test_a_peer_supplied_report_is_not_a_measurement(release):
    bridge, pairing_id, secret = _bridge(release, measure=False, specs=())
    await bridge.start()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{bridge.ws_port}/ws") as ws:
            await ws.send(json.dumps(_hello(pairing_id, secret, package_sha256=release["extension_digest"])))
            await asyncio.wait_for(ws.recv(), 3)
            await ws.send(
                json.dumps(
                    {
                        "type": "verification_report",
                        "challenge_id": HEX_ID,
                        "nonce": "b" * 32,
                        "components": [
                            {
                                "name": COMPONENT_EXTENSION,
                                "digest": release["extension_digest"],
                                "extension_id": EXT_ID,
                                "version": VERSION,
                                "measurement_id": "c" * 32,
                                "file_count": 99,
                                "measured_at": time.time(),
                            }
                        ],
                    }
                )
            )
            with pytest.raises(websockets.ConnectionClosed) as excinfo:
                await asyncio.wait_for(ws.recv(), 3)
            assert excinfo.value.rcvd.code == 4400  # unknown frame type

        assert bridge.session is None or bridge.current_trust().state != VERIFIED
        with pytest.raises(UntrustedArtifact):
            await bridge.submit_generation("image", _request(), approval_binding="a" * 64)
    finally:
        await bridge.stop()


# --------------------------------------------------------------------------- #
# 4 — the measurement binds to the session (and identity is never conflated)
# --------------------------------------------------------------------------- #
def test_a_measurement_cannot_be_reused_by_another_session(release):
    store = ChallengeStore()
    attestation = SessionAttestation(
        store=store,
        anchor=release["anchor"],
        required_components=(COMPONENT_EXTENSION, COMPONENT_MCP),
    )
    challenge = attestation.issue(
        session_id="session-1", companion_id=COMPANION_ID, extension_id=EXT_ID
    )
    submission = VerificationSubmission(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        components=(_report(COMPONENT_EXTENSION, release["extension_digest"]),),
    )
    with pytest.raises(AttestationRejected) as excinfo:
        attestation.accept(
            submission,
            session_id="session-2",
            companion_id=COMPANION_ID,
            hello_extension_id=EXT_ID,
            hello_version=VERSION,
            hello_digest="",
        )
    assert excinfo.value.code == "challenge_wrong_session"


def test_the_extension_id_is_never_used_as_the_companion_identity(release):
    """The party being checked cannot be the party that vouches for the check."""
    store = ChallengeStore()
    attestation = SessionAttestation(
        store=store,
        anchor=release["anchor"],
        required_components=(COMPONENT_EXTENSION, COMPONENT_MCP),
    )
    challenge = attestation.issue(
        session_id="session-1", companion_id=COMPANION_ID, extension_id=EXT_ID
    )
    submission = VerificationSubmission(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        components=(_report(COMPONENT_EXTENSION, release["extension_digest"]),),
    )
    with pytest.raises(AttestationRejected) as excinfo:
        attestation.accept(
            submission,
            session_id="session-1",
            companion_id=EXT_ID,  # the extension pretending to be the companion
            hello_extension_id=EXT_ID,
            hello_version=VERSION,
            hello_digest="",
        )
    assert excinfo.value.code == "challenge_wrong_companion"
    assert challenge.companion_id != challenge.extension_id


def _report(name: str, digest: str = "", **overrides: Any) -> ComponentReport:
    payload: dict[str, Any] = {
        "name": name,
        "digest": overrides.pop("digest", "") or digest or "a" * 64,
        "extension_id": EXT_ID if name == COMPONENT_EXTENSION else "",
        "version": VERSION if name == COMPONENT_EXTENSION else "",
        "measurement_id": HEX_ID,
        "measured_at": time.time(),
        "file_count": 3,
        "digest_algorithm": "sha256-tree-runtime-v1",
    }
    payload.update(overrides)
    return ComponentReport(**payload)


# --------------------------------------------------------------------------- #
# 5 — a manifest-required component cannot be dropped by configuration
# --------------------------------------------------------------------------- #
def test_configuration_cannot_downgrade_the_signed_requirement(release):
    """Phase 3C.2.3.1: ``FLOW_REQUIRED_COMPONENTS`` is additive, so a shorter
    configured list cannot shrink the manifest's requirement. The old
    ``required_component_downgrade`` failure is gone; the union is computed instead,
    and a submission missing the manifest's component still fails verification.
    """
    attestation = SessionAttestation(
        store=ChallengeStore(),
        anchor=release["anchor"],
        required_components=(COMPONENT_EXTENSION,),  # config names the extension only
    )
    assert set(attestation.effective_required_components()) == {
        COMPONENT_EXTENSION,
        COMPONENT_MCP,
    }

    challenge = attestation.issue(
        session_id="session-1", companion_id=COMPANION_ID, extension_id=EXT_ID
    )
    submission = VerificationSubmission(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        components=(_report(COMPONENT_EXTENSION, release["extension_digest"]),),
    )
    evidence = attestation.accept(
        submission,
        session_id="session-1",
        companion_id=COMPANION_ID,
        hello_extension_id=EXT_ID,
        hello_version=VERSION,
        hello_digest="",
    )
    assert "required_component_downgrade" not in evidence.failures
    assert f"component_not_measured:{COMPONENT_MCP}" in evidence.failures
    assert evidence.component_states[COMPONENT_MCP] == "NOT_MEASURED"


async def test_a_manifest_required_mcp_server_cannot_be_omitted(release):
    """Configured extension only, signed manifest requires the MCP server too."""
    bridge, pairing_id, secret = _bridge(release, specs=(release["specs"][0],))
    await bridge.start()
    try:
        snapshot = await _connect_and_measure(bridge, pairing_id, secret)
    finally:
        await bridge.stop()

    trust = snapshot["trust"]
    assert trust["state"] == INSTALLED_ARTIFACT_NOT_FOUND, trust
    assert "component_not_configured" in trust["reasons"]
    assert trust["trusted"] is False


# --------------------------------------------------------------------------- #
# 6 — a legacy (v1) anchor is readable but never live-capable
# --------------------------------------------------------------------------- #
def test_a_v1_manifest_cannot_establish_live_trust(tmp_path, release):
    document = {
        **_manifest_document(
            release["extension_digest"], schema="crusor.flow.release.v1", components=[]
        ),
    }
    document.pop("components", None)
    document["artifact"] = {"sha256": release["extension_digest"]}
    path = _write_manifest(tmp_path / "v1.json", document, release["private"])
    anchor = verify_release_manifest(path, release["public"])
    assert anchor.present is True
    assert anchor.identifies_components is False
    assert anchor.live_capable is False

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
        component_states={COMPONENT_EXTENSION: COMPONENT_VERIFIED},
        required_components=(COMPONENT_EXTENSION,),
    )
    decision = aggregate_trust(
        source_gate_status="AUTHORIZED",
        anchor=anchor,
        provenance=None,
        evidence=evidence,
        now=time.time(),
    )
    assert decision.state == UNVERIFIED
    assert "anchor_not_live_capable" in decision.reasons
    guard = evaluate_live_generation_guard(
        live_generation_flag=True,
        source_gate_status="AUTHORIZED",
        decision=decision,
        approval_binding="a" * 64,
        budget_controls=GOOD_SPENDING,
        idempotency_enforced=True,
    )
    assert guard.allowed is False


# --------------------------------------------------------------------------- #
# 7 — an extension-only pin is insufficient for a multi-component integration
# --------------------------------------------------------------------------- #
def test_an_extension_only_pin_cannot_establish_trust(release):
    policy = TrustPolicy(
        trusted_artifact_sha256=release["extension_digest"], trusted_release=VERSION
    )
    anchor = resolve_trust_anchor(policy)
    assert anchor.present is True
    assert anchor.live_capable is False

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
        anchor=anchor,
        provenance=None,
        evidence=evidence,
        now=time.time(),
    )
    assert decision.state == UNVERIFIED
    assert "anchor_not_live_capable" in decision.reasons


# --------------------------------------------------------------------------- #
# 8 — a previously verified measurement is rejected once its window passes
# --------------------------------------------------------------------------- #
async def test_a_stale_previously_verified_measurement_fails_the_guard(release):
    issued_at = time.time()
    decision = _verified_decision(release, now=issued_at)
    assert decision.state == VERIFIED

    later = issued_at + 301
    assert decision.is_fresh(issued_at) is True
    assert decision.is_fresh(later) is False

    guard = evaluate_live_generation_guard(
        live_generation_flag=True,
        source_gate_status="AUTHORIZED",
        decision=decision,
        approval_binding="a" * 64,
        budget_controls=GOOD_SPENDING,
        idempotency_enforced=True,
        now=later,
    )
    assert guard.allowed is False
    assert f"installed_artifact:{MEASUREMENT_STALE}" in guard.blockers

    transport = _RecordingTransport()
    stale = decision.expired_copy(later)
    coordinator = GenerationCoordinator(
        transport,
        live_generation_enabled=True,
        trust_provider=lambda: stale,
        budget_ledger=FlowBudgetLedger(cap_usd=5.0),
    )
    with pytest.raises(UntrustedArtifact):
        await coordinator.submit("image", _request(), approval_binding="a" * 64)
    assert transport.sent == []


# --------------------------------------------------------------------------- #
# 9 — missing metadata cannot produce a fresh report
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"measured_at": 0.0}, "measurement_timestamp_missing"),
        ({"measurement_id": ""}, "measurement_id_missing"),
        ({"measurement_id": "z" * 32}, "measurement_id_missing"),
        ({"file_count": 0}, "empty_measurement"),
        ({"digest": "not-a-digest"}, "measurement_malformed"),
        ({"measured_at": time.time() + 3600}, "measurement_outside_challenge_window"),
    ],
)
def test_incomplete_metadata_is_refused(release, overrides, code):
    attestation = SessionAttestation(store=ChallengeStore(), anchor=release["anchor"])
    challenge = attestation.issue(
        session_id="session-1", companion_id=COMPANION_ID, extension_id=EXT_ID
    )
    fields = {"digest": release["extension_digest"], **overrides}
    submission = VerificationSubmission(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        components=(_report(COMPONENT_EXTENSION, **fields),),
    )
    with pytest.raises(AttestationRejected) as excinfo:
        attestation.accept(
            submission,
            session_id="session-1",
            companion_id=COMPANION_ID,
            hello_extension_id=EXT_ID,
            hello_version=VERSION,
            hello_digest="",
        )
    assert excinfo.value.code == code


# --------------------------------------------------------------------------- #
# 10 — duplicate component names are rejected
# --------------------------------------------------------------------------- #
def test_a_duplicate_component_name_is_refused(release):
    attestation = SessionAttestation(
        store=ChallengeStore(),
        anchor=release["anchor"],
        required_components=(COMPONENT_EXTENSION, COMPONENT_MCP),
    )
    challenge = attestation.issue(
        session_id="session-1", companion_id=COMPANION_ID, extension_id=EXT_ID
    )
    submission = VerificationSubmission(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        components=(
            _report(COMPONENT_EXTENSION, release["extension_digest"]),
            _report(COMPONENT_EXTENSION, release["extension_digest"]),
        ),
    )
    with pytest.raises(AttestationRejected) as excinfo:
        attestation.accept(
            submission,
            session_id="session-1",
            companion_id=COMPANION_ID,
            hello_extension_id=EXT_ID,
            hello_version=VERSION,
            hello_digest="",
        )
    assert excinfo.value.code == "duplicate_component"


# --------------------------------------------------------------------------- #
# 11 — runtime dependencies are inside the digest
# --------------------------------------------------------------------------- #
def test_tampering_with_a_runtime_dependency_changes_the_digest(release):
    spec = InstallationSpec(root=release["mcp_root"], layout=LAYOUT_TREE)
    before = InstalledArtifactVerifier(COMPONENT_MCP).measure(spec)
    (release["mcp_root"] / "node_modules" / "flow-sdk" / "index.js").write_text(
        "module.exports = 0;\n", encoding="utf-8"
    )
    after = InstalledArtifactVerifier(COMPONENT_MCP).measure(spec)
    assert after.digest != before.digest
    assert after.file_count == before.file_count
    # node_modules and __pycache__ are *measured*, not skipped.
    names = {item.path for item in after.files}
    assert "node_modules/flow-sdk/index.js" in names
    assert "__pycache__/server.pyc" in names


async def test_a_patched_runtime_dependency_fails_the_bridge(release):
    (release["mcp_root"] / "node_modules" / "flow-sdk" / "index.js").write_text(
        "module.exports = 'patched';\n", encoding="utf-8"
    )
    bridge, pairing_id, secret = _bridge(release)
    await bridge.start()
    try:
        snapshot = await _connect_and_measure(bridge, pairing_id, secret)
    finally:
        await bridge.stop()
    trust = snapshot["trust"]
    assert trust["state"] != VERIFIED
    assert trust["components"][COMPONENT_MCP]["state"] != COMPONENT_VERIFIED


# --------------------------------------------------------------------------- #
# 12 — release authenticity is not authorization to modify the source
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "gate_status",
    ["SOURCE_ABSENT", "LICENSING_BLOCKED", "LICENSE_EVIDENCE_PRESENT", "AUTHORIZATION_REVIEW_REQUIRED"],
)
def test_an_unauthorized_source_gate_blocks_verified(release, gate_status):
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
        component_states={COMPONENT_EXTENSION: COMPONENT_VERIFIED},
        required_components=(COMPONENT_EXTENSION,),
    )
    decision = aggregate_trust(
        source_gate_status=gate_status,
        anchor=release["anchor"],
        provenance=None,
        evidence=evidence,
        now=time.time(),
    )
    assert decision.state != VERIFIED
    assert decision.trusted is False
    guard = evaluate_live_generation_guard(
        live_generation_flag=True,
        source_gate_status=gate_status,
        decision=decision,
        approval_binding="a" * 64,
        budget_controls=GOOD_SPENDING,
        idempotency_enforced=True,
    )
    assert guard.allowed is False
    assert f"upstream_source:{gate_status}" in guard.blockers


# --------------------------------------------------------------------------- #
# 13 — no real budget control means no generation
# --------------------------------------------------------------------------- #
class _Settings:
    def __init__(self, **values: Any) -> None:
        self.flow_generation_budget_usd = values.pop("cap", 0.0)
        self.flow_budget_enforcement = values.pop("enforcement", "none")
        for key, value in values.items():
            setattr(self, key, value)


def test_a_budget_that_is_not_enforced_is_not_a_control():
    for settings in (
        _Settings(),
        _Settings(cap=10.0),
        _Settings(enforcement="flow_budget_ledger"),
        _Settings(enforcement="flow_budget_ledger", cap=0.0),
    ):
        controls = spending_controls_from_settings(settings)
        assert controls.active is False
        assert flow_ledger_from_settings(settings) is None

    enforced = _Settings(enforcement="flow_budget_ledger", cap=10.0)
    ledger = flow_ledger_from_settings(enforced)
    assert isinstance(ledger, FlowBudgetLedger)
    assert spending_controls_from_settings(enforced).active is True
    # The guard and the coordinator must be describing the same ledger.
    assert spending_controls_from_settings(enforced, ledger=ledger).cap_usd == ledger.cap_usd


async def test_missing_budget_enforcement_blocks_a_verified_generation(release):
    decision = _verified_decision(release)
    assert decision.state == VERIFIED

    guard = evaluate_live_generation_guard(
        live_generation_flag=True,
        source_gate_status="AUTHORIZED",
        decision=decision,
        approval_binding="a" * 64,
        budget_controls=NO_SPENDING,
        idempotency_enforced=True,
    )
    assert guard.allowed is False
    assert "budget_controls_missing" in guard.blockers

    transport = _RecordingTransport()
    coordinator = GenerationCoordinator(
        transport,
        live_generation_enabled=True,
        trust_provider=lambda: decision,
        budget_ledger=None,  # nothing to reserve against
    )
    assert coordinator.idempotency_enforced is True
    # Phase 3C.2.3.1: a zero cap is no longer a ledger that refuses at reserve time;
    # it is not a ledger at all, because "0" reads as unlimited to everybody who
    # skims it. Only a finite, positive cap builds an enforceable ledger.
    with pytest.raises(InvalidBudgetCap):
        FlowBudgetLedger(cap_usd=0.0)
    assert flow_ledger_from_settings(SimpleNamespace(
        flow_budget_enforcement="none", flow_generation_budget_usd=5.0
    )) is None
    await coordinator.submit("image", _request(), approval_binding="a" * 64, idempotency_key="k1")
    # A coordinator without a ledger will dispatch when the *caller* forgets the
    # guard; that is why the guard is the only entry point to it.
    assert len(transport.sent) == 1


# --------------------------------------------------------------------------- #
# 14 — an approval cannot bypass a trust failure
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "defect,gate",
    [
        ("not_measured", "AUTHORIZED"),
        ("stale", "AUTHORIZED"),
        ("digest_mismatch", "AUTHORIZED"),
        ("source_absent", "SOURCE_ABSENT"),
        ("anchor_invalid", "AUTHORIZED"),
    ],
)
def test_founder_approval_cannot_substitute_for_trust(release, defect, gate):
    evidence = TrustEvidence(
        supported_protocol=True,
        claims_well_formed=True,
        components_consistent=True,
        release_anchor_valid=defect != "anchor_invalid",
        installed_artifact_measured=defect != "not_measured",
        installed_artifact_digest_matches_anchor=defect != "digest_mismatch",
        runtime_identity_correlated=True,
        measurement_fresh=defect != "stale",
        measurement_expires_at=time.time() + (300 if defect != "stale" else -1),
        component_states={COMPONENT_EXTENSION: COMPONENT_VERIFIED},
        required_components=(COMPONENT_EXTENSION,),
    )
    decision = aggregate_trust(
        source_gate_status=gate,
        anchor=release["anchor"],
        provenance=None,
        evidence=evidence,
        now=time.time(),
    )
    assert decision.trusted is False, defect
    for approval in ("", "a" * 64, "founder-approved-binding"):
        guard = evaluate_live_generation_guard(
            live_generation_flag=True,
            source_gate_status=gate,
            decision=decision,
            approval_binding=approval,
            budget_controls=GOOD_SPENDING,
            idempotency_enforced=True,
        )
        assert guard.allowed is False, (defect, approval)


# --------------------------------------------------------------------------- #
# 15 — every failure path dispatches zero commands
# --------------------------------------------------------------------------- #
async def test_every_blocked_path_writes_nothing_to_the_transport(release):
    verified = _verified_decision(release)
    cases: list[tuple[str, dict[str, Any], Any]] = [
        ("live_disabled", {}, LiveGenerationDisabled()),
        ("untrusted", {"trust_provider": lambda: _verified_decision(release).expired_copy()}, None),
        (
            "no_approval",
            {"trust_provider": lambda: verified, "budget_ledger": FlowBudgetLedger(cap_usd=5.0)},
            None,
        ),
        (
            "budget_exhausted",
            {"trust_provider": lambda: verified, "budget_ledger": FlowBudgetLedger(cap_usd=0.001)},
            BudgetUnavailable(),
        ),
    ]
    for name, kwargs, expected in cases:
        transport = _RecordingTransport()
        live = "live_generation_enabled" not in kwargs
        kwargs = {k: v for k, v in kwargs.items()}
        coordinator = GenerationCoordinator(
            transport,
            live_generation_enabled=(name != "live_disabled"),
            **kwargs,
        )
        ledger = coordinator.budget_ledger
        before = ledger.snapshot() if ledger is not None else None
        approval = "" if name == "no_approval" else "a" * 64
        with pytest.raises(Exception) as excinfo:
            await coordinator.submit("image", _request(), approval_binding=approval)
        assert transport.sent == [], name
        if ledger is not None:
            assert ledger.snapshot() == before, name
        if expected is not None:
            assert isinstance(excinfo.value, (type(expected), UntrustedArtifact)), (name, excinfo.value)


# --------------------------------------------------------------------------- #
# settings wiring (§2) — every root arrives explicitly, nothing is guessed
# --------------------------------------------------------------------------- #
def test_settings_become_explicit_install_specs():
    settings = _Settings(
        **{
            "flow_extension_install_root": "/opt/crusor/ext",
            "flow_extension_install_layout": "versioned",
            "flow_mcp_install_root": "/opt/crusor/mcp",
            "flow_mcp_install_layout": "tree",
            "flow_companion_install_root": "",
            "flow_companion_install_layout": "tree",
        }
    )
    specs = {spec.component: spec for spec in _install_components_from_settings(settings)}
    assert set(specs) == {COMPONENT_EXTENSION, COMPONENT_MCP}
    assert specs[COMPONENT_MCP].layout == LAYOUT_TREE
    assert COMPONENT_COMPANION not in specs  # not configured, not guessed


def test_an_invalid_layout_is_refused_at_config_time(release):
    with pytest.raises(BridgeConfigError):
        BridgeConfig(
            pairing_id=generate_pairing_id(),
            pairing_secret=generate_pairing_secret(),
            require_installed_measurement=True,
            release_manifest_path=str(release["manifest_path"]),
            release_manifest_public_key=release["public"],
            install_components=(
                ComponentInstallSpec(
                    component=COMPONENT_EXTENSION,
                    root=str(release["extension_root"]),
                    layout="tree",
                ),
            ),
        ).validate()

    with pytest.raises(BridgeConfigError):
        BridgeConfig(
            pairing_id=generate_pairing_id(),
            pairing_secret=generate_pairing_secret(),
            install_components=(
                ComponentInstallSpec(component="evil_sidecar", root="/tmp/x", layout="tree"),
            ),
        ).validate()

    with pytest.raises(BridgeConfigError):
        BridgeConfig(
            pairing_id=generate_pairing_id(),
            pairing_secret=generate_pairing_secret(),
            install_components=(
                ComponentInstallSpec(component=COMPONENT_MCP, root="/tmp/a", layout="tree"),
                ComponentInstallSpec(component=COMPONENT_MCP, root="/tmp/b", layout="tree"),
            ),
        ).validate()
