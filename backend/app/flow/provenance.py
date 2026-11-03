"""Extension provenance and protocol negotiation (Phase 3C.2 §10).

The audit found four components of the same package disagreeing about their own
version (manifest ``1.2.1``, README ``1.2.0``, service-worker HELLO ``1.2.0``,
MCP server ``1.0.0``). That is not a cosmetic problem: a canary cannot prove which
code it exercised, and a "compatible" claim cannot be trusted when the component
making it is not the component that will run.

So the bridge does not take a version on faith. Before a single command is
exchanged, the extension's HELLO must carry:

* the protocol it speaks — and this build supports exactly one;
* the version of **every** component it ships;
* the source commit and the build identifier the package was produced from, so a
  release can be tied to a reproducible artifact;
* optionally the package digest, for byte-level verification.

Anything missing, unsupported, or internally inconsistent is refused at the
handshake. Crusor cannot compel a third-party build to be honest, but it can
decline to run one that is not self-consistent, and it can refuse to accept a
version string as proof that the *rest* of the package matches.

Nothing here is derived from the extension's source: the field names and the
rules are Crusor's own protocol requirement (see :data:`UPSTREAM_BLOCKERS` U3/U8).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from app.flow import protocol as proto

#: ``1.2.1``, ``1.2.1-beta.2``, ``1.2.1+build.7``.
SEMVER = re.compile(r"^(?P<major>\d{1,4})\.(?P<minor>\d{1,4})\.(?P<patch>\d{1,4})(?:[-+][0-9A-Za-z.\-]{1,32})?$")
#: Short or full git SHA.
SOURCE_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
#: A build identifier: hex, dashes, dots, or an ISO-ish timestamp.
BUILD_ID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._\-]{5,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ProvenanceRejected(proto.FlowProtocolError):
    """The HELLO did not carry provable, self-consistent provenance."""

    code = "flow_provenance_rejected"


class UnsupportedProtocol(ProvenanceRejected):
    """The extension speaks a protocol this build does not implement."""

    code = "flow_unsupported_protocol"


@dataclass(frozen=True, slots=True)
class ExtensionProvenance:
    """What the extension says about itself — verified, not believed."""

    protocol: str
    extension_id: str
    extension_version: str
    service_worker_version: str
    mcp_version: str
    source_commit: str
    build_id: str
    package_sha256: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "protocol": self.protocol,
            "extension_id": self.extension_id,
            "extension_version": self.extension_version,
            "service_worker_version": self.service_worker_version,
            "mcp_version": self.mcp_version,
            "source_commit": self.source_commit,
            "build_id": self.build_id,
            "package_sha256": self.package_sha256,
        }

    def fingerprint(self) -> str:
        """Stable digest of the declared provenance (safe to log and to store)."""
        canonical = "|".join(
            [
                self.protocol,
                self.extension_id,
                self.extension_version,
                self.service_worker_version,
                self.mcp_version,
                self.source_commit,
                self.build_id,
                self.package_sha256,
            ]
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def release_line(self) -> str:
        match = SEMVER.match(self.extension_version)
        return f"{match.group('major')}.{match.group('minor')}" if match else ""


@dataclass(frozen=True, slots=True)
class ProvenanceReport:
    provenance: ExtensionProvenance
    warnings: tuple[str, ...] = field(default_factory=tuple)
    checks: tuple[str, ...] = field(default_factory=tuple)

    @property
    def trusted(self) -> bool:
        return not self.warnings

    def as_dict(self) -> dict[str, Any]:
        return {
            "provenance": self.provenance.as_dict(),
            "fingerprint": self.provenance.fingerprint(),
            "release_line": self.provenance.release_line,
            "trusted": self.trusted,
            "warnings": list(self.warnings),
            "checks": list(self.checks),
        }


def _require(condition: bool, reason: str, *, details: dict[str, Any] | None = None) -> None:
    if not condition:
        raise ProvenanceRejected(reason, details=details or {})


def provenance_from_hello(hello: Any) -> ExtensionProvenance:
    """Extract provenance from a validated :class:`proto.HelloMessage`."""
    return ExtensionProvenance(
        protocol=str(getattr(hello, "protocol", "") or ""),
        extension_id=str(getattr(hello, "extension_id", "") or ""),
        extension_version=str(getattr(hello, "extension_version", "") or ""),
        service_worker_version=str(getattr(hello, "service_worker_version", "") or ""),
        mcp_version=str(getattr(hello, "mcp_version", "") or ""),
        source_commit=str(getattr(hello, "source_commit", "") or "").lower(),
        build_id=str(getattr(hello, "build_id", "") or ""),
        package_sha256=str(getattr(hello, "package_sha256", "") or "").lower(),
    )


def verify_provenance(
    provenance: ExtensionProvenance,
    *,
    supported_protocols: tuple[str, ...] = proto.SUPPORTED_PROTOCOLS,
    allow_component_drift: bool = False,
) -> ProvenanceReport:
    """Refuse anything a canary could not prove, and warn where proof is partial.

    Hard failures (handshake refused):

    * an unsupported protocol;
    * a missing or malformed component version, commit, or build id;
    * ``extension_version != service_worker_version`` — the same package cannot
      legitimately ship two versions of the same component;
    * a malformed package digest when a digest is supplied at all.

    Soft failures (``trusted == False``, and refused unless the operator opts in
    with ``allow_component_drift``):

    * the MCP server is on a different ``major.minor`` release line to the
      extension — exactly the ``1.2.1`` vs ``1.0.0`` split the audit found.
    """
    checks: list[str] = []

    if provenance.protocol not in supported_protocols:
        raise UnsupportedProtocol(
            f"The extension protocol {provenance.protocol!r} is not supported by this "
            f"bridge (supported: {', '.join(supported_protocols)}).",
            details={
                "presented": provenance.protocol,
                "supported": list(supported_protocols),
            },
        )
    checks.append("protocol_supported")

    _require(bool(provenance.extension_id), "The HELLO did not identify the extension.")
    checks.append("extension_identified")

    for name in ("extension_version", "service_worker_version", "mcp_version"):
        value = getattr(provenance, name)
        _require(
            bool(SEMVER.match(value)),
            f"The HELLO carried a missing or malformed {name} ({value!r}).",
            details={"field": name},
        )
    checks.append("component_versions_well_formed")

    _require(
        bool(SOURCE_COMMIT.match(provenance.source_commit)),
        "The HELLO did not carry a source commit, so the build cannot be tied to a "
        "release.",
        details={"field": "source_commit"},
    )
    checks.append("source_commit_present")

    _require(
        bool(BUILD_ID.match(provenance.build_id)),
        "The HELLO did not carry a build identifier.",
        details={"field": "build_id"},
    )
    checks.append("build_id_present")

    if provenance.package_sha256:
        _require(
            bool(SHA256.match(provenance.package_sha256)),
            "The package digest was not a SHA-256.",
            details={"field": "package_sha256"},
        )
        checks.append("package_digest_well_formed")

    _require(
        provenance.extension_version == provenance.service_worker_version,
        "The extension and its service worker report different versions "
        f"({provenance.extension_version} vs {provenance.service_worker_version}). "
        "A single package cannot ship two versions of the same component.",
        details={
            "extension_version": provenance.extension_version,
            "service_worker_version": provenance.service_worker_version,
        },
    )
    checks.append("extension_matches_service_worker")

    warnings: list[str] = []
    extension_line = provenance.release_line
    mcp_match = SEMVER.match(provenance.mcp_version)
    if mcp_match:
        mcp_line = f"{mcp_match.group('major')}.{mcp_match.group('minor')}"
        if extension_line and mcp_line != extension_line:
            warnings.append(
                "mcp_version_release_line_mismatch: the MCP server is on "
                f"{mcp_line} while the extension is on {extension_line}"
            )
    if warnings and not allow_component_drift:
        raise ProvenanceRejected(
            "The extension ships components from different release lines, so the "
            "package cannot be reproduced from one build: " + "; ".join(warnings),
            details={"warnings": warnings},
        )

    return ProvenanceReport(provenance=provenance, warnings=tuple(warnings), checks=tuple(checks))


#: Files that must exist in an upstream package before Crusor may touch it. Kept
#: here (not in a docstring) so the audit tool and the status payload agree.
REQUIRED_UPSTREAM_FILES: tuple[str, ...] = (
    "manifest.json",
    "service-worker.js",
    "content-script-main.js",
    "content-script-isolated.js",
    "mcp-server/src/server.js",
    "mcp-server/src/ws-bridge.js",
    "mcp-server/package.json",
)

#: The evidence that would authorise modifying that package. Any one of these is
#: sufficient; none of them is present.
LICENSE_EVIDENCE: tuple[str, ...] = (
    "LICENSE",
    "LICENSE.md",
    "LICENSE.txt",
    "COPYING",
    "AUTHORIZATION.md",
    "PERMISSION.md",
)
