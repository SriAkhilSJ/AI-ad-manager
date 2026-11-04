"""Real, billable creative providers — and the composite that pairs them.

Phase 3B shipped a deterministic mock and an inert Flow boundary. That left the
product with no way to produce an actual image or an actual video. This module
adds two supported providers, both reached over published HTTP APIs:

* :class:`OpenAIImagesProvider` — ``POST https://api.openai.com/v1/images/generations``.
  The response carries base64 image bytes (or a short-lived URL for older
  models). The call is synchronous at the provider end, so the adapter finishes
  the job at submit time and stores the bytes in a scratch cache keyed by job id;
  status polling then reports what is actually there.
* :class:`ReplicateVideoProvider` — ``POST https://api.replicate.com/v1/predictions``
  with a model ``version`` and an ``input`` object. The prediction id is polled at
  ``GET /v1/predictions/{id}`` and cancelled at ``POST /v1/predictions/{id}/cancel``;
  the output URL is downloaded when the prediction succeeds.

Rules this module keeps:

* **No pricing is invented.** Both providers are billable, and the generation
  service refuses them unless the founder has entered a verified price and set a
  non-zero creative budget. Nothing here decides that a price is "probably" fine.
* **No credential is reused.** The image provider reads ``OPENAI_API_KEY``; the
  video provider reads ``REPLICATE_API_TOKEN``. Neither falls back to the other.
* **Failures do not leak vendor bodies.** A provider error becomes a typed
  :class:`CreativeProviderError` carrying a status code, never the response text —
  which can echo prompts, account ids or key fragments.
* **Integrity is checked on the bytes.** A response that is not a recognisable
  image/video is an error, not an asset.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import httpx

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
from app.creative.media import inspect_media

OPENAI_PROVIDER_NAME = "openai"
REPLICATE_PROVIDER_NAME = "replicate"

OPENAI_IMAGES_PATH = "/images/generations"
REPLICATE_PREDICTIONS_PATH = "/predictions"

#: gpt-image-1 accepts three sizes; the closest match wins for other ratios.
OPENAI_SIZES = {
    "1:1": "1024x1024",
    "4:5": "1024x1536",
    "9:16": "1024x1536",
    "2:3": "1024x1536",
    "3:2": "1536x1024",
    "16:9": "1536x1024",
}


def _iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _cache_dir(settings: Settings) -> Path:
    raw = getattr(settings, "creative_provider_cache_dir", "") or "/tmp/crusor-creative"
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


class _Cached:
    """Tiny on-disk cache so a submitted asset survives until it is retrieved.

    A crash between submit and retrieve loses the cached bytes; the job then
    reports that the asset is no longer available and must be regenerated, rather
    than inventing a file.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def put(self, job_id: str, data: bytes, metadata: dict[str, Any]) -> None:
        (self.directory / f"{job_id}.bin").write_bytes(data)
        (self.directory / f"{job_id}.json").write_text(
            json.dumps({**metadata, "sha256": hashlib.sha256(data).hexdigest()}), "utf-8"
        )

    def get(self, job_id: str) -> tuple[bytes, dict[str, Any]] | None:
        data_path = self.directory / f"{job_id}.bin"
        meta_path = self.directory / f"{job_id}.json"
        if not data_path.exists() or not meta_path.exists():
            return None
        metadata = json.loads(meta_path.read_text("utf-8"))
        data = data_path.read_bytes()
        # Integrity: the bytes must still match what was written.
        if hashlib.sha256(data).hexdigest() != metadata.get("sha256"):
            return None
        return data, metadata

    def drop(self, job_id: str) -> None:
        for suffix in (".bin", ".json"):
            (self.directory / f"{job_id}{suffix}").unlink(missing_ok=True)


class OpenAIImagesProvider:
    """OpenAI Images API. Image only; every job is billable."""

    name = OPENAI_PROVIDER_NAME

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self.cache = _Cached(_cache_dir(self.settings))

    # -------------------------------------------------------------- internals
    @property
    def api_key(self) -> str:
        return (getattr(self.settings, "openai_api_key", "") or "").strip()

    @property
    def model(self) -> str:
        return getattr(self.settings, "openai_image_model", "") or "gpt-image-1"

    @property
    def base_url(self) -> str:
        return (getattr(self.settings, "openai_base_url", "") or "https://api.openai.com/v1").rstrip("/")

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _http(self) -> httpx.AsyncClient:
        return self._client or httpx.AsyncClient(timeout=float(getattr(self.settings, "creative_job_timeout_seconds", 300)))

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        client = self._http()
        try:
            response = await client.post(
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=body,
            )
        except httpx.HTTPError as exc:
            # Ambiguous: the request may have reached the provider and billed.
            raise CreativeProviderError(
                f"OpenAI images request failed: {type(exc).__name__}",
                code="creative_transport_error",
                retryable=True,
                ambiguous=True,
            ) from None
        if response.status_code >= 400:
            raise CreativeProviderError(
                f"OpenAI images returned HTTP {response.status_code}",
                code="creative_provider_rejected",
                retryable=response.status_code == 429 or response.status_code >= 500,
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError:
            raise CreativeProviderError(
                "OpenAI images returned a non-JSON body",
                code="creative_bad_response",
                status_code=response.status_code,
            ) from None
        return payload if isinstance(payload, dict) else {}

    # --------------------------------------------------------------- contract
    async def get_capabilities(self) -> CreativeCapabilities:
        return CreativeCapabilities(
            provider=self.name,
            supports_image=True,
            supports_video=False,
            is_mock=False,
            billable=True,
            requires_authorization=True,
            aspect_ratios=tuple(OPENAI_SIZES),
            max_image_dimension=1536,
            video_playback="none",
            limits={"model": self.model},
            notes=(
                "Requires CREATIVE_ALLOW_PAID_GENERATION=true, a verified price in "
                "CREATIVE_PRICING_JSON and a non-zero creative budget.",
                "Sizes are limited to 1024x1024, 1536x1024 and 1024x1536.",
            ),
        )

    async def submit_image_generation(self, request: GenerationRequest) -> GenerationSubmission:
        if not self.available:
            raise IntegrationUnavailableError(
                "OpenAI image generation needs OPENAI_API_KEY; no key is configured."
            )
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": request.prompt,
            "n": 1,
            "size": OPENAI_SIZES.get(request.aspect_ratio, "1024x1024"),
        }
        if request.parameters.get("quality"):
            body["quality"] = request.parameters["quality"]
        payload = await self._post(OPENAI_IMAGES_PATH, body)

        rows = payload.get("data") or []
        if not rows:
            raise CreativeProviderError(
                "OpenAI images returned no image", code="creative_empty_result"
            )
        entry = rows[0] if isinstance(rows[0], dict) else {}
        if entry.get("b64_json"):
            try:
                data = base64.b64decode(entry["b64_json"], validate=False)
            except Exception:  # noqa: BLE001 - corrupt payload is a provider error
                raise CreativeProviderError(
                    "OpenAI images returned undecodable base64",
                    code="creative_bad_response",
                ) from None
        elif entry.get("url"):
            data = await self._download(entry["url"])
        else:
            raise CreativeProviderError(
                "OpenAI images returned neither bytes nor a URL",
                code="creative_bad_response",
            )

        info = inspect_media(data)
        if not info.recognized:
            raise CreativeProviderError(
                "The bytes returned are not a recognisable image",
                code="creative_integrity_failed",
            )

        job_id = f"openai_{uuid.uuid4().hex}"
        self.cache.put(
            job_id,
            data,
            {
                "provider": self.name,
                "model": self.model,
                "mime_type": info.mime_type,
                "width": info.width,
                "height": info.height,
                "submitted_at": _iso(),
                "prompt_sha256": hashlib.sha256(request.prompt.encode()).hexdigest(),
            },
        )
        return GenerationSubmission(
            provider=self.name,
            job_id=job_id,
            status=GenerationStatus.SUCCEEDED.value,
            submitted_prompt=request.prompt,
            parameters={"model": self.model, "size": body["size"]},
            created_at=_iso(),
            notes=("Provider call completed synchronously; the bytes are cached.",),
        )

    async def submit_video_generation(self, request: GenerationRequest) -> GenerationSubmission:
        raise IntegrationUnavailableError(
            "The OpenAI image provider cannot generate video. Configure "
            "CREATIVE_VIDEO_PROVIDER=replicate for video."
        )

    async def get_generation_status(self, job_id: str) -> GenerationState:
        cached = self.cache.get(job_id)
        if cached is None:
            return GenerationState(
                job_id=job_id,
                status=GenerationStatus.FAILED.value,
                error_code="creative_asset_unavailable",
                error_message=(
                    "The generated image is no longer in the provider cache; regenerate it."
                ),
                retryable=True,
            )
        _, metadata = cached
        return GenerationState(
            job_id=job_id,
            status=GenerationStatus.SUCCEEDED.value,
            progress=1.0,
            metadata={
                key: metadata.get(key) for key in ("model", "width", "height", "mime_type")
            },
        )

    async def retrieve_asset(self, job_id: str) -> GeneratedMedia:
        cached = self.cache.get(job_id)
        if cached is None:
            raise CreativeProviderError(
                "The generated image is no longer in the provider cache; regenerate it.",
                code="creative_asset_unavailable",
                retryable=True,
            )
        data, metadata = cached
        info = inspect_media(data)
        return GeneratedMedia(
            job_id=job_id,
            data=data,
            mime_type=info.mime_type or metadata.get("mime_type") or "image/png",
            filename=f"{job_id}{info.extension or '.png'}",
            width=info.width or metadata.get("width"),
            height=info.height or metadata.get("height"),
            metadata={"provider": self.name, "model": metadata.get("model")},
        )

    async def cancel_generation(self, job_id: str) -> bool:
        # The provider call already completed; cancelling means discarding the asset.
        self.cache.drop(job_id)
        return True

    async def _download(self, url: str) -> bytes:
        client = self._http()
        response = await client.get(url)
        if response.status_code >= 400:
            raise CreativeProviderError(
                f"Downloading the image returned HTTP {response.status_code}",
                code="creative_download_failed",
                retryable=True,
                status_code=response.status_code,
            )
        return response.content


class ReplicateVideoProvider:
    """Replicate predictions API. Video only; every job is billable."""

    name = REPLICATE_PROVIDER_NAME

    STATUS_MAP = {
        "starting": GenerationStatus.SUBMITTED.value,
        "processing": GenerationStatus.RUNNING.value,
        "succeeded": GenerationStatus.SUCCEEDED.value,
        "failed": GenerationStatus.FAILED.value,
        "canceled": GenerationStatus.CANCELLED.value,
    }

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client

    @property
    def api_token(self) -> str:
        return (getattr(self.settings, "replicate_api_token", "") or "").strip()

    @property
    def version(self) -> str:
        return (getattr(self.settings, "replicate_video_model", "") or "").strip()

    @property
    def base_url(self) -> str:
        return (getattr(self.settings, "replicate_base_url", "") or "https://api.replicate.com/v1").rstrip("/")

    @property
    def available(self) -> bool:
        return bool(self.api_token and self.version)

    def _http(self) -> httpx.AsyncClient:
        return self._client or httpx.AsyncClient(timeout=float(getattr(self.settings, "creative_job_timeout_seconds", 300)))

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self.api_token}", "Content-Type": "application/json"}

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        client = self._http()
        try:
            response = await client.request(
                method, f"{self.base_url}{path}", headers=self._headers, json=body
            )
        except httpx.HTTPError as exc:
            raise CreativeProviderError(
                f"Replicate request failed: {type(exc).__name__}",
                code="creative_transport_error",
                retryable=True,
                ambiguous=True,
            ) from None
        if response.status_code >= 400:
            raise CreativeProviderError(
                f"Replicate returned HTTP {response.status_code}",
                code="creative_provider_rejected",
                retryable=response.status_code == 429 or response.status_code >= 500,
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError:
            raise CreativeProviderError(
                "Replicate returned a non-JSON body", code="creative_bad_response"
            ) from None
        return payload if isinstance(payload, dict) else {}

    async def get_capabilities(self) -> CreativeCapabilities:
        return CreativeCapabilities(
            provider=self.name,
            supports_image=False,
            supports_video=True,
            is_mock=False,
            billable=True,
            requires_authorization=True,
            aspect_ratios=("9:16", "16:9", "1:1"),
            max_duration_seconds=10,
            video_playback="encoded",
            limits={"model_version": self.version or None},
            notes=(
                "Requires REPLICATE_API_TOKEN and REPLICATE_VIDEO_MODEL "
                "(a pinned owner/model:version).",
                "Duration and aspect are passed through to the model; the model's own "
                "limits apply and are reported by the API.",
            ),
        )

    async def submit_image_generation(self, request: GenerationRequest) -> GenerationSubmission:
        raise IntegrationUnavailableError(
            "The Replicate video provider cannot generate images. Configure "
            "CREATIVE_IMAGE_PROVIDER=openai for images."
        )

    async def submit_video_generation(self, request: GenerationRequest) -> GenerationSubmission:
        if not self.available:
            raise IntegrationUnavailableError(
                "Replicate video generation needs REPLICATE_API_TOKEN and "
                "REPLICATE_VIDEO_MODEL; one of them is missing."
            )
        payload = await self._call(
            "POST",
            REPLICATE_PREDICTIONS_PATH,
            {"version": self.version, "input": self._input(request)},
        )
        prediction_id = payload.get("id")
        if not prediction_id:
            raise CreativeProviderError(
                "Replicate accepted no prediction id", code="creative_bad_response"
            )
        status = self.STATUS_MAP.get(str(payload.get("status", "")), GenerationStatus.SUBMITTED.value)
        return GenerationSubmission(
            provider=self.name,
            job_id=str(prediction_id),
            status=status,
            submitted_prompt=request.prompt,
            parameters={"version": self.version, **self._input(request)},
            created_at=_iso(),
        )

    def _input(self, request: GenerationRequest) -> dict[str, Any]:
        body: dict[str, Any] = {"prompt": request.prompt}
        if request.negative_prompt:
            body["negative_prompt"] = request.negative_prompt
        if request.duration_seconds:
            body["duration"] = request.duration_seconds
        if request.aspect_ratio:
            body["aspect_ratio"] = request.aspect_ratio
        for key in ("seed", "fps", "resolution"):
            value = request.parameters.get(key)
            if value:
                body[key] = value
        return body

    async def get_generation_status(self, job_id: str) -> GenerationState:
        if not self.available:
            raise IntegrationUnavailableError("Replicate credentials are not configured.")
        payload = await self._call("GET", f"{REPLICATE_PREDICTIONS_PATH}/{job_id}")
        raw_status = str(payload.get("status", ""))
        status = self.STATUS_MAP.get(raw_status, GenerationStatus.RUNNING.value)
        error = payload.get("error")
        return GenerationState(
            job_id=job_id,
            status=status,
            progress=1.0 if status == GenerationStatus.SUCCEEDED.value else 0.0,
            error_code="creative_provider_failed" if status == GenerationStatus.FAILED.value else "",
            error_message=str(error)[:300] if error else "",
            retryable=status == GenerationStatus.FAILED.value,
            metadata={"output_urls": self._output_urls(payload)},
        )

    @staticmethod
    def _output_urls(payload: dict[str, Any]) -> list[str]:
        output = payload.get("output")
        if isinstance(output, str):
            return [output]
        if isinstance(output, list):
            return [str(item) for item in output if isinstance(item, str)]
        return []

    async def retrieve_asset(self, job_id: str) -> GeneratedMedia:
        payload = await self._call("GET", f"{REPLICATE_PREDICTIONS_PATH}/{job_id}")
        if str(payload.get("status")) != "succeeded":
            raise CreativeProviderError(
                f"Prediction is '{payload.get('status')}'; there is no asset yet.",
                code="creative_not_ready",
                retryable=True,
            )
        urls = self._output_urls(payload)
        if not urls:
            raise CreativeProviderError(
                "Replicate reported success but returned no output URL",
                code="creative_empty_result",
            )
        client = self._http()
        response = await client.get(urls[0])
        if response.status_code >= 400:
            raise CreativeProviderError(
                f"Downloading the video returned HTTP {response.status_code}",
                code="creative_download_failed",
                retryable=True,
                status_code=response.status_code,
            )
        data = response.content
        info = inspect_media(data)
        # ``inspect_media`` reports the signature-derived mime for video but leaves
        # ``recognized`` image-only, so the mime is the integrity signal here.
        if info.mime_type not in {"video/mp4", "video/webm"}:
            raise CreativeProviderError(
                "The bytes returned are not a recognisable video",
                code="creative_integrity_failed",
            )
        metrics = payload.get("metrics") or {}
        return GeneratedMedia(
            job_id=job_id,
            data=data,
            mime_type=info.mime_type or "video/mp4",
            filename=f"{job_id}{info.extension or '.mp4'}",
            duration_seconds=metrics.get("duration") or None,
            metadata={
                "provider": self.name,
                "predict_time": metrics.get("predict_time"),
                "sha256": hashlib.sha256(data).hexdigest(),
            },
        )

    async def cancel_generation(self, job_id: str) -> bool:
        if not self.available:
            return False
        try:
            await self._call("POST", f"{REPLICATE_PREDICTIONS_PATH}/{job_id}/cancel")
        except CreativeProviderError:
            return False
        return True


class CompositeCreativeProvider:
    """Pair one image provider with one video provider under a single contract.

    The name is the pair (``openai+replicate``) so the generation service's
    "is this the provider the operator configured?" check still has something
    exact to compare against.
    """

    def __init__(self, image: CreativeProvider, video: CreativeProvider) -> None:
        self.image = image
        self.video = video
        self.name = f"{image.name}+{video.name}"

    async def get_capabilities(self) -> CreativeCapabilities:
        image_caps = await self.image.get_capabilities()
        video_caps = await self.video.get_capabilities()
        return CreativeCapabilities(
            provider=self.name,
            supports_image=image_caps.supports_image,
            supports_video=video_caps.supports_video,
            is_mock=False,
            billable=image_caps.billable or video_caps.billable,
            requires_authorization=True,
            aspect_ratios=tuple(sorted(set(image_caps.aspect_ratios) | set(video_caps.aspect_ratios))),
            max_image_dimension=max(image_caps.max_image_dimension, video_caps.max_image_dimension),
            max_duration_seconds=max(image_caps.max_duration_seconds, video_caps.max_duration_seconds),
            video_playback=video_caps.video_playback,
            limits={"image": image_caps.limits, "video": video_caps.limits},
            notes=image_caps.notes + video_caps.notes,
        )

    async def submit_image_generation(self, request: GenerationRequest) -> GenerationSubmission:
        return await self.image.submit_image_generation(request)

    async def submit_video_generation(self, request: GenerationRequest) -> GenerationSubmission:
        return await self.video.submit_video_generation(request)

    def _route(self, job_id: str) -> CreativeProvider:
        # Replicate ids are opaque strings it issued; OpenAI ids are prefixed.
        return self.image if str(job_id).startswith("openai_") else self.video

    async def get_generation_status(self, job_id: str) -> GenerationState:
        return await self._route(job_id).get_generation_status(job_id)

    async def retrieve_asset(self, job_id: str) -> GeneratedMedia:
        return await self._route(job_id).retrieve_asset(job_id)

    async def cancel_generation(self, job_id: str) -> bool:
        return await self._route(job_id).cancel_generation(job_id)


def build_provider(
    name: str, settings: Settings | None = None, *, client: httpx.AsyncClient | None = None
) -> CreativeProvider | None:
    """Construct one provider by name, or ``None`` when the name is not a remote."""
    key = (name or "").strip().lower()
    if key == OPENAI_PROVIDER_NAME:
        return OpenAIImagesProvider(settings, client=client)
    if key == REPLICATE_PROVIDER_NAME:
        return ReplicateVideoProvider(settings, client=client)
    return None


async def probe(provider: CreativeProvider) -> dict[str, Any]:
    """Capabilities plus availability, for the readiness report."""
    caps = await provider.get_capabilities()
    payload = caps.as_dict()
    payload["available"] = getattr(provider, "available", None)
    return payload


def remote_ready(provider: CreativeProvider) -> bool:
    return bool(getattr(provider, "available", False))


def describe_requirement(provider: CreativeProvider) -> str:
    """The exact environment variables that are missing for this provider."""
    if isinstance(provider, OpenAIImagesProvider):
        missing = [] if provider.api_key else ["OPENAI_API_KEY"]
        return ", ".join(missing)
    if isinstance(provider, ReplicateVideoProvider):
        missing = []
        if not provider.api_token:
            missing.append("REPLICATE_API_TOKEN")
        if not provider.version:
            missing.append("REPLICATE_VIDEO_MODEL")
        return ", ".join(missing)
    return ""
