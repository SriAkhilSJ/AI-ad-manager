"""Upstream source and licensing gate (Phase 3C.2 §2, corrected in 3C.2.1).

The gate answers one question — *may Crusor modify and redistribute this
upstream package?* — and it must never answer "yes" because a file with a
particular name exists. Phase 3C.2 did exactly that: any file called ``LICENSE``,
including an empty one, set ``AUTHORIZED``.

Phase 3C.2.1 splits the answer into five states so that "there is licence-shaped
text here" and "a named human reviewed this package and concluded we may modify
it" can never be confused again:

``SOURCE_ABSENT``
    No package at the configured path. Nothing upstream can be done at all.
``LICENSING_BLOCKED``
    A package exists but contains no substantive licence or permission document
    (empty and whitespace-only files are not evidence). Cannot proceed.
``LICENSE_EVIDENCE_PRESENT``
    Substantive licence-shaped documents exist, but no human attestation has been
    supplied. Presence of a document is evidence *for a reviewer*, not permission.
``AUTHORIZATION_REVIEW_REQUIRED``
    An attestation exists but is incomplete, internally inconsistent, stored
    inside the package it describes, or bound to a different package digest.
``AUTHORIZED``
    A complete attestation, written by a named reviewer, whose recorded package
    digest equals the digest of the package actually present.

Only ``AUTHORIZED`` sets ``authorized_to_modify`` / ``can_vendor_source`` /
``upstream_remediation_allowed``. Every other state leaves all three ``False``.

Nothing here interprets a licence. The module records what evidence exists
(name, size, SHA-256), computes the package's tree digest, and checks that a
human's attestation is complete and refers to *these* bytes. See
:mod:`app.flow.authorization`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.flow.authorization import (
    AuthorizationAttestation,
    EvidenceFile,
    load_attestation,
    package_tree_digest,
    scan_evidence,
)
from app.flow.provenance import LICENSE_EVIDENCE, REQUIRED_UPSTREAM_FILES

STATUS_SOURCE_ABSENT = "SOURCE_ABSENT"
STATUS_LICENSING_BLOCKED = "LICENSING_BLOCKED"
STATUS_LICENSE_EVIDENCE_PRESENT = "LICENSE_EVIDENCE_PRESENT"
STATUS_AUTHORIZATION_REVIEW_REQUIRED = "AUTHORIZATION_REVIEW_REQUIRED"
STATUS_AUTHORIZED = "AUTHORIZED"

#: States in which the scanner may read the package. Reading is not modifying;
#: findings are what a reviewer needs in order to decide.
READABLE_STATES = (
    STATUS_LICENSE_EVIDENCE_PRESENT,
    STATUS_AUTHORIZATION_REVIEW_REQUIRED,
    STATUS_AUTHORIZED,
)

#: States in which third-party source may be edited, forked or vendored.
AUTHORIZING_STATES = (STATUS_AUTHORIZED,)


def _remediation_requirements() -> list[dict[str, str]]:
    """The seven upstream changes, with the file each one lives in.

    Kept next to the gate so the blocked report and the status payload always list
    the same seven items, in the same order as the original audit.
    """
    return [
        {
            "blocker": "U1",
            "file": "content-script-main.js",
            "requirement": (
                "Remove the wildcard token broadcast and the page-accessible "
                "get_token / get_flow_context handlers; keep bearer material inside "
                "the extension's isolated world."
            ),
        },
        {
            "blocker": "U2",
            "file": "content-script-main.js",
            "requirement": (
                "Replace api_post(url, body, token) with fixed allowlisted Flow "
                "endpoints; validate method, schema and destination before attaching "
                "authorization; reject encoded, redirected and alternate origins."
            ),
        },
        {
            "blocker": "U3",
            "file": "mcp-server/src/ws-bridge.js + service-worker.js",
            "requirement": (
                "Speak flow.bridge.v1 to the companion instead of running a second "
                "unauthenticated bridge on 8792: authenticated HELLO, Host/Origin "
                "checks, size and timeout limits, no CORS."
            ),
        },
        {
            "blocker": "U4",
            "file": "mcp-server/src/server.js + service-worker.js",
            "requirement": (
                "Ship a restricted tool surface (status, generate_image, "
                "generate_video, check_video_status, export_asset) and remove "
                "eval_script, native_*, batch/rapid/autonomous tooling."
            ),
        },
        {
            "blocker": "U5",
            "file": "manifest.json",
            "requirement": (
                "Replace http://*/* and https://*/* with the Flow/Labs origins "
                "actually used, and justify each remaining permission."
            ),
        },
        {
            "blocker": "U6",
            "file": "service-worker.js",
            "requirement": (
                "Remove the post-submit retry; create one request id before dispatch, "
                "report UNKNOWN_SUBMISSION on a lost response, and never resubmit."
            ),
        },
        {
            "blocker": "U7",
            "file": "service-worker.js",
            "requirement": (
                "Implement export_asset: return media bytes (bounded chunks) for a "
                "correlated job instead of a Chrome download id."
            ),
        },
    ]


@dataclass(frozen=True, slots=True)
class SourceGate:
    status: str
    package_path: str
    present: bool
    license_files: tuple[str, ...] = ()
    authorization_files: tuple[str, ...] = ()
    missing_files: tuple[str, ...] = ()
    reason: str = ""
    remediation: tuple[dict[str, str], ...] = field(default_factory=tuple)
    package_digest: str = ""
    evidence: tuple[EvidenceFile, ...] = ()
    attestation: AuthorizationAttestation = field(default_factory=AuthorizationAttestation)

    @property
    def authorized(self) -> bool:
        return self.status in AUTHORIZING_STATES

    @property
    def readable(self) -> bool:
        return self.status in READABLE_STATES

    def as_dict(self) -> dict[str, Any]:
        substantive = [item for item in self.evidence if item.substantive]
        return {
            "status": self.status,
            "package_path": self.package_path,
            "source_present": self.present,
            "package_digest_algorithm": "sha256-tree-v1",
            "package_digest": self.package_digest,
            "license_files": list(self.license_files),
            "authorization_files": list(self.authorization_files),
            "license_evidence": [item.as_dict() for item in self.evidence],
            "substantive_license_evidence": [item.name for item in substantive],
            "missing_files": list(self.missing_files),
            "authorized_to_modify": self.authorized,
            "can_vendor_source": self.authorized,
            "upstream_remediation_allowed": self.authorized,
            "scan_permitted": self.readable,
            "evidence_alone_authorizes": False,
            "human_review_required": self.status != STATUS_AUTHORIZED,
            "attestation": self.attestation.as_dict(),
            "reason": self.reason,
            "required_files": list(REQUIRED_UPSTREAM_FILES),
            "license_evidence_searched": list(LICENSE_EVIDENCE),
            "authorization_requirements": [
                "an attestation stored outside the package",
                "upstream project URL and exact release tag or commit",
                "the package tree digest the reviewer reviewed",
                "the licence name, evidence file and that file's SHA-256",
                "explicit modification and redistribution permission",
                "reviewer identity, review timestamp and evidence reference",
            ],
            "remediation_requirements": list(self.remediation),
        }


def _blocked(
    status: str, *, path: str, present: bool, reason: str, remediation: tuple[dict[str, str], ...]
) -> SourceGate:
    return SourceGate(
        status=status,
        package_path=path,
        present=present,
        reason=reason,
        remediation=remediation,
    )


def evaluate_source_gate(
    package_path: str | Path | None = None,
    *,
    attestation_path: str | Path | None = None,
) -> SourceGate:
    """Decide, from the filesystem alone, whether upstream work is permitted."""
    remediation = tuple(_remediation_requirements())
    configured = str(package_path or os.getenv("FLOW_SOURCE_PACKAGE_PATH", "") or "").strip()

    if not configured:
        return _blocked(
            STATUS_SOURCE_ABSENT,
            path="",
            present=False,
            reason=(
                "No upstream package is configured or present. The DeepX Flow Connector "
                "source archive is not supplied to this environment, so U1-U7 cannot be "
                "implemented here and no source may be copied into this repository."
            ),
            remediation=remediation,
        )

    root = Path(configured).expanduser()
    if not root.exists() or not root.is_dir():
        return SourceGate(
            status=STATUS_SOURCE_ABSENT,
            package_path=configured,
            present=False,
            missing_files=tuple(REQUIRED_UPSTREAM_FILES),
            reason=f"The configured package path {configured!r} does not exist.",
            remediation=remediation,
        )

    evidence = scan_evidence(root, LICENSE_EVIDENCE)
    substantive = tuple(item for item in evidence if item.substantive)
    missing = tuple(name for name in REQUIRED_UPSTREAM_FILES if not (root / name).is_file())
    digest = package_tree_digest(root)
    licences = tuple(item.name for item in evidence)

    if not substantive:
        detail = (
            "the licence files present are empty or whitespace-only"
            if evidence
            else "no licence and no authorisation document"
        )
        return SourceGate(
            status=STATUS_LICENSING_BLOCKED,
            package_path=str(root),
            present=True,
            license_files=licences,
            missing_files=missing,
            package_digest=digest,
            evidence=evidence,
            reason=(
                f"A package is present but {detail}, so Crusor may not modify, fork or "
                "redistribute it. File presence alone never authorizes anything; supply "
                "a licence or written permission and a human-reviewed attestation."
            ),
            remediation=remediation,
        )

    attestation = load_attestation(
        attestation_path or os.getenv("FLOW_AUTHORIZATION_ATTESTATION_PATH", ""),
        package_root=root,
        observed_digest=digest,
        evidence=evidence,
    )
    authorization_files = tuple(
        name for name in ("AUTHORIZATION.md", "PERMISSION.md") if (root / name).is_file()
    )

    if not attestation.present:
        return SourceGate(
            status=STATUS_LICENSE_EVIDENCE_PRESENT,
            package_path=str(root),
            present=True,
            license_files=licences,
            authorization_files=authorization_files,
            missing_files=missing,
            package_digest=digest,
            evidence=evidence,
            attestation=attestation,
            reason=(
                "Licence-shaped documents exist in the package, but no human-reviewed "
                "authorization attestation was supplied. Evidence is not permission: "
                "set FLOW_AUTHORIZATION_ATTESTATION_PATH to a reviewed attestation "
                "stored outside the package."
            ),
            remediation=remediation,
        )

    if not attestation.authorized:
        problems = ", ".join(attestation.problems) or "attestation incomplete"
        return SourceGate(
            status=STATUS_AUTHORIZATION_REVIEW_REQUIRED,
            package_path=str(root),
            present=True,
            license_files=licences,
            authorization_files=authorization_files,
            missing_files=missing,
            package_digest=digest,
            evidence=evidence,
            attestation=attestation,
            reason=(
                f"An authorization attestation is present but does not authorize this "
                f"package ({problems}). A reviewer must confirm the rights, the exact "
                f"release and the package digest {digest[:16]}... before upstream source "
                "may be touched."
            ),
            remediation=remediation,
        )

    return SourceGate(
        status=STATUS_AUTHORIZED,
        package_path=str(root),
        present=True,
        license_files=licences,
        authorization_files=authorization_files,
        missing_files=missing,
        package_digest=digest,
        evidence=evidence,
        attestation=attestation,
        reason=(
            "A human-reviewed attestation is bound to this package's digest and records "
            f"permission to modify and redistribute ({attestation.licence_name}); "
            f"reviewed by {attestation.reviewer} at {attestation.reviewed_at}."
        ),
        remediation=remediation,
    )


def source_gate_payload(
    package_path: str | Path | None = None,
    *,
    attestation_path: str | Path | None = None,
) -> dict[str, Any]:
    return evaluate_source_gate(package_path, attestation_path=attestation_path).as_dict()
