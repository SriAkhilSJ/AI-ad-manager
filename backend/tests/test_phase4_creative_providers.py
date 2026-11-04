"""Phase 4.2 — real creative providers (OpenAI images, Replicate video).

Every HTTP call here is served by ``httpx.MockTransport``. That proves the
adapter's request shape, its polling, its integrity checks and its refusal paths.
It is **not** evidence that OpenAI or Replicate generated anything: no live call
has been made from this repository, and the readiness report says so.
"""

from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import IntegrationUnavailableError
from app.creative.contracts import CreativeProviderError, GenerationRequest, GenerationStatus
from app.creative.media import render_brand_frame
from app.creative.providers import (
    MockCreativeProvider,
    configured_provider_name,
    creative_provider_status,
    get_creative_provider,
)
from app.creative.remote import (
    CompositeCreativeProvider,
    OpenAIImagesProvider,
    ReplicateVideoProvider,
    build_provider,
)

PNG = render_brand_frame(width=256, height=320, seed=3)
#: Smallest possible MP4-shaped payload: ftyp box at offset 4 is what the media
#: inspector keys on.
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64


def settings(**overrides) -> Settings:
    base = dict(
        environment="test",
        creative_image_provider="openai",
        creative_video_provider="replicate",
        openai_api_key="sk-test-key",
        openai_image_model="gpt-image-1",
        replicate_api_token="r8-test-token",
        replicate_video_model="owner/model:abc123",
        creative_provider_cache_dir="/tmp/crusor-creative-test",
    )
    base.update(overrides)
    return Settings(**base)


def request(job_type: str = "image", **overrides) -> GenerationRequest:
    base = dict(
        job_type=job_type,
        prompt="A charcoal developer desk, minimal, premium",
        negative_prompt="text, logo",
        aspect_ratio="4:5",
        duration_seconds=5,
    )
    base.update(overrides)
    return GenerationRequest(**base)


class Recorder:
    def __init__(self, handler):
        self.requests: list[httpx.Request] = []
        self.handler = handler

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        return self.handler(req)

    @property
    def calls(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]


def client(recorder: Recorder) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(recorder))


# --------------------------------------------------------------- image: happy
async def test_openai_image_job_returns_verified_bytes():
    recorder = Recorder(
        lambda req: httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
        )
    )
    provider = OpenAIImagesProvider(settings(), client=client(recorder))

    submission = await provider.submit_image_generation(request())
    assert submission.provider == "openai"
    assert submission.job_id.startswith("openai_")

    state = await provider.get_generation_status(submission.job_id)
    assert state.status == GenerationStatus.SUCCEEDED.value

    media = await provider.retrieve_asset(submission.job_id)
    assert media.data == PNG
    assert media.mime_type == "image/png"
    assert (media.width, media.height) == (256, 320)

    sent = json.loads(recorder.requests[0].content)
    assert recorder.calls == ["POST /v1/images/generations"]
    assert sent["model"] == "gpt-image-1"
    assert sent["size"] == "1024x1536"  # 4:5 maps to the tallest supported size
    assert sent["n"] == 1
    assert recorder.requests[0].headers["authorization"] == "Bearer sk-test-key"
    assert "sk-test-key" not in str(recorder.requests[0].url)


async def test_openai_image_downloads_a_url_when_the_model_returns_one():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/images/generations"):
            return httpx.Response(200, json={"data": [{"url": "https://cdn.example/img.png"}]})
        return httpx.Response(200, content=PNG)

    recorder = Recorder(handler)
    provider = OpenAIImagesProvider(settings(), client=client(recorder))
    submission = await provider.submit_image_generation(request())
    media = await provider.retrieve_asset(submission.job_id)
    assert media.data == PNG
    assert recorder.calls[-1] == "GET /img.png"


# ------------------------------------------------------------- image: failures
async def test_openai_rejection_is_typed_and_does_not_leak_the_body():
    recorder = Recorder(
        lambda req: httpx.Response(401, json={"error": {"message": "Incorrect API key sk-secret"}})
    )
    provider = OpenAIImagesProvider(settings(), client=client(recorder))
    with pytest.raises(CreativeProviderError) as excinfo:
        await provider.submit_image_generation(request())
    assert excinfo.value.status_code == 401
    assert "sk-secret" not in str(excinfo.value)
    assert excinfo.value.retryable is False


async def test_openai_rate_limit_is_retryable():
    recorder = Recorder(lambda req: httpx.Response(429, json={"error": {"message": "slow down"}}))
    provider = OpenAIImagesProvider(settings(), client=client(recorder))
    with pytest.raises(CreativeProviderError) as excinfo:
        await provider.submit_image_generation(request())
    assert excinfo.value.retryable is True


async def test_openai_transport_failure_is_ambiguous():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    provider = OpenAIImagesProvider(settings(), client=client(Recorder(handler)))
    with pytest.raises(CreativeProviderError) as excinfo:
        await provider.submit_image_generation(request())
    assert excinfo.value.ambiguous is True, (
        "a billed request that timed out must never be assumed to have failed"
    )


async def test_openai_bytes_that_are_not_an_image_are_rejected():
    recorder = Recorder(
        lambda req: httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(b"<html>not an image</html>").decode()}]}
        )
    )
    provider = OpenAIImagesProvider(settings(), client=client(recorder))
    with pytest.raises(CreativeProviderError) as excinfo:
        await provider.submit_image_generation(request())
    assert excinfo.value.code == "creative_integrity_failed"


async def test_openai_requires_its_own_key():
    provider = OpenAIImagesProvider(settings(openai_api_key=None))
    assert provider.available is False
    with pytest.raises(IntegrationUnavailableError) as excinfo:
        await provider.submit_image_generation(request())
    assert "OPENAI_API_KEY" in str(excinfo.value)


async def test_openai_does_not_borrow_the_replicate_token():
    provider = OpenAIImagesProvider(
        settings(openai_api_key=None, replicate_api_token="r8-test-token")
    )
    assert provider.available is False, "credentials must never be shared across providers"


async def test_missing_cache_reports_a_regeneration_not_a_blank_asset():
    provider = OpenAIImagesProvider(settings())
    state = await provider.get_generation_status("openai_missing")
    assert state.status == GenerationStatus.FAILED.value
    assert state.error_code == "creative_asset_unavailable"
    with pytest.raises(CreativeProviderError):
        await provider.retrieve_asset("openai_missing")


async def test_cancel_discards_the_cached_asset():
    recorder = Recorder(
        lambda req: httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
    )
    provider = OpenAIImagesProvider(settings(), client=client(recorder))
    submission = await provider.submit_image_generation(request())
    assert await provider.cancel_generation(submission.job_id) is True
    with pytest.raises(CreativeProviderError):
        await provider.retrieve_asset(submission.job_id)


# --------------------------------------------------------------- video: happy
def replicate_handler(media_bytes: bytes = MP4, statuses=("starting", "processing", "succeeded")):
    state = {"i": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path.endswith("/predictions"):
            return httpx.Response(
                201,
                json={
                    "id": "pred_1",
                    "status": statuses[0],
                    "urls": {"get": "https://api.replicate.com/v1/predictions/pred_1",
                             "cancel": "https://api.replicate.com/v1/predictions/pred_1/cancel"},
                },
            )
        if req.method == "POST" and path.endswith("/cancel"):
            return httpx.Response(200, json={"id": "pred_1", "status": "canceled"})
        if req.method == "GET" and path.endswith("/predictions/pred_1"):
            status = statuses[min(state["i"], len(statuses) - 1)]
            state["i"] += 1
            payload = {"id": "pred_1", "status": status, "metrics": {"predict_time": 7.5}}
            if status == "succeeded":
                payload["output"] = ["https://cdn.example/video.mp4"]
            return httpx.Response(200, json=payload)
        if path.endswith("/video.mp4"):
            return httpx.Response(200, content=media_bytes)
        return httpx.Response(404, json={})

    return handler


async def test_replicate_video_polls_then_downloads_and_verifies():
    recorder = Recorder(replicate_handler())
    provider = ReplicateVideoProvider(settings(), client=client(recorder))

    submission = await provider.submit_video_generation(request("video"))
    assert submission.job_id == "pred_1"
    assert submission.status == GenerationStatus.SUBMITTED.value

    first = await provider.get_generation_status("pred_1")
    assert first.status in {
        GenerationStatus.SUBMITTED.value,
        GenerationStatus.RUNNING.value,
        GenerationStatus.SUCCEEDED.value,
    }

    # Poll until the provider reports success (the fake advances one step per GET).
    for _ in range(5):
        state = await provider.get_generation_status("pred_1")
        if state.status == GenerationStatus.SUCCEEDED.value:
            break
    assert state.status == GenerationStatus.SUCCEEDED.value

    media = await provider.retrieve_asset("pred_1")
    assert media.mime_type == "video/mp4"
    # Replicate reports predict_time, not a duration; nothing is inferred here.
    assert media.metadata["predict_time"] == 7.5
    assert media.duration_seconds is None
    assert media.data == MP4

    sent = json.loads(recorder.requests[0].content)
    assert sent["version"] == "owner/model:abc123"
    assert sent["input"]["prompt"]
    assert sent["input"]["duration"] == 5
    assert recorder.requests[0].headers["authorization"] == "Token r8-test-token"
    assert "r8-test-token" not in str(recorder.requests[0].url)


async def test_replicate_failure_is_reported_as_failed_with_the_reason():
    recorder = Recorder(
        lambda req: httpx.Response(
            200, json={"id": "pred_1", "status": "failed", "error": "model crashed"}
        )
    )
    provider = ReplicateVideoProvider(settings(), client=client(recorder))
    state = await provider.get_generation_status("pred_1")
    assert state.status == GenerationStatus.FAILED.value
    assert "model crashed" in state.error_message


async def test_replicate_video_bytes_that_are_not_video_are_rejected():
    recorder = Recorder(replicate_handler(media_bytes=PNG, statuses=("succeeded",)))
    provider = ReplicateVideoProvider(settings(), client=client(recorder))
    with pytest.raises(CreativeProviderError) as excinfo:
        await provider.retrieve_asset("pred_1")
    assert excinfo.value.code == "creative_integrity_failed"


async def test_replicate_cancel_calls_the_cancel_endpoint():
    recorder = Recorder(replicate_handler())
    provider = ReplicateVideoProvider(settings(), client=client(recorder))
    assert await provider.cancel_generation("pred_1") is True
    assert recorder.calls == ["POST /v1/predictions/pred_1/cancel"]


async def test_replicate_requires_both_token_and_pinned_model():
    no_model = ReplicateVideoProvider(settings(replicate_video_model=""))
    assert no_model.available is False
    with pytest.raises(IntegrationUnavailableError):
        await no_model.submit_video_generation(request("video"))


async def test_providers_refuse_the_other_medium():
    image = OpenAIImagesProvider(settings())
    video = ReplicateVideoProvider(settings())
    with pytest.raises(IntegrationUnavailableError):
        await image.submit_video_generation(request("video"))
    with pytest.raises(IntegrationUnavailableError):
        await video.submit_image_generation(request("image"))


# ------------------------------------------------------------------ selection
def test_per_medium_configuration_builds_a_composite():
    provider = get_creative_provider(settings())
    assert isinstance(provider, CompositeCreativeProvider)
    assert provider.name == "openai+replicate"
    assert configured_provider_name(settings()) == "openai+replicate"


def test_default_remains_the_free_mock():
    provider = get_creative_provider(Settings(environment="test"))
    assert isinstance(provider, MockCreativeProvider)


def test_single_remote_can_be_selected_by_name():
    provider = get_creative_provider(
        Settings(environment="test", creative_provider="openai", openai_api_key="sk-x")
    )
    assert isinstance(provider, OpenAIImagesProvider)


def test_composite_routes_by_job_id_prefix():
    provider = CompositeCreativeProvider(
        OpenAIImagesProvider(settings()), ReplicateVideoProvider(settings())
    )
    assert isinstance(provider._route("openai_abc"), OpenAIImagesProvider)
    assert isinstance(provider._route("pred_1"), ReplicateVideoProvider)


# ------------------------------------------------------------- readiness report
def test_status_reports_exactly_what_is_missing():
    blocked = creative_provider_status(settings(replicate_video_model=""))
    assert blocked["status"] == "configured_but_blocked"
    assert blocked["providers"]["video"]["missing"] == "REPLICATE_VIDEO_MODEL"
    assert blocked["available"] is False

    ready = creative_provider_status(
        settings(creative_allow_paid_generation=True, creative_campaign_budget_usd=5.0,
                 creative_pricing_json={"image": 0.04, "video": 0.30})
    )
    assert ready["providers"]["image"]["available"] is True
    assert ready["providers"]["video"]["available"] is True
    assert ready["available"] is True
    assert ready["video_playback"] == "encoded"
    assert ready["blocked_by"] == []


def test_paid_generation_is_blocked_without_authorisation_even_when_ready():
    report = creative_provider_status(settings(creative_allow_paid_generation=False))
    assert "CREATIVE_ALLOW_PAID_GENERATION is false" in report["blocked_by"]
    assert report["paid_generation_enabled"] is False


def test_status_never_claims_a_live_generation():
    report = creative_provider_status(
        settings(creative_allow_paid_generation=True, creative_campaign_budget_usd=5.0,
                 creative_pricing_json={"image": 0.04, "video": 0.30})
    )
    assert report["available"] is True
    # "available" means configured and permitted — never that a call succeeded.
    assert "verification" not in report or report.get("verification") != "verified"
