"""Phase 3B service, provider, media and QA tests.

Everything runs against the deterministic mock provider. No paid call is made, no
network request leaves the process, and no test can publish anything.
"""

from __future__ import annotations

import hashlib
import struct
import uuid
import zlib
from datetime import datetime, timedelta, timezone

import pytest

from app.agents.base import AgentContext
from app.agents.creative import (
    BASE_NEGATIVE_PROMPT,
    CreativeAgent,
    CreativeInput,
    build_director_brief,
)
from app.core.config import Settings
from app.core.errors import IntegrationUnavailableError, InvalidStateTransition
from app.creative.contracts import (
    CreativeProvider,
    CreativeProviderError,
    GenerationRequest,
    GenerationStatus,
)
from app.creative.media import (
    analyze_png,
    decode_png_pixels,
    dimensions_for,
    inspect_media,
    render_brand_frame,
    validate_upload,
)
from app.creative.providers import (
    DisabledCreativeProvider,
    FlowCreativeProvider,
    MockCreativeProvider,
    creative_provider_status,
    get_creative_provider,
)
from app.models.brand import BrandProfile
from app.models.content import ContentItem, ContentVersion
from app.models.creative import CreativeAsset, CreativeAssetStatus
from app.models.creative_brief import CreativeBrief
from app.models.creative_job import CreativeGenerationJob
from app.services.creative import CreativeService
from app.services.creative_briefs import CreativeBriefService
from app.services.creative_generation import CreativeGenerationService
from app.services.creative_qa import CreativeQAService

SMALL = Settings(
    environment="test",
    creative_provider="mock",
    creative_mock_max_dimension=240,
    creative_job_timeout_seconds=60,
)


# --------------------------------------------------------------------------- #
# media layer
# --------------------------------------------------------------------------- #
def test_render_produces_a_structurally_valid_png():
    data = render_brand_frame(width=32, height=40, seed=3)
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    position = 8
    tags: list[str] = []
    idat = b""
    while position < len(data):
        length = struct.unpack(">I", data[position : position + 4])[0]
        tag = data[position + 4 : position + 8]
        payload = data[position + 8 : position + 8 + length]
        crc = struct.unpack(">I", data[position + 8 + length : position + 12 + length])[0]
        assert crc == zlib.crc32(tag + payload) & 0xFFFFFFFF, f"bad CRC for {tag!r}"
        tags.append(tag.decode("ascii"))
        if tag == b"IDAT":
            idat += payload
        position += length + 12
    assert tags == ["IHDR", "IDAT", "IEND"]
    raw = zlib.decompress(idat)
    assert len(raw) == 40 * (32 * 3 + 1)


def test_png_round_trip_dimensions_and_palette():
    width, height = dimensions_for("4:5", 200)
    data = render_brand_frame(width=width, height=height, seed=1)
    info = inspect_media(data)
    assert info.recognized and info.mime_type == "image/png"
    assert (info.width, info.height) == (width, height)

    stats = analyze_png(data, sample_step=3)
    assert stats.dark_ratio > 0.5, "the Crusor system is charcoal-first"
    assert stats.purple_ratio < 0.5, "purple must stay an accent"
    assert stats.mean_luma < 90


def test_rendering_is_deterministic_but_seed_sensitive():
    a = render_brand_frame(width=48, height=48, seed=1)
    b = render_brand_frame(width=48, height=48, seed=1)
    c = render_brand_frame(width=48, height=48, seed=2)
    assert a == b
    assert a != c


def test_video_poster_is_darker_and_still_a_png():
    poster = render_brand_frame(width=48, height=64, seed=1, video_poster=True)
    plain = render_brand_frame(width=48, height=64, seed=1)
    assert inspect_media(poster).mime_type == "image/png"
    assert analyze_png(poster, sample_step=2).mean_luma < analyze_png(
        plain, sample_step=2
    ).mean_luma


def test_png_decoder_rejects_unsupported_variants():
    with pytest.raises(ValueError):
        decode_png_pixels(b"not a png")


def test_inspect_media_identifies_types_by_signature():
    assert inspect_media(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32).mime_type == "image/png"
    assert inspect_media(b"\xff\xd8\xff\xe0" + b"\x00" * 32).mime_type == "image/jpeg"
    assert inspect_media(b"GIF89a" + b"\x00" * 32).mime_type == "image/gif"
    assert inspect_media(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 16).mime_type == "video/mp4"
    assert inspect_media(b"%PDF-1.7").mime_type == "application/pdf"
    unknown = inspect_media(b"<html>not media</html>")
    assert unknown.recognized is False and unknown.mime_type is None


def test_upload_validation_rejects_mime_spoofing():
    # A ZIP masquerading as a PNG must not be accepted on the strict path.
    data = b"PK\x03\x04" + b"\x00" * 64
    info, _warnings, error = validate_upload(
        data, declared_mime="image/png", filename="hero.png", max_bytes=1024 * 1024, strict=True
    )
    assert error is not None and "Unrecognised" in error
    assert info.recognized is False

    # The legacy permissive path records the problem instead of refusing.
    _info, warnings, error = validate_upload(
        data, declared_mime="image/png", filename="hero.png", max_bytes=1024 * 1024, strict=False
    )
    assert error is None and warnings


def test_upload_validation_enforces_size_and_extension():
    png = render_brand_frame(width=16, height=16, seed=1)
    _info, _warnings, error = validate_upload(
        png, declared_mime="image/png", filename="a.png", max_bytes=10, strict=True
    )
    assert error and "limit" in error

    _info, _warnings, error = validate_upload(
        png, declared_mime="image/png", filename="a.jpg", max_bytes=10**6, strict=True
    )
    assert error and "Extension" in error

    _info, _warnings, error = validate_upload(
        png, declared_mime="image/png", filename="a.png", max_bytes=10**6, strict=True
    )
    assert error is None


# --------------------------------------------------------------------------- #
# provider contract
# --------------------------------------------------------------------------- #
def test_mock_provider_satisfies_the_contract():
    provider = MockCreativeProvider(SMALL)
    assert isinstance(provider, CreativeProvider)


async def test_mock_capabilities_are_honest():
    caps = await MockCreativeProvider(SMALL).get_capabilities()
    assert caps.provider == "mock"
    assert caps.is_mock is True
    assert caps.billable is False
    assert caps.requires_authorization is False
    assert caps.supports_image and caps.supports_video
    # A poster frame is not a video stream, and the provider says so.
    assert caps.video_playback == "poster_only"
    assert any("poster frame" in note for note in caps.notes)


async def test_mock_generation_is_deterministic_and_self_verifying():
    provider = MockCreativeProvider(SMALL)
    request = GenerationRequest(
        job_type="image", prompt="charcoal developer visual", aspect_ratio="4:5", seed=4
    )
    first = await provider.submit_image_generation(request)
    second = await provider.submit_image_generation(request)
    assert first.job_id == second.job_id, "the same request must not create a second job"
    assert first.cost_estimate_usd == 0.0

    state = await provider.get_generation_status(first.job_id)
    assert state.status in (GenerationStatus.SUCCEEDED.value, GenerationStatus.RUNNING.value)

    media = await provider.retrieve_asset(first.job_id)
    info = inspect_media(media.data)
    assert info.recognized
    assert (info.width, info.height) == (media.width, media.height)
    assert media.metadata["is_mock"] is True
    assert media.metadata["encoded_video"] is None

    again = await provider.retrieve_asset(first.job_id)
    assert again.data == media.data


async def test_mock_video_is_labelled_a_poster_not_a_video():
    provider = MockCreativeProvider(SMALL)
    submission = await provider.submit_video_generation(
        GenerationRequest(job_type="video", prompt="ship faster", aspect_ratio="16:9", seed=1)
    )
    media = await provider.retrieve_asset(submission.job_id)
    assert media.metadata["video_playback"] == "poster_only"
    assert media.metadata["encoded_video"] is False
    assert inspect_media(media.data).mime_type == "image/png"


async def test_mock_provider_simulates_failures_without_randomness():
    provider = MockCreativeProvider(SMALL)
    with pytest.raises(CreativeProviderError) as exc:
        await provider.submit_image_generation(
            GenerationRequest(job_type="image", prompt="x", parameters={"mock_simulate": "fail"})
        )
    assert exc.value.retryable is True

    stalled = await provider.submit_image_generation(
        GenerationRequest(job_type="image", prompt="x", parameters={"mock_simulate": "timeout"})
    )
    state = await provider.get_generation_status(stalled.job_id)
    assert state.status == GenerationStatus.RUNNING.value

    ambiguous = await provider.submit_image_generation(
        GenerationRequest(
            job_type="image", prompt="x", parameters={"mock_simulate": "ambiguous_timeout"}
        )
    )
    assert (await provider.get_generation_status(ambiguous.job_id)).ambiguous is True


async def test_flow_provider_fails_closed_and_documents_requirements():
    provider = FlowCreativeProvider()
    caps = await provider.get_capabilities()
    assert caps.provider == "flow"
    assert caps.supports_image is False and caps.supports_video is False
    assert caps.requires_authorization is True

    for call in (
        lambda: provider.submit_image_generation(GenerationRequest(job_type="image", prompt="x")),
        lambda: provider.retrieve_asset("job"),
        lambda: provider.get_generation_status("job"),
        lambda: provider.cancel_generation("job"),
    ):
        with pytest.raises(IntegrationUnavailableError):
            await call()

    required = FlowCreativeProvider.required_information()
    assert len(required) >= 5
    assert any("manifest.json" in item for item in required)


async def test_disabled_provider_refuses_everything():
    provider = DisabledCreativeProvider()
    with pytest.raises(IntegrationUnavailableError):
        await provider.submit_image_generation(GenerationRequest(job_type="image", prompt="x"))
    assert (await provider.get_capabilities()).supports_image is False


def test_provider_selection_defaults_to_mock():
    assert isinstance(get_creative_provider(Settings(environment="test")), MockCreativeProvider)
    assert isinstance(
        get_creative_provider(Settings(environment="test", creative_provider="flow")),
        FlowCreativeProvider,
    )
    assert isinstance(
        get_creative_provider(Settings(environment="test", creative_provider="disabled")),
        DisabledCreativeProvider,
    )


def test_status_reports_flow_as_unavailable_without_pretending():
    status = creative_provider_status(Settings(environment="test", creative_provider="flow"))
    assert status["available"] is False
    assert status["flow_connector"]["source_found"] is False
    assert status["flow_connector"]["required_information"]

    mock_status = creative_provider_status(Settings(environment="test"))
    assert mock_status["is_mock"] is True
    assert mock_status["live_generation_enabled"] is False


# --------------------------------------------------------------------------- #
# Creative Director briefs
# --------------------------------------------------------------------------- #
async def test_director_brief_has_every_required_field(session, brand_row):
    ctx = AgentContext(session=session, brand=brand_row, gateway=None, extra={})
    agent = CreativeAgent(Settings(environment="test"))
    output = await agent.execute(
        ctx,
        CreativeInput(
            platform="instagram",
            asset_type="video",
            request_director_brief=True,
            campaign_objective="Developer acquisition",
            content_body="Build and ship with ease.",
            content_hash="deadbeef",
            audience="software developers",
            prohibited_claims=["10x"],
        ),
    )
    brief = output.director_brief
    assert brief is not None
    for field in (
        "campaign_id",
        "content_id",
        "content_version_id",
        "platform",
        "asset_type",
        "creative_objective",
        "visual_concept",
        "scenes",
        "subjects",
        "background",
        "lighting",
        "composition",
        "colour_palette",
        "typography",
        "brand_asset_requirements",
        "image_prompt",
        "negative_prompt",
        "aspect_ratio",
        "resolution",
        "duration_seconds",
        "reference_assets",
        "validation_constraints",
    ):
        assert field in brief.as_dict(), f"missing brief field {field}"
    assert brief.content_hash == "deadbeef"
    assert brief.asset_type == "video"
    assert brief.duration_seconds and brief.duration_seconds > 0
    assert brief.aspect_ratio == "4:5"
    assert any("10x" in note for note in brief.negative_prompt)
    assert brief.validation_constraints["human_visual_review_required"] is True
    assert brief.validation_constraints["product_ui"] == "conceptual_only"


async def test_director_brief_uses_platform_aspect_ratios(session, brand_row):
    ctx = AgentContext(session=session, brand=brand_row, gateway=None, extra={})
    agent = CreativeAgent(Settings(environment="test"))
    expected = {
        "instagram": "4:5",
        "linkedin": "1.91:1",
        "youtube": "16:9",
        "x": "16:9",
        "facebook": "1.91:1",
        "reddit": "16:9",
    }
    for platform, ratio in expected.items():
        output = await agent.execute(
            ctx,
            CreativeInput(
                platform=platform,
                asset_type="image",
                request_director_brief=True,
                content_body="ship faster",
            ),
        )
        assert output.director_brief.aspect_ratio == ratio


async def test_director_brief_never_mutates_content(session, brand_row):
    item = ContentItem(
        brand_profile_id=brand_row.id,
        title="Original title",
        platform="linkedin",
        format="social_post",
        status="DRAFT",
    )
    session.add(item)
    await session.flush()
    version = ContentVersion(
        content_item_id=item.id,
        version_number=1,
        headline="Headline",
        body="Body copy that must not change.",
        content_hash="hash-1",
    )
    session.add(version)
    await session.flush()

    before = (version.headline, version.body, version.content_hash, item.title)
    ctx = AgentContext(session=session, brand=brand_row, gateway=None, extra={})
    await CreativeAgent(Settings(environment="test")).execute(
        ctx,
        CreativeInput(
            platform="linkedin",
            request_director_brief=True,
            content_item_id=item.id,
            content_version_id=version.id,
            content_hash=version.content_hash,
            content_body=version.body,
        ),
    )
    await session.refresh(version)
    await session.refresh(item)
    assert (version.headline, version.body, version.content_hash, item.title) == before


async def test_legacy_creative_agent_behaviour_is_unchanged(session, brand_row):
    ctx = AgentContext(session=session, brand=brand_row, gateway=None, extra={})
    output = await CreativeAgent(Settings(environment="test")).execute(
        ctx, CreativeInput(platform="instagram", format="carousel", concept="launch")
    )
    assert output.brief is not None
    assert output.brief.aspect_ratio == "4:5"
    assert output.director_brief is None, "no brief should be produced unless requested"


async def test_brand_negative_constraints_are_applied_by_default():
    brief = build_director_brief(
        AgentContext(session=None, brand=None, gateway=None, extra={}),
        CreativeInput(platform="x", content_body="hello"),
    )
    for constraint in BASE_NEGATIVE_PROMPT:
        assert constraint in brief.negative_prompt


# --------------------------------------------------------------------------- #
# Creative QA
# --------------------------------------------------------------------------- #
async def _make_asset(session, *, data: bytes, brief=None, storage=None, **overrides):
    from types import SimpleNamespace

    asset = CreativeAsset(
        kind="image",
        status=CreativeAssetStatus.READY.value,
        filename="a.png",
        mime_type="image/png",
        detected_mime="image/png",
        size_bytes=len(data),
        storage_key="creatives/x/a.png",
        checksum=hashlib.sha256(data).hexdigest(),
        width=inspect_media(data).width,
        height=inspect_media(data).height,
        prompt="charcoal developer visual",
        is_mock=True,
        metadata_json={"platform": "instagram", "is_mock": True},
        brief_id=brief.id if brief else None,
    )
    session.add(asset)
    await session.flush()
    return asset


class _MemoryStorage:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def get(self, key: str) -> bytes:
        return self.data

    async def put(self, key, data, content_type=None, metadata=None):  # type: ignore[no-untyped-def]
        self.data = data
        return key

    async def exists(self, key: str) -> bool:
        return True

    async def delete(self, key: str) -> bool:
        return True


async def test_qa_passes_a_well_formed_mock_frame(session, brand_row):
    data = render_brand_frame(width=400, height=500, seed=1)
    asset = await _make_asset(session, data=data)
    brief = CreativeBrief(
        brand_profile_id=brand_row.id,
        platform="instagram",
        asset_type="image",
        version_number=1,
        creative_objective="charcoal developer visual",
        visual_concept="charcoal developer visual",
        image_prompt="charcoal developer visual",
        aspect_ratio="4:5",
        resolution="400x500",
        content_hash="abc",
    )
    session.add(brief)
    await session.flush()
    asset.brief_id = brief.id
    report = await CreativeQAService(session, _MemoryStorage(data)).evaluate(
        asset=asset, brief=brief, brand=brand_row
    )
    assert report["verdict"] in ("pass", "pass_with_warnings")
    assert report["performed_checks"] > 5
    assert report["visual_verification"] == "not_performed"
    assert report["human_visual_review_required"] is True
    rules = {check["rule"]: check for check in report["checks"]}
    assert rules["text_readability"]["performed"] is False
    assert rules["human_visual_review"]["performed"] is False
    assert rules["pixel_analysis"]["performed"] is True


async def test_qa_blocks_unsupported_claims(session, brand_row):
    data = render_brand_frame(width=200, height=250, seed=1)
    asset = await _make_asset(session, data=data)
    asset.prompt = "Crusor makes developers 300% faster and is used by 50,000 developers"
    report = await CreativeQAService(session, _MemoryStorage(data)).evaluate(
        asset=asset, brand=brand_row
    )
    assert report["verdict"] == "blocked"
    assert report["blocking_count"] >= 1
    assert any(issue["rule"] == "unsupported_claim" for issue in report["issues"])


async def test_qa_blocks_logo_recreation(session, brand_row):
    data = render_brand_frame(width=200, height=250, seed=1)
    asset = await _make_asset(session, data=data)
    asset.prompt = "recreate the Crusor logo in metallic silver"
    report = await CreativeQAService(session, _MemoryStorage(data)).evaluate(
        asset=asset, brand=brand_row
    )
    assert report["verdict"] == "blocked"
    assert any(issue["rule"] == "logo_recreation_requested" for issue in report["issues"])


async def test_qa_blocks_fabricated_product_ui(session, brand_row):
    data = render_brand_frame(width=200, height=250, seed=1)
    asset = await _make_asset(session, data=data)
    asset.prompt = "screenshot of the Crusor IDE with the agent panel open"
    report = await CreativeQAService(session, _MemoryStorage(data)).evaluate(
        asset=asset, brand=brand_row
    )
    assert report["verdict"] == "blocked"
    assert any(issue["rule"] == "fabricated_product_ui" for issue in report["issues"])


async def test_qa_detects_checksum_mismatch(session, brand_row):
    data = render_brand_frame(width=120, height=150, seed=1)
    asset = await _make_asset(session, data=data)
    asset.checksum = "0" * 64
    report = await CreativeQAService(session, _MemoryStorage(data)).evaluate(
        asset=asset, brand=brand_row
    )
    assert report["verdict"] == "blocked"
    assert any(issue["rule"] == "checksum_mismatch" for issue in report["issues"])


async def test_qa_marks_undecodable_formats_as_not_performed(session, brand_row):
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 200
    asset = await _make_asset(session, data=jpeg)
    asset.mime_type = "image/jpeg"
    report = await CreativeQAService(session, _MemoryStorage(jpeg)).evaluate(
        asset=asset, brand=brand_row
    )
    rules = {check["rule"]: check for check in report["checks"]}
    assert rules["pixel_analysis"]["performed"] is False
    assert rules["brand_dark_base"]["performed"] is False
    assert report["visual_verification"] == "not_performed"


async def test_qa_flags_off_palette_frames(session, brand_row):
    # A flat white frame violates the charcoal-first rule.
    pixels = b"\xff\xff\xff" * (64 * 64)
    from app.creative.media import encode_png

    white = encode_png(64, 64, pixels)
    asset = await _make_asset(session, data=white)
    report = await CreativeQAService(session, _MemoryStorage(white)).evaluate(
        asset=asset, brief=None, brand=brand_row
    )
    rules = {check["rule"]: check for check in report["checks"]}
    assert rules["brand_dark_base"]["passed"] is False
    assert any(issue["rule"] == "brand_dark_base_missing" for issue in report["issues"])


# --------------------------------------------------------------------------- #
# brief persistence and generation jobs (service level)
# --------------------------------------------------------------------------- #
async def _content_version(session, brand_row) -> tuple[ContentItem, ContentVersion]:
    item = ContentItem(
        brand_profile_id=brand_row.id,
        title="Crusor launch post",
        platform="instagram",
        format="social_post",
        status="DRAFT",
    )
    session.add(item)
    await session.flush()
    version = ContentVersion(
        content_item_id=item.id,
        version_number=1,
        headline="Build and Ship with Ease",
        body="Crusor AI is an AI-powered IDE. Build and ship with ease.",
        call_to_action="Try it.",
        content_hash="content-hash-1",
    )
    session.add(version)
    await session.flush()
    item.current_version_id = version.id
    await session.flush()
    return item, version


async def _brief(session, brand_row, version, *, asset_type="image"):
    service = CreativeBriefService(session, SMALL)
    brief, created = await service.create(
        content_version_id=version.id, asset_type=asset_type
    )
    await session.flush()
    return brief, created


async def test_brief_is_created_bound_to_the_exact_version(session, brand_row):
    item, version = await _content_version(session, brand_row)
    brief, created = await _brief(session, brand_row, version)
    assert created is True
    assert brief.content_version_id == version.id
    assert brief.content_item_id == item.id
    assert brief.content_hash == version.content_hash
    assert brief.version_number == 1
    assert brief.image_prompt and brief.negative_prompt
    assert brief.generation_metadata["content_modified"] is False
    assert brief.colour_palette.get("accent")

    await session.refresh(version)
    assert version.body == "Crusor AI is an AI-powered IDE. Build and ship with ease."


async def test_brief_creation_is_idempotent_unless_a_new_version_is_forced(session, brand_row):
    _item, version = await _content_version(session, brand_row)
    first, created_first = await _brief(session, brand_row, version)
    second, created_second = await _brief(session, brand_row, version)
    assert created_first is True and created_second is False
    assert second.id == first.id

    service = CreativeBriefService(session, SMALL)
    third, created_third = await service.create(
        content_version_id=version.id, asset_type="image", force_new_version=True
    )
    assert created_third is True
    assert third.version_number == 2
    assert third.id != first.id


async def test_video_brief_carries_duration_and_ratio(session, brand_row):
    _item, version = await _content_version(session, brand_row)
    brief, _created = await _brief(session, brand_row, version, asset_type="video")
    assert brief.asset_type == "video"
    assert brief.duration_seconds and brief.duration_seconds > 0
    assert brief.resolution


class _BillableProvider:
    """A fake paid provider. It records calls so tests can prove it was never used.

    It answers to the name ``flow`` because that is the only billable provider slot
    the settings allow; the point under test is the authorisation gate, not routing.
    """

    name = "flow"

    def __init__(self) -> None:
        self.calls = 0

    async def get_capabilities(self):  # type: ignore[no-untyped-def]
        from app.creative.contracts import CreativeCapabilities

        return CreativeCapabilities(
            provider=self.name,
            supports_image=True,
            supports_video=False,
            is_mock=False,
            billable=True,
            requires_authorization=True,
        )

    async def submit_image_generation(self, request):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise AssertionError("a refused paid provider must never be invoked")

    async def submit_video_generation(self, request):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise AssertionError("a refused paid provider must never be invoked")

    async def get_generation_status(self, job_id):  # type: ignore[no-untyped-def]
        raise AssertionError("not reachable")

    async def retrieve_asset(self, job_id):  # type: ignore[no-untyped-def]
        raise AssertionError("not reachable")

    async def cancel_generation(self, job_id):  # type: ignore[no-untyped-def]
        return False


async def test_paid_provider_is_refused_by_default(session, brand_row, tmp_path):
    from app.storage.base import LocalFilesystemStorage

    _item, version = await _content_version(session, brand_row)
    brief, _ = await _brief(session, brand_row, version)
    provider = _BillableProvider()
    settings = Settings(
        environment="test",
        creative_provider="flow",
        creative_allow_paid_generation=False,
        creative_campaign_budget_usd=5.0,
    )
    service = CreativeGenerationService(
        session, settings=settings, provider=provider, storage=LocalFilesystemStorage(tmp_path)
    )
    with pytest.raises(IntegrationUnavailableError) as exc:
        await service.submit(brief=brief)
    assert "Paid creative generation is disabled" in str(exc.value)
    assert provider.calls == 0


async def test_zero_creative_budget_blocks_paid_generation(session, brand_row, tmp_path):
    from app.storage.base import LocalFilesystemStorage

    _item, version = await _content_version(session, brand_row)
    brief, _ = await _brief(session, brand_row, version)
    provider = _BillableProvider()
    settings = Settings(
        environment="test",
        creative_provider="flow",
        creative_allow_paid_generation=True,
        creative_campaign_budget_usd=0.0,
        creative_pricing_json={"image": 0.5},
    )
    service = CreativeGenerationService(
        session, settings=settings, provider=provider, storage=LocalFilesystemStorage(tmp_path)
    )
    with pytest.raises(IntegrationUnavailableError) as exc:
        await service.submit(brief=brief)
    assert "budget is zero" in str(exc.value).lower()
    assert provider.calls == 0


async def test_unknown_pricing_is_never_treated_as_zero(session, brand_row, tmp_path):
    from app.storage.base import LocalFilesystemStorage

    _item, version = await _content_version(session, brand_row)
    brief, _ = await _brief(session, brand_row, version)
    provider = _BillableProvider()
    settings = Settings(
        environment="test",
        creative_provider="flow",
        creative_allow_paid_generation=True,
        creative_campaign_budget_usd=10.0,
    )
    service = CreativeGenerationService(
        session, settings=settings, provider=provider, storage=LocalFilesystemStorage(tmp_path)
    )
    with pytest.raises(IntegrationUnavailableError) as exc:
        await service.submit(brief=brief)
    assert "unknown" in str(exc.value).lower()
    assert provider.calls == 0


async def test_mock_job_completes_and_persists_a_verified_asset(session, brand_row, tmp_path):
    from app.storage.base import LocalFilesystemStorage

    storage = LocalFilesystemStorage(tmp_path)
    item, version = await _content_version(session, brand_row)
    brief, _ = await _brief(session, brand_row, version)

    service = CreativeGenerationService(
        session, settings=SMALL, storage=storage
    )
    job, created = await service.submit(brief=brief, parameters={"seed": 2})
    assert created is True
    assert job.status == GenerationStatus.SUCCEEDED.value
    assert job.is_mock is True
    assert job.cost_estimate_usd == 0.0 and job.cost_actual_usd == 0.0
    assert job.external_job_id.startswith("mock:")
    assert job.asset_id is not None

    asset = await session.get(CreativeAsset, job.asset_id)
    assert asset is not None
    assert asset.brief_id == brief.id
    assert asset.brief_version == brief.version_number
    assert asset.generation_job_id == job.id
    assert asset.content_version_id == version.id
    assert asset.campaign_id == item.campaign_id
    assert asset.is_mock is True
    assert asset.mime_type == "image/png"
    assert asset.detected_mime == "image/png"
    assert asset.width and asset.height
    assert asset.aspect_ratio == brief.aspect_ratio
    assert asset.asset_version == 1

    # The bytes really are on disk, and they hash to the recorded checksum.
    stored = await storage.get(asset.storage_key)
    assert hashlib.sha256(stored).hexdigest() == asset.checksum
    assert len(stored) == asset.size_bytes
    info = inspect_media(stored)
    assert (info.width, info.height) == (asset.width, asset.height)

    # QA ran automatically and is honest about what it did not do.
    assert asset.validation_status in {"pass", "pass_with_warnings", "needs_revision"}
    assert asset.qa_report["visual_verification"] == "not_performed"
    assert job.audit_trail and job.audit_trail[-1]["event"] == "succeeded"


async def test_generation_is_idempotent_and_never_duplicates_assets(session, brand_row, tmp_path):
    from app.storage.base import LocalFilesystemStorage

    _item, version = await _content_version(session, brand_row)
    brief, _ = await _brief(session, brand_row, version)
    service = CreativeGenerationService(
        session, settings=SMALL, storage=LocalFilesystemStorage(tmp_path)
    )
    first, created_first = await service.submit(brief=brief, parameters={"seed": 7})
    second, created_second = await service.submit(brief=brief, parameters={"seed": 7})
    assert created_first is True
    assert created_second is False
    assert second.id == first.id

    keyed, _ = await service.submit(
        brief=brief, parameters={"seed": 9}, idempotency_key="creative-key-1"
    )
    replay, created = await service.submit(
        brief=brief, parameters={"seed": 9}, idempotency_key="creative-key-1"
    )
    assert replay.id == keyed.id and created is False

    with pytest.raises(InvalidStateTransition):
        await service.submit(
            brief=brief, parameters={"seed": 11}, idempotency_key="creative-key-1"
        )

    assets = list((await session.execute(select_assets())).scalars().all())
    assert len(assets) == 2, "two distinct requests, two assets, no duplicates"


def select_assets():  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    return select(CreativeAsset)


async def test_failed_job_records_a_retryable_error(session, brand_row, tmp_path):
    from app.storage.base import LocalFilesystemStorage

    _item, version = await _content_version(session, brand_row)
    brief, _ = await _brief(session, brand_row, version)
    service = CreativeGenerationService(
        session, settings=SMALL, storage=LocalFilesystemStorage(tmp_path)
    )
    job, _ = await service.submit(brief=brief, parameters={"mock_simulate": "fail"})
    assert job.status == GenerationStatus.FAILED.value
    assert job.error_code == "mock_simulated_failure"
    assert job.error_retryable is True
    assert job.asset_id is None

    retried = await service.retry(job, brief=brief)
    assert retried.attempts == 2
    assert retried.status == GenerationStatus.FAILED.value
    with pytest.raises(InvalidStateTransition):
        await service.retry(retried, brief=brief)


async def test_timeout_is_marked_ambiguous_and_not_auto_retried(session, brand_row, tmp_path):
    from app.storage.base import LocalFilesystemStorage

    _item, version = await _content_version(session, brand_row)
    brief, _ = await _brief(session, brand_row, version)
    settings = Settings(
        environment="test", creative_provider="mock", creative_mock_max_dimension=120
    )
    service = CreativeGenerationService(
        session, settings=settings, storage=LocalFilesystemStorage(tmp_path)
    )
    job, _ = await service.submit(
        brief=brief, parameters={"mock_simulate": "ambiguous_timeout"}
    )
    assert job.status == GenerationStatus.RUNNING.value, "submitted jobs start running"
    # Rewind the submission so the deadline has demonstrably passed, then poll.
    job.submitted_at = job.submitted_at - timedelta(seconds=job.timeout_seconds + 60)
    session.add(job)
    await session.flush()
    job = await service.advance(job, brief=brief)
    assert job.status == GenerationStatus.TIMED_OUT.value
    assert job.ambiguous is True

    with pytest.raises(InvalidStateTransition) as exc:
        await service.retry(job, brief=brief)
    assert "ambiguously" in str(exc.value)

    authorized = await service.retry(job, brief=brief, authorize_ambiguous=True)
    assert authorized.attempts == 2


async def test_timeout_math_survives_a_naive_timestamp(session, brand_row, tmp_path):
    """SQLite returns naive datetimes; the deadline arithmetic must still work."""
    from app.storage.base import LocalFilesystemStorage

    _item, version = await _content_version(session, brand_row)
    brief, _ = await _brief(session, brand_row, version)
    service = CreativeGenerationService(
        session, settings=SMALL, storage=LocalFilesystemStorage(tmp_path)
    )
    job, _ = await service.submit(brief=brief, parameters={"mock_simulate": "timeout"})
    # Simulate a row that has been through the database.
    job.submitted_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        seconds=job.timeout_seconds + 30
    )
    session.add(job)
    await session.flush()
    advanced = await service.advance(job, brief=brief)
    assert advanced.status == GenerationStatus.TIMED_OUT.value


async def test_cancel_stops_a_running_job(session, brand_row, tmp_path):
    from app.storage.base import LocalFilesystemStorage

    _item, version = await _content_version(session, brand_row)
    brief, _ = await _brief(session, brand_row, version)
    service = CreativeGenerationService(
        session, settings=SMALL, storage=LocalFilesystemStorage(tmp_path)
    )
    job, _ = await service.submit(brief=brief, parameters={"mock_simulate": "timeout"})
    assert job.status == GenerationStatus.RUNNING.value
    cancelled = await service.cancel(job)
    assert cancelled.status == GenerationStatus.CANCELLED.value
    with pytest.raises(InvalidStateTransition):
        await service.cancel(cancelled)


async def test_new_asset_version_preserves_the_previous_one(session, brand_row, tmp_path):
    from app.storage.base import LocalFilesystemStorage

    storage = LocalFilesystemStorage(tmp_path)
    _item, version = await _content_version(session, brand_row)
    brief, _ = await _brief(session, brand_row, version)
    service = CreativeGenerationService(session, settings=SMALL, storage=storage)
    job, _ = await service.submit(brief=brief, parameters={"seed": 1})
    original = await session.get(CreativeAsset, job.asset_id)
    original_checksum = original.checksum
    original_bytes = await storage.get(original.storage_key)

    new_bytes = render_brand_frame(width=60, height=75, seed=5)
    key = f"creatives/{uuid.uuid4().hex}/replacement.png"
    stored = await storage.put(key, new_bytes, content_type="image/png")
    replacement, invalidated = await CreativeService(session).create_version(
        original,
        data=new_bytes,
        mime_type="image/png",
        filename="replacement.png",
        storage_key=stored.key,
        actor_id=None,
        public_url=stored.public_url,
        width=60,
        height=75,
        is_mock=False,
    )
    assert replacement.asset_version == original.asset_version + 1
    assert replacement.supersedes_asset_id == original.id
    assert replacement.checksum != original_checksum
    assert replacement.storage_key != original.storage_key
    assert replacement.is_mock is False
    assert replacement.selected is False, "a replacement never inherits selection"
    assert (replacement.width, replacement.height) == (60, 75)

    await session.refresh(original)
    assert original.status == CreativeAssetStatus.SUPERSEDED.value
    assert original.checksum == original_checksum
    # The original bytes are still exactly where they were.
    assert await storage.get(original.storage_key) == original_bytes


async def test_selection_binds_the_asset_and_approval_covers_the_combination(
    session, brand_row, tmp_path
):
    from app.models.approval import ApprovalRecord
    from app.services.approval import ApprovalService
    from app.storage.base import LocalFilesystemStorage

    storage = LocalFilesystemStorage(tmp_path)
    item, version = await _content_version(session, brand_row)
    brief, _ = await _brief(session, brand_row, version)
    service = CreativeGenerationService(session, settings=SMALL, storage=storage)
    job, _ = await service.submit(brief=brief, parameters={"seed": 3})
    asset = await session.get(CreativeAsset, job.asset_id)

    asset_service = CreativeService(session)
    asset, invalidated = await asset_service.select(asset, actor_id=None)
    assert asset.selected is True

    approvals = ApprovalService(session)
    await approvals.apply(item.id, "submit", actor_id=None, actor_role="marketer")
    item, record, _ = await approvals.apply(
        item.id, "approve", actor_id=None, actor_role="founder"
    )
    assert record is not None
    assert record.creative_asset_id == asset.id
    assert record.creative_brief_id == brief.id
    assert record.creative_brief_version == brief.version_number
    assert record.creative_asset_version == asset.asset_version
    assert record.creative_binding_hash
    assert await approvals.valid_approval(item) is not None

    # Selecting a different asset invalidates the approval but keeps its history.
    second_job, _ = await service.submit(brief=brief, parameters={"seed": 8})
    second = await session.get(CreativeAsset, second_job.asset_id)
    second, invalidated = await asset_service.select(second, actor_id=None)
    assert invalidated >= 1
    refreshed = await approvals.get_item(item.id)
    assert await approvals.valid_approval(refreshed) is None
    rows = list((await session.execute(select_records(item.id))).scalars().all())
    assert any(row.id == record.id for row in rows), "approval history must be preserved"
    assert any(row.action == "approve" for row in rows)


def select_records(item_id):  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from app.models.approval import ApprovalRecord

    return select(ApprovalRecord).where(ApprovalRecord.content_item_id == item_id)


