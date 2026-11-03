"""Creative providers: the deterministic mock, the Flow adapter boundary, selection.

**Flow Connector status (re-verified for Phase 3B).** The extension source is not
in this repository and not on this filesystem: no ``manifest.json``, no service
worker, no ``externally_connectable`` declaration, no exported ``.crx``, no MCP
server and no local bridge. Nothing about its messaging protocol can therefore be
implemented from evidence, so :class:`FlowCreativeProvider` is an *adapter
boundary*: the six contract methods exist and every one of them fails closed with
:class:`IntegrationUnavailableError`. It cannot fabricate a job id, and it never
reports itself as available.

The mock provider is the only provider wired for execution. It is stateless by
construction: a job id encodes the canonical request, so status polling and asset
retrieval work across process restarts without an in-memory registry, and the
same request always yields the same bytes (idempotency for free).
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any

from app.core.config import Settings, get_settings
from app.core.errors import IntegrationUnavailableError
from app.creative.contracts import (
    CreativeCapabilities,
    CreativeProvider,
    CreativeProviderError,
    GeneratedMedia,
    GenerationRequest,
    GenerationState,
    GenerationStatus,
    GenerationSubmission,
)
from app.creative.media import ASPECT_RATIOS, KIND_BY_MIME, dimensions_for, inspect_media, render_brand_frame

MOCK_PROVIDER_NAME = "mock"
FLOW_PROVIDER_NAME = "flow"

#: Seconds the mock job spends "running" before it reports success. Overridable
#: per request via ``parameters["mock_delay_seconds"]``.
DEFAULT_MOCK_DELAY_SECONDS = 0.0
MOCK_VIDEO_DELAY_SECONDS = 2.0


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


class MockCreativeProvider:
    """Deterministic, free, self-identifying creative provider.

    * image requests render a real PNG using the Crusor visual system;
    * video requests render a PNG **poster frame** — no video codec is exercised,
      and ``capabilities().video_playback`` says so;
    * ``parameters["mock_simulate"]`` selects a failure mode for tests:
      ``fail``, ``timeout``, ``running``, ``ambiguous_timeout``.
    """

    name = MOCK_PROVIDER_NAME

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings
        self.max_dimension = int(
            getattr(settings, "creative_mock_max_dimension", 960) or 960
        )

    # ------------------------------------------------------------------ caps
    async def get_capabilities(self) -> CreativeCapabilities:
        return CreativeCapabilities(
            provider=self.name,
            supports_image=True,
            supports_video=True,
            is_mock=True,
            billable=False,
            requires_authorization=False,
            aspect_ratios=tuple(ASPECT_RATIOS.keys()),
            max_image_dimension=self.max_dimension,
            max_duration_seconds=30,
            video_playback="poster_only",
            limits={"variants": 4, "seed_range": "0-9999"},
            notes=(
                "Deterministic mock provider: no model is called and nothing is billed.",
                "Video requests produce a PNG poster frame; no video stream is encoded.",
                "Assets are synthetic abstract frames, not screenshots of Crusor software.",
            ),
        )

    # -------------------------------------------------------------- job ids
    def _job_id(self, request: GenerationRequest) -> str:
        canonical = json.dumps(request.canonical(), sort_keys=True, default=str)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        stamp = int(request.parameters.get("mock_submitted_at") or time.time())
        payload = _b64(canonical.encode("utf-8"))
        return f"mock:{stamp}:{digest}:{payload}"

    def _decode(self, job_id: str) -> tuple[int, dict[str, Any]]:
        parts = job_id.split(":", 3)
        if len(parts) != 4 or parts[0] != "mock":
            raise CreativeProviderError(
                "Unknown mock job id.", code="invalid_job_id", retryable=False
            )
        try:
            stamp = int(parts[1])
            canonical = json.loads(_unb64(parts[3]).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - a corrupt id is a client error
            raise CreativeProviderError(
                "Malformed mock job id.", code="invalid_job_id", retryable=False
            ) from exc
        return stamp, canonical

    def _request_from(self, canonical: dict[str, Any]) -> GenerationRequest:
        return GenerationRequest(
            job_type=str(canonical.get("job_type") or "image"),
            prompt=str(canonical.get("prompt") or ""),
            negative_prompt=str(canonical.get("negative_prompt") or ""),
            aspect_ratio=str(canonical.get("aspect_ratio") or "4:5"),
            resolution=str(canonical.get("resolution") or ""),
            duration_seconds=int(canonical.get("duration_seconds") or 0),
            seed=int(canonical.get("seed") or 0),
            parameters=dict(canonical.get("parameters") or {}),
            brief_id=str(canonical.get("brief_id") or ""),
        )

    # ------------------------------------------------------------- submits
    async def _submit(self, request: GenerationRequest, kind: str) -> GenerationSubmission:
        if kind not in ("image", "video"):
            raise CreativeProviderError(
                f"Unsupported generation kind '{kind}'.", code="unsupported_kind"
            )
        if not request.prompt.strip():
            raise CreativeProviderError("A prompt is required.", code="empty_prompt")
        if request.aspect_ratio not in ASPECT_RATIOS:
            request.aspect_ratio = "4:5"
        job_id = self._job_id(request)
        simulate = str(request.parameters.get("mock_simulate") or "").strip().lower()
        if simulate == "fail":
            raise CreativeProviderError(
                "Mock provider simulated a generation failure.",
                code="mock_simulated_failure",
                retryable=True,
            )
        return GenerationSubmission(
            provider=self.name,
            job_id=job_id,
            status=(
                GenerationStatus.RUNNING.value
                if simulate in ("timeout", "running", "ambiguous_timeout")
                else GenerationStatus.SUBMITTED.value
            ),
            submitted_prompt=request.prompt,
            parameters=dict(request.parameters),
            created_at=_iso(stamp=None),
            cost_estimate_usd=0.0,
            ambiguous=False,
            notes=("mock provider: no external request was made",),
        )

    async def submit_image_generation(self, request: GenerationRequest) -> GenerationSubmission:
        return await self._submit(request, "image")

    async def submit_video_generation(self, request: GenerationRequest) -> GenerationSubmission:
        return await self._submit(request, "video")

    # -------------------------------------------------------------- polling
    async def get_generation_status(self, job_id: str) -> GenerationState:
        stamp, canonical = self._decode(job_id)
        request = self._request_from(canonical)
        simulate = str(request.parameters.get("mock_simulate") or "").strip().lower()
        if simulate == "fail":
            return GenerationState(
                job_id=job_id,
                status=GenerationStatus.FAILED.value,
                error_code="mock_simulated_failure",
                error_message="Mock provider simulated a generation failure.",
                retryable=True,
            )
        if simulate == "ambiguous_timeout":
            return GenerationState(
                job_id=job_id,
                status=GenerationStatus.RUNNING.value,
                ambiguous=True,
                metadata={"note": "provider cannot confirm whether the job ran"},
            )
        if simulate in ("timeout", "running"):
            return GenerationState(job_id=job_id, status=GenerationStatus.RUNNING.value)

        delay = float(
            request.parameters.get(
                "mock_delay_seconds",
                MOCK_VIDEO_DELAY_SECONDS
                if request.job_type == "video"
                else DEFAULT_MOCK_DELAY_SECONDS,
            )
        )
        elapsed = max(0, int(time.time()) - stamp)
        status = (
            GenerationStatus.SUCCEEDED.value
            if elapsed >= delay
            else GenerationStatus.RUNNING.value
        )
        progress = 100.0 if status == GenerationStatus.SUCCEEDED.value else min(
            95.0, 100.0 * elapsed / delay if delay else 100.0
        )
        return GenerationState(
            job_id=job_id,
            status=status,
            progress=round(progress, 1),
            cost_actual_usd=0.0,
            metadata={"mock": True, "elapsed_seconds": elapsed},
        )

    # ------------------------------------------------------------- retrieval
    async def retrieve_asset(self, job_id: str) -> GeneratedMedia:
        _stamp, canonical = self._decode(job_id)
        request = self._request_from(canonical)
        template = getattr(self.settings, "creative_mock_palette", None) or {}
        width, height = dimensions_for(request.aspect_ratio, self.max_dimension)
        data = render_brand_frame(
            width=width,
            height=height,
            seed=request.seed,
            palette=dict(template),
            video_poster=request.job_type == "video",
        )
        info = inspect_media(data)
        if not info.recognized or info.width != width or info.height != height:
            raise CreativeProviderError(
                "Mock provider produced bytes it could not verify.",
                code="mock_self_check_failed",
            )
        suffix = ".png"
        return GeneratedMedia(
            job_id=job_id,
            data=data,
            mime_type=info.mime_type or "image/png",
            filename=f"mock-{request.job_type}-{request.seed}{suffix}",
            width=width,
            height=height,
            duration_seconds=float(request.duration_seconds or 0) or None,
            cost_actual_usd=0.0,
            metadata={
                "mock": True,
                "is_mock": True,
                "job_type": request.job_type,
                "aspect_ratio": request.aspect_ratio,
                "seed": request.seed,
                # Stated explicitly so no UI or report can imply real footage.
                "video_playback": "poster_only" if request.job_type == "video" else "image",
                "encoded_video": False if request.job_type == "video" else None,
                "note": (
                    "Synthetic abstract frame from the deterministic mock provider. "
                    "Not a screenshot of Crusor software and not model-generated art."
                ),
            },
        )

    # --------------------------------------------------------------- cancel
    async def cancel_generation(self, job_id: str) -> bool:
        state = await self.get_generation_status(job_id)
        return state.status in (GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value,
                                GenerationStatus.SUBMITTED.value)


class FlowCreativeProvider:
    """Adapter boundary for the Google Flow Connector.

    Present so the integration point is explicit and testable; **not**
    implemented, because the protocol is unknown. Every call fails closed with the
    exact information still required.
    """

    name = FLOW_PROVIDER_NAME
    available = False

    REQUIRED_INFORMATION: tuple[str, ...] = (
        "The Flow Connector source: manifest.json (manifest_version, permissions, "
        "host_permissions, externally_connectable).",
        "The background service worker's message contract: message types, request "
        "and response payloads, and the extension id used for onMessageExternal.",
        "How an extension-initiated generation is authorised (session token, "
        "native messaging host, or user-visible confirmation).",
        "Where generated files land: a download URL, a local path, or an object "
        "store the backend can read.",
        "Job lifecycle semantics: accepted / queued / running / failed states, "
        "timeouts, cancellation support and rate limits.",
        "Terms-of-service confirmation that programmatic generation via the "
        "extension is permitted for this account.",
    )

    def _unavailable(self) -> IntegrationUnavailableError:
        return IntegrationUnavailableError(
            "Google Flow integration is disabled: the Connector source is not "
            "available, so no messaging protocol can be implemented from evidence."
        )

    async def get_capabilities(self) -> CreativeCapabilities:
        return CreativeCapabilities(
            provider=self.name,
            supports_image=False,
            supports_video=False,
            is_mock=False,
            billable=True,
            requires_authorization=True,
            video_playback="none",
            notes=(
                "Flow integration is not implemented. Verification of the Connector "
                "source is required before any adapter code is written.",
                *self.REQUIRED_INFORMATION,
            ),
        )

    async def submit_image_generation(self, request: GenerationRequest) -> GenerationSubmission:
        raise self._unavailable()

    async def submit_video_generation(self, request: GenerationRequest) -> GenerationSubmission:
        raise self._unavailable()

    async def get_generation_status(self, job_id: str) -> GenerationState:
        raise self._unavailable()

    async def retrieve_asset(self, job_id: str) -> GeneratedMedia:
        raise self._unavailable()

    async def cancel_generation(self, job_id: str) -> bool:
        raise self._unavailable()

    @classmethod
    def required_information(cls) -> list[str]:
        return list(cls.REQUIRED_INFORMATION)


class DisabledCreativeProvider:
    """Explicit opt-out: every generation attempt is refused."""

    name = "disabled"

    async def get_capabilities(self) -> CreativeCapabilities:
        return CreativeCapabilities(
            provider=self.name,
            supports_image=False,
            supports_video=False,
            is_mock=False,
            billable=False,
            requires_authorization=True,
            notes=("Creative generation is disabled (CREATIVE_PROVIDER=disabled).",),
        )

    def _refuse(self) -> IntegrationUnavailableError:
        return IntegrationUnavailableError(
            "Creative generation is disabled (CREATIVE_PROVIDER=disabled)."
        )

    async def submit_image_generation(self, request: GenerationRequest) -> GenerationSubmission:
        raise self._refuse()

    async def submit_video_generation(self, request: GenerationRequest) -> GenerationSubmission:
        raise self._refuse()

    async def get_generation_status(self, job_id: str) -> GenerationState:
        raise self._refuse()

    async def retrieve_asset(self, job_id: str) -> GeneratedMedia:
        raise self._refuse()

    async def cancel_generation(self, job_id: str) -> bool:
        raise self._refuse()


def _iso(stamp: float | None) -> str:
    from datetime import datetime, timezone

    moment = datetime.fromtimestamp(stamp, tz=timezone.utc) if stamp else datetime.now(timezone.utc)
    return moment.isoformat()


def get_creative_provider(settings: Settings | None = None) -> CreativeProvider:
    """Resolve the configured provider. Mock is the default and the only one wired."""
    settings = settings or get_settings()
    choice = (getattr(settings, "creative_provider", "mock") or "mock").lower()
    if choice == "mock":
        return MockCreativeProvider(settings)
    if choice == "flow":
        return FlowCreativeProvider()
    return DisabledCreativeProvider()


def creative_provider_status(settings: Settings | None = None) -> dict[str, Any]:
    """Capability report for ``/system/status`` and ``/creative-studio/capabilities``."""
    settings = settings or get_settings()
    choice = (getattr(settings, "creative_provider", "mock") or "mock").lower()
    paid_authorised = bool(getattr(settings, "creative_allow_paid_generation", False))

    connector = {
        "integration": "google_flow_connector",
        "status": "not_available",
        "available": False,
        "enabled": False,
        "source_found": False,
        "required_information": FlowCreativeProvider.required_information(),
        "note": (
            "The Flow Connector source is absent from this repository, so its protocol "
            "is unknown and no adapter is active."
        ),
    }

    if choice == "mock":
        return {
            "flow_connector": connector,
            "provider": MOCK_PROVIDER_NAME,
            "status": "available",
            "available": True,
            "is_mock": True,
            "billable": False,
            "paid_generation_enabled": False,
            "live_generation_enabled": False,
            "video_playback": "poster_only",
            "note": (
                "Deterministic mock provider. No model or external service is called, and "
                "video requests produce a poster frame rather than an encoded video."
            ),
        }
    if choice == "flow":
        return {
            "flow_connector": connector,
            "provider": FLOW_PROVIDER_NAME,
            "status": "unavailable",
            "available": False,
            "is_mock": False,
            "billable": True,
            "paid_generation_enabled": False,
            "live_generation_enabled": False,
            "note": (
                "Google Flow is configured but cannot run: the Connector source is absent, "
                "so its protocol is unknown. No request is attempted."
            ),
        }
    return {
        "flow_connector": connector,
        "provider": "disabled",
        "status": "unavailable",
        "available": False,
        "is_mock": False,
        "billable": False,
        "paid_generation_enabled": False,
        "live_generation_enabled": False,
        "paid_authorised": paid_authorised,
        "note": "Creative generation is disabled (CREATIVE_PROVIDER=disabled).",
    }


def mime_kind(mime_type: str) -> str:
    return KIND_BY_MIME.get(mime_type, "document")
