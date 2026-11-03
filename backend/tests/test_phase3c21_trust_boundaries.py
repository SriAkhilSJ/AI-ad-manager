"""Phase 3C.2.1 — the three trust-boundary corrections, tested behaviourally.

Phase 3C.2 shipped three claims that were weaker than they looked. This suite
pins the corrected behaviour, and each test fails if the correction is reverted.

**L. Licensing gate.** A file named ``LICENSE`` used to set ``AUTHORIZED``. Now the
gate distinguishes five states, and only a human-reviewed attestation bound to the
package's own tree digest authorizes anything. Nothing here interprets a licence;
the tests assert that *presence* never becomes *permission*.

**P. Provenance.** A well-formed HELLO used to be reported as ``trusted``. Now
``claims_well_formed``, ``components_consistent`` and
``artifact_integrity_verified`` are separate facts, and trust is the conjunction of
all three. A digest or commit the extension reports about itself is never its own
anchor; only a digest Crusor pinned, or an Ed25519-signed release manifest whose
signature verifies against a key held outside the extension, counts.

**X. Export ticket binding.** A token for ticket B, posted to ticket A's URL, used
to mutate ticket B before the mismatch was noticed. Now the expected ticket id goes
into the service, which compares identity before any storage write, database
mutation or audit row.

No Google account, no Flow tab, no generation, no paid call; media bytes are PNGs
built in-process and every provider is local.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.creative.media import encode_png
from app.flow import protocol as proto
from app.flow.authorization import package_tree_digest
from app.flow.bridge import BridgeConfig, BridgeConfigError
from app.flow.provenance import (
    ExtensionProvenance,
    ProvenanceRejected,
    TrustPolicy,
    verify_provenance,
    verify_release_manifest,
)
from app.flow.source_gate import (
    STATUS_AUTHORIZATION_REVIEW_REQUIRED,
    STATUS_AUTHORIZED,
    STATUS_LICENSE_EVIDENCE_PRESENT,
    STATUS_LICENSING_BLOCKED,
    evaluate_source_gate,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FLOW_API = "/api/v1/flow"
COMPANION_HEADERS = {"X-Companion-Id": "companion-test", "X-Companion-Secret": "c" * 43}
DIGEST = "a" * 64
COMMIT = "0343884"

SUBSTANTIVE_LICENCE = (
    "MIT License\n\n"
    "Permission is hereby granted, free of charge, to any person obtaining a copy "
    "of this software and associated documentation files.\n"
)


# --------------------------------------------------------------------------- L
# L. the licensing gate: presence is evidence, never permission
# --------------------------------------------------------------------------- #
REQUIRED_FILES = (
    "manifest.json",
    "service-worker.js",
    "content-script-main.js",
    "content-script-isolated.js",
    "mcp-server/src/server.js",
    "mcp-server/src/ws-bridge.js",
    "mcp-server/package.json",
)


def _package(tmp_path: Path, *, licence: str | None = None) -> Path:
    root = tmp_path / "pkg"
    for name in REQUIRED_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"// {name}\n", encoding="utf-8")
    (root / "manifest.json").write_text('{"version": "1.2.1"}', encoding="utf-8")
    if licence is not None:
        (root / "LICENSE").write_text(licence, encoding="utf-8")
    return root


def _attestation(root: Path, **overrides: Any) -> dict[str, Any]:
    licence_bytes = (root / "LICENSE").read_bytes()
    document: dict[str, Any] = {
        "schema": "crusor.flow.authorization.v1",
        "upstream_project_url": "https://github.com/example/deepx-flow",
        "release": {"tag": "v1.2.1", "commit": COMMIT},
        "package": {"sha256": package_tree_digest(root)},
        "licence": {
            "name": "MIT",
            "evidence_reference": "LICENSE",
            "evidence_sha256": hashlib.sha256(licence_bytes).hexdigest(),
        },
        "rights": {"modification_permitted": True, "redistribution_permitted": True},
        "review": {
            "reviewer": "Ada Lovelace",
            "reviewed_at": "2026-11-03T18:00:00Z",
            "evidence_reference": "legal-ticket-1234",
        },
    }
    document.update(overrides)
    return document


def _review(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "attestation.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_an_arbitrary_licence_file_does_not_authorize_modification(tmp_path):
    """The Phase 3C.2 defect: any file called LICENSE flipped the gate to AUTHORIZED."""
    root = _package(tmp_path, licence="All rights reserved. Proprietary and confidential.\n")
    gate = evaluate_source_gate(root)
    assert gate.status == STATUS_LICENSE_EVIDENCE_PRESENT
    assert gate.authorized is False
    payload = gate.as_dict()
    assert payload["authorized_to_modify"] is False
    assert payload["can_vendor_source"] is False
    assert payload["upstream_remediation_allowed"] is False
    assert payload["evidence_alone_authorizes"] is False
    assert payload["human_review_required"] is True


@pytest.mark.parametrize("content", ["", "   ", "\n\n\t  \n", "x"])
def test_incomplete_licence_evidence_remains_blocked(tmp_path, content):
    gate = evaluate_source_gate(_package(tmp_path, licence=content))
    assert gate.status == STATUS_LICENSING_BLOCKED
    assert gate.authorized is False
    assert all(not item.substantive for item in gate.evidence)


def test_evidence_present_but_no_human_authorization_is_still_blocked(tmp_path):
    root = _package(tmp_path, licence=SUBSTANTIVE_LICENCE)
    gate = evaluate_source_gate(root)
    assert gate.status == STATUS_LICENSE_EVIDENCE_PRESENT
    assert gate.authorized is False
    assert gate.attestation.present is False


def test_an_authorization_document_inside_the_package_is_not_an_attestation(tmp_path):
    """A permission letter shipped *inside* the thing it permits is not independent."""
    root = _package(tmp_path, licence=SUBSTANTIVE_LICENCE)
    (root / "AUTHORIZATION.md").write_text(
        "We authorize Crusor to modify and redistribute this package.\n", encoding="utf-8"
    )
    gate = evaluate_source_gate(root)
    assert "AUTHORIZATION.md" in gate.authorization_files
    assert gate.authorized is False

    inside = _review(root, _attestation(root))
    gate = evaluate_source_gate(root, attestation_path=str(inside))
    assert gate.status == STATUS_AUTHORIZATION_REVIEW_REQUIRED
    assert "attestation_inside_package" in gate.attestation.problems
    assert gate.authorized is False


def test_a_reviewed_authorization_is_bound_to_the_exact_package_digest(tmp_path):
    root = _package(tmp_path, licence=SUBSTANTIVE_LICENCE)
    digest = package_tree_digest(root)
    attestation = _review(tmp_path, _attestation(root))
    gate = evaluate_source_gate(root, attestation_path=str(attestation))

    assert gate.status == STATUS_AUTHORIZED
    assert gate.authorized is True
    assert gate.package_digest == digest
    assert gate.attestation.bound is True
    assert gate.attestation.package_sha256 == digest
    payload = gate.as_dict()
    assert payload["authorized_to_modify"] is True
    assert payload["attestation"]["reviewer"] == "Ada Lovelace"
    assert payload["attestation"]["review_reference"] == "legal-ticket-1234"
    assert payload["attestation"]["modification_permitted"] is True
    assert payload["attestation"]["redistribution_permitted"] is True
    assert payload["attestation"]["upstream_project_url"].startswith("https://")


def test_changing_the_source_package_invalidates_the_attestation(tmp_path):
    root = _package(tmp_path, licence=SUBSTANTIVE_LICENCE)
    attestation = _review(tmp_path, _attestation(root))
    assert evaluate_source_gate(root, attestation_path=str(attestation)).status == STATUS_AUTHORIZED

    # One byte in one file is enough: the review was of *these* bytes.
    (root / "service-worker.js").write_text("// patched\n", encoding="utf-8")
    gate = evaluate_source_gate(root, attestation_path=str(attestation))
    assert gate.status == STATUS_AUTHORIZATION_REVIEW_REQUIRED
    assert gate.authorized is False
    assert "package_digest_mismatch" in gate.attestation.problems


@pytest.mark.parametrize(
    "mutation,expected_problem",
    [
        ({"rights": {"redistribution_permitted": True}}, "modification_permitted_not_asserted"),
        ({"rights": {"modification_permitted": True}}, "redistribution_permitted_not_asserted"),
        ({"rights": {"modification_permitted": "true", "redistribution_permitted": True}},
         "modification_permitted_not_asserted"),
        ({"review": {"reviewed_at": "2026-11-03T18:00:00Z", "evidence_reference": "t"}},
         "reviewer_missing"),
        ({"review": {"reviewer": "Ada", "evidence_reference": "t"}}, "reviewed_at_missing"),
        ({"review": {"reviewer": "Ada", "reviewed_at": "2026-11-03T18:00:00Z"}},
         "review_evidence_reference_missing"),
        ({"licence": {"name": "MIT", "evidence_reference": ""}},
         "licence_evidence_reference_missing"),
        ({"upstream_project_url": "example.com/repo"}, "upstream_project_url_invalid"),
        ({"release": {}}, "release_identity_missing"),
        ({"schema": "crusor.flow.authorization.v0"}, "attestation_schema_unsupported"),
    ],
)
def test_incomplete_attestations_never_authorize(tmp_path, mutation, expected_problem):
    root = _package(tmp_path, licence=SUBSTANTIVE_LICENCE)
    document = _attestation(root, **mutation)
    if "licence" in mutation:
        document["licence"] = {
            "evidence_reference": "LICENSE",
            "evidence_sha256": hashlib.sha256((root / "LICENSE").read_bytes()).hexdigest(),
            **mutation["licence"],
        }
    attestation = _review(tmp_path, document)
    gate = evaluate_source_gate(root, attestation_path=str(attestation))
    assert gate.authorized is False
    assert gate.status == STATUS_AUTHORIZATION_REVIEW_REQUIRED
    assert expected_problem in gate.attestation.problems


def test_the_attestation_records_the_package_identity_it_describes(tmp_path):
    root = _package(tmp_path, licence=SUBSTANTIVE_LICENCE)
    gate = evaluate_source_gate(root, attestation_path=str(_review(tmp_path, _attestation(root))))
    record = gate.as_dict()["attestation"]
    assert record["package_sha256"] == gate.package_digest
    assert record["observed_package_sha256"] == gate.package_digest
    assert record["release_commit"] == COMMIT
    assert record["release_tag"] == "v1.2.1"
    assert record["licence_name"] == "MIT"
    assert record["reviewed_at"] == "2026-11-03T18:00:00Z"
    # Evidence is described, not interpreted.
    assert gate.as_dict()["package_digest_algorithm"] == "sha256-tree-v1"


# --------------------------------------------------------------------------- P
# P. provenance: trust requires an anchor Crusor already holds
# --------------------------------------------------------------------------- #
def _provenance(**overrides: Any) -> ExtensionProvenance:
    base: dict[str, Any] = {
        "protocol": proto.PROTOCOL_VERSION,
        "extension_id": "abcdefghijklmnopabcdefghijklmnop",
        "extension_version": "1.3.0",
        "service_worker_version": "1.3.0",
        "mcp_version": "1.3.0",
        "source_commit": COMMIT,
        "build_id": "build-2026-11-03",
        "package_sha256": DIGEST,
    }
    base.update(overrides)
    return ExtensionProvenance(**base)


def test_a_self_reported_commit_does_not_establish_trust():
    """The commit is supplied by the party being checked; that is all it is."""
    report = verify_provenance(_provenance(source_commit="deadbee"))
    assert report.claims_well_formed is True
    assert report.artifact_integrity_verified is False
    assert report.trusted is False
    assert report.trust_state == "UNVERIFIED"


def test_a_self_reported_package_hash_does_not_establish_trust():
    """A digest with nothing to compare it against is a claim, not a measurement."""
    report = verify_provenance(_provenance(package_sha256="b" * 64))
    assert report.anchor.present is False
    assert report.artifact_integrity_verified is False
    assert report.trusted is False
    assert any("artifact_integrity_unverified" in w for w in report.warnings)


def test_a_missing_digest_refuses_trusted_status_even_when_one_is_pinned():
    policy = TrustPolicy(trusted_artifact_sha256=DIGEST)
    report = verify_provenance(_provenance(package_sha256=""), policy=policy)
    assert report.artifact_integrity_verified is False
    assert report.trusted is False
    assert any("artifact_digest_missing" in w for w in report.warnings)


def test_a_digest_mismatch_is_not_trusted():
    policy = TrustPolicy(trusted_artifact_sha256="c" * 64)
    report = verify_provenance(_provenance(), policy=policy)
    assert report.artifact_integrity_verified is False
    assert report.trusted is False
    assert any("artifact_digest_mismatch" in w for w in report.warnings)


def test_a_pinned_digest_and_release_verify_the_package():
    policy = TrustPolicy(
        trusted_artifact_sha256=DIGEST, trusted_release="1.3.0", trusted_source_commit=COMMIT
    )
    report = verify_provenance(_provenance(), policy=policy)
    assert report.trusted is True
    assert report.trust_state == "VERIFIED"
    assert report.anchor.source == "pinned_digest"
    assert "artifact_digest_matches_trusted_anchor" in report.checks


def test_a_release_other_than_the_supported_one_is_refused():
    policy = TrustPolicy(trusted_artifact_sha256=DIGEST, trusted_release="1.3.1")
    with pytest.raises(ProvenanceRejected) as excinfo:
        verify_provenance(_provenance(), policy=policy)
    assert "not the release this build supports" in str(excinfo.value)

    policy = TrustPolicy(trusted_artifact_sha256=DIGEST, trusted_source_commit="fffffff")
    with pytest.raises(ProvenanceRejected):
        verify_provenance(_provenance(), policy=policy)


def test_component_drift_is_never_trusted_even_when_the_operator_allows_it():
    policy = TrustPolicy(trusted_artifact_sha256=DIGEST, allow_component_drift=True)
    report = verify_provenance(_provenance(mcp_version="1.0.0"), policy=policy)
    assert report.components_consistent is False
    assert report.artifact_integrity_verified is True
    # Two out of three is not trust.
    assert report.trusted is False
    assert report.component_drift_accepted is True


def test_the_drift_flag_cannot_bypass_artifact_verification():
    policy = TrustPolicy(trusted_artifact_sha256="d" * 64, allow_component_drift=True)
    report = verify_provenance(_provenance(mcp_version="1.0.0"), policy=policy)
    assert report.artifact_integrity_verified is False
    assert report.trusted is False


def _signed_manifest(tmp_path: Path, *, artifact: str = DIGEST, tamper: bool = False):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    public_b64 = base64.b64encode(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode()

    body = {
        "schema": "crusor.flow.release.v1",
        "release": {"version": "1.3.0", "commit": COMMIT},
        "artifact": {"sha256": artifact},
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    signature = key.sign(canonical)
    document = {**body, "signature": base64.b64encode(signature).decode()}
    if tamper:
        document["artifact"] = {"sha256": "e" * 64}  # signature no longer matches
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "release-manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path, public_b64


def test_a_verified_release_manifest_produces_a_trusted_report(tmp_path):
    path, public_b64 = _signed_manifest(tmp_path)
    policy = TrustPolicy(release_manifest_path=str(path), release_manifest_public_key=public_b64)
    report = verify_provenance(_provenance(), policy=policy)
    assert report.trusted is True
    assert report.anchor.source == "signed_release_manifest"
    assert report.anchor.signature_verified is True
    assert report.anchor.artifact_sha256 == DIGEST


def test_an_invalid_signature_is_rejected(tmp_path):
    path, public_b64 = _signed_manifest(tmp_path, tamper=True)
    with pytest.raises(ProvenanceRejected) as excinfo:
        verify_release_manifest(path, public_b64)
    assert "signature" in str(excinfo.value).lower()

    # A different key cannot vouch for this manifest either.
    _, other_public = _signed_manifest(tmp_path / "other")
    with pytest.raises(ProvenanceRejected):
        verify_release_manifest(path, other_public)


def test_an_unsigned_or_malformed_manifest_is_rejected(tmp_path):
    unsigned = tmp_path / "unsigned.json"
    unsigned.write_text(
        json.dumps(
            {
                "schema": "crusor.flow.release.v1",
                "release": {"version": "1.3.0", "commit": COMMIT},
                "artifact": {"sha256": DIGEST},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ProvenanceRejected):
        verify_release_manifest(unsigned, "M" * 44)

    with pytest.raises(ProvenanceRejected):
        verify_release_manifest(tmp_path / "missing.json", "M" * 44)

    # A manifest whose artifact digest does not match the installed package is not
    # trusted either: the manifest is an anchor, not a rubber stamp.
    path, public_b64 = _signed_manifest(tmp_path, artifact="f" * 64)
    report = verify_provenance(
        _provenance(),
        policy=TrustPolicy(release_manifest_path=str(path), release_manifest_public_key=public_b64),
    )
    assert report.trusted is False
    assert report.artifact_integrity_verified is False


def test_live_generation_cannot_be_enabled_without_a_trust_anchor():
    """Spending money requires knowing which bytes are spending it."""
    config = BridgeConfig(
        pairing_id="pair-1",
        pairing_secret="s" * 43,
        live_generation_enabled=True,
    )
    with pytest.raises(BridgeConfigError) as excinfo:
        config.validate()
    assert "trusted artifact anchor" in str(excinfo.value)

    anchored = BridgeConfig(
        pairing_id="pair-1",
        pairing_secret="s" * 43,
        live_generation_enabled=True,
        trusted_artifact_sha256=DIGEST,
    )
    assert anchored.validate().trust_policy.has_anchor is True


# --------------------------------------------------------------------------- X
# X. export tickets: identity is checked inside the service, before any mutation
# --------------------------------------------------------------------------- #
def _png(width: int = 20, height: int = 24, shade: int = 70) -> bytes:
    return encode_png(width, height, bytes([shade, shade, shade]) * (width * height))


def _split(data: bytes, size: int) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


@pytest.fixture
def companion_settings(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    from app.core.config import get_settings

    monkeypatch.setenv("FLOW_COMPANION_ID", "companion-test")
    monkeypatch.setenv("FLOW_COMPANION_SECRET", "c" * 43)
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path / "storage"))
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def _storage_dir_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()] if root.exists() else []


async def test_a_token_for_another_ticket_cannot_mutate_this_one(
    session, tmp_path, companion_settings
):
    from app.models.creative import CreativeAsset
    from app.services.flow_gateway import FlowAssetExportService
    from app.storage.base import LocalFilesystemStorage

    storage = LocalFilesystemStorage(tmp_path / "assets")
    service = FlowAssetExportService(session, companion_settings, storage)
    ticket_a, _token_a = await service.create_ticket(
        companion_id="companion-test", filename="a.png"
    )
    ticket_b, token_b = await service.create_ticket(
        companion_id="companion-test", filename="b.png"
    )
    chunk = _png()

    with pytest.raises(Exception) as excinfo:
        await service.accept_chunk(
            token=token_b,
            index=0,
            total=1,
            chunk_sha256=hashlib.sha256(chunk).hexdigest(),
            data=chunk,
            companion_id="companion-test",
            expected_ticket_id=ticket_a.id,
        )
    assert "does not belong" in str(excinfo.value)

    # No database mutation on either ticket, no staging object, no asset.
    await session.flush()
    assert ticket_a.status == "pending"
    assert ticket_a.chunks_received == 0
    assert ticket_a.received_bytes == 0
    assert ticket_a.upload_mode == "single"
    assert not ticket_a.chunk_manifest
    assert ticket_b.status == "pending"
    assert ticket_b.chunks_received == 0
    assert not ticket_b.chunk_manifest
    assert await storage.exists(f"flow/staging/{ticket_a.id}/000000.part") is False
    assert await storage.exists(f"flow/staging/{ticket_b.id}/000000.part") is False
    assert _storage_dir_files(tmp_path / "assets") == []
    assert await session.scalar(
        __import__("sqlalchemy").select(__import__("sqlalchemy").func.count()).select_from(CreativeAsset)
    ) == 0

    # The rejection leaves the transaction usable: roll back and nothing survives.
    await session.rollback()


async def test_a_matching_token_still_uploads_in_order(session, tmp_path, companion_settings):
    from app.services.flow_gateway import FlowAssetExportService
    from app.storage.base import LocalFilesystemStorage

    storage = LocalFilesystemStorage(tmp_path / "assets")
    service = FlowAssetExportService(session, companion_settings, storage)
    media = _png(24, 28)
    checksum = hashlib.sha256(media).hexdigest()
    ticket, token = await service.create_ticket(
        companion_id="companion-test",
        expected_sha256=checksum,
        content_type="image/png",
        filename="bound.png",
    )
    chunks = _split(media, 200)
    for index, chunk in enumerate(chunks):
        ticket, asset = await service.accept_chunk(
            token=token,
            index=index,
            total=len(chunks),
            chunk_sha256=hashlib.sha256(chunk).hexdigest(),
            data=chunk,
            companion_id="companion-test",
            expected_ticket_id=ticket.id,
        )
    assert asset is not None
    assert ticket.status == "consumed"
    assert ticket.checksum == checksum
    assert await storage.get(asset.storage_key) == media


async def test_the_http_route_rejects_a_cross_ticket_token_without_mutation(
    client, auth, session, users, brand_row, companion_settings, tmp_path
):
    from sqlalchemy import func, select

    from app.models.creative import CreativeAsset

    def _create(name: str) -> dict[str, Any]:
        return {"companion_id": "companion-test", "filename": name}

    first = await client.post(f"{FLOW_API}/exports", json=_create("one.png"), headers=auth("marketer"))
    second = await client.post(f"{FLOW_API}/exports", json=_create("two.png"), headers=auth("marketer"))
    ticket_a = first.json()
    ticket_b = second.json()
    chunk = _png()

    response = await client.post(
        f"{FLOW_API}/exports/{ticket_a['id']}/chunks",
        content=chunk,
        headers={
            **COMPANION_HEADERS,
            "X-Export-Token": ticket_b["upload_token"],
            "X-Chunk-Index": "0",
            "X-Chunk-Count": "1",
            "X-Chunk-Sha256": hashlib.sha256(chunk).hexdigest(),
        },
    )
    assert response.status_code == 422, response.text
    assert "does not belong" in response.json()["error"]["message"]

    # Neither ticket moved, and no asset appeared.
    for ticket in (ticket_a, ticket_b):
        stored = await client.get(f"{FLOW_API}/exports/{ticket['id']}", headers=auth())
        body = stored.json()
        assert body["status"] == "pending"
        assert body["chunks_received"] == 0
        assert body["asset_id"] is None

    count = await session.scalar(select(func.count()).select_from(CreativeAsset))
    assert count == 0

    # The correct pairing still works over HTTP, so the check is identity, not luck.
    ok = await client.post(
        f"{FLOW_API}/exports/{ticket_a['id']}/chunks",
        content=chunk,
        headers={
            **COMPANION_HEADERS,
            "X-Export-Token": ticket_a["upload_token"],
            "X-Chunk-Index": "0",
            "X-Chunk-Count": "1",
            "X-Chunk-Sha256": hashlib.sha256(chunk).hexdigest(),
        },
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "consumed"
    assert ok.json()["asset_id"]


async def test_a_foreign_companion_still_cannot_use_a_ticket(client, auth, companion_settings):
    created = await client.post(
        f"{FLOW_API}/exports",
        json={"companion_id": "companion-test", "filename": "x.png"},
        headers=auth("marketer"),
    )
    ticket = created.json()
    chunk = _png()
    response = await client.post(
        f"{FLOW_API}/exports/{ticket['id']}/chunks",
        content=chunk,
        headers={
            "X-Companion-Id": "companion-other",
            "X-Companion-Secret": "c" * 43,
            "X-Export-Token": ticket["upload_token"],
            "X-Chunk-Index": "0",
            "X-Chunk-Count": "1",
            "X-Chunk-Sha256": hashlib.sha256(chunk).hexdigest(),
        },
    )
    assert response.status_code == 401, response.text
