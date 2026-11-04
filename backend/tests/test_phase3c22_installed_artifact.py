"""Phase 3C.2.2 — independent installed-artifact verification.

Everything here uses synthetic extension directories and synthetic signed release
manifests. No DeepX source exists in this repository, none is downloaded, and no
browser or Google account is involved. A synthetic package passing these tests
proves the *verification machinery* works; it proves nothing about the real DeepX
extension, which remains unmeasurable until an authorized release artifact exists.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.flow import protocol as proto
from app.flow.authorization import package_tree_digest
from app.flow.installed_artifact import (
    InstalledArtifactVerifier,
    InstallationSpec,
    MeasurementError,
    MeasurementPolicy,
    chrome_extension_id_from_key,
    component_frame,
    measure_components,
)
from app.flow.provenance import (
    COMPONENT_COMPANION,
    COMPONENT_EXTENSION,
    COMPONENT_MCP,
    RELEASE_MANIFEST_SCHEMA,
    ProvenanceRejected,
    TrustPolicy,
    resolve_trust_anchor,
    verify_release_manifest,
)
from app.flow.trust import (
    ARTIFACT_DIGEST_MISMATCH,
    SpendingControls,
    ARTIFACT_MEASUREMENT_FAILED,
    COMPONENT_UNVERIFIED,
    COMPONENT_VERIFIED,
    INSTALLED_ARTIFACT_NOT_FOUND,
    MEASUREMENT_STALE,
    RELEASE_ANCHOR_MISSING,
    SOURCE_ABSENT,
    UNVERIFIED,
    VERIFIED,
    AttestationRejected,
    ChallengeStore,
    ComponentReport,
    SessionAttestation,
    TrustEvidence,
    TrustRegistry,
    UntrustedArtifact,
    VerificationSubmission,
    aggregate_trust,
    evaluate_live_generation_guard,
)

#: Spend-control fixtures: the guard now asks for a real control object.
VERIFIED_SPENDING = SpendingControls(
    enforcement="flow_budget_ledger", cap_usd=5.0, ledger_wired=True
)
NO_SPENDING = SpendingControls()

BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent

#: A syntactically valid Chrome id (32 chars, a-p).
EXT_ID = "abcdefghijklmnopabcdefghijklmnop"
OTHER_EXT_ID = "ponmlkjihgfedcbaponmlkjihgfedcba"
VERSION = "1.3.0"
COMMIT = "0343884"
BUILD_ID = "build-2026-11-03"


# --------------------------------------------------------------------------- #
# fixtures: synthetic extension + synthetic signed release
# --------------------------------------------------------------------------- #
def _extension_dir(
    base: Path,
    *,
    version: str = VERSION,
    extension_id: str = EXT_ID,
    manifest_extra: dict[str, Any] | None = None,
    service_worker: str = "sw.js",
    files: dict[str, bytes] | None = None,
    with_key: bool = False,
) -> Path:
    root = base / "Extensions" / extension_id / version
    root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "manifest_version": 3,
        "name": "Synthetic Flow Connector",
        "version": version,
        "background": {"service_worker": service_worker},
    }
    if with_key:
        manifest["key"] = base64.b64encode(b"synthetic-public-key-material-0123456789").decode()
    manifest.update(manifest_extra or {})
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if service_worker and ".." not in service_worker and not service_worker.startswith("/"):
        (root / service_worker).write_text(
            "// synthetic service worker\nchrome.runtime.onMessage.addListener(() => {});\n",
            encoding="utf-8",
        )
    (root / "content.js").write_text("// synthetic content script\n", encoding="utf-8")
    for name, data in (files or {}).items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return private, base64.b64encode(public).decode()


def _sign(document: dict[str, Any], private: Ed25519PrivateKey) -> dict[str, Any]:
    body = {key: value for key, value in document.items() if key != "signature"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**body, "signature": base64.b64encode(private.sign(canonical)).decode()}


def _manifest_document(
    extension_digest: str,
    *,
    version: str = VERSION,
    extension_id: str = EXT_ID,
    commit: str = COMMIT,
    components: list[dict[str, Any]] | None = None,
    schema: str = RELEASE_MANIFEST_SCHEMA,
    artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema": schema,
        "product": "deepx-flow-connector",
        # Phase 3C.2.3.1: a live-capable release declares its digest semantics; an
        # anchor that leaves them implicit is not live-capable.
        "digest_algorithm": "sha256-tree-runtime-v1",
        "release": {
            "version": version,
            "commit": commit,
            "build_id": BUILD_ID,
            "tag": f"v{version}",
            "released_at": "2026-11-01T00:00:00Z",
        },
        "extension": {
            "id": extension_id,
            "version": version,
            "protocol": proto.PROTOCOL_VERSION,
            "sha256": extension_digest,
        },
    }
    if components is None:
        document["components"] = [
            {
                "name": COMPONENT_EXTENSION,
                "version": version,
                "sha256": extension_digest,
                "extension_id": extension_id,
                "protocol": proto.PROTOCOL_VERSION,
                "build_id": BUILD_ID,
                "required": True,
            }
        ]
    else:
        document["components"] = components
    if artifact is not None:
        document["artifact"] = artifact
    return document


def _write_manifest(path: Path, document: dict[str, Any], private: Ed25519PrivateKey) -> Path:
    path.write_text(json.dumps(_sign(document, private), indent=2), encoding="utf-8")
    return path


@pytest.fixture()
def release(tmp_path: Path) -> dict[str, Any]:
    """A synthetic extension plus a signed manifest that describes it exactly."""
    root = _extension_dir(tmp_path)
    digest = package_tree_digest(root)
    private, public = _keypair()
    manifest_path = _write_manifest(tmp_path / "release.json", _manifest_document(digest), private)
    return {
        "root": root,
        "digest": digest,
        "private": private,
        "public": public,
        "manifest_path": manifest_path,
        "policy": TrustPolicy(
            release_manifest_path=str(manifest_path),
            release_manifest_public_key=public,
        ),
        "spec": InstallationSpec(root=root, expected_extension_id=EXT_ID, expected_version=VERSION),
    }


# --------------------------------------------------------------------------- #
# R — release verification
# --------------------------------------------------------------------------- #
def test_a_signed_manifest_identifies_every_component(release):
    anchor = verify_release_manifest(release["manifest_path"], release["public"])
    assert anchor.signature_verified is True
    assert anchor.schema == RELEASE_MANIFEST_SCHEMA
    assert anchor.product == "deepx-flow-connector"
    assert anchor.release == VERSION
    assert anchor.source_commit == COMMIT
    assert anchor.build_id == BUILD_ID
    assert anchor.protocol == proto.PROTOCOL_VERSION
    assert anchor.extension_id == EXT_ID
    assert anchor.released_at == "2026-11-01T00:00:00Z"
    assert anchor.components[COMPONENT_EXTENSION] == release["digest"]
    assert anchor.identifies_components is True
    assert anchor.required_components == (COMPONENT_EXTENSION,)


def test_a_tampered_manifest_body_is_rejected(release, tmp_path):
    document = _manifest_document(release["digest"])
    signed = _sign(document, release["private"])
    signed["release"]["version"] = "9.9.9"
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(signed), encoding="utf-8")
    with pytest.raises(ProvenanceRejected) as excinfo:
        verify_release_manifest(path, release["public"])
    assert "signature" in str(excinfo.value).lower()


def test_a_manifest_signed_by_another_key_is_rejected(release, tmp_path):
    _, other_public = _keypair()
    with pytest.raises(ProvenanceRejected):
        verify_release_manifest(release["manifest_path"], other_public)


def test_a_manifest_without_an_artifact_digest_establishes_nothing(release, tmp_path):
    document = _manifest_document(release["digest"], artifact={"sha256": ""}, components=[])
    document["extension"].pop("sha256")
    path = _write_manifest(tmp_path / "no-digest.json", document, release["private"])
    with pytest.raises(ProvenanceRejected):
        verify_release_manifest(path, release["public"])


def test_an_unsigned_manifest_is_rejected(release, tmp_path):
    path = tmp_path / "unsigned.json"
    path.write_text(json.dumps(_manifest_document(release["digest"])), encoding="utf-8")
    with pytest.raises(ProvenanceRejected):
        verify_release_manifest(path, release["public"])


def test_an_unknown_component_is_rejected(release, tmp_path):
    components = [
        {"name": COMPONENT_EXTENSION, "sha256": release["digest"], "required": True},
        {"name": "evil_sidecar", "sha256": "a" * 64},
    ]
    path = _write_manifest(
        tmp_path / "unknown.json", _manifest_document(release["digest"], components=components), release["private"]
    )
    with pytest.raises(ProvenanceRejected) as excinfo:
        verify_release_manifest(path, release["public"])
    assert "unknown component" in str(excinfo.value).lower()


def test_a_component_without_a_digest_is_rejected(release, tmp_path):
    components = [{"name": COMPONENT_EXTENSION, "sha256": release["digest"]}, {"name": COMPONENT_MCP}]
    path = _write_manifest(
        tmp_path / "no-component-digest.json",
        _manifest_document(release["digest"], components=components),
        release["private"],
    )
    with pytest.raises(ProvenanceRejected):
        verify_release_manifest(path, release["public"])


def test_a_legacy_single_artifact_manifest_cannot_identify_components(release, tmp_path):
    document = _manifest_document(
        release["digest"],
        schema="crusor.flow.release.v1",
        components=[],
        artifact={"sha256": release["digest"]},
    )
    document.pop("components")
    path = _write_manifest(tmp_path / "v1.json", document, release["private"])
    anchor = verify_release_manifest(path, release["public"])
    assert anchor.present is True
    assert anchor.identifies_components is False


def test_a_manifest_that_disagrees_with_itself_is_rejected(release, tmp_path):
    components = [{"name": COMPONENT_EXTENSION, "sha256": "b" * 64}]
    path = _write_manifest(
        tmp_path / "self-contradictory.json",
        _manifest_document(release["digest"], components=components),
        release["private"],
    )
    with pytest.raises(ProvenanceRejected):
        verify_release_manifest(path, release["public"])


# --------------------------------------------------------------------------- #
# M — installed measurement
# --------------------------------------------------------------------------- #
def test_a_matching_install_measures_to_the_release_digest(release):
    report = InstalledArtifactVerifier().measure(release["spec"])
    assert report.digest == release["digest"]
    assert report.digest_algorithm == "sha256-tree-runtime-v1"
    assert report.extension_id == EXT_ID
    assert report.extension_version == VERSION
    assert report.service_worker == "sw.js"
    assert report.file_count == 3
    assert report.measurement_id and len(report.measurement_id) == 32
    assert report.duration_ms >= 0
    assert "files_read_stable" in report.checks
    assert any("local_filesystem_only" in item for item in report.limitations)


def test_the_measurement_and_the_gate_compute_the_same_digest(release, tmp_path):
    """Shared test vector: both producers must agree exactly."""
    root = release["root"]
    assert InstalledArtifactVerifier().measure(release["spec"]).digest == package_tree_digest(root)
    # Same bytes, different producer: a copy measured elsewhere matches too.
    copy_root = tmp_path / "second-profile" / "Extensions" / EXT_ID / VERSION
    import shutil

    shutil.copytree(root, copy_root)
    copied = InstalledArtifactVerifier().measure(
        InstallationSpec(root=copy_root, expected_extension_id=EXT_ID, expected_version=VERSION)
    )
    assert copied.digest == package_tree_digest(root)


def test_the_runtime_profile_includes_what_the_source_profile_skips(release):
    """Phase 3C.2.3: dependency code is measured, not silently skipped.

    The licensing profile ignores `node_modules`/`__pycache__` because they are build
    noise in a vendor drop. The installed-runtime profile cannot: those files are
    code the process loads, so excluding them would let a patched dependency report
    the release digest.
    """
    root = release["root"]
    (root / "node_modules" / "dep").mkdir(parents=True)
    (root / "node_modules" / "dep" / "index.js").write_text("// dep\n", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01")

    runtime = InstalledArtifactVerifier().measure(release["spec"])
    source = InstalledArtifactVerifier().measure(
        InstallationSpec(
            root=root,
            expected_extension_id=EXT_ID,
            expected_version=VERSION,
            policy=MeasurementPolicy(digest_algorithm="sha256-tree-v1"),
        )
    )
    assert runtime.digest_algorithm == "sha256-tree-runtime-v1"
    assert source.digest_algorithm == "sha256-tree-v1"
    # The source profile still agrees with the licensing gate's own digest.
    assert source.digest == package_tree_digest(root)
    # The runtime profile sees the dependency, so it cannot equal the source digest.
    assert runtime.digest != source.digest
    assert runtime.file_count == source.file_count + 2


def test_a_modified_service_worker_changes_the_digest(release):
    before = InstalledArtifactVerifier().measure(release["spec"])
    (release["root"] / "sw.js").write_text("// patched\n", encoding="utf-8")
    after = InstalledArtifactVerifier().measure(release["spec"])
    assert after.digest != before.digest
    assert after.digest != release["digest"]


def test_a_modified_manifest_changes_the_digest(release):
    manifest_path = release["root"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["name"] = "Synthetic Flow Connector (patched)"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    assert InstalledArtifactVerifier().measure(release["spec"]).digest != release["digest"]


def test_an_added_executable_changes_the_digest_and_violates_a_strict_policy(release):
    (release["root"] / "extra.js").write_text("// unannounced code\n", encoding="utf-8")
    lenient = InstalledArtifactVerifier().measure(release["spec"])
    assert lenient.digest != release["digest"]

    strict = InstallationSpec(
        root=release["root"],
        expected_extension_id=EXT_ID,
        expected_version=VERSION,
        policy=MeasurementPolicy(allow_extra_files=False),
    )
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier().measure(strict)
    assert excinfo.value.code == "unexpected_file_in_package"


def test_a_missing_service_worker_is_refused(release):
    (release["root"] / "sw.js").unlink()
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier().measure(release["spec"])
    assert excinfo.value.code == "missing_service_worker"


def test_the_wrong_extension_id_is_refused(release):
    other = InstallationSpec(
        root=release["root"], expected_extension_id=OTHER_EXT_ID, expected_version=VERSION
    )
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier().measure(other)
    assert excinfo.value.code == "extension_identity_mismatch"


def test_a_symlinked_file_is_refused(release, tmp_path):
    target = tmp_path / "outside.js"
    target.write_text("// outside\n", encoding="utf-8")
    (release["root"] / "linked.js").symlink_to(target)
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier().measure(release["spec"])
    assert excinfo.value.code == "symlink_in_package"


def test_a_symlinked_directory_is_refused(release, tmp_path):
    target = tmp_path / "outside_dir"
    target.mkdir()
    (target / "evil.js").write_text("// outside\n", encoding="utf-8")
    (release["root"] / "sub").symlink_to(target, target_is_directory=True)
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier().measure(release["spec"])
    assert excinfo.value.code == "symlink_in_package"


def test_a_symlinked_root_is_refused(release, tmp_path):
    link = tmp_path / "root-link"
    link.symlink_to(release["root"], target_is_directory=True)
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier().measure(
            InstallationSpec(root=link, expected_extension_id=EXT_ID, expected_version=VERSION)
        )
    assert excinfo.value.code == "symlink_in_package"


@pytest.mark.parametrize(
    "declared",
    ["../../etc/passwd", "/etc/passwd", "sub/../../escape.js", ""],
)
def test_a_traversing_manifest_path_is_refused(tmp_path, declared):
    root = _extension_dir(tmp_path, service_worker=declared or "sw.js")
    if not declared:
        manifest = json.loads((root / "manifest.json").read_text())
        manifest["background"] = {}
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier().measure(
            InstallationSpec(root=root, expected_extension_id=EXT_ID, expected_version=VERSION)
        )
    assert excinfo.value.code in {
        "unsafe_relative_path",
        "missing_service_worker",
        "invalid_package_layout",
    }


def test_a_missing_installation_is_not_guessed_at(tmp_path):
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier().measure(
            InstallationSpec(root=tmp_path / "nope", expected_extension_id=EXT_ID)
        )
    assert excinfo.value.code == "installed_artifact_not_found"


def test_a_directory_without_an_extension_id_is_not_guessed_at(release):
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier().measure(InstallationSpec(root=release["root"], layout="directory"))
    assert excinfo.value.code in {"unsupported_package_layout", "extension_id_not_configured"}


def test_an_unpacked_directory_is_identified_from_the_manifest_key(tmp_path):
    root = tmp_path / "unpacked"
    root.mkdir()
    key = base64.b64encode(b"unpacked-public-key-material-0123456789").decode()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": 3,
                "name": "Synthetic unpacked",
                "version": VERSION,
                "key": key,
                "background": {"service_worker": "sw.js"},
            }
        ),
        encoding="utf-8",
    )
    (root / "sw.js").write_text("// synthetic\n", encoding="utf-8")
    derived = chrome_extension_id_from_key(key)
    report = InstalledArtifactVerifier().measure(
        InstallationSpec(
            root=root, layout="directory", expected_extension_id=derived, expected_version=VERSION
        )
    )
    assert report.extension_id == derived
    with pytest.raises(MeasurementError):
        InstalledArtifactVerifier().measure(
            InstallationSpec(
                root=root, layout="directory", expected_extension_id=EXT_ID, expected_version=VERSION
            )
        )


def test_an_unsupported_manifest_version_is_refused(tmp_path):
    root = _extension_dir(tmp_path, manifest_extra={"manifest_version": 9})
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier().measure(
            InstallationSpec(root=root, expected_extension_id=EXT_ID, expected_version=VERSION)
        )
    assert excinfo.value.code == "unsupported_package_layout"


def test_an_unexpected_version_is_refused(release):
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier().measure(
            InstallationSpec(root=release["root"], expected_extension_id=EXT_ID, expected_version="1.4.0")
        )
    assert excinfo.value.code == "unexpected_version"


def test_a_file_added_during_measurement_fails_the_report(release, monkeypatch):
    """Phase 3C.2.3.1: a file appearing in a directory mid-scan fails the scan.

    The injection is done *through the directory descriptor the scan is using*, which
    is the only way a writer can add an entry to the directory being measured. The
    scan re-lists each directory against its descriptor and refuses the mismatch.
    """
    import app.flow.installed_artifact as module

    real_read = module._read_file_at
    injected = {"done": False}

    def read_then_inject(dir_fd, name, relative, *, max_bytes):
        data, st = real_read(dir_fd, name, relative, max_bytes=max_bytes)
        if not injected["done"] and relative != "manifest.json":
            injected["done"] = True
            fd = os.open(
                "injected.js",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
                dir_fd=dir_fd,
            )
            try:
                os.write(fd, b"// injected mid-measurement\n")
            finally:
                os.close(fd)
        return data, st

    monkeypatch.setattr(module, "_read_file_at", read_then_inject)
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier().measure(release["spec"])
    assert excinfo.value.code == "file_added_during_measurement"
    assert injected["done"] is True
    (release["root"] / "injected.js").unlink()


def test_a_file_changed_during_measurement_fails_the_report(release, monkeypatch):
    target = (release["root"] / "sw.js").resolve()
    real_fstat = os.fstat
    seen: dict[int, int] = {}

    def mutating_fstat(fd: int):
        result = real_fstat(fd)
        try:
            path = Path(os.readlink(f"/proc/self/fd/{fd}")).resolve()
        except OSError:  # pragma: no cover - /proc not available
            return result
        if path != target:
            return result
        seen[fd] = seen.get(fd, 0) + 1
        if seen[fd] == 2:
            values = (
                result.st_mode,
                result.st_ino,
                result.st_dev,
                result.st_nlink,
                result.st_uid,
                result.st_gid,
                result.st_size,
                result.st_atime,
                result.st_mtime + 5.0,
                result.st_ctime,
            )
            return os.stat_result(values)
        return result

    monkeypatch.setattr(os, "fstat", mutating_fstat)
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier().measure(release["spec"])
    assert excinfo.value.code == "file_mutated_during_measurement"


def test_a_replaced_file_is_detected_by_inode_check(release, monkeypatch):
    """The name must still point at the inode that was read, checked by descriptor."""
    real_stat = os.stat

    def swapping_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if kwargs.get("dir_fd") is not None and str(path) == "sw.js":
            values = list(result)
            values[1] = result.st_ino + 1  # st_ino
            return os.stat_result(values)
        return result

    monkeypatch.setattr(os, "stat", swapping_stat)
    with pytest.raises(MeasurementError) as excinfo:
        InstalledArtifactVerifier().measure(release["spec"])
    assert excinfo.value.code == "file_replaced_during_measurement"


def test_component_frames_carry_measurement_identity(release):
    report = InstalledArtifactVerifier().measure(release["spec"])
    frame = component_frame(report)
    assert frame["name"] == "chrome_extension"
    assert frame["digest"] == release["digest"]
    assert frame["measurement_id"] == report.measurement_id
    assert frame["measured_at"] == report.finished_at
    assert frame["limitations"]


def test_every_configured_component_is_measured_independently(release, tmp_path):
    """Two executables, two measurements. Neither vouches for the other."""
    mcp_root = tmp_path / "mcp"
    mcp_root.mkdir()
    key = base64.b64encode(b"synthetic-mcp-public-key-material-0123456789").decode()
    (mcp_root / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "Synthetic MCP", "version": VERSION, "key": key}),
        encoding="utf-8",
    )
    (mcp_root / "server.js").write_text("// synthetic mcp\n", encoding="utf-8")
    mcp_spec = InstallationSpec(
        root=mcp_root,
        layout="directory",
        expected_extension_id=chrome_extension_id_from_key(key),
        expected_version=VERSION,
        policy=MeasurementPolicy(require_service_worker=False),
    )
    frames = measure_components({COMPONENT_EXTENSION: release["spec"], COMPONENT_MCP: mcp_spec})
    assert {frame["name"] for frame in frames} == {COMPONENT_EXTENSION, COMPONENT_MCP}
    assert frames[0]["digest"] != frames[1]["digest"]

    # A component that cannot be measured fails the whole measurement rather than
    # being quietly skipped and assumed fine.
    broken = tmp_path / "broken-mcp"
    broken.mkdir()
    with pytest.raises(MeasurementError):
        measure_components(
            {
                COMPONENT_EXTENSION: release["spec"],
                COMPONENT_MCP: InstallationSpec(
                    root=broken, layout="directory", expected_extension_id="x" * 32
                ),
            }
        )


# --------------------------------------------------------------------------- #
# C — challenge and session binding
# --------------------------------------------------------------------------- #
def _submit(
    store: ChallengeStore,
    *,
    digest: str,
    extension_id: str = EXT_ID,
    version: str = VERSION,
    measured_at: float | None = None,
    components: list[ComponentReport] | None = None,
) -> tuple[VerificationSubmission, str]:
    challenge = store.issue(session_id="session-1", companion_id=EXT_ID, extension_id=EXT_ID)
    reports = components or [
        ComponentReport(
            name=COMPONENT_EXTENSION,
            digest=digest,
            extension_id=extension_id,
            version=version,
            file_count=1, measurement_id="a" * 32,
            measured_at=challenge.issued_at if measured_at is None else measured_at,
        )
    ]
    return VerificationSubmission(
        challenge_id=challenge.challenge_id, nonce=challenge.nonce, components=tuple(reports)
    ), challenge.challenge_id


def _attestation(release) -> SessionAttestation:
    anchor = verify_release_manifest(release["manifest_path"], release["public"])
    return SessionAttestation(store=ChallengeStore(ttl_seconds=60), anchor=anchor, freshness_seconds=300)


def test_a_fresh_challenge_bound_measurement_correlates_identity(release):
    attestation = _attestation(release)
    challenge = attestation.store.issue(
        session_id="session-1", companion_id=EXT_ID, extension_id=EXT_ID
    )
    submission = VerificationSubmission(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        components=(
            ComponentReport(
                name=COMPONENT_EXTENSION,
                digest=release["digest"],
                extension_id=EXT_ID,
                version=VERSION,
                file_count=1, measurement_id="a" * 32,
                measured_at=challenge.issued_at,
            ),
        ),
    )
    evidence = attestation.accept(
        submission,
        session_id="session-1",
        companion_id=EXT_ID,
        hello_extension_id=EXT_ID,
        hello_version=VERSION,
        hello_digest=release["digest"],
    )
    assert evidence.installed_artifact_measured is True
    assert evidence.installed_artifact_digest_matches_anchor is True
    assert evidence.runtime_identity_correlated is True
    assert evidence.measurement_fresh is True
    assert evidence.hello_digest_claim_matches_anchor is True
    assert evidence.measurement_trust_state == VERIFIED
    assert evidence.failures == ()


def test_an_expired_challenge_is_refused(release):
    anchor = verify_release_manifest(release["manifest_path"], release["public"])
    store = ChallengeStore(ttl_seconds=0.01)
    attestation = SessionAttestation(store=store, anchor=anchor)
    submission, _ = _submit(store, digest=release["digest"])
    import time

    time.sleep(0.05)
    with pytest.raises(AttestationRejected) as excinfo:
        attestation.accept(
            submission,
            session_id="session-1",
            companion_id=EXT_ID,
            hello_extension_id=EXT_ID,
            hello_version=VERSION,
            hello_digest=release["digest"],
        )
    assert excinfo.value.code in {"challenge_expired", "challenge_unknown"}


def test_a_replayed_challenge_is_refused(release):
    attestation = _attestation(release)
    submission, _ = _submit(attestation.store, digest=release["digest"])
    attestation.accept(
        submission,
        session_id="session-1",
        companion_id=EXT_ID,
        hello_extension_id=EXT_ID,
        hello_version=VERSION,
        hello_digest=release["digest"],
    )
    with pytest.raises(AttestationRejected) as excinfo:
        attestation.accept(
            submission,
            session_id="session-1",
            companion_id=EXT_ID,
            hello_extension_id=EXT_ID,
            hello_version=VERSION,
            hello_digest=release["digest"],
        )
    assert excinfo.value.code == "challenge_replayed"


def test_a_challenge_issued_to_another_companion_is_refused(release):
    attestation = _attestation(release)
    submission, _ = _submit(attestation.store, digest=release["digest"])
    with pytest.raises(AttestationRejected) as excinfo:
        attestation.accept(
            submission,
            session_id="session-1",
            companion_id="another-companion",
            hello_extension_id=EXT_ID,
            hello_version=VERSION,
            hello_digest=release["digest"],
        )
    assert excinfo.value.code == "challenge_wrong_companion"


def test_a_challenge_issued_to_another_extension_is_refused(release):
    attestation = _attestation(release)
    submission, _ = _submit(attestation.store, digest=release["digest"])
    with pytest.raises(AttestationRejected) as excinfo:
        attestation.accept(
            submission,
            session_id="session-1",
            companion_id=EXT_ID,
            hello_extension_id=OTHER_EXT_ID,
            hello_version=VERSION,
            hello_digest=release["digest"],
        )
    assert excinfo.value.code == "challenge_wrong_extension"


def test_a_report_from_another_session_is_refused(release):
    attestation = _attestation(release)
    submission, _ = _submit(attestation.store, digest=release["digest"])
    with pytest.raises(AttestationRejected) as excinfo:
        attestation.accept(
            submission,
            session_id="session-2",
            companion_id=EXT_ID,
            hello_extension_id=EXT_ID,
            hello_version=VERSION,
            hello_digest=release["digest"],
        )
    assert excinfo.value.code == "challenge_wrong_session"


def test_a_measurement_from_before_the_challenge_is_refused(release):
    """Phase 3C.2.3: an earlier measurement cannot be bound to a later session."""
    attestation = _attestation(release)
    challenge = attestation.store.issue(
        session_id="session-1", companion_id=EXT_ID, extension_id=EXT_ID
    )
    submission = VerificationSubmission(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        components=(
            ComponentReport(
                name=COMPONENT_EXTENSION,
                digest=release["digest"],
                extension_id=EXT_ID,
                version=VERSION,
                measurement_id="a" * 32,
                file_count=1,
                measured_at=challenge.issued_at - 120,
            ),
        ),
    )
    with pytest.raises(AttestationRejected) as excinfo:
        attestation.accept(
            submission,
            session_id="session-1",
            companion_id=EXT_ID,
            hello_extension_id=EXT_ID,
            hello_version=VERSION,
            hello_digest=release["digest"],
        )
    assert excinfo.value.code == "measurement_outside_challenge_window"


def test_an_old_measurement_outside_the_freshness_window_is_refused(release):
    attestation = _attestation(release)
    challenge = attestation.store.issue(
        session_id="session-1", companion_id=EXT_ID, extension_id=EXT_ID
    )
    submission = VerificationSubmission(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        components=(
            ComponentReport(
                name=COMPONENT_EXTENSION,
                digest=release["digest"],
                extension_id=EXT_ID,
                version=VERSION,
                measurement_id="a" * 32,
                file_count=1,
                measured_at=challenge.issued_at,
            ),
        ),
    )
    attestation.clock = lambda: challenge.issued_at + 10_000
    with pytest.raises(AttestationRejected) as excinfo:
        attestation.accept(
            submission,
            session_id="session-1",
            companion_id=EXT_ID,
            hello_extension_id=EXT_ID,
            hello_version=VERSION,
            hello_digest=release["digest"],
        )
    assert excinfo.value.code == "measurement_outside_challenge_window"


def test_a_measurement_claiming_to_be_from_the_future_is_refused(release):
    attestation = _attestation(release)
    challenge = attestation.store.issue(
        session_id="session-1", companion_id=EXT_ID, extension_id=EXT_ID
    )
    submission = VerificationSubmission(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        components=(
            ComponentReport(
                name=COMPONENT_EXTENSION,
                digest=release["digest"],
                extension_id=EXT_ID,
                version=VERSION,
                measurement_id="a" * 32,
                file_count=1,
                measured_at=challenge.issued_at + 3_600,
            ),
        ),
    )
    with pytest.raises(AttestationRejected) as excinfo:
        attestation.accept(
            submission,
            session_id="session-1",
            companion_id=EXT_ID,
            hello_extension_id=EXT_ID,
            hello_version=VERSION,
            hello_digest=release["digest"],
        )
    assert excinfo.value.code == "measurement_outside_challenge_window"


def test_a_hello_digest_that_contradicts_the_measurement_is_recorded(release):
    attestation = _attestation(release)
    submission, _ = _submit(attestation.store, digest=release["digest"])
    evidence = attestation.accept(
        submission,
        session_id="session-1",
        companion_id=EXT_ID,
        hello_extension_id=EXT_ID,
        hello_version=VERSION,
        hello_digest="b" * 64,
    )
    assert evidence.hello_digest_claim_matches_anchor is False
    assert evidence.installed_artifact_digest_matches_anchor is True


def test_a_hello_cannot_set_a_measured_field():
    """The HELLO model has no field that could populate measured evidence."""
    hello_fields = set(proto.HelloMessage.model_fields)
    for field_name in (
        "installed_artifact_measured",
        "installed_artifact_digest_matches_anchor",
        "runtime_identity_correlated",
        "measurement_fresh",
        "measurement_trust_state",
        "hello_digest_claim_matches_anchor",
    ):
        assert field_name not in hello_fields
    evidence = TrustEvidence()
    assert evidence.installed_artifact_measured is False
    assert evidence.measurement_trust_state == UNVERIFIED


# --------------------------------------------------------------------------- #
# T — trust-state aggregation
# --------------------------------------------------------------------------- #
def _anchored(release):
    return verify_release_manifest(release["manifest_path"], release["public"])


def _verified_evidence(release) -> TrustEvidence:
    attestation = _attestation(release)
    submission, _ = _submit(attestation.store, digest=release["digest"])
    return attestation.accept(
        submission,
        session_id="session-1",
        companion_id=EXT_ID,
        hello_extension_id=EXT_ID,
        hello_version=VERSION,
        hello_digest=release["digest"],
    )


def _decide(release, *, anchor=None, evidence=None, source_gate_status="AUTHORIZED"):
    return aggregate_trust(
        source_gate_status=source_gate_status,
        anchor=anchor if anchor is not None else _anchored(release),
        provenance=None,
        evidence=evidence,
        session_id="session-1",
        measured_extension_id=EXT_ID,
        measured_version=VERSION,
        measured_digest=release["digest"],
    )


def test_a_fully_verified_synthetic_release_is_trusted(release):
    decision = _decide(release, evidence=_verified_evidence(release))
    assert decision.state == VERIFIED
    assert decision.trusted is True
    assert decision.reasons == ()


def test_no_anchor_is_never_trusted(release):
    from app.flow.provenance import TrustAnchor

    decision = _decide(release, anchor=TrustAnchor(), evidence=_verified_evidence(release))
    assert decision.state == RELEASE_ANCHOR_MISSING
    assert decision.trusted is False


def test_a_signed_manifest_without_a_measurement_is_not_trusted(release):
    decision = _decide(release, evidence=None)
    assert decision.state == INSTALLED_ARTIFACT_NOT_FOUND
    assert decision.trusted is False


def test_a_measurement_without_a_trusted_manifest_is_not_trusted(release):
    from app.flow.provenance import TrustAnchor

    decision = _decide(release, anchor=TrustAnchor(), evidence=_verified_evidence(release))
    assert decision.trusted is False


def test_a_self_reported_digest_alone_is_never_trusted(release):
    """The HELLO's own hash, with no measurement and no anchor, changes nothing."""
    from app.flow.provenance import TrustAnchor

    evidence = TrustEvidence(
        supported_protocol=True,
        claims_well_formed=True,
        components_consistent=True,
        hello_digest_claim_matches_anchor=True,
    )
    decision = _decide(release, anchor=TrustAnchor(), evidence=evidence)
    assert decision.trusted is False
    assert decision.state == RELEASE_ANCHOR_MISSING


def test_a_mismatching_artifact_is_not_trusted(release, tmp_path):
    other_root = _extension_dir(tmp_path / "other", version="1.3.1")
    other_digest = package_tree_digest(other_root)
    attestation = _attestation(release)
    submission, _ = _submit(attestation.store, digest=other_digest)
    evidence = attestation.accept(
        submission,
        session_id="session-1",
        companion_id=EXT_ID,
        hello_extension_id=EXT_ID,
        hello_version=VERSION,
        hello_digest=release["digest"],
    )
    decision = _decide(release, evidence=evidence)
    assert decision.state == ARTIFACT_DIGEST_MISMATCH
    assert decision.trusted is False


def test_a_stale_measurement_is_not_trusted(release):
    """Freshness is a property of the evidence, and aggregation honours it."""
    evidence = TrustEvidence(
        supported_protocol=True,
        claims_well_formed=True,
        components_consistent=True,
        release_anchor_valid=True,
        installed_artifact_measured=True,
        installed_artifact_digest_matches_anchor=True,
        runtime_identity_correlated=True,
        measurement_fresh=False,
        measurement_expires_at=1.0,
        component_states={COMPONENT_EXTENSION: COMPONENT_VERIFIED},
        required_components=(COMPONENT_EXTENSION,),
    )
    decision = _decide(release, evidence=evidence)
    assert decision.state == MEASUREMENT_STALE
    assert decision.trusted is False


def test_a_missing_required_component_keeps_the_extension_untrusted(release):
    anchor = _anchored(release)
    components = [
        {
            "name": COMPONENT_EXTENSION,
            "sha256": release["digest"],
            "required": True,
        },
        {"name": COMPONENT_MCP, "sha256": "c" * 64, "required": True},
    ]
    private, public = _keypair()
    path = _write_manifest(
        release["manifest_path"].parent / "with-mcp.json",
        _manifest_document(release["digest"], components=components),
        private,
    )
    anchor = verify_release_manifest(path, public)
    attestation = SessionAttestation(
        store=ChallengeStore(ttl_seconds=60),
        anchor=anchor,
        required_components=(COMPONENT_EXTENSION, COMPONENT_MCP),
    )
    submission, _ = _submit(attestation.store, digest=release["digest"])
    evidence = attestation.accept(
        submission,
        session_id="session-1",
        companion_id=EXT_ID,
        hello_extension_id=EXT_ID,
        hello_version=VERSION,
        hello_digest=release["digest"],
    )
    assert evidence.component_states[COMPONENT_MCP] == "NOT_MEASURED"
    decision = _decide(release, anchor=anchor, evidence=evidence)
    assert decision.state == COMPONENT_UNVERIFIED
    assert decision.trusted is False


def test_absent_upstream_source_is_its_own_state(release):
    decision = _decide(release, evidence=_verified_evidence(release), source_gate_status="SOURCE_ABSENT")
    assert decision.state == SOURCE_ABSENT
    assert decision.trusted is False


def test_a_measurement_failure_state_is_not_trusted(release):
    evidence = TrustEvidence(
        supported_protocol=True,
        claims_well_formed=True,
        release_anchor_valid=True,
        installed_artifact_measured=True,
        measurement_fresh=True,
        failures=("file_mutated_during_measurement",),
    )
    decision = _decide(release, evidence=evidence)
    assert decision.state == ARTIFACT_MEASUREMENT_FAILED
    assert decision.trusted is False


def test_every_state_is_distinct_and_reported_by_name():
    from app.flow.trust import ALL_TRUST_STATES

    assert len(set(ALL_TRUST_STATES)) == 9
    assert "VERIFIED" in ALL_TRUST_STATES and "UNVERIFIED" in ALL_TRUST_STATES


# --------------------------------------------------------------------------- #
# G — generation guard
# --------------------------------------------------------------------------- #
def test_the_guard_lists_every_blocker(release):
    guard = evaluate_live_generation_guard(
        live_generation_flag=False,
        source_gate_status="SOURCE_ABSENT",
        decision=None,
        approval_binding="",
        budget_controls=NO_SPENDING,
        idempotency_enforced=True,
    )
    assert guard.allowed is False
    assert "live_generation_disabled" in guard.blockers
    assert any(item.startswith("upstream_source") for item in guard.blockers)
    assert any(item.startswith("installed_artifact") for item in guard.blockers)
    assert "founder_approval_missing" in guard.blockers
    assert "budget_controls_missing" in guard.blockers


def test_the_guard_allows_only_a_complete_set(release):
    decision = _decide(release, evidence=_verified_evidence(release))
    guard = evaluate_live_generation_guard(
        live_generation_flag=True,
        source_gate_status="AUTHORIZED",
        decision=decision,
        approval_binding="a" * 64,
        budget_controls=VERIFIED_SPENDING,
        idempotency_enforced=True,
    )
    assert guard.allowed is True
    assert guard.blockers == ()


def test_an_approval_cannot_bypass_a_failed_measurement(release, tmp_path):
    other_root = _extension_dir(tmp_path / "other", version="1.3.1")
    attestation = _attestation(release)
    submission, _ = _submit(attestation.store, digest=package_tree_digest(other_root))
    evidence = attestation.accept(
        submission,
        session_id="session-1",
        companion_id=EXT_ID,
        hello_extension_id=EXT_ID,
        hello_version=VERSION,
        hello_digest=release["digest"],
    )
    decision = _decide(release, evidence=evidence)
    guard = evaluate_live_generation_guard(
        live_generation_flag=True,
        source_gate_status="AUTHORIZED",
        decision=decision,
        approval_binding="a" * 64,
        budget_controls=VERIFIED_SPENDING,
        idempotency_enforced=True,
    )
    assert guard.allowed is False
    assert any(item.startswith("installed_artifact") for item in guard.blockers)


@pytest.mark.asyncio
async def test_the_coordinator_refuses_to_dispatch_for_an_unverified_artifact(release):
    from app.flow.coordinator import GenerationCoordinator, UntrustedArtifact as CoordinatorUntrusted
    from app.flow import protocol as flowproto

    class Transport:
        def __init__(self) -> None:
            self.sent: list[Any] = []

        async def send(self, envelope):  # noqa: ANN001
            self.sent.append(envelope)

        async def await_result(self, command_id, timeout):  # noqa: ANN001
            raise AssertionError("no command may be sent")

    transport = Transport()
    coordinator = GenerationCoordinator(
        transport,
        live_generation_enabled=True,
        trust_provider=lambda: _decide(release, evidence=None),
    )
    request = flowproto.GenerationCommand(prompt="synthetic", model="imagen-3", count=1)
    with pytest.raises(CoordinatorUntrusted):
        await coordinator.submit("image", request, approval_binding="a" * 64)
    assert transport.sent == []


@pytest.mark.asyncio
async def test_the_coordinator_dispatches_only_for_a_verified_artifact(release):
    from app.flow.coordinator import GenerationCoordinator
    from app.flow import protocol as flowproto

    class Transport:
        def __init__(self) -> None:
            self.sent: list[Any] = []

        async def send(self, envelope):  # noqa: ANN001
            self.sent.append(envelope)

        async def await_result(self, command_id, timeout):  # noqa: ANN001
            return flowproto.ResultEnvelope(
                type="result", id=command_id, ok=True, job_id="", result={"media_id": "media-1"}
            )

    transport = Transport()
    coordinator = GenerationCoordinator(
        transport,
        live_generation_enabled=True,
        trust_provider=lambda: _decide(release, evidence=_verified_evidence(release)),
    )
    request = flowproto.GenerationCommand(prompt="synthetic", model="imagen-3", count=1)
    job = await coordinator.submit("image", request, approval_binding="a" * 64)
    assert job.lifecycle.value == "SUBMITTED"
    assert len(transport.sent) == 1


def test_live_generation_stays_disabled_in_settings():
    from app.core.config import Settings

    settings = Settings(environment="test")
    assert settings.flow_live_generation_enabled is False
    assert settings.flow_require_installed_measurement is False
    assert settings.flow_extension_install_root == ""


# --------------------------------------------------------------------------- #
# S — status and audit
# --------------------------------------------------------------------------- #
def test_status_reports_the_trust_state_without_secrets():
    from app.core.config import Settings
    from app.services.flow_gateway import flow_status_payload

    payload = flow_status_payload(
        Settings(
            environment="test",
            flow_companion_secret="super-secret-companion-value-0123456789",
            flow_pairing_secret="super-secret-pairing-value-0123456789",
        )
    )
    trust = payload["trust"]
    assert trust["state"] in {"SOURCE_ABSENT", "RELEASE_ANCHOR_MISSING", "UNVERIFIED"}
    assert trust["trusted"] is False
    assert len(trust["states_supported"]) == 9
    assert set(trust["components"]) == {COMPONENT_EXTENSION, COMPONENT_MCP, COMPONENT_COMPANION}
    assert trust["unverified_components"]
    serialised = json.dumps(trust).lower()
    for forbidden in ("nonce", "private_key", "access_token", "authorization", "pairing_secret"):
        assert forbidden not in serialised
    assert "super-secret" not in serialised


def test_the_registry_records_measurement_events_and_revocations(release):
    registry = TrustRegistry()
    good = _decide(release, evidence=_verified_evidence(release))
    registry.record("session-1", good)
    assert registry.latest() is good

    registry.record("session-1", _decide(release, evidence=None))
    actions = [event["action"] for event in registry.events()]
    assert "flow.trust.measurement_succeeded" in actions
    assert "flow.trust.trust_revoked" in actions


def test_a_replayed_challenge_is_auditable(release):
    registry = TrustRegistry()
    attestation = _attestation(release)
    submission, _ = _submit(attestation.store, digest=release["digest"])
    attestation.accept(
        submission,
        session_id="session-1",
        companion_id=EXT_ID,
        hello_extension_id=EXT_ID,
        hello_version=VERSION,
        hello_digest=release["digest"],
    )
    with pytest.raises(AttestationRejected) as excinfo:
        attestation.accept(
            submission,
            session_id="session-1",
            companion_id=EXT_ID,
            hello_extension_id=EXT_ID,
            hello_version=VERSION,
            hello_digest=release["digest"],
        )
    registry.audit(f"flow.trust.{excinfo.value.code}", session_id="session-1")
    assert registry.events()[-1]["action"] == "flow.trust.challenge_replayed"


# --------------------------------------------------------------------------- #
# B — bridge integration
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_bridge_measures_locally_and_trusts_the_session(release, monkeypatch):
    """The production path: this process reads the roots, the peer only gets an ack."""
    import asyncio

    import websockets

    from app.flow.bridge import BridgeConfig, ComponentInstallSpec, FlowBridge
    from app.flow.security import generate_pairing_id, generate_pairing_secret

    monkeypatch.setattr(FlowBridge, "_source_gate_status", lambda self: "AUTHORIZED")
    pairing_id = generate_pairing_id()
    secret = generate_pairing_secret()
    bridge = FlowBridge(
        BridgeConfig(
            pairing_id=pairing_id,
            pairing_secret=secret,
            http_port=0,
            ws_port=0,
            extension_ids=(EXT_ID,),
            handshake_timeout=2.0,
            companion_id="companion-fixture",
            release_manifest_path=str(release["manifest_path"]),
            release_manifest_public_key=release["public"],
            require_installed_measurement=True,
            install_components=(
                ComponentInstallSpec(component=COMPONENT_EXTENSION, root=str(release["root"])),
            ),
        )
    )
    await bridge.start()
    try:
        hello = {
            "type": "hello",
            "protocol": proto.PROTOCOL_VERSION,
            "pairing_id": pairing_id,
            "pairing_secret": secret,
            "extension_id": EXT_ID,
            "extension_version": VERSION,
            "service_worker_version": VERSION,
            "mcp_version": VERSION,
            "source_commit": COMMIT,
            "build_id": BUILD_ID,
            "package_sha256": release["digest"],
            "flow_tab_present": True,
        }
        async with websockets.connect(f"ws://127.0.0.1:{bridge.ws_port}/ws") as ws:
            await ws.send(json.dumps(hello))
            ack = json.loads(await asyncio.wait_for(ws.recv(), 3))
            assert ack["type"] == "hello_ack"
            assert ack["measurement_required"] is True
            # The peer is told nothing it could use to manufacture a measurement.
            assert ack["provenance_trusted"] is False
            session = bridge.session
            assert session is not None
            if session.measurement_task is not None:
                await asyncio.wait_for(session.measurement_task, 15)
            for _ in range(40):
                if bridge.snapshot()["trust"]["state"] != "UNVERIFIED":
                    break
                await asyncio.sleep(0.1)
        snapshot = bridge.snapshot()
        assert snapshot["trust"]["state"] == VERIFIED, snapshot["trust"]["reasons"]
        assert snapshot["trust"]["measured"]["artifact_digest"] == release["digest"]
        assert snapshot["trust"]["measured"]["source"] == "local_companion"
        assert snapshot["trust"]["components"][COMPONENT_EXTENSION]["state"] == COMPONENT_VERIFIED
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_a_peer_supplied_report_cannot_establish_trust(release, monkeypatch):
    """A verification_report frame over the extension socket is not a measurement."""
    import asyncio

    import websockets

    from app.flow.bridge import BridgeConfig, FlowBridge
    from app.flow.security import generate_pairing_id, generate_pairing_secret

    monkeypatch.setattr(FlowBridge, "_source_gate_status", lambda self: "AUTHORIZED")
    pairing_id = generate_pairing_id()
    secret = generate_pairing_secret()
    bridge = FlowBridge(
        BridgeConfig(
            pairing_id=pairing_id,
            pairing_secret=secret,
            http_port=0,
            ws_port=0,
            extension_ids=(EXT_ID,),
            handshake_timeout=2.0,
            companion_id="companion-fixture",
            release_manifest_path=str(release["manifest_path"]),
            release_manifest_public_key=release["public"],
            require_installed_measurement=False,
        )
    )
    await bridge.start()
    try:
        hello = {
            "type": "hello",
            "protocol": proto.PROTOCOL_VERSION,
            "pairing_id": pairing_id,
            "pairing_secret": secret,
            "extension_id": EXT_ID,
            "extension_version": VERSION,
            "service_worker_version": VERSION,
            "mcp_version": VERSION,
            "source_commit": COMMIT,
            "build_id": BUILD_ID,
            "package_sha256": release["digest"],
        }
        async with websockets.connect(f"ws://127.0.0.1:{bridge.ws_port}/ws") as ws:
            await ws.send(json.dumps(hello))
            await asyncio.wait_for(ws.recv(), 3)  # hello_ack
            await ws.send(
                json.dumps(
                    {
                        "type": "verification_report",
                        "challenge_id": "a" * 32,
                        "nonce": "b" * 32,
                        "components": [
                            {
                                "name": COMPONENT_EXTENSION,
                                "digest": release["digest"],
                                "extension_id": EXT_ID,
                                "version": VERSION,
                                "measurement_id": "c" * 32,
                                "file_count": 3,
                                "measured_at": 1_800_000_000.0,
                            }
                        ],
                    }
                )
            )
            with pytest.raises(websockets.ConnectionClosed) as excinfo:
                await asyncio.wait_for(ws.recv(), 3)
            # Unknown frame type: the protocol has no peer-measurement verb at all.
            assert excinfo.value.rcvd is not None
            assert excinfo.value.rcvd.code == 4400
        assert bridge.snapshot()["trust"]["state"] != VERIFIED
        assert bridge.snapshot()["trust"]["trusted"] is False
    finally:
        await bridge.stop()
