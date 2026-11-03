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

Phase 3C.2.1 — trust is not the same question as well-formedness
----------------------------------------------------------------

Phase 3C.2 reported ``trusted`` whenever a HELLO carried well-formed version
strings, a commit and a build id. That is a *syntactic* check: every one of those
fields is supplied by the party being checked, so a HELLO could be perfectly
consistent and entirely fabricated. The report now separates four independent
facts, and only their conjunction is trust:

``claims_well_formed``
    The HELLO parses, every component reports a semver, and a commit and build id
    are present. Necessary, never sufficient.
``components_consistent``
    The package does not disagree with itself (extension == service worker; MCP
    on the same release line unless an operator explicitly accepts drift).
``artifact_integrity_verified``
    The bytes actually installed match a digest *Crusor already trusted* — a
    pinned digest, or a release manifest whose Ed25519 signature verifies against
    a public key held outside the extension. A digest the extension reports about
    itself proves nothing and is never used as its own anchor.
``provenance_trusted``
    All of the above. Absent independent verification the report says
    ``UNVERIFIED`` rather than softening the word "trusted".
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
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


TRUST_STATE_VERIFIED = "VERIFIED"
TRUST_STATE_UNVERIFIED = "UNVERIFIED"

#: The signed release manifest a distributor may publish out of band.
RELEASE_MANIFEST_SCHEMA = "crusor.flow.release.v2"
#: Phase 3C.2.1 manifests. Still readable, but they describe a single artifact and
#: therefore cannot establish the identity of every executable component.
RELEASE_MANIFEST_SCHEMA_V1 = "crusor.flow.release.v1"
SUPPORTED_MANIFEST_SCHEMAS = (RELEASE_MANIFEST_SCHEMA, RELEASE_MANIFEST_SCHEMA_V1)

#: Digest profiles a signed release may name for component identity. A runtime
#: measurement must never be compared against a source-licensing digest.
RUNTIME_DIGEST_ALGORITHMS = ("sha256-tree-runtime-v1", "sha256-tree-v1")

#: Executable components a signed release may identify individually.
COMPONENT_EXTENSION = "chrome_extension"
COMPONENT_MCP = "mcp_server"
COMPONENT_COMPANION = "local_companion"
KNOWN_COMPONENTS = (COMPONENT_EXTENSION, COMPONENT_MCP, COMPONENT_COMPANION)

ANCHOR_NONE = "none"
ANCHOR_PINNED = "pinned_digest"
ANCHOR_MANIFEST = "signed_release_manifest"


@dataclass(frozen=True, slots=True)
class TrustAnchor:
    """What Crusor independently believes about the package it will run.

    ``source`` records *why* the anchor is trusted: a digest pinned in Crusor's
    own configuration, or a release manifest carrying a valid Ed25519 signature.
    ``none`` means there is no independent expectation, so nothing can be
    verified however well-formed the HELLO is.
    """

    source: str = ANCHOR_NONE
    artifact_sha256: str = ""
    release: str = ""
    source_commit: str = ""
    signature_verified: bool = False
    #: Phase 3C.2.2 — a manifest may identify each executable component.
    schema: str = ""
    product: str = ""
    extension_id: str = ""
    build_id: str = ""
    protocol: str = ""
    released_at: str = ""
    components: dict[str, str] = field(default_factory=dict)
    required_components: tuple[str, ...] = ()
    digest_algorithm: str = ""

    @property
    def present(self) -> bool:
        return self.source != ANCHOR_NONE and bool(self.artifact_sha256)

    @property
    def identifies_components(self) -> bool:
        """A live integration needs per-component identity, not one blob digest."""
        return bool(self.components) and COMPONENT_EXTENSION in self.components

    @property
    def live_capable(self) -> bool:
        """Whether this anchor can ever support a live generation decision.

        Phase 3C.2.3: a signature proves authenticity of a *release*, not that every
        executable was identified. Live capability needs the v2 schema, a verified
        signature, individually identified components, and a digest for every
        component the release itself marks required. A pinned single digest and a
        legacy v1 manifest are readable but never live-capable.
        """
        if self.source != ANCHOR_MANIFEST:
            return False
        if self.schema != RELEASE_MANIFEST_SCHEMA:
            return False
        if not self.signature_verified or not self.identifies_components:
            return False
        if not SHA256.match(self.artifact_sha256):
            return False
        for name in self.required_components:
            if not SHA256.match(self.components.get(name, "")):
                return False
        return True

    def digest_for(self, component: str) -> str:
        if component == COMPONENT_EXTENSION and not self.components:
            return self.artifact_sha256
        return self.components.get(component, "")

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "artifact_sha256": self.artifact_sha256,
            "release": self.release,
            "source_commit": self.source_commit,
            "signature_verified": self.signature_verified,
            "schema": self.schema,
            "product": self.product,
            "extension_id": self.extension_id,
            "build_id": self.build_id,
            "protocol": self.protocol,
            "released_at": self.released_at,
            "components": dict(self.components),
            "required_components": list(self.required_components),
            "identifies_components": self.identifies_components,
            "live_capable": self.live_capable,
            "digest_algorithm": self.digest_algorithm,
        }


@dataclass(frozen=True, slots=True)
class TrustPolicy:
    """The independent expectations, configured by the operator, never by the peer."""

    trusted_artifact_sha256: str = ""
    trusted_release: str = ""
    trusted_source_commit: str = ""
    release_manifest_path: str = ""
    release_manifest_public_key: str = ""
    allow_component_drift: bool = False

    @property
    def has_anchor(self) -> bool:
        return bool(self.trusted_artifact_sha256 or self.release_manifest_path)

    def as_dict(self) -> dict[str, Any]:
        """The shape of the policy. Never the key, never the manifest contents."""
        return {
            "anchor_configured": self.has_anchor,
            "anchor_kind": (
                "signed_release_manifest"
                if self.release_manifest_path
                else ("pinned_digest" if self.trusted_artifact_sha256 else "none")
            ),
            "release_pinned": bool(self.trusted_release),
            "source_commit_pinned": bool(self.trusted_source_commit),
            "manifest_public_key_configured": bool(self.release_manifest_public_key),
            "component_drift_allowed": self.allow_component_drift,
        }

    @classmethod
    def from_settings(cls, settings: Any) -> "TrustPolicy":
        def value(name: str) -> str:
            return str(getattr(settings, name, "") or "").strip()

        return cls(
            trusted_artifact_sha256=value("flow_trusted_artifact_sha256").lower(),
            trusted_release=value("flow_trusted_release"),
            trusted_source_commit=value("flow_trusted_source_commit").lower(),
            release_manifest_path=value("flow_release_manifest_path"),
            release_manifest_public_key=value("flow_release_manifest_public_key"),
            allow_component_drift=bool(getattr(settings, "flow_allow_component_drift", False)),
        )


def _manifest_body(document: dict[str, Any]) -> bytes:
    body = {key: value for key, value in document.items() if key != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_release_manifest(path: str | Path, public_key_b64: str) -> TrustAnchor:
    """Verify an Ed25519-signed release manifest and return its anchor.

    A manifest is only useful if the signature is checked against a public key
    Crusor already holds. Anything else — including an unsigned manifest, a
    manifest signed by a key that arrives with it, or a manifest that fails to
    parse — raises rather than degrading to "trusted enough".
    """
    location = Path(str(path)).expanduser()
    if not location.is_file():
        raise ProvenanceRejected(
            "The configured release manifest is missing, so the installed artifact "
            "cannot be verified.",
            details={"field": "release_manifest"},
        )
    try:
        document = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProvenanceRejected(
            "The release manifest could not be read as JSON.",
            details={"field": "release_manifest"},
        ) from exc
    if not isinstance(document, dict) or document.get("schema") not in SUPPORTED_MANIFEST_SCHEMAS:
        raise ProvenanceRejected(
            "The release manifest did not declare a supported schema.",
            details={"field": "release_manifest_schema"},
        )

    signature_b64 = str(document.get("signature") or "").strip()
    if not signature_b64 or not public_key_b64:
        raise ProvenanceRejected(
            "The release manifest is unsigned, and trust is never taken from the "
            "artifact about itself.",
            details={"field": "release_manifest_signature"},
        )

    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except Exception as exc:  # noqa: BLE001 - crypto unavailable
        raise ProvenanceRejected(
            "Ed25519 verification is unavailable in this environment, so the release "
            "manifest cannot be verified.",
            details={"field": "release_manifest_crypto"},
        ) from exc

    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
        key.verify(base64.b64decode(signature_b64, validate=True), _manifest_body(document))
    except InvalidSignature as exc:
        raise ProvenanceRejected(
            "The release manifest signature does not verify against the configured "
            "public key.",
            details={"field": "release_manifest_signature"},
        ) from exc
    except ProvenanceRejected:
        raise
    except Exception as exc:  # noqa: BLE001 - malformed key or signature encoding
        raise ProvenanceRejected(
            "The release manifest signature or public key was malformed.",
            details={"field": "release_manifest_signature"},
        ) from exc

    release = document.get("release") if isinstance(document.get("release"), dict) else {}
    schema = str(document.get("schema") or "")

    declared_algorithm = str(document.get("digest_algorithm") or "").strip()
    if declared_algorithm and declared_algorithm not in RUNTIME_DIGEST_ALGORITHMS:
        raise ProvenanceRejected(
            f"The signed manifest declares an unsupported digest algorithm "
            f"{declared_algorithm!r}.",
            details={"field": "release_manifest_digest_algorithm"},
        )
    components: dict[str, str] = {}
    required: list[str] = []
    extension_id = ""
    protocol = ""
    build_id = ""
    for entry in document.get("components") or []:
        if not isinstance(entry, dict):
            raise ProvenanceRejected(
                "A component entry in the signed manifest was malformed.",
                details={"field": "release_manifest_components"},
            )
        name = str(entry.get("name") or "").strip()
        if name not in KNOWN_COMPONENTS:
            raise ProvenanceRejected(
                f"The signed manifest identified an unknown component {name!r}.",
                details={"field": "release_manifest_components", "component": name},
            )
        digest_value = str(entry.get("sha256") or "").strip().lower()
        if not SHA256.match(digest_value):
            raise ProvenanceRejected(
                f"The signed manifest gave component {name!r} no SHA-256 digest.",
                details={"field": "release_manifest_components", "component": name},
            )
        if name in components:
            raise ProvenanceRejected(
                f"The signed manifest declared component {name!r} more than once.",
                details={"field": "release_manifest_components", "component": name},
            )
        entry_algorithm = str(entry.get("digest_algorithm") or "").strip()
        if entry_algorithm and entry_algorithm != declared_algorithm:
            raise ProvenanceRejected(
                f"Component {name!r} declares digest algorithm {entry_algorithm!r}, which "
                f"contradicts the release's {declared_algorithm!r}.",
                details={"field": "release_manifest_digest_algorithm", "component": name},
            )
        components[name] = digest_value
        if bool(entry.get("required")):
            required.append(name)
        if name == COMPONENT_EXTENSION:
            extension_id = str(entry.get("extension_id") or "").strip()
            protocol = str(entry.get("protocol") or "").strip()
            build_id = str(entry.get("build_id") or "").strip()

    extension = document.get("extension") if isinstance(document.get("extension"), dict) else {}
    artifact = document.get("artifact") if isinstance(document.get("artifact"), dict) else {}
    digest = str(
        artifact.get("sha256") or extension.get("sha256") or components.get(COMPONENT_EXTENSION) or ""
    ).strip().lower()
    if not SHA256.match(digest):
        raise ProvenanceRejected(
            "The signed release manifest did not state a SHA-256 artifact digest.",
            details={"field": "release_manifest_artifact"},
        )
    if components and COMPONENT_EXTENSION in components and components[COMPONENT_EXTENSION] != digest:
        raise ProvenanceRejected(
            "The signed manifest disagrees with itself about the extension digest.",
            details={"field": "release_manifest_artifact"},
        )
    extension_id = extension_id or str(extension.get("id") or "").strip()
    protocol = protocol or str(extension.get("protocol") or "").strip()
    build_id = build_id or str(release.get("build_id") or "").strip()

    return TrustAnchor(
        source=ANCHOR_MANIFEST,
        artifact_sha256=digest,
        release=str(release.get("version") or "").strip(),
        source_commit=str(release.get("commit") or "").strip().lower(),
        signature_verified=True,
        schema=schema,
        product=str(document.get("product") or "").strip(),
        extension_id=extension_id,
        build_id=build_id,
        protocol=protocol,
        released_at=str(release.get("released_at") or "").strip(),
        components=components,
        required_components=tuple(required),
        digest_algorithm=declared_algorithm,
    )


def resolve_trust_anchor(policy: TrustPolicy | None) -> TrustAnchor:
    """Decide what Crusor independently expects the installed artifact to be."""
    if policy is None:
        return TrustAnchor()
    if policy.release_manifest_path:
        return verify_release_manifest(
            policy.release_manifest_path, policy.release_manifest_public_key
        )
    digest = (policy.trusted_artifact_sha256 or "").strip().lower()
    if SHA256.match(digest):
        return TrustAnchor(
            source=ANCHOR_PINNED,
            artifact_sha256=digest,
            release=policy.trusted_release,
            source_commit=policy.trusted_source_commit,
        )
    return TrustAnchor()


@dataclass(frozen=True, slots=True)
class ProvenanceReport:
    provenance: ExtensionProvenance
    warnings: tuple[str, ...] = field(default_factory=tuple)
    checks: tuple[str, ...] = field(default_factory=tuple)
    claims_well_formed: bool = True
    components_consistent: bool = True
    artifact_integrity_verified: bool = False
    anchor: TrustAnchor = field(default_factory=TrustAnchor)
    component_drift_accepted: bool = False

    @property
    def anchors_present(self) -> bool:
        return self.anchor.present

    @property
    def trust_state(self) -> str:
        return TRUST_STATE_VERIFIED if self.trusted else TRUST_STATE_UNVERIFIED

    @property
    def trusted(self) -> bool:
        """Trust requires all three proofs. Two out of three is not trust."""
        return bool(
            self.claims_well_formed
            and self.components_consistent
            and self.artifact_integrity_verified
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "provenance": self.provenance.as_dict(),
            "fingerprint": self.provenance.fingerprint(),
            "release_line": self.provenance.release_line,
            "claims_well_formed": self.claims_well_formed,
            "components_consistent": self.components_consistent,
            "artifact_integrity_verified": self.artifact_integrity_verified,
            "provenance_trusted": self.trusted,
            "trust_state": self.trust_state,
            "anchor": self.anchor.as_dict(),
            "anchors_present": self.anchors_present,
            "component_drift_accepted": self.component_drift_accepted,
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
    policy: TrustPolicy | None = None,
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
    consistent = True
    drift_accepted = False
    extension_line = provenance.release_line
    mcp_match = SEMVER.match(provenance.mcp_version)
    if mcp_match:
        mcp_line = f"{mcp_match.group('major')}.{mcp_match.group('minor')}"
        if extension_line and mcp_line != extension_line:
            warnings.append(
                "mcp_version_release_line_mismatch: the MCP server is on "
                f"{mcp_line} while the extension is on {extension_line}"
            )
            consistent = False
    allow_drift = bool(allow_component_drift or (policy.allow_component_drift if policy else False))
    if not consistent:
        if not allow_drift:
            raise ProvenanceRejected(
                "The extension ships components from different release lines, so the "
                "package cannot be reproduced from one build: " + "; ".join(warnings),
                details={"warnings": warnings},
            )
        # Accepted drift is still drift: it can never yield a trusted report.
        drift_accepted = True
        checks.append("component_drift_accepted_by_operator")

    anchor = resolve_trust_anchor(policy)
    integrity = False
    if not anchor.present:
        # No independent expectation exists, so nothing about the *artifact* was
        # verified. Say so plainly rather than inheriting trust from syntax.
        warnings.append(
            "artifact_integrity_unverified: no pinned artifact digest or signed "
            "release manifest is configured, so the installed package cannot be "
            "compared against a trusted expectation"
        )
    else:
        checks.append(f"trust_anchor_{anchor.source}")
        expected_release = anchor.release or (
            policy.trusted_release.strip() if policy and policy.trusted_release else ""
        )
        expected_commit = anchor.source_commit or (
            policy.trusted_source_commit.strip().lower() if policy else ""
        )
        if expected_release and expected_release != provenance.extension_version:
            raise ProvenanceRejected(
                "The connecting extension is not the release this build supports: "
                f"expected {expected_release}, got {provenance.extension_version}.",
                details={
                    "expected_release": expected_release,
                    "presented": provenance.extension_version,
                },
            )
        checks.append("release_matches_trusted_release")
        if expected_commit and expected_commit != provenance.source_commit:
            raise ProvenanceRejected(
                "The connecting extension reports a different source commit from the "
                f"one this build supports (expected {expected_commit}, got "
                f"{provenance.source_commit}).",
                details={"expected_commit": expected_commit, "presented": provenance.source_commit},
            )
        checks.append("source_commit_matches_trusted_commit")

        if not provenance.package_sha256:
            # A pinned expectation with nothing to compare against is not a match.
            warnings.append(
                "artifact_digest_missing: a trusted artifact digest is configured but "
                "the HELLO carried none, so the installed bytes were not verified"
            )
        elif provenance.package_sha256 != anchor.artifact_sha256:
            warnings.append(
                "artifact_digest_mismatch: the installed package digest "
                f"{provenance.package_sha256[:16]}... does not match the trusted digest "
                f"{anchor.artifact_sha256[:16]}..."
            )
        else:
            integrity = True
            checks.append("artifact_digest_matches_trusted_anchor")

    return ProvenanceReport(
        provenance=provenance,
        warnings=tuple(warnings),
        checks=tuple(checks),
        claims_well_formed=True,
        components_consistent=consistent,
        artifact_integrity_verified=integrity,
        anchor=anchor,
        component_drift_accepted=drift_accepted,
    )


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
