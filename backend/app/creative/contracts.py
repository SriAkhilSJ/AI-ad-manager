"""Provider-independent creative generation contract.

Phase 3B must be able to swap a mock provider for a real one without touching the
job lifecycle, the storage layer or the UI. Everything a provider may say about
itself is declared here, including the things it cannot do.

The contract is intentionally *narrower* than "generate anything": a provider
receives a fully-resolved prompt and parameters and returns a job id. It never
decides budget, approval or storage policy — those belong to the service layer.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class GenerationStatus(str, enum.Enum):
    """The persisted lifecycle of one generation job."""

    QUEUED = "queued"
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @classmethod
    def values(cls) -> list[str]:
        return [status.value for status in cls]

    @classmethod
    def terminal(cls) -> set["GenerationStatus"]:
        return {
            cls.SUCCEEDED,
            cls.FAILED,
            cls.CANCELLED,
            cls.TIMED_OUT,
        }


class GenerationKind(str, enum.Enum):
    IMAGE = "image"
    VIDEO = "video"

    @classmethod
    def values(cls) -> list[str]:
        return [kind.value for kind in cls]


@dataclass(frozen=True)
class CreativeCapabilities:
    """What a provider can honestly do.

    ``video_playback`` exists because the mock provider renders a *poster frame*
    for video requests rather than encoding a real video stream. Claiming
    otherwise would make every downstream status report a lie.
    """

    provider: str
    supports_image: bool
    supports_video: bool
    is_mock: bool
    billable: bool
    requires_authorization: bool
    aspect_ratios: tuple[str, ...] = ()
    max_image_dimension: int = 0
    max_duration_seconds: int = 0
    video_playback: str = "none"
    limits: dict[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "supports_image": self.supports_image,
            "supports_video": self.supports_video,
            "is_mock": self.is_mock,
            "billable": self.billable,
            "requires_authorization": self.requires_authorization,
            "aspect_ratios": list(self.aspect_ratios),
            "max_image_dimension": self.max_image_dimension,
            "max_duration_seconds": self.max_duration_seconds,
            "video_playback": self.video_playback,
            "limits": dict(self.limits),
            "notes": list(self.notes),
        }


@dataclass
class GenerationRequest:
    """A fully-resolved generation request (no brief lookups inside a provider)."""

    job_type: str
    prompt: str
    negative_prompt: str = ""
    aspect_ratio: str = "4:5"
    resolution: str = ""
    duration_seconds: int = 0
    seed: int = 0
    parameters: dict[str, Any] = field(default_factory=dict)
    brief_id: str = ""
    content_item_id: str = ""
    content_version_id: str = ""
    idempotency_key: str = ""

    def canonical(self) -> dict[str, Any]:
        return {
            "job_type": self.job_type,
            "prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
            "aspect_ratio": self.aspect_ratio,
            "resolution": self.resolution,
            "duration_seconds": self.duration_seconds,
            "seed": self.seed,
            "parameters": self.parameters,
            "brief_id": self.brief_id,
        }


@dataclass
class GenerationSubmission:
    provider: str
    job_id: str
    status: str
    submitted_prompt: str
    parameters: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    cost_estimate_usd: float | None = None
    ambiguous: bool = False
    notes: tuple[str, ...] = ()


@dataclass
class GenerationState:
    job_id: str
    status: str
    progress: float = 0.0
    error_code: str = ""
    error_message: str = ""
    retryable: bool = False
    #: True when the provider cannot say whether the job ran (timeout, 5xx,
    #: connection reset). An ambiguous job must never be silently resubmitted.
    ambiguous: bool = False
    cost_actual_usd: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class GeneratedMedia:
    job_id: str
    data: bytes
    mime_type: str
    filename: str
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    cost_actual_usd: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class CreativeProviderError(Exception):
    """Normalised provider failure. Never leaks vendor response bodies."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "creative_provider_error",
        retryable: bool = False,
        ambiguous: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.ambiguous = ambiguous
        self.status_code = status_code


@runtime_checkable
class CreativeProvider(Protocol):
    """The six capabilities every creative provider must offer."""

    name: str

    async def get_capabilities(self) -> CreativeCapabilities: ...

    async def submit_image_generation(self, request: GenerationRequest) -> GenerationSubmission: ...

    async def submit_video_generation(self, request: GenerationRequest) -> GenerationSubmission: ...

    async def get_generation_status(self, job_id: str) -> GenerationState: ...

    async def retrieve_asset(self, job_id: str) -> GeneratedMedia: ...

    async def cancel_generation(self, job_id: str) -> bool: ...
