"""Flow bridge protocol (Phase 3C.1).

This module is the *contract* between a local Flow Companion and the third-party
Chrome extension. It is deliberately small: five commands in, everything else
rejected, and every message validated before anything is dispatched.

**Provenance.** No DeepX/Flow extension source is vendored here. The archive was
inspected statically in an external audit; its licence is unconfirmed, so the
extension is treated as an opaque third party that speaks this documented
protocol. Nothing in this module is copied from that archive — the message shapes
are Crusor's own, and the extension needs a small upstream change to speak them
(see :data:`UPSTREAM_BLOCKERS`).

Command allowlist (the only operations Crusor may request):

======================================  ====================================
``status``                              readiness + non-secret metadata
``generate_image``                      one image, ``count == 1``
``generate_video``                      one video, ``count == 1``
``check_video_status``                  poll a correlated video job
``export_asset``                        return verified media bytes
======================================  ====================================

Everything else — ``eval_script``, ``native_click``, ``native_key``,
``batch_generate``, ``rapid_batch_dispatch``, ``autonomous_storyboard_pipeline``,
``rename_project``, ``open_new_project`` and the unrestricted MCP tool collection
— is refused by name, before a message is written to any socket.
"""

from __future__ import annotations

from app.flow.delivery import DeliveryOutcome

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PROTOCOL_VERSION = "flow.bridge.v1"

#: Protocols this build speaks. Anything else is refused at the handshake, from
#: Crusor's side, without waiting for the extension to agree (Phase 3C.2 §10).
SUPPORTED_PROTOCOLS: tuple[str, ...] = (PROTOCOL_VERSION,)

#: WebSocket close codes used by the bridge. They are part of the contract, so
#: they live here rather than as magic numbers in ``bridge.py``.
WS_CLOSE_MALFORMED = 4400
WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_SUPERSEDED = 4000
WS_CLOSE_PROVENANCE = 4422
WS_CLOSE_PROTOCOL = 4426
#: Phase 3C.2.2 reserved this for a rejected measurement report. Phase 3C.2.3 removed
#: the peer-report path entirely (the local process measures), so nothing sends it;
#: the value is kept only so older logs remain readable.
WS_CLOSE_MEASUREMENT = 4428

#: Fields a HELLO must carry before the bridge will exchange a single command.
REQUIRED_PROVENANCE_FIELDS: tuple[str, ...] = (
    "extension_version",
    "service_worker_version",
    "mcp_version",
    "source_commit",
    "build_id",
)

#: The complete set of commands Crusor may ever send to the extension.
ALLOWED_COMMANDS: tuple[str, ...] = (
    "status",
    "generate_image",
    "generate_video",
    "check_video_status",
    "export_asset",
)

#: Commands the upstream extension/bridge exposes that Crusor must never reach.
DENIED_COMMANDS: dict[str, str] = {
    "eval_script": "Arbitrary page-context JavaScript execution is never exposed.",
    "native_click": "Raw browser input synthesis is not needed for generation.",
    "native_key": "Raw browser input synthesis is not needed for generation.",
    "batch_generate": "Crusor submits exactly one generation per approval.",
    "rapid_batch_dispatch": "Rapid dispatch defeats one-at-a-time serialisation.",
    "autonomous_storyboard_pipeline": "Autonomous multi-shot pipelines bypass approval.",
    "rename_project": "Project mutation is outside the creative pipeline.",
    "open_new_project": "Project creation is outside the creative pipeline.",
    "download_media": "Downloads cannot deliver verified bytes; use export_asset.",
    "upload_asset": "Uploading local files into Flow is not a Crusor operation.",
    "cancel_job": "Batch cancellation is replaced by per-job cancellation semantics.",
    "get_token": "Raw Google bearer tokens are never exposed to page JavaScript.",
    "get_flow_context": "Flow context is returned through status without secrets.",
    "api_post": "Arbitrary authenticated requests are replaced by fixed endpoints.",
}

#: Upstream changes that cannot be made from this repository. Reported verbatim by
#: ``/flow/status`` and in the Phase 3C.1 readiness document.
UPSTREAM_BLOCKERS: tuple[dict[str, str], ...] = (
    {
        "id": "U1",
        "area": "page-visible token channel",
        "finding": (
            "content-script-main.js posts DEEPX_AUTH_TOKEN_CAPTURED with "
            "window.postMessage(..., '*') and answers get_token / get_flow_context "
            "from any page script."
        ),
        "required_change": (
            "Delete the token broadcast and the get_token / get_flow_context "
            "handlers; keep tokens inside the extension's isolated world."
        ),
        "blocked_because": "The extension source is not supplied and its licence is unconfirmed.",
    },
    {
        "id": "U2",
        "area": "arbitrary authenticated POST",
        "finding": "api_post(url, body, token) attaches the bearer token to a caller-supplied URL.",
        "required_change": (
            "Replace it with fixed, allowlisted Google Flow endpoints and return only "
            "non-secret results."
        ),
        "blocked_because": "Requires editing content-script-main.js inside the extension package.",
    },
    {
        "id": "U3",
        "area": "pairing authentication",
        "finding": (
            "The service worker connects to ws://127.0.0.1:8792/ws and sends HELLO "
            "without any credential."
        ),
        "required_change": (
            "Send pairing_id/pairing_secret in the HELLO frame and refuse to run "
            "unpaired; support the flow.bridge.v1 envelope."
        ),
        "blocked_because": "Unmodified extensions cannot authenticate, so they are refused.",
    },
    {
        "id": "U4",
        "area": "generation commands",
        "finding": (
            "eval_script, batch_generate, rapid_batch_dispatch, "
            "autonomous_storyboard_pipeline, rename_project and open_new_project are "
            "registered MCP tools."
        ),
        "required_change": (
            "Ship a restricted build, or gate the powerful tools behind a local "
            "developer flag that Crusor never sets."
        ),
        "blocked_because": "Tool registration lives in the third-party MCP server.",
    },
    {
        "id": "U5",
        "area": "host permissions",
        "finding": "manifest.json declares http://*/* and https://*/* host permissions.",
        "required_change": "Reduce to the Flow/Labs hosts actually used and document each one.",
        "blocked_because": "manifest.json is part of the third-party package.",
    },
    {
        "id": "U6",
        "area": "ambiguous submit retry",
        "finding": (
            "fastFillAndSubmitPrompt re-modifies the prompt and retries once when it "
            "cannot observe that submission succeeded."
        ),
        "required_change": (
            "After the submit action is dispatched, report UNKNOWN_SUBMISSION and stop; "
            "never resubmit automatically."
        ),
        "blocked_because": "The retry lives in service-worker.js.",
    },
    {
        "id": "U7",
        "area": "asset delivery",
        "finding": (
            "deepx_download_media returns {downloadId, status} and never hands bytes to "
            "Crusor; generation returns URLs that a remote backend cannot fetch."
        ),
        "required_change": (
            "Implement export_asset: return the media bytes (or stream them to a "
            "companion-issued one-time URL) for a correlated job."
        ),
        "blocked_because": "Requires a new command in the extension service worker.",
    },
)


class SubmissionState(str, Enum):
    """Submission outcome. ``FAILED`` and ``UNKNOWN_SUBMISSION`` are different things.

    A failure means the provider told us the attempt did not happen (or failed after
    it started and reported so). An unknown submission means the dispatch was sent
    and we cannot prove what became of it — resubmitting could create a second,
    billable generation, so it is never automatic.
    """

    PENDING = "pending"
    DISPATCHED = "dispatched"
    SUBMITTED = "submitted"
    #: The provider confirmed the job exists and is still working on it.
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN_SUBMISSION = "unknown_submission"
    CANCELLED = "cancelled"


class FlowLifecycle(str, Enum):
    """The eight submission states required by Phase 3C.2 §8.

    ``SubmissionState`` remains the coordinator's internal vocabulary; this is the
    canonical state machine every surface reports, so a UI or an operator sees the
    same eight names no matter which transport produced them.
    """

    NOT_SUBMITTED = "NOT_SUBMITTED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN_SUBMISSION = "UNKNOWN_SUBMISSION"
    CANCELLED = "CANCELLED"


#: Legal transitions. Everything not listed is refused by :func:`transition`.
#:
#: Note the shape: once a job leaves ``SUBMITTING`` it can only reach
#: ``SUBMITTED``/``RUNNING``/``SUCCEEDED``, or fall into ``UNKNOWN_SUBMISSION``.
#: There is deliberately **no** edge from ``UNKNOWN_SUBMISSION`` back to
#: ``NOT_SUBMITTED``: an ambiguous attempt can never be re-armed for dispatch.
LEGAL_TRANSITIONS: dict[FlowLifecycle, frozenset[FlowLifecycle]] = {
    FlowLifecycle.NOT_SUBMITTED: frozenset(
        {FlowLifecycle.SUBMITTING, FlowLifecycle.CANCELLED, FlowLifecycle.FAILED}
    ),
    FlowLifecycle.SUBMITTING: frozenset(
        {
            FlowLifecycle.SUBMITTED,
            FlowLifecycle.RUNNING,
            FlowLifecycle.SUCCEEDED,
            FlowLifecycle.FAILED,
            FlowLifecycle.UNKNOWN_SUBMISSION,
        }
    ),
    FlowLifecycle.SUBMITTED: frozenset(
        {
            FlowLifecycle.RUNNING,
            FlowLifecycle.SUCCEEDED,
            FlowLifecycle.FAILED,
            FlowLifecycle.UNKNOWN_SUBMISSION,
            FlowLifecycle.CANCELLED,
        }
    ),
    FlowLifecycle.RUNNING: frozenset(
        {
            FlowLifecycle.SUCCEEDED,
            FlowLifecycle.FAILED,
            FlowLifecycle.UNKNOWN_SUBMISSION,
            FlowLifecycle.CANCELLED,
        }
    ),
    FlowLifecycle.SUCCEEDED: frozenset(),
    FlowLifecycle.FAILED: frozenset({FlowLifecycle.NOT_SUBMITTED}),  # explicit re-arm only
    FlowLifecycle.UNKNOWN_SUBMISSION: frozenset({FlowLifecycle.SUCCEEDED, FlowLifecycle.FAILED}),
    FlowLifecycle.CANCELLED: frozenset(),
}

#: Internal coordinator state -> canonical lifecycle state.
_STATE_TO_LIFECYCLE: dict["SubmissionState", FlowLifecycle] = {
    SubmissionState.PENDING: FlowLifecycle.NOT_SUBMITTED,
    SubmissionState.DISPATCHED: FlowLifecycle.SUBMITTING,
    SubmissionState.SUBMITTED: FlowLifecycle.SUBMITTED,
    SubmissionState.RUNNING: FlowLifecycle.RUNNING,
    SubmissionState.COMPLETED: FlowLifecycle.SUCCEEDED,
    SubmissionState.FAILED: FlowLifecycle.FAILED,
    SubmissionState.UNKNOWN_SUBMISSION: FlowLifecycle.UNKNOWN_SUBMISSION,
    SubmissionState.CANCELLED: FlowLifecycle.CANCELLED,
}


def lifecycle_for(state: "SubmissionState") -> FlowLifecycle:
    return _STATE_TO_LIFECYCLE[state]


def transition(current: FlowLifecycle, target: FlowLifecycle) -> FlowLifecycle:
    """Return ``target`` if the move is legal, else raise :class:`IllegalTransition`."""
    if current is target:
        return target
    if target not in LEGAL_TRANSITIONS[current]:
        raise IllegalTransition(
            f"{current.value} -> {target.value} is not a legal generation transition.",
            details={"from": current.value, "to": target.value},
        )
    return target


TERMINAL_SUBMISSION_STATES = frozenset(
    {
        SubmissionState.COMPLETED,
        SubmissionState.FAILED,
        SubmissionState.UNKNOWN_SUBMISSION,
        SubmissionState.CANCELLED,
    }
)

#: States from which an automatic retry is permitted. ``UNKNOWN_SUBMISSION`` is
#: absent on purpose — it is the whole point of the state.
RETRYABLE_SUBMISSION_STATES = frozenset({SubmissionState.FAILED})


class FlowProtocolError(Exception):
    """A protocol violation: bad message, unknown command, or missing binding."""

    code = "flow_protocol_error"

    def __init__(self, message: str = "", *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class CommandDenied(FlowProtocolError):
    """The command is real but not on Crusor's allowlist."""

    code = "command_not_allowed"
    #: Phase 3C.2.3.2 — the allowlist is checked before any socket operation, so this
    #: refusal is provable non-delivery and never carries a billing consequence.
    delivery = DeliveryOutcome.DEFINITELY_NOT_SENT


class UnsafeBinding(FlowProtocolError):
    """The result cannot be tied to the job that asked for it."""

    code = "unsafe_result_binding"


class IllegalTransition(FlowProtocolError):
    """A state change the state machine does not allow."""

    code = "flow_illegal_transition"


# --------------------------------------------------------------------------- #
# message shapes
# --------------------------------------------------------------------------- #
class HelloMessage(BaseModel):
    """First frame on the WebSocket. Nothing is dispatched before it validates.

    Phase 3C.2 adds provenance: the extension must report the version of *every*
    component it ships plus the commit and build identifier it was produced from.
    A HELLO that merely claims a version is not evidence, so the fields are
    cross-checked by :func:`app.flow.provenance.verify_provenance`.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["hello"] = "hello"
    protocol: str = PROTOCOL_VERSION
    pairing_id: str = Field(min_length=1, max_length=128)
    pairing_secret: str = Field(min_length=16, max_length=256)
    extension_id: str = Field(default="", max_length=128)
    extension_version: str = Field(default="", max_length=64)
    #: Phase 3C.2 provenance — required by the bridge, validated by verify_provenance.
    service_worker_version: str = Field(default="", max_length=64)
    mcp_version: str = Field(default="", max_length=64)
    source_commit: str = Field(default="", max_length=64)
    build_id: str = Field(default="", max_length=128)
    package_sha256: str = Field(default="", max_length=64)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    flow_tab_present: bool = False

    @field_validator("protocol")
    @classmethod
    def _protocol(cls, value: str) -> str:
        if value not in SUPPORTED_PROTOCOLS:
            raise ValueError(f"unsupported protocol {value!r}")
        return value


class HeartbeatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["heartbeat"] = "heartbeat"
    flow_tab_present: bool = False
    busy: bool = False


class CommandEnvelope(BaseModel):
    """Bridge -> extension."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["command"] = "command"
    id: str
    command: str
    job_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    issued_at: str
    expires_at: str
    request_hash: str = ""


class ResultEnvelope(BaseModel):
    """Extension -> bridge."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["result"] = "result"
    id: str
    ok: bool = False
    job_id: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None


class GenerationCommand(BaseModel):
    """One generation attempt. ``count`` is fixed at 1 by design."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=4000)
    negative_prompt: str = Field(default="", max_length=2000)
    aspect_ratio: str = Field(default="4:5", max_length=16)
    model: str = Field(default="", max_length=64)
    duration_seconds: int = Field(default=0, ge=0, le=120)
    count: Literal[1] = 1
    output_kind: Literal["image", "video"] = "image"
    idempotency_key: str = Field(default="", max_length=128)
    reference_media_ids: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("prompt")
    @classmethod
    def _no_control_chars(cls, value: str) -> str:
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value):
            raise ValueError("prompt contains control characters")
        return value

    def prompt_hash(self) -> str:
        """Stable hash of everything that changes what the provider generates."""
        canonical = "|".join(
            [
                self.output_kind,
                self.prompt,
                self.negative_prompt,
                self.aspect_ratio,
                self.model,
                str(self.duration_seconds),
                str(self.count),
            ]
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def request_hash(self) -> str:
        canonical = "|".join(
            [self.prompt_hash(), self.idempotency_key, ",".join(sorted(self.reference_media_ids))]
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ExportOffer(BaseModel):
    """What the extension says it is about to hand over, before the bytes arrive."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    media_id: str = Field(min_length=1, max_length=512)
    content_type: str = Field(min_length=3, max_length=128)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)


class ExportPayload(BaseModel):
    """Verified bytes, ready for the companion to upload to Crusor."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    media_id: str
    content_type: str
    sha256: str
    size_bytes: int
    data: bytes = Field(repr=False)


class FlowErrorCode(str, Enum):
    """Normalised error vocabulary. Upstream text never reaches Crusor verbatim."""

    BRIDGE_UNAVAILABLE = "flow_bridge_unavailable"
    NOT_PAIRED = "flow_not_paired"
    AUTH_FAILED = "flow_auth_failed"
    EXTENSION_DISCONNECTED = "flow_extension_disconnected"
    FLOW_TAB_MISSING = "flow_tab_missing"
    COMMAND_TIMEOUT = "flow_command_timeout"
    COMMAND_FAILED = "flow_command_failed"
    JOB_IN_PROGRESS = "flow_job_in_progress"
    UNKNOWN_SUBMISSION = "flow_unknown_submission"
    FOREIGN_RESULT = "flow_foreign_result"
    EXPORT_UNVERIFIED = "flow_export_unverified"
    LIVE_GENERATION_DISABLED = "flow_live_generation_disabled"
    APPROVAL_REQUIRED = "flow_approval_required"


@dataclass(slots=True)
class ErrorDescriptor:
    code: FlowErrorCode
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)


#: Upstream error strings -> Crusor vocabulary. Anything unmatched becomes
#: ``COMMAND_FAILED`` with ``retryable=False``: an unknown failure is never retried
#: automatically.
_ERROR_MAP: tuple[tuple[re.Pattern[str], FlowErrorCode, str, bool], ...] = (
    (re.compile(r"not\s*paired|unauthori[sz]ed|forbidden|403", re.I), FlowErrorCode.AUTH_FAILED, "The extension rejected the pairing credential.", False),
    (re.compile(r"no\s*flow\s*tab|tab\s*(?:not|missing)", re.I), FlowErrorCode.FLOW_TAB_MISSING, "No Flow tab is open in the paired browser.", False),
    (re.compile(r"timeout|timed\s*out", re.I), FlowErrorCode.COMMAND_TIMEOUT, "The extension did not answer in time.", False),
    (re.compile(r"disconnect|socket\s*closed|bridge\s*down", re.I), FlowErrorCode.EXTENSION_DISCONNECTED, "The extension connection dropped.", False),
    (re.compile(r"download|asset|export", re.I), FlowErrorCode.EXPORT_UNVERIFIED, "The media could not be exported for verification.", False),
)


def normalise_error(error: dict[str, Any] | str | None) -> ErrorDescriptor:
    """Map an upstream error to Crusor's vocabulary, never echoing raw text.

    The raw string is kept in ``details['upstream_code']`` only when it looks like a
    short code (no whitespace, ≤ 64 chars), so a stray dump of a request — possibly
    containing a bearer token — cannot flow back into logs or the UI.
    """
    if error is None:
        return ErrorDescriptor(FlowErrorCode.COMMAND_FAILED, "The command failed.")
    if isinstance(error, str):
        raw, code_hint = error, ""
    else:
        raw = str(error.get("message") or error.get("error") or "")
        code_hint = str(error.get("code") or "")

    haystack = f"{code_hint} {raw}"
    for pattern, code, message, retryable in _ERROR_MAP:
        if pattern.search(haystack):
            return ErrorDescriptor(code, message, retryable=retryable)

    details: dict[str, Any] = {}
    if code_hint and len(code_hint) <= 64 and not re.search(r"\s", code_hint):
        details["upstream_code"] = code_hint
    return ErrorDescriptor(
        FlowErrorCode.COMMAND_FAILED,
        "The extension reported a failure.",
        retryable=False,
        details=details,
    )


def command_allowed(command: str) -> bool:
    return command in ALLOWED_COMMANDS


def deny_reason(command: str) -> str:
    """Why a command is refused. Unknown names get a generic reason, not an echo."""
    if command in DENIED_COMMANDS:
        return DENIED_COMMANDS[command]
    if command in ALLOWED_COMMANDS:
        return ""
    return "Unknown command: Crusor only issues the five allowlisted operations."


def ensure_allowed(command: str) -> str:
    if not command_allowed(command):
        raise CommandDenied(
            deny_reason(command), details={"command": command, "allowed": list(ALLOWED_COMMANDS)}
        )
    return command


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime | None = None) -> str:
    return (moment or utcnow()).isoformat()
