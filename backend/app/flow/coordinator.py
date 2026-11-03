"""Generation state machine for a single Flow job (Phase 3C.1).

The coordinator owns the safety rules that the upstream extension does not:

* **one attempt per request** — ``count`` is pinned to 1 and no loop submits twice;
* **one live job at a time** — a second submission is refused while one is open;
* **``UNKNOWN_SUBMISSION`` after dispatch** — once a submit action has been written
  to the transport, a timeout, a dropped socket or an unreadable response can never
  become an automatic resubmission. ``FAILED`` is reserved for attempts that the
  provider *told us* did not happen;
* **correlation** — results must echo the job id they belong to, and a media
  fingerprint already claimed by another job can never satisfy a second one, so a
  stale or unrelated Flow tile is rejected (``flow_foreign_result``);
* **export verification** — bytes are hashed and sized before they are offered to
  Crusor; nothing is marked stored on the strength of a URL or a download id.

I/O lives behind :class:`CommandTransport`, which is why this module can be tested
with mocked extension messages while the real bridge — and the real extension —
remain unused in this phase.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Literal, Protocol

from app.flow import protocol as proto
from app.flow.protocol import (
    CommandEnvelope,
    ErrorDescriptor,
    ExportPayload,
    FlowErrorCode,
    GenerationCommand,
    ResultEnvelope,
    SubmissionState,
    TERMINAL_SUBMISSION_STATES,
    iso,
    new_id,
    normalise_error,
    utcnow,
)

ALLOWED_EXPORT_TYPES = ("image/png", "image/jpeg", "image/webp", "video/mp4", "video/webm")

_MEDIA_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-. :/]{1,512}$")


class CoordinatorError(Exception):
    """Base class: every failure has a stable code and a safe message."""

    code: str = "flow_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class LiveGenerationDisabled(CoordinatorError):
    code = FlowErrorCode.LIVE_GENERATION_DISABLED.value

    def __init__(self) -> None:
        super().__init__(
            "Live Flow generation is disabled. Phase 3C.1 hardens the bridge and the "
            "protocol only; a canary requires Phase 3C.2 acceptance and explicit "
            "founder authorisation.",
            details={"enabled": False, "phase": "3C.1"},
        )


class ApprovalRequired(CoordinatorError):
    code = FlowErrorCode.APPROVAL_REQUIRED.value

    def __init__(self, message: str = "A founder approval binding is required.") -> None:
        super().__init__(message)


class NotPaired(CoordinatorError):
    """No authenticated extension session is attached to the bridge."""

    code = FlowErrorCode.NOT_PAIRED.value

    def __init__(self) -> None:
        super().__init__(
            "No paired Flow extension is connected. Pair the browser extension and "
            "retry once it reports an authenticated session.",
            details={"paired": False},
        )


class JobInProgress(CoordinatorError):
    code = FlowErrorCode.JOB_IN_PROGRESS.value

    def __init__(self, job_id: str, live: int) -> None:
        super().__init__(
            "Another Flow generation is already live. Crusor serialises generation; "
            "wait for it to settle or be reconciled.",
            details={"live_job_id": job_id, "live_jobs": live},
        )


class UnknownSubmissionRemains(CoordinatorError):
    code = FlowErrorCode.UNKNOWN_SUBMISSION.value

    def __init__(self, job_id: str) -> None:
        super().__init__(
            "This job was dispatched but its outcome is unknown. It is never "
            "resubmitted automatically: reconcile it in the Flow tab first.",
            details={"job_id": job_id, "auto_retry": False},
        )


class ForeignResult(CoordinatorError):
    code = FlowErrorCode.FOREIGN_RESULT.value

    def __init__(self, reason: str, *, job_id: str = "", details: dict[str, Any] | None = None) -> None:
        super().__init__(
            f"The provider result cannot be attributed to this job ({reason}).",
            details={"job_id": job_id, **(details or {})},
        )


class ExportUnverified(CoordinatorError):
    code = FlowErrorCode.EXPORT_UNVERIFIED.value


class IdempotencyConflict(CoordinatorError):
    """The same idempotency key was reused for a different request (3C.2 §8)."""

    code = "flow_idempotency_conflict"

    def __init__(self, key: str, *, job_id: str = "") -> None:
        super().__init__(
            "This idempotency key already belongs to a different generation request. "
            "Use a new key for a new prompt, or replay the original request unchanged.",
            details={"idempotency_key": key[:64], "job_id": job_id},
        )


class CancellationAmbiguous(CoordinatorError):
    """A job was already dispatched, so cancelling cannot undo the attempt."""

    code = "flow_cancel_ambiguous"

    def __init__(self, job_id: str, state: str) -> None:
        super().__init__(
            "This generation was already dispatched. Cancelling it locally cannot prove "
            "Google did not start it, so the job is marked UNKNOWN_SUBMISSION and must "
            "be reconciled rather than retried.",
            details={"job_id": job_id, "state": state},
        )


class ReconciliationRejected(CoordinatorError):
    """Evidence offered for a job does not actually belong to that job."""

    code = "flow_reconciliation_rejected"


#: Phase 3C.2.3: one class, not two. The coordinator used to define its own
#: ``UntrustedArtifact``, so a caller that caught the trust module's class let the
#: coordinator's copy escape — a refusal that looked handled and was not.
from app.flow.trust import UntrustedArtifact  # noqa: E402  (after proto import)


class CommandTransport(Protocol):
    """The only I/O the coordinator needs. The bridge implements it over WS."""

    async def send(self, envelope: CommandEnvelope) -> None: ...

    async def await_result(self, command_id: str, timeout: float) -> ResultEnvelope: ...


def media_fingerprint(job_id: str, media_id: str, prompt_hash: str) -> str:
    """Bind a provider media id to the job *and* the prompt that produced it."""
    return hashlib.sha256(f"{job_id}|{media_id}|{prompt_hash}".encode("utf-8")).hexdigest()


@dataclass
class FlowJob:
    job_id: str
    kind: Literal["image", "video"]
    request: GenerationCommand
    approval_binding: str
    state: SubmissionState = SubmissionState.PENDING
    dispatched: bool = False
    attempts: int = 0
    media_id: str = ""
    media_fingerprint: str = ""
    output_url: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    error: ErrorDescriptor | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def lifecycle(self) -> proto.FlowLifecycle:
        """Canonical eight-state view of this job (Phase 3C.2 §8)."""
        return proto.lifecycle_for(self.state)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_SUBMISSION_STATES

    @property
    def live(self) -> bool:
        return not self.terminal

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "state": self.state.value,
            "lifecycle": self.lifecycle.value,
            "dispatched": self.dispatched,
            "attempts": self.attempts,
            "prompt_hash": self.request.prompt_hash(),
            "request_hash": self.request_hash,
            "approval_binding": self.approval_binding,
            "media_id": self.media_id,
            "media_fingerprint": self.media_fingerprint,
            "output_url": self.output_url,
            "error": (
                {
                    "code": self.error.code.value,
                    "message": self.error.message,
                    "retryable": self.error.retryable,
                    "details": self.error.details,
                }
                if self.error
                else None
            ),
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }

    @property
    def request_hash(self) -> str:
        return self.request.request_hash()


class GenerationCoordinator:
    """Serialised, correlation-checked generation over an abstract transport."""

    def __init__(
        self,
        transport: CommandTransport,
        *,
        live_generation_enabled: bool = False,
        trust_provider: Callable[[], Any] | None = None,
        budget_ledger: Any = None,
        max_live_jobs: int = 1,
        command_timeout: float = 120.0,
        export_max_bytes: int = 25 * 1024 * 1024,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if max_live_jobs < 1:
            raise ValueError("max_live_jobs must be >= 1")
        self.transport = transport
        self.live_generation_enabled = live_generation_enabled
        #: Phase 3C.2.2 §9: when supplied, a generation may only proceed while the
        #: provider reports a VERIFIED installed artifact. The bridge always supplies
        #: one; tests that exercise the lifecycle in isolation do not need to.
        self.trust_provider = trust_provider
        #: Phase 3C.2.3 §9: a real reservation ledger, not a descriptive boolean.
        self.budget_ledger = budget_ledger
        self.max_live_jobs = max_live_jobs
        self.command_timeout = command_timeout
        self.export_max_bytes = export_max_bytes
        self.clock = clock
        self._jobs: dict[str, FlowJob] = {}
        self._by_request: dict[str, str] = {}
        self._by_media: dict[str, str] = {}
        #: media id -> owning job. A tile belongs to exactly one job, so a stale or
        #: unrelated tile in another tab can never be claimed as this job's output.
        self._media_owner: dict[str, str] = {}
        #: idempotency key -> (job id, request hash). A key may only ever map to one
        #: request; reuse with a different payload is refused, not silently merged.
        self._by_idempotency: dict[str, tuple[str, str]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ views
    @property
    def jobs(self) -> list[FlowJob]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    @property
    def idempotency_enforced(self) -> bool:
        """Whether duplicate-paid-generation protection is actually active.

        Derived from the mechanism the submit path uses (the key→request map and the
        request-hash guard), not from a value a caller passes in.
        """
        return self._by_idempotency is not None and self._by_request is not None

    @property
    def live_jobs(self) -> list[FlowJob]:
        return [job for job in self._jobs.values() if job.live]

    def get(self, job_id: str) -> FlowJob | None:
        return self._jobs.get(job_id)

    def lifecycle_counts(self) -> dict[str, int]:
        counts = {state.value: 0 for state in proto.FlowLifecycle}
        for job in self._jobs.values():
            counts[job.lifecycle.value] += 1
        return counts

    def snapshot(self) -> dict[str, Any]:
        return {
            "live_generation_enabled": self.live_generation_enabled,
            "max_live_jobs": self.max_live_jobs,
            "live_jobs": len(self.live_jobs),
            "lifecycle_counts": self.lifecycle_counts(),
            "jobs": [job.as_dict() for job in self.jobs[:20]],
        }

    # --------------------------------------------------------------- submit
    async def submit(
        self,
        kind: Literal["image", "video"],
        request: GenerationCommand,
        *,
        approval_binding: str,
        job_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> FlowJob:
        """Send exactly one attempt. Never loops, never rewrites the prompt.

        ``idempotency_key`` is the duplicate-paid-generation guard from Phase 3C.2
        §8: one key maps to one request for the lifetime of the coordinator. Reuse
        with an identical payload replays the existing job and writes nothing to the
        socket; reuse with a different payload is refused outright rather than
        silently folding two different generations into one approval.
        """
        if not self.live_generation_enabled:
            # Checked before anything touches a transport: no request is built,
            # no command is written, no socket is opened.
            raise LiveGenerationDisabled()
        # Every refusal below happens *before* a reservation and before a request
        # is built, so a blocked generation costs nothing and writes nothing.
        if self.trust_provider is not None:
            decision = self.trust_provider()
            if not getattr(decision, "trusted", False) or not getattr(
                decision, "is_fresh", lambda: True
            )():
                state = getattr(decision, "state", "UNVERIFIED")
                raise UntrustedArtifact(
                    "The installed extension is not verified, so no generation may be "
                    f"dispatched (trust state {state}).",
                    details={"trust_state": state},
                )
        if not approval_binding:
            raise ApprovalRequired()
        # Phase 3C.2.3 §9: a real reservation against a real cap. With no ledger the
        # caller holds no budget control, and the live guard refuses before we get
        # here; a ledger that cannot cover the estimate raises rather than guessing.
        estimate = self.budget_ledger.reserve(kind=kind) if self.budget_ledger is not None else 0.0

        key = (idempotency_key or request.idempotency_key or "").strip()
        request_hash = request.request_hash()

        async with self._lock:
            if key:
                existing = self._by_idempotency.get(key)
                if existing is not None:
                    existing_job_id, existing_hash = existing
                    if existing_hash != request_hash:
                        raise IdempotencyConflict(key, job_id=existing_job_id)
                    return self._jobs[existing_job_id]
            replay = self._by_request.get(request_hash)
            if replay:
                return self._jobs[replay]
            if self.live_jobs:
                current = self.live_jobs[0]
                raise JobInProgress(current.job_id, len(self.live_jobs))

            job = FlowJob(
                job_id=job_id or new_id(),
                kind=kind,
                request=request,
                approval_binding=approval_binding,
                created_at=self.clock(),
                updated_at=self.clock(),
            )
            self._jobs[job.job_id] = job
            self._by_request[request_hash] = job.job_id
            if key:
                self._by_idempotency[key] = (job.job_id, request_hash)

        envelope = CommandEnvelope(
            id=new_id(),
            command="generate_image" if kind == "image" else "generate_video",
            job_id=job.job_id,
            payload={
                **request.model_dump(),
                "count": 1,
                "one_attempt_only": True,
            },
            issued_at=iso(self.clock()),
            expires_at=iso(
                datetime.fromtimestamp(
                    self.clock().timestamp() + self.command_timeout, tz=self.clock().tzinfo
                )
            ),
            request_hash=request.request_hash(),
        )

        try:
            await self.transport.send(envelope)
        except Exception as exc:  # noqa: BLE001 - transport failure before dispatch
            job.state = SubmissionState.FAILED
            job.dispatched = False
            job.attempts = 1
            job.error = ErrorDescriptor(
                FlowErrorCode.EXTENSION_DISCONNECTED,
                "The command could not be delivered, so no generation was dispatched.",
                retryable=True,
                details={"transport": type(exc).__name__},
            )
            job.updated_at = self.clock()
            return job

        # From this line on, the submit action exists in the world.
        job.dispatched = True
        job.attempts = 1
        job.state = SubmissionState.DISPATCHED
        job.updated_at = self.clock()

        try:
            result = await self.transport.await_result(envelope.id, self.command_timeout)
        except asyncio.TimeoutError:
            return self._mark_unknown(job, "the extension did not answer after dispatch")
        except Exception as exc:  # noqa: BLE001 - dropped socket, decode error, ...
            return self._mark_unknown(job, f"transport failed after dispatch ({type(exc).__name__})")

        return self._apply_submission_result(job, result)

    def _apply_submission_result(self, job: FlowJob, result: ResultEnvelope) -> FlowJob:
        if result.job_id and result.job_id != job.job_id:
            return self._mark_unknown(
                job, "the extension answered with a different job id", foreign=result.job_id
            )

        if result.ok:
            job.result = dict(result.result)
            media_id = str(result.result.get("media_id") or "")
            if media_id:
                if not _MEDIA_ID_PATTERN.match(media_id):
                    return self._mark_unknown(job, "the media id was malformed")
                self._claim_media(job, media_id)
                job.state = SubmissionState.SUBMITTED
            else:
                # Submitted but the UI path has no durable media id yet: this is
                # still not a completed job, and it is *not* a failure either.
                job.state = SubmissionState.SUBMITTED
            job.updated_at = self.clock()
            return job

        raw_error = result.error or {}
        dispatched = raw_error.get("dispatched") if isinstance(raw_error, dict) else None
        descriptor = normalise_error(raw_error)
        if dispatched is False:
            # The provider says the submit never happened: a retry is safe, so the
            # dispatch flag is cleared alongside the state. Absent an explicit
            # ``dispatched: false`` the attempt stays ambiguous by construction.
            job.dispatched = False
            job.state = SubmissionState.FAILED
            job.error = ErrorDescriptor(
                descriptor.code, descriptor.message, retryable=True, details=descriptor.details
            )
        else:
            job.state = SubmissionState.UNKNOWN_SUBMISSION
            job.error = ErrorDescriptor(
                FlowErrorCode.UNKNOWN_SUBMISSION,
                "The submit action was dispatched and its outcome is unknown. Crusor "
                "will not resubmit it automatically.",
                retryable=False,
                details={"upstream_code": descriptor.details.get("upstream_code", "")},
            )
        job.updated_at = self.clock()
        return job

    def _mark_unknown(
        self, job: FlowJob, reason: str, *, foreign: str = ""
    ) -> FlowJob:
        job.state = SubmissionState.UNKNOWN_SUBMISSION
        job.error = ErrorDescriptor(
            FlowErrorCode.UNKNOWN_SUBMISSION,
            "The generation was dispatched but its outcome could not be confirmed. "
            "It will not be resubmitted automatically.",
            retryable=False,
            details={"reason": reason, "foreign_job_id": foreign} if foreign else {"reason": reason},
        )
        job.updated_at = self.clock()
        return job

    def _claim_media(self, job: FlowJob, media_id: str) -> None:
        fingerprint = media_fingerprint(job.job_id, media_id, job.request.prompt_hash())
        owner = self._media_owner.get(media_id) or self._by_media.get(fingerprint)
        if owner and owner != job.job_id:
            raise ForeignResult("that media id already belongs to another job", job_id=job.job_id)
        self._media_owner[media_id] = job.job_id
        self._by_media[fingerprint] = job.job_id
        job.media_id = media_id
        job.media_fingerprint = fingerprint

    # ---------------------------------------------------------- status poll
    async def check_status(self, job_id: str) -> FlowJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise CoordinatorError("Unknown Flow job.", details={"job_id": job_id})
        if job.terminal or job.state is SubmissionState.SUBMITTED:
            if job.state is SubmissionState.SUBMITTED:
                return await self._poll(job)
            return job
        return await self._poll(job)

    async def _poll(self, job: FlowJob) -> FlowJob:
        if job.state is SubmissionState.DISPATCHED:
            # A dispatched-but-unconfirmed job is polled for evidence of what
            # happened; polling never re-submits anything.
            pass
        if job.kind != "video":
            # Image generation in the upstream UI path exposes no durable status
            # endpoint; the job stays SUBMITTED until an export proves completion.
            return job
        envelope = CommandEnvelope(
            id=new_id(),
            command="check_video_status",
            job_id=job.job_id,
            payload={"job_id": job.job_id, "media_id": job.media_id},
            issued_at=iso(self.clock()),
            expires_at=iso(self.clock()),
        )
        await self.transport.send(envelope)
        try:
            result = await self.transport.await_result(envelope.id, self.command_timeout)
        except Exception:  # noqa: BLE001
            return job

        if result.job_id and result.job_id != job.job_id:
            raise ForeignResult("status answer carried a different job id", job_id=job.job_id)

        if result.ok:
            payload = dict(result.result)
            status = str(payload.get("status") or "").lower()
            media_id = str(payload.get("media_id") or "")
            if media_id and media_id != job.media_id:
                fingerprint = media_fingerprint(job.job_id, media_id, job.request.prompt_hash())
                if fingerprint in self._by_media and self._by_media[fingerprint] != job.job_id:
                    raise ForeignResult("status answer carried a foreign media id", job_id=job.job_id)
                self._claim_media(job, media_id)
            job.result = payload
            if status in {"running", "in_progress", "queued"}:
                proto.transition(job.lifecycle, proto.FlowLifecycle.RUNNING)
                job.state = SubmissionState.RUNNING
            elif status in {"failed", "error"}:
                job.state = SubmissionState.FAILED
                job.error = ErrorDescriptor(
                    FlowErrorCode.COMMAND_FAILED, "The provider reported a failed generation."
                )
            elif status in {"succeeded", "completed", "ready"}:
                job.state = SubmissionState.COMPLETED
            else:
                job.state = SubmissionState.SUBMITTED
        else:
            descriptor = normalise_error(result.error)
            job.error = descriptor
            job.state = SubmissionState.FAILED
        job.updated_at = self.clock()
        return job

    # ----------------------------------------------------------------- retry
    async def retry(self, job_id: str, *, approval_binding: str) -> FlowJob:
        """Retry only a proven no-dispatch failure. Unknown submissions stay put."""
        job = self._jobs.get(job_id)
        if job is None:
            raise CoordinatorError("Unknown Flow job.", details={"job_id": job_id})
        if job.state is SubmissionState.UNKNOWN_SUBMISSION:
            raise UnknownSubmissionRemains(job.job_id)
        if job.state is not SubmissionState.FAILED:
            raise CoordinatorError(
                "Only a failed attempt can be retried.",
                details={"state": job.state.value},
            )
        if job.dispatched:
            # Even a FAILED state that was dispatched is ambiguous by construction.
            job.state = SubmissionState.UNKNOWN_SUBMISSION
            raise UnknownSubmissionRemains(job.job_id)
        if not self.live_generation_enabled:
            raise LiveGenerationDisabled()
        if not approval_binding:
            raise ApprovalRequired()

        job.attempts += 1
        job.state = SubmissionState.PENDING
        job.approval_binding = approval_binding
        job.error = None

        envelope = CommandEnvelope(
            id=new_id(),
            command="generate_image" if job.kind == "image" else "generate_video",
            job_id=job.job_id,
            payload={**job.request.model_dump(), "count": 1, "retry_attempt": job.attempts},
            issued_at=iso(self.clock()),
            expires_at=iso(self.clock()),
            request_hash=job.request.request_hash(),
        )
        try:
            await self.transport.send(envelope)
        except Exception:  # noqa: BLE001
            job.state = SubmissionState.FAILED
            job.error = ErrorDescriptor(
                FlowErrorCode.EXTENSION_DISCONNECTED,
                "The retry could not be delivered; no generation was dispatched.",
                retryable=True,
            )
            job.updated_at = self.clock()
            return job

        job.dispatched = True
        job.state = SubmissionState.DISPATCHED
        try:
            result = await self.transport.await_result(envelope.id, self.command_timeout)
        except asyncio.TimeoutError:
            return self._mark_unknown(job, "no answer after retry dispatch")
        except Exception as exc:  # noqa: BLE001
            return self._mark_unknown(job, f"transport failed after retry ({type(exc).__name__})")
        return self._apply_submission_result(job, result)

    # ---------------------------------------------------------------- cancel
    async def cancel(self, job_id: str) -> FlowJob:
        """Stop tracking a job without ever implying the provider did not start it.

        * ``NOT_SUBMITTED`` — nothing was dispatched, so ``CANCELLED`` is a fact.
        * ``SUBMITTING`` — the frame left the socket; raising
          :class:`CancellationAmbiguous` after marking it ``UNKNOWN_SUBMISSION`` is
          the only honest answer, because a local cancel cannot recall a submit.
        * ``SUBMITTED``/``RUNNING`` — Google has the job. It is marked ``CANCELLED``
          for Crusor's purposes with an explicit note that the provider may still
          finish, and it is not retryable.
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise CoordinatorError("Unknown Flow job.", details={"job_id": job_id})
        if job.terminal:
            raise CoordinatorError(
                "This job is already terminal.", details={"state": job.state.value}
            )

        if job.state is SubmissionState.DISPATCHED:
            job.state = SubmissionState.UNKNOWN_SUBMISSION
            job.error = ErrorDescriptor(
                FlowErrorCode.UNKNOWN_SUBMISSION,
                "Cancelled while the submit was in flight. The outcome is unknown and "
                "will not be resubmitted automatically.",
                retryable=False,
                details={"reason": "cancelled_during_submit"},
            )
            job.updated_at = self.clock()
            raise CancellationAmbiguous(job.job_id, job.state.value)

        if job.state in {SubmissionState.SUBMITTED, SubmissionState.RUNNING}:
            job.state = SubmissionState.CANCELLED
            job.error = ErrorDescriptor(
                FlowErrorCode.COMMAND_FAILED,
                "Cancelled after submission. The provider may still complete the "
                "generation; any media that appears is not attached automatically.",
                retryable=False,
                details={"provider_may_still_complete": True},
            )
        else:
            job.state = SubmissionState.CANCELLED
            job.error = None
        job.updated_at = self.clock()
        return job

    # ----------------------------------------------------------- reconcile
    def reconcile(
        self,
        job_id: str,
        *,
        request_hash: str = "",
        media_id: str = "",
        status: str = "",
        evidence_source: str = "operator",
    ) -> FlowJob:
        """Attach verified evidence to a job. Success needs a *correlated* media id.

        Reconciliation is the only path out of ``UNKNOWN_SUBMISSION``, and it is
        deliberately one-way: it can confirm a success when the evidence carries a
        media id belonging to this job, and it can leave the job unknown. It never
        manufactures a failure, because a manufactured failure is a retry, and a
        retry of an ambiguous submit is a second billable generation.
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise CoordinatorError("Unknown Flow job.", details={"job_id": job_id})
        if request_hash and request_hash != job.request_hash:
            raise ReconciliationRejected(
                "The evidence belongs to a different request.",
                details={"job_id": job_id},
            )

        if media_id:
            if not _MEDIA_ID_PATTERN.match(media_id):
                raise ReconciliationRejected(
                    "The media id in the evidence is malformed.", details={"job_id": job_id}
                )
            try:
                self._claim_media(job, media_id)
            except ForeignResult as exc:
                raise ReconciliationRejected(
                    "That media id already belongs to another job, so it cannot "
                    "confirm this one.",
                    details={"job_id": job_id, **exc.details},
                ) from exc

        normalised = status.strip().lower()
        if normalised in {"succeeded", "completed", "ready"}:
            if not job.media_id:
                job.result = {**job.result, "reconciliation": "refused_without_media_id"}
                job.updated_at = self.clock()
                raise ReconciliationRejected(
                    "A success claim needs a correlated media id; none was supplied.",
                    details={"job_id": job_id},
                )
            if job.state is SubmissionState.UNKNOWN_SUBMISSION:
                proto.transition(job.lifecycle, proto.FlowLifecycle.SUCCEEDED)
            job.state = SubmissionState.COMPLETED
            job.error = None
            job.result = {
                **job.result,
                "reconciled": True,
                "evidence_source": evidence_source,
                "status": normalised,
            }
            job.updated_at = self.clock()
            return job

        job.result = {
            **job.result,
            "reconciliation": "inconclusive",
            "evidence_source": evidence_source,
            "status": normalised,
        }
        job.updated_at = self.clock()
        return job

    # ---------------------------------------------------------------- export
    async def export(self, job_id: str) -> ExportPayload:
        """Ask for the media bytes and verify them before anyone stores them."""
        job = self._jobs.get(job_id)
        if job is None:
            raise CoordinatorError("Unknown Flow job.", details={"job_id": job_id})
        if not job.media_id:
            raise ExportUnverified(
                "The job has no correlated media id yet, so no bytes can be exported."
            )

        envelope = CommandEnvelope(
            id=new_id(),
            command="export_asset",
            job_id=job.job_id,
            payload={"job_id": job.job_id, "media_id": job.media_id},
            issued_at=iso(self.clock()),
            expires_at=iso(self.clock()),
        )
        await self.transport.send(envelope)
        try:
            result = await self.transport.await_result(envelope.id, self.command_timeout)
        except Exception as exc:  # noqa: BLE001
            raise ExportUnverified(
                "The extension did not return the media bytes.",
                details={"reason": type(exc).__name__},
            ) from exc

        if not result.ok:
            raise ExportUnverified(
                "The extension could not export the media.",
                details={"code": normalise_error(result.error).code.value},
            )
        if result.job_id and result.job_id != job.job_id:
            raise ExportUnverified(
                "The exported bytes belong to a different job.",
                details={"expected": job.job_id},
            )

        payload = result.result or {}
        if str(payload.get("job_id") or job.job_id) != job.job_id:
            raise ExportUnverified("The export payload is not bound to this job.")
        if str(payload.get("media_id") or "") != job.media_id:
            raise ExportUnverified("The export payload carries a different media id.")

        declared_sha = str(payload.get("sha256") or "").lower()
        declared_size = int(payload.get("size_bytes") or 0)
        content_type = str(payload.get("content_type") or "").split(";")[0].strip().lower()
        encoded = payload.get("bytes_b64") or ""

        if content_type not in ALLOWED_EXPORT_TYPES:
            raise ExportUnverified(
                "The exported media type is not accepted.", details={"content_type": content_type}
            )
        if not declared_sha or not re.fullmatch(r"[0-9a-f]{64}", declared_sha):
            raise ExportUnverified("The export did not declare a valid SHA-256.")
        if declared_size <= 0 or declared_size > self.export_max_bytes:
            raise ExportUnverified(
                "The export size is outside the accepted range.",
                details={"size_bytes": declared_size, "max": self.export_max_bytes},
            )
        try:
            data = base64.b64decode(encoded, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ExportUnverified("The exported bytes were not valid base64.") from exc
        if len(data) != declared_size:
            raise ExportUnverified(
                "The exported byte count does not match what was declared.",
                details={"actual": len(data), "declared": declared_size},
            )
        actual_sha = hashlib.sha256(data).hexdigest()
        if actual_sha != declared_sha:
            raise ExportUnverified(
                "The exported bytes do not hash to the declared checksum.",
                details={"actual": actual_sha[:16], "declared": declared_sha[:16]},
            )

        job.result = {**job.result, "export_verified": True, "export_sha256": actual_sha}
        job.updated_at = self.clock()
        return ExportPayload(
            job_id=job.job_id,
            media_id=job.media_id,
            content_type=content_type,
            sha256=actual_sha,
            size_bytes=len(data),
            data=data,
        )
