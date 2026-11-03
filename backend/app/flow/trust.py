"""Trust aggregation, session attestation and the live-generation guard (3C.2.2).

Three questions that Phase 3C.2.1 kept apart now need to be joined, carefully:

* **What does the release say?** — a signed manifest, verified against a key Crusor
  holds (:mod:`app.flow.provenance`).
* **What is installed?** — a measurement of the actual files on disk
  (:mod:`app.flow.installed_artifact`).
* **Is that measurement about *this* session?** — a single-use, short-lived challenge
  answered by the companion running beside the browser (:class:`ChallengeStore`).

Only the conjunction of an anchor, a fresh measurement that matches it, correlated
runtime identity and verified components yields ``VERIFIED``. Each missing piece has
its own state, because "not trusted" is not a diagnosis.

The honest limit, restated here because it is easy to forget: a challenge proves that
*the companion* measured *some files* and reported them for *this session*. It does
not prove that the browser is executing those files, and it cannot make a compromised
local machine tell the truth. That is why ``runtime_identity_correlated`` is derived
from the HELLO the *extension* sent (its id and version) agreeing with the measurement,
and why every report carries its limitations verbatim.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from app.flow import protocol as proto
from app.flow.provenance import (
    COMPONENT_COMPANION,
    COMPONENT_EXTENSION,
    COMPONENT_MCP,
    ProvenanceReport,
    TrustAnchor,
)

logger = logging.getLogger("app.flow.trust")

# --------------------------------------------------------------------------- #
# states
# --------------------------------------------------------------------------- #
SOURCE_ABSENT = "SOURCE_ABSENT"
RELEASE_ANCHOR_MISSING = "RELEASE_ANCHOR_MISSING"
INSTALLED_ARTIFACT_NOT_FOUND = "INSTALLED_ARTIFACT_NOT_FOUND"
ARTIFACT_MEASUREMENT_FAILED = "ARTIFACT_MEASUREMENT_FAILED"
ARTIFACT_DIGEST_MISMATCH = "ARTIFACT_DIGEST_MISMATCH"
MEASUREMENT_STALE = "MEASUREMENT_STALE"
COMPONENT_UNVERIFIED = "COMPONENT_UNVERIFIED"
UNVERIFIED = "UNVERIFIED"
VERIFIED = "VERIFIED"

ALL_TRUST_STATES: tuple[str, ...] = (
    SOURCE_ABSENT,
    RELEASE_ANCHOR_MISSING,
    INSTALLED_ARTIFACT_NOT_FOUND,
    ARTIFACT_MEASUREMENT_FAILED,
    ARTIFACT_DIGEST_MISMATCH,
    MEASUREMENT_STALE,
    COMPONENT_UNVERIFIED,
    UNVERIFIED,
    VERIFIED,
)

COMPONENT_VERIFIED = "VERIFIED"
COMPONENT_UNVERIFIED_STATE = "UNVERIFIED"
COMPONENT_NOT_MEASURED = "NOT_MEASURED"
COMPONENT_MISMATCH = "MISMATCH"


class AttestationRejected(Exception):
    """A challenge or measurement report was not acceptable. Fails closed."""

    code = "flow_attestation_rejected"

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": dict(self.details)}


class UntrustedArtifact(proto.FlowProtocolError):
    """Live generation was asked for while the installed artifact is not verified."""

    code = "flow_untrusted_artifact"


# --------------------------------------------------------------------------- #
# evidence
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class TrustEvidence:
    """Every fact the trust decision rests on, each with its own source.

    ``claimed_*`` facts come from the peer and can never move a ``measured_*`` fact.
    The measured fields are written only by :class:`SessionAttestation`, which
    accepts them only from a challenge-bound companion report.
    """

    supported_protocol: bool = False
    claims_well_formed: bool = False
    components_consistent: bool = False
    release_anchor_valid: bool = False
    hello_digest_claim_matches_anchor: bool = False
    installed_artifact_measured: bool = False
    installed_artifact_digest_matches_anchor: bool = False
    runtime_identity_correlated: bool = False
    measurement_fresh: bool = False
    measurement_trust_state: str = UNVERIFIED
    component_states: dict[str, str] = field(default_factory=dict)
    required_components: tuple[str, ...] = (COMPONENT_EXTENSION,)
    failures: tuple[str, ...] = ()
    measurement_id: str = ""
    measured_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "supported_protocol": self.supported_protocol,
            "claims_well_formed": self.claims_well_formed,
            "components_consistent": self.components_consistent,
            "release_anchor_valid": self.release_anchor_valid,
            "hello_digest_claim_matches_anchor": self.hello_digest_claim_matches_anchor,
            "installed_artifact_measured": self.installed_artifact_measured,
            "installed_artifact_digest_matches_anchor": self.installed_artifact_digest_matches_anchor,
            "runtime_identity_correlated": self.runtime_identity_correlated,
            "measurement_fresh": self.measurement_fresh,
            "measurement_trust_state": self.measurement_trust_state,
            "component_states": dict(self.component_states),
            "required_components": list(self.required_components),
            "failures": list(self.failures),
            "measurement_id": self.measurement_id,
            "measured_at": self.measured_at,
        }


@dataclass(frozen=True, slots=True)
class TrustDecision:
    state: str
    evidence: TrustEvidence
    reasons: tuple[str, ...] = ()
    decided_at: float = field(default_factory=time.time)
    anchor_source: str = ""
    expected_extension_id: str = ""
    expected_version: str = ""
    measured_extension_id: str = ""
    measured_version: str = ""
    measured_digest: str = ""
    session_id: str = ""

    @property
    def trusted(self) -> bool:
        return self.state == VERIFIED

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.evidence.measured_at) if self.evidence.measured_at else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "trusted": self.trusted,
            "reasons": list(self.reasons),
            "evidence": self.evidence.as_dict(),
            "anchor_source": self.anchor_source,
            "expected": {
                "extension_id": self.expected_extension_id,
                "version": self.expected_version,
            },
            "measured": {
                "extension_id": self.measured_extension_id,
                "version": self.measured_version,
                "digest": self.measured_digest,
                "age_seconds": round(self.age_seconds, 3),
            },
            "session_id": self.session_id,
            "measured_at": self.evidence.measured_at,
        }


def empty_decision(reason: str = "no_measurement", *, state: str = UNVERIFIED) -> TrustDecision:
    return TrustDecision(
        state=state,
        evidence=TrustEvidence(failures=(reason,)),
        reasons=(reason,),
    )


# --------------------------------------------------------------------------- #
# challenges
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Challenge:
    challenge_id: str
    nonce: str
    session_id: str
    companion_id: str
    extension_id: str
    issued_at: float
    expires_at: float
    consumed_at: float = 0.0

    @property
    def consumed(self) -> bool:
        return bool(self.consumed_at)

    def expired(self, now: float) -> bool:
        return now >= self.expires_at

    def as_dict(self) -> dict[str, Any]:
        """Public shape. The nonce is deliberately absent from status payloads."""
        return {
            "challenge_id": self.challenge_id,
            "expires_at": self.expires_at,
            "issued_at": self.issued_at,
            "consumed": self.consumed,
            "valid": not self.consumed and not self.expired(time.time()),
        }


class ChallengeStore:
    """Single-use, short-lived, session-bound challenges.

    A challenge is issued per connection. It expires quickly, may be consumed once,
    and is bound to the session, companion and extension that it was issued to, so a
    report from a different companion (or a replay) cannot be applied to this
    session.
    """

    def __init__(self, *, ttl_seconds: float = 120.0, clock: Callable[[], float] = time.time) -> None:
        if ttl_seconds <= 0:
            raise ValueError("challenge ttl must be positive")
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._challenges: dict[str, Challenge] = {}

    def issue(
        self, *, session_id: str, companion_id: str, extension_id: str
    ) -> Challenge:
        now = self._clock()
        challenge = Challenge(
            challenge_id=uuid.uuid4().hex,
            nonce=secrets.token_urlsafe(32),
            session_id=session_id,
            companion_id=companion_id,
            extension_id=extension_id,
            issued_at=now,
            expires_at=now + self.ttl_seconds,
        )
        self._challenges[challenge.challenge_id] = challenge
        self._prune(now)
        return challenge

    def consume(
        self,
        challenge_id: str,
        *,
        session_id: str,
        companion_id: str,
        extension_id: str,
        nonce: str = "",
    ) -> Challenge:
        now = self._clock()
        challenge = self._challenges.get(str(challenge_id or ""))
        if challenge is None:
            raise AttestationRejected(
                "challenge_unknown", "The verification challenge is not one this session issued."
            )
        if challenge.consumed:
            raise AttestationRejected(
                "challenge_replayed", "This verification challenge has already been used."
            )
        if challenge.expired(now):
            raise AttestationRejected(
                "challenge_expired", "This verification challenge has expired."
            )
        if challenge.session_id != session_id:
            raise AttestationRejected(
                "challenge_wrong_session",
                "This verification challenge belongs to another session.",
            )
        if challenge.companion_id and challenge.companion_id != companion_id:
            raise AttestationRejected(
                "challenge_wrong_companion",
                "This verification challenge was issued to another companion.",
            )
        if extension_id and challenge.extension_id and challenge.extension_id != extension_id:
            raise AttestationRejected(
                "challenge_wrong_extension",
                "This verification challenge was issued to another extension.",
            )
        if nonce and not hmac.compare_digest(str(nonce), challenge.nonce):
            raise AttestationRejected(
                "challenge_nonce_mismatch", "The report did not carry the challenge nonce."
            )
        self._challenges[challenge.challenge_id] = Challenge(
            challenge_id=challenge.challenge_id,
            nonce=challenge.nonce,
            session_id=challenge.session_id,
            companion_id=challenge.companion_id,
            extension_id=challenge.extension_id,
            issued_at=challenge.issued_at,
            expires_at=challenge.expires_at,
            consumed_at=now,
        )
        return challenge

    def peek(self, challenge_id: str) -> Challenge | None:
        return self._challenges.get(str(challenge_id or ""))

    def ttl_remaining(self, challenge_id: str) -> float:
        challenge = self._challenges.get(str(challenge_id or ""))
        return 0.0 if challenge is None else max(0.0, challenge.expires_at - self._clock())

    def _prune(self, now: float) -> None:
        stale = [key for key, item in self._challenges.items() if now - item.expires_at > 3600]
        for key in stale:
            self._challenges.pop(key, None)


# --------------------------------------------------------------------------- #
# session attestation
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ComponentReport:
    name: str
    digest: str
    extension_id: str = ""
    version: str = ""
    measurement_id: str = ""
    measured_at: float = 0.0
    limitations: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VerificationSubmission:
    challenge_id: str
    nonce: str
    components: tuple[ComponentReport, ...]

    def component(self, name: str) -> ComponentReport | None:
        for item in self.components:
            if item.name == name:
                return item
        return None


@dataclass
class SessionAttestation:
    """Applies a challenge-bound measurement to a session, once, freshly."""

    store: ChallengeStore
    anchor: TrustAnchor
    required_components: tuple[str, ...] = (COMPONENT_EXTENSION,)
    freshness_seconds: float = 300.0
    clock: Callable[[], float] = time.time

    def issue(self, *, session_id: str, companion_id: str, extension_id: str) -> Challenge:
        if not self.anchor.present:
            raise AttestationRejected(
                "release_anchor_missing",
                "No trusted release anchor is configured, so a measurement could not be "
                "compared with anything.",
            )
        return self.store.issue(
            session_id=session_id, companion_id=companion_id, extension_id=extension_id
        )

    def accept(
        self,
        submission: VerificationSubmission,
        *,
        session_id: str,
        companion_id: str,
        hello_extension_id: str,
        hello_version: str,
        hello_digest: str,
    ) -> TrustEvidence:
        challenge = self.store.consume(
            submission.challenge_id,
            session_id=session_id,
            companion_id=companion_id,
            extension_id=hello_extension_id,
            nonce=submission.nonce,
        )
        extension = submission.component(COMPONENT_EXTENSION)
        if extension is None:
            raise AttestationRejected(
                "component_missing",
                "The report did not measure the Chrome extension.",
                details={"required": list(self.required_components)},
            )

        failures: list[str] = []
        component_states: dict[str, str] = {}

        measured_digest = str(extension.digest or "").lower()
        if not self._is_sha256(measured_digest):
            raise AttestationRejected(
                "measurement_malformed", "The reported extension digest is not a SHA-256."
            )

        expected_digest = self.anchor.digest_for(COMPONENT_EXTENSION)
        digest_matches = bool(expected_digest) and hmac.compare_digest(measured_digest, expected_digest)
        if not digest_matches:
            failures.append("installed_artifact_digest_mismatch")
        component_states[COMPONENT_EXTENSION] = (
            COMPONENT_VERIFIED if digest_matches else COMPONENT_MISMATCH
        )

        # Runtime correlation: the extension's own HELLO must agree with the bytes
        # the companion measured. Two independent sources agreeing is evidence; one
        # source repeating itself is not.
        identity_ok = bool(extension.extension_id) and extension.extension_id == hello_extension_id
        version_ok = bool(extension.version) and extension.version == hello_version
        if self.anchor.extension_id:
            identity_ok = identity_ok and extension.extension_id == self.anchor.extension_id
        if self.anchor.release:
            version_ok = version_ok and extension.version == self.anchor.release
        if not identity_ok:
            failures.append("extension_identity_mismatch")
        if not version_ok:
            failures.append("extension_version_mismatch")

        # A HELLO digest that contradicts the measured bytes is worth recording even
        # when the measurement matches the anchor: the peer is lying about itself.
        hello_claim_matches = bool(hello_digest) and hello_digest == measured_digest

        for name in self.required_components:
            if name == COMPONENT_EXTENSION:
                continue
            report = submission.component(name)
            if report is None:
                component_states[name] = COMPONENT_NOT_MEASURED
                failures.append(f"component_not_measured:{name}")
                continue
            expected = self.anchor.digest_for(name)
            if not expected:
                component_states[name] = COMPONENT_UNVERIFIED_STATE
                failures.append(f"component_not_in_release:{name}")
                continue
            if hmac.compare_digest(str(report.digest or "").lower(), expected):
                component_states[name] = COMPONENT_VERIFIED
            else:
                component_states[name] = COMPONENT_MISMATCH
                failures.append(f"component_digest_mismatch:{name}")

        now = self.clock()
        measured_at = extension.measured_at or challenge.issued_at
        # A measurement must have been taken during this challenge, not before it,
        # and not "in the future" (which a skewed or lying companion could claim).
        from_future = measured_at > now + 5.0
        fresh = (
            not from_future
            and (now - measured_at) <= self.freshness_seconds
            and measured_at >= challenge.issued_at - 1.0
        )
        if from_future:
            failures.append("measurement_from_future")
        elif not fresh:
            failures.append("measurement_stale")

        measured = bool(measured_digest)
        evidence = TrustEvidence(
            supported_protocol=True,
            claims_well_formed=True,
            components_consistent=True,
            release_anchor_valid=self.anchor.signature_verified or self.anchor.source != "none",
            hello_digest_claim_matches_anchor=hello_claim_matches,
            installed_artifact_measured=measured,
            installed_artifact_digest_matches_anchor=digest_matches,
            runtime_identity_correlated=identity_ok and version_ok,
            measurement_fresh=fresh,
            measurement_trust_state=UNVERIFIED if failures else VERIFIED,
            component_states=component_states,
            required_components=tuple(self.required_components),
            failures=tuple(dict.fromkeys(failures)),
            measurement_id=extension.measurement_id,
            measured_at=measured_at,
        )
        return evidence

    @staticmethod
    def _is_sha256(value: str) -> bool:
        return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


# --------------------------------------------------------------------------- #
# aggregation
# --------------------------------------------------------------------------- #
def aggregate_trust(
    *,
    source_gate_status: str,
    anchor: TrustAnchor,
    provenance: ProvenanceReport | None,
    evidence: TrustEvidence | None,
    session_id: str = "",
    expected_extension_id: str = "",
    expected_version: str = "",
    measured_extension_id: str = "",
    measured_version: str = "",
    measured_digest: str = "",
) -> TrustDecision:
    """The one place that decides whether an installed extension is trusted.

    Ordered from the most fundamental missing piece outward, so the reported state
    names the earliest thing a human would have to fix.
    """
    reasons: list[str] = []

    def decision(state: str, extra: tuple[str, ...] = ()) -> TrustDecision:
        return TrustDecision(
            state=state,
            evidence=evidence or TrustEvidence(),
            reasons=tuple(dict.fromkeys((*reasons, *extra))),
            anchor_source=anchor.source,
            expected_extension_id=expected_extension_id or anchor.extension_id,
            expected_version=expected_version or anchor.release,
            measured_extension_id=measured_extension_id,
            measured_version=measured_version,
            measured_digest=measured_digest,
            session_id=session_id,
        )

    if source_gate_status in {"SOURCE_ABSENT", "LICENSING_BLOCKED"}:
        reasons.append(f"source_gate:{source_gate_status}")
        return decision(SOURCE_ABSENT)
    if not anchor.present:
        reasons.append("release_anchor_missing")
        return decision(RELEASE_ANCHOR_MISSING)
    if provenance is not None and not provenance.claims_well_formed:
        reasons.append("claims_malformed")
        return decision(UNVERIFIED)
    if provenance is not None and not provenance.components_consistent:
        reasons.append("component_drift")
        return decision(UNVERIFIED)
    if evidence is None or not evidence.installed_artifact_measured:
        reasons.append("installed_artifact_not_measured")
        return decision(INSTALLED_ARTIFACT_NOT_FOUND)
    if evidence.failures:
        # The measurement exists but something about it failed; name the earliest.
        first = evidence.failures[0]
        if "digest_mismatch" in first:
            reasons.extend(evidence.failures)
            return decision(ARTIFACT_DIGEST_MISMATCH)
        if "stale" in first:
            reasons.extend(evidence.failures)
            return decision(MEASUREMENT_STALE)
        if first.startswith("component_"):
            reasons.extend(evidence.failures)
            return decision(COMPONENT_UNVERIFIED)
        reasons.extend(evidence.failures)
        return decision(ARTIFACT_MEASUREMENT_FAILED)
    if not evidence.measurement_fresh:
        reasons.append("measurement_stale")
        return decision(MEASUREMENT_STALE)
    if not evidence.runtime_identity_correlated:
        reasons.append("runtime_identity_uncorrelated")
        return decision(COMPONENT_UNVERIFIED)
    for name, state in evidence.component_states.items():
        if state != COMPONENT_VERIFIED:
            reasons.append(f"component_not_verified:{name}")
            return decision(COMPONENT_UNVERIFIED)
    if not evidence.installed_artifact_digest_matches_anchor:
        reasons.append("artifact_digest_mismatch")
        return decision(ARTIFACT_DIGEST_MISMATCH)
    if not evidence.release_anchor_valid:
        reasons.append("release_anchor_invalid")
        return decision(UNVERIFIED)
    return decision(VERIFIED)


# Brand names used by docs and status payloads, kept next to the states.
COMPONENT_DISPLAY = {
    COMPONENT_EXTENSION: "Chrome extension",
    COMPONENT_MCP: "MCP server",
    COMPONENT_COMPANION: "Local companion",
}


# --------------------------------------------------------------------------- #
# registry + audit
# --------------------------------------------------------------------------- #
AUDIT_MEASUREMENT_STARTED = "flow.trust.measurement_started"
AUDIT_MEASUREMENT_SUCCEEDED = "flow.trust.measurement_succeeded"
AUDIT_DIGEST_MISMATCH = "flow.trust.digest_mismatch"
AUDIT_CHALLENGE_REPLAYED = "flow.trust.challenge_replayed"
AUDIT_MEASUREMENT_EXPIRED = "flow.trust.measurement_expired"
AUDIT_IDENTITY_MISMATCH = "flow.trust.extension_identity_mismatch"
AUDIT_TRUST_REVOKED = "flow.trust.trust_revoked"


@dataclass
class TrustAuditEvent:
    action: str
    at: float
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "at": datetime.fromtimestamp(self.at, tz=timezone.utc).isoformat(),
            "detail": dict(self.detail),
        }


class TrustRegistry:
    """The most recent decision per session, plus a bounded audit trail.

    In-memory by design: a measurement is about a live connection, and persisting a
    "trusted" flag across restarts is how stale attestations become permanent ones.
    """

    def __init__(self, *, max_events: int = 256) -> None:
        self._decisions: dict[str, TrustDecision] = {}
        self._latest: TrustDecision | None = None
        self._events: list[TrustAuditEvent] = []
        self._max_events = max_events

    # ---------------------------------------------------------------- record
    def record(self, session_id: str, decision: TrustDecision) -> TrustDecision:
        previous = self._decisions.get(session_id)
        self._decisions[session_id] = decision
        self._latest = decision
        if previous is not None and previous.trusted and not decision.trusted:
            self.audit(AUDIT_TRUST_REVOKED, session_id=session_id, state=decision.state)
        if decision.trusted:
            self.audit(
                AUDIT_MEASUREMENT_SUCCEEDED,
                session_id=session_id,
                state=decision.state,
                digest=decision.measured_digest[:16],
                measurement_id=decision.evidence.measurement_id,
            )
        elif "digest_mismatch" in " ".join(decision.reasons):
            self.audit(AUDIT_DIGEST_MISMATCH, session_id=session_id, digest=decision.measured_digest[:16])
        elif "identity" in " ".join(decision.reasons):
            self.audit(AUDIT_IDENTITY_MISMATCH, session_id=session_id, state=decision.state)
        elif "stale" in " ".join(decision.reasons):
            self.audit(AUDIT_MEASUREMENT_EXPIRED, session_id=session_id, state=decision.state)
        return decision

    def audit(self, action: str, **detail: Any) -> TrustAuditEvent:
        event = TrustAuditEvent(action=action, at=time.time(), detail=detail)
        self._events.append(event)
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events :]
        logger.info("flow_trust_event action=%s detail=%s", action, sorted(detail))
        return event

    # ----------------------------------------------------------------- read
    def for_session(self, session_id: str) -> TrustDecision | None:
        return self._decisions.get(session_id)

    def latest(self) -> TrustDecision | None:
        return self._latest

    def events(self) -> list[dict[str, Any]]:
        return [event.as_dict() for event in self._events]

    def clear(self, session_id: str | None = None) -> None:
        if session_id is None:
            self._decisions.clear()
            self._latest = None
            return
        self._decisions.pop(session_id, None)
        if self._latest is not None and self._latest.session_id == session_id:
            self._latest = None


#: One registry per process, so a status handler and a bridge in the same process
#: see the same decisions. Measurements never persist across restarts.
_GLOBAL_REGISTRY = TrustRegistry()


def get_registry() -> TrustRegistry:
    return _GLOBAL_REGISTRY


# --------------------------------------------------------------------------- #
# live-generation guard
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class GuardDecision:
    allowed: bool
    blockers: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "blockers": list(self.blockers)}


def evaluate_live_generation_guard(
    *,
    live_generation_flag: bool,
    source_gate_status: str,
    decision: TrustDecision | None,
    approval_binding: str,
    budget_configured: bool,
    idempotency_enforced: bool,
) -> GuardDecision:
    """All eight prerequisites from §9, evaluated in the order a human fixes them.

    A configuration flag alone never satisfies this: ``live_generation_flag`` is
    necessary and, by itself, useless.
    """
    blockers: list[str] = []
    if not live_generation_flag:
        blockers.append("live_generation_disabled")
    if source_gate_status != "AUTHORIZED":
        blockers.append(f"upstream_source:{source_gate_status}")
    if decision is None or not decision.trusted:
        blockers.append(f"installed_artifact:{(decision.state if decision else UNVERIFIED)}")
    if not approval_binding:
        blockers.append("founder_approval_missing")
    if not idempotency_enforced:
        blockers.append("idempotency_not_enforced")
    if not budget_configured:
        blockers.append("budget_controls_missing")
    return GuardDecision(allowed=not blockers, blockers=tuple(blockers))


def trust_status_payload(
    *,
    policy_anchor: TrustAnchor,
    decision: TrustDecision | None,
    registry: TrustRegistry,
    challenge: Challenge | None = None,
    source_gate_status: str,
    required_components: tuple[str, ...] = (COMPONENT_EXTENSION,),
) -> dict[str, Any]:
    """Everything an operator needs, and nothing an attacker could use.

    No nonce, no pairing secret, no key material, no filesystem paths beyond the
    installation root the operator themselves configured.
    """
    decision = decision or empty_decision()
    components: dict[str, Any] = {}
    for name in (COMPONENT_EXTENSION, COMPONENT_MCP, COMPONENT_COMPANION):
        state = decision.evidence.component_states.get(name, COMPONENT_NOT_MEASURED)
        components[name] = {
            "label": COMPONENT_DISPLAY[name],
            "state": state,
            "required": name in required_components,
            "expected_digest": policy_anchor.digest_for(name),
        }
    unverified = [name for name, item in components.items() if item["state"] != COMPONENT_VERIFIED]
    return {
        "state": decision.state,
        "trusted": decision.trusted,
        "states_supported": list(ALL_TRUST_STATES),
        "reasons": list(decision.reasons),
        "source_gate": source_gate_status,
        "anchor": {
            "type": policy_anchor.source,
            "present": policy_anchor.present,
            "schema": policy_anchor.schema,
            "product": policy_anchor.product,
            "signature_verified": policy_anchor.signature_verified,
            "identifies_components": policy_anchor.identifies_components,
            "released_at": policy_anchor.released_at,
        },
        "expected": {
            "extension_id": policy_anchor.extension_id or decision.expected_extension_id,
            "version": policy_anchor.release or decision.expected_version,
            "source_commit": policy_anchor.source_commit,
            "build_id": policy_anchor.build_id,
            "protocol": policy_anchor.protocol,
            "artifact_digest": policy_anchor.digest_for(COMPONENT_EXTENSION),
        },
        "measured": {
            "extension_id": decision.measured_extension_id,
            "version": decision.measured_version,
            "artifact_digest": decision.measured_digest,
            "measurement_id": decision.evidence.measurement_id,
            "measured_at": decision.evidence.measured_at,
            "age_seconds": round(decision.age_seconds, 3),
            "fresh": decision.evidence.measurement_fresh,
        },
        "challenge": {
            "issued": challenge is not None,
            "valid": bool(challenge and not challenge.consumed and not challenge.expired(time.time())),
            "expires_in_seconds": (
                round(max(0.0, challenge.expires_at - time.time()), 3) if challenge else 0.0
            ),
        },
        "components": components,
        "unverified_components": unverified,
        "evidence": decision.evidence.as_dict(),
        "recent_events": registry.events()[-8:],
        "limitations": [
            "A measurement proves what is on disk, not what the browser has loaded.",
            "A challenge binds a report to a session; it cannot make a compromised "
            "machine honest.",
        ],
    }


def measurement_fingerprint(*parts: str) -> str:
    """Short, log-safe fingerprint of measurement identity."""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


__all__ = [
    "ALL_TRUST_STATES",
    "ARTIFACT_DIGEST_MISMATCH",
    "ARTIFACT_MEASUREMENT_FAILED",
    "AttestationRejected",
    "Challenge",
    "ChallengeStore",
    "COMPONENT_COMPANION",
    "COMPONENT_EXTENSION",
    "COMPONENT_MCP",
    "COMPONENT_UNVERIFIED",
    "COMPONENT_VERIFIED",
    "ComponentReport",
    "GuardDecision",
    "INSTALLED_ARTIFACT_NOT_FOUND",
    "MEASUREMENT_STALE",
    "RELEASE_ANCHOR_MISSING",
    "SOURCE_ABSENT",
    "SessionAttestation",
    "TrustDecision",
    "TrustEvidence",
    "TrustRegistry",
    "UNVERIFIED",
    "UntrustedArtifact",
    "VERIFIED",
    "VerificationSubmission",
    "aggregate_trust",
    "empty_decision",
    "get_registry",
    "evaluate_live_generation_guard",
    "trust_status_payload",
]
