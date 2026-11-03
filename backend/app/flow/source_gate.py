"""Upstream source and licensing gate (Phase 3C.2 §2).

Phase 3C.2 asks for U1–U7 to be fixed *in the extension*. Before any line of
third-party code may be modified, copied, or forked, four things must exist:

1. identifiable upstream source;
2. an exact version or commit;
3. a licence file, or explicit written permission;
4. permission to modify **and** to use the result in this integration.

This module answers that question mechanically, from the filesystem, so the answer
cannot drift from reality in a document. It never copies, moves, or reads source
into this repository: it reports presence, licence evidence, and a status.

Three outcomes:

``SOURCE_ABSENT``
    No package at the configured path. Everything upstream is blocked.
``LICENSING_BLOCKED``
    A package exists but carries no licence and no authorisation file. Crusor must
    not modify or redistribute it, so the remediation work stays blocked.
``AUTHORIZED``
    A package with licence evidence exists. The audit tool may run its scans and a
    human may begin the U1–U7 remediation on that copy.

Default configuration is ``SOURCE_ABSENT``: no path, no licence, no permission.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.flow.provenance import LICENSE_EVIDENCE, REQUIRED_UPSTREAM_FILES

STATUS_SOURCE_ABSENT = "SOURCE_ABSENT"
STATUS_LICENSING_BLOCKED = "LICENSING_BLOCKED"
STATUS_AUTHORIZED = "AUTHORIZED"


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

    @property
    def authorized(self) -> bool:
        return self.status == STATUS_AUTHORIZED

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "package_path": self.package_path,
            "source_present": self.present,
            "license_files": list(self.license_files),
            "authorization_files": list(self.authorization_files),
            "missing_files": list(self.missing_files),
            "authorized_to_modify": self.authorized,
            "can_vendor_source": self.authorized,
            "upstream_remediation_allowed": self.authorized,
            "reason": self.reason,
            "required_files": list(REQUIRED_UPSTREAM_FILES),
            "license_evidence_searched": list(LICENSE_EVIDENCE),
            "remediation_requirements": list(self.remediation),
        }


def evaluate_source_gate(package_path: str | Path | None = None) -> SourceGate:
    """Decide, from the filesystem alone, whether upstream work is permitted."""
    remediation = tuple(_remediation_requirements())
    configured = str(package_path or os.getenv("FLOW_SOURCE_PACKAGE_PATH", "") or "").strip()

    if not configured:
        return SourceGate(
            status=STATUS_SOURCE_ABSENT,
            package_path="",
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

    licences = tuple(name for name in LICENSE_EVIDENCE if (root / name).is_file())
    missing = tuple(name for name in REQUIRED_UPSTREAM_FILES if not (root / name).is_file())
    if not licences:
        return SourceGate(
            status=STATUS_LICENSING_BLOCKED,
            package_path=str(root),
            present=True,
            missing_files=missing,
            reason=(
                "A package is present but carries no licence and no authorisation "
                "document, so Crusor may not modify, fork, or redistribute it. Obtain a "
                "licence or explicit written permission before touching the source."
            ),
            remediation=remediation,
        )

    return SourceGate(
        status=STATUS_AUTHORIZED,
        package_path=str(root),
        present=True,
        license_files=licences,
        missing_files=missing,
        reason="Licence evidence found; upstream remediation is permitted on this copy.",
        remediation=remediation,
    )


def source_gate_payload(package_path: str | Path | None = None) -> dict[str, Any]:
    return evaluate_source_gate(package_path).as_dict()
