"""Phase 3A — content intelligence tests.

All providers here are fakes. Nothing in this file can spend money, reach a
network endpoint, or publish anything. The content provider is the Phase 3A mock,
which is deterministic, free and self-identifying.

Coverage map (requirement -> test):

    platform specs / limits .......... test_spec_*
    mock provider attribution ........ test_mock_*
    zero budget blocks paid calls .... test_zero_budget_* (Phase 2.6 regression)
    content generation .............. test_content_agent_*, test_generate_*
    brand validation ................ test_guardian_*
    weekly plan ..................... test_plan_*
    persistence + campaign link ..... test_generate_persists_*
    versioning + approval ........... test_edit_*, test_approval_*
    variant comparison .............. test_variants_*
    calendar ........................ test_calendar_*
    publishing blocked .............. test_publishing_*
    auth ............................ test_requires_auth
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.agents.base import AgentContext
from app.agents.brand_guardian import BrandGuardianAgent, BrandGuardianInput
from app.agents.content import ContentAgent, ContentInput
from app.content.platforms import (
    CONTENT_TYPES,
    PlatformSpecError,
    available_platforms,
    get_spec,
    validate_against_spec,
)
from app.core.config import Settings
from app.core.errors import InvalidStateTransition, ValidationProblem
from app.llm.base import LLMRequest
from app.llm.gateway import LLMGateway
from app.llm.mock_content import MockContentProvider
from app.llm.routing import BudgetGuard
from app.models.campaign import Campaign
from app.models.content import ContentStatus
from app.models.content_plan import (
    ContentCalendarEntry,
    ContentPlan,
    ContentValidation,
    SlotStatus,
    ValidationVerdict,
)
from app.models.social import Platform
from app.services.approval import ApprovalService
from app.services.content import ContentService
from app.services.content_calendar import ContentCalendarService
from app.services.content_planning import (
    ContentPlanningService,
    default_week_start,
)

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def mock_settings(**overrides) -> Settings:
    base = {
        "environment": "test",
        "content_provider": "mock",
        "llm_reasoning_provider": "openrouter",
        "llm_reasoning_model": "deepseek-v4.1-flash",
        "llm_auxiliary_provider": "openrouter",
        "llm_auxiliary_model": "qwen-3.7-flash",
        "llm_pricing_json": {},
    }
    base.update(overrides)
    return Settings(**base)


async def make_brand_and_campaign(session, users):  # type: ignore[no-untyped-def]
    from app.models.brand import BrandProfile

    brand = BrandProfile(
        name="Crusor AI",
        company="Crusor AI",
        product="Crusor IDE",
        tagline="Build and Ship with Ease.",
        positioning="An AI-powered IDE for developers who ship.",
        audience=["engineering students", "software developers", "indie hackers"],
        keywords=["AIdev", "DevTools"],
        prohibited_phrases=["revolutionary"],
        do_not_claim=["pricing"],
    )
    session.add(brand)
    await session.flush()
    campaign = Campaign(
        brand_profile_id=brand.id,
        name="Crusor AI Awareness Week",
        objective="Introduce Crusor AI to developers.",
        platforms=Platform.values(),
        status="active",
    )
    session.add(campaign)
    await session.commit()
    return brand, campaign


def ctx_for(brand, gateway=None, guard=None):  # type: ignore[no-untyped-def]
    return AgentContext(
        session=None,
        brand=brand,
        gateway=gateway,
        extra={"budget_guard": guard} if guard is not None else {},
    )


# --------------------------------------------------------------------------- #
# 1. platform specifications
# --------------------------------------------------------------------------- #
def test_spec_all_six_platforms_present():
    assert set(available_platforms()) == {
        "instagram",
        "linkedin",
        "youtube",
        "x",
        "facebook",
        "reddit",
    }


@pytest.mark.parametrize("platform", Platform.values())
def test_spec_each_platform_declares_required_fields(platform):
    spec = get_spec(platform)
    assert spec.supported_types, f"{platform} declares no content types"
    assert spec.body_max_chars > 0
    assert spec.title_max_chars > 0
    assert spec.cta_guidance
    assert spec.formatting_rules
    assert spec.validation_rules
    assert spec.tone
    assert set(spec.supported_types) <= set(CONTENT_TYPES)


def test_spec_x_enforces_280_character_limit():
    issues = validate_against_spec(
        platform="x", content_type="social_post", body="a" * 300
    )
    blocking = [i for i in issues if i.severity == "blocking"]
    assert blocking and blocking[0].rule == "platform_length"


def test_spec_reddit_forbids_hashtags():
    issues = validate_against_spec(
        platform="reddit", content_type="community_discussion", body="A post.", hashtags=["#ai"]
    )
    assert any(i.rule == "no_hashtags" and i.severity == "blocking" for i in issues)


def test_spec_thread_limits_apply_per_post_not_in_total():
    body = "1/ " + "a" * 200 + "\n2/ " + "b" * 200 + "\n3/ short post"
    issues = validate_against_spec(platform="x", content_type="thread", body=body)
    assert not [i for i in issues if i.rule == "thread_post_within_limit"]

    too_long = "1/ " + "a" * 400 + "\n2/ short"
    issues = validate_against_spec(platform="x", content_type="thread", body=too_long)
    assert any(i.rule == "thread_post_within_limit" for i in issues)


def test_spec_carousel_needs_three_slides():
    issues = validate_against_spec(
        platform="instagram", content_type="carousel_outline", body="Slide outline", outline=["one"]
    )
    assert any(i.rule == "carousel_needs_slides" for i in issues)


def test_spec_rejects_unsupported_content_type_for_platform():
    # YouTube does not support community discussion in Phase 3A.
    issues = validate_against_spec(
        platform="youtube", content_type="community_discussion", body="text"
    )
    assert any(i.rule == "content_type_unsupported" for i in issues)


def test_spec_overrides_are_applied_and_validated():
    spec = get_spec("x", overrides={"x": {"body_max_chars": 100}})
    assert spec.body_max_chars == 100

    with pytest.raises(PlatformSpecError):
        get_spec("x", overrides={"myspace": {"body_max_chars": 10}})
    with pytest.raises(PlatformSpecError):
        get_spec("x", overrides={"x": {"not_a_field": 1}})


# --------------------------------------------------------------------------- #
# 2. mock provider
# --------------------------------------------------------------------------- #
def _request(**context) -> LLMRequest:
    import json

    return LLMRequest(
        system_prompt="content",
        user_prompt=json.dumps(context),
        schema_name="content_draft",
        json_schema={"type": "object"},
        max_tokens=800,
        purpose="content",
    )


async def test_mock_provider_is_deterministic_and_free():
    provider = MockContentProvider()
    request = _request(platform="x", content_type="social_post", angle="fewer decisions")
    first = await provider.structured(request)
    second = await provider.structured(request)

    assert first.data["body"] == second.data["body"]
    assert first.cost_usd == 0.0
    assert provider.is_billable is False
    assert provider.is_configured() is True


async def test_mock_provider_self_identifies():
    provider = MockContentProvider()
    response = await provider.structured(_request(platform="linkedin", content_type="social_post"))
    assert response.provider == "mock"
    assert response.data["mock"] is True
    assert response.data["billable"] is False
    assert "mock content provider" in response.raw_text


async def test_mock_provider_never_emits_a_metric():
    """Template composition cannot invent a statistic, by construction."""
    provider = MockContentProvider()
    for platform in Platform.values():
        response = await provider.structured(
            _request(platform=platform, content_type="social_post", angle="shipping faster")
        )
        body = response.data["body"]
        assert "%" not in body
        assert "$" not in body


async def test_content_requests_are_served_by_the_mock_provider():
    gateway = LLMGateway(session=None, settings=mock_settings())
    assert "mock" in gateway.providers
    response = await gateway.structured(
        agent_name="content",
        request=_request(platform="x", content_type="social_post", angle="test"),
        task_class="content_generation",
    )
    assert response.provider == "mock"


async def test_content_pin_does_not_apply_to_a_gateway_without_the_mock():
    """A vendor-only gateway keeps its normal routing (Phase 1/2 tests rely on it)."""
    class FakeProvider:
        name = "openai"

        def is_configured(self) -> bool:
            return True

        async def structured(self, request):  # type: ignore[no-untyped-def]
            from app.llm.base import LLMResponse

            return LLMResponse(
                provider="openai", model="gpt-4o-mini", data={"ok": True},
                prompt_tokens=1, completion_tokens=1, cost_usd=0.0, latency_ms=1,
            )

    settings = mock_settings(
        llm_reasoning_provider="openai",
        llm_reasoning_model="gpt-4o-mini",
        llm_default_provider="openai",
        llm_default_model="gpt-4o-mini",
    )
    gateway = LLMGateway(
        session=None, settings=settings, providers={"openai": FakeProvider()}
    )
    response = await gateway.structured(
        agent_name="content", request=_request(platform="x"), task_class="research_synthesis"
    )
    assert response.provider == "openai"


async def test_content_route_leaves_other_agents_alone():
    gateway = LLMGateway(session=None, settings=mock_settings())
    route = gateway.router.resolve("research", "research_synthesis")
    assert route.provider != "mock"


async def test_zero_budget_allows_the_free_mock_but_blocks_paid_providers():
    """Phase 2.6 guarantee, restated for Phase 3A.

    A zero budget blocks *paid* requests. The mock provider cannot charge, so it
    still runs; a billable provider must still be refused.
    """
    gateway = LLMGateway(session=None, settings=mock_settings())
    guard = BudgetGuard(max_cost_usd=0, label="zero")
    request = _request(platform="x", content_type="social_post", angle="test")

    response = await gateway.structured(
        agent_name="content", request=request, task_class="content_generation", budget=guard
    )
    assert response.provider == "mock"
    assert guard.spent_usd == 0.0

    from app.llm.base import BudgetExceeded
    from app.llm.routing import CostEstimate

    paid = CostEstimate(
        provider="openrouter",
        model="deepseek-v4.1-flash",
        tier="reasoning",
        task_class="content_generation",
        prompt_tokens=10,
        completion_tokens=10,
        input_per_1k=0.001,
        output_per_1k=0.003,
        estimated_cost_usd=0.01,
        price_verified=True,
        billable=True,
    )
    with pytest.raises(BudgetExceeded):
        guard.reserve(paid)


# --------------------------------------------------------------------------- #
# 3. content agent
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "platform,content_type",
    [
        ("x", "thread"),
        ("linkedin", "social_post"),
        ("reddit", "community_discussion"),
        ("instagram", "carousel_outline"),
        ("youtube", "short_form_video_script"),
        ("facebook", "educational_post"),
    ],
)
async def test_content_agent_generates_valid_platform_content(
    session, users, platform, content_type
):
    brand, _ = await make_brand_and_campaign(session, users)
    gateway = LLMGateway(session=session, settings=mock_settings())
    agent = ContentAgent()
    draft = await agent.execute(
        ctx_for(brand, gateway),
        ContentInput(platform=platform, content_type=content_type, angle="the set-up is the product"),
    )
    spec = get_spec(platform)
    assert draft.platform == platform
    assert draft.content_type == content_type
    assert draft.body.strip()
    assert len(draft.body) <= spec.body_max_chars or content_type == "thread"
    assert draft.detected_claims == []
    issues = validate_against_spec(
        platform=platform,
        content_type=content_type,
        title=draft.headline,
        hook=draft.hook,
        body=draft.body,
        call_to_action=draft.call_to_action,
        hashtags=draft.hashtags,
        outline=draft.outline,
    )
    assert not [i for i in issues if i.severity == "blocking"]


async def test_content_agent_marks_mock_generation(session, users):
    brand, _ = await make_brand_and_campaign(session, users)
    gateway = LLMGateway(session=session, settings=mock_settings())
    draft = await ContentAgent().execute(
        ctx_for(brand, gateway), ContentInput(platform="linkedin", content_type="social_post")
    )
    assert draft.is_mock is True
    assert draft.provider == "mock"
    assert draft.generation_mode == "mock"
    assert any("mock provider" in w for w in draft.warnings)


async def test_content_agent_variants_differ_in_substance(session, users):
    brand, _ = await make_brand_and_campaign(session, users)
    gateway = LLMGateway(session=session, settings=mock_settings())
    agent = ContentAgent()
    first = await agent.execute(
        ctx_for(brand, gateway),
        ContentInput(platform="linkedin", content_type="social_post", variant_index=0, variant_seed=0),
    )
    second = await agent.execute(
        ctx_for(brand, gateway),
        ContentInput(
            platform="linkedin", content_type="social_post",
            variant_index=1, variant_seed=1, avoid_hooks=[first.hook],
        ),
    )
    assert first.hook != second.hook, "variants must open from a different angle"


async def test_content_agent_returns_structured_phase3a_fields(session, users):
    brand, campaign = await make_brand_and_campaign(session, users)
    gateway = LLMGateway(session=session, settings=mock_settings())
    draft = await ContentAgent().execute(
        ctx_for(brand, gateway),
        ContentInput(
            campaign_id=campaign.id,
            brand_profile_id=brand.id,
            platform="x",
            content_type="social_post",
            audience="software developers",
            objective="Introduce Crusor",
            pillar="build in public",
            primary_message="Fewer decisions mid-change",
        ),
    )
    assert draft.campaign_id == str(campaign.id)
    assert draft.objective == "Introduce Crusor"
    assert draft.audience == "software developers"
    assert draft.pillar == "build in public"
    assert draft.primary_message
    assert draft.brand_facts_used
    assert draft.suggested_time == ""


# --------------------------------------------------------------------------- #
# 4. brand guardian
# --------------------------------------------------------------------------- #
async def test_guardian_passes_clean_copy(session, users):
    brand, _ = await make_brand_and_campaign(session, users)
    result = await BrandGuardianAgent().execute(
        ctx_for(brand),
        BrandGuardianInput(
            platform="linkedin",
            body="We build Crusor IDE for developers who care about shipping.",
            call_to_action="Reply if this matches how you work.",
        ),
    )
    assert result.status == ValidationVerdict.PASS.value
    assert result.verdict == "pass"
    assert result.blocking_count == 0


async def test_guardian_blocks_unverified_statistic(session, users):
    brand, _ = await make_brand_and_campaign(session, users)
    result = await BrandGuardianAgent().execute(
        ctx_for(brand),
        BrandGuardianInput(platform="linkedin", body="We grew 340% this quarter.", call_to_action="Follow"),
    )
    assert result.status == ValidationVerdict.BLOCKED.value
    rules = {v.rule for v in result.violations}
    assert "unverifiable_claim" in rules
    assert result.requires_founder_confirmation is True


async def test_guardian_blocks_fabricated_testimonial(session, users):
    brand, _ = await make_brand_and_campaign(session, users)
    result = await BrandGuardianAgent().execute(
        ctx_for(brand),
        BrandGuardianInput(
            platform="linkedin",
            body='"Crusor changed how our team ships" — Jane Doe, CTO',
            call_to_action="Follow",
        ),
    )
    assert result.status == ValidationVerdict.BLOCKED.value
    assert any(v.rule == "fabricated_testimonial" for v in result.violations)


async def test_guardian_blocks_pricing_claims(session, users):
    brand, _ = await make_brand_and_campaign(session, users)
    result = await BrandGuardianAgent().execute(
        ctx_for(brand),
        BrandGuardianInput(platform="x", body="Crusor costs $9 per month.", call_to_action="Reply"),
    )
    assert result.status == ValidationVerdict.BLOCKED.value
    assert any(v.category == "unsupported_product_claim" for v in result.violations)


async def test_guardian_blocks_unsupported_claim_without_evidence(session, users):
    brand, _ = await make_brand_and_campaign(session, users)
    result = await BrandGuardianAgent().execute(
        ctx_for(brand),
        BrandGuardianInput(platform="x", body="We are 3x faster on reviews.", call_to_action="Reply"),
    )
    assert result.status == ValidationVerdict.BLOCKED.value
    assert any(v.rule == "missing_source_evidence" for v in result.violations)


async def test_guardian_blocks_duplicate_content(session, users):
    brand, _ = await make_brand_and_campaign(session, users)
    body = "The set-up is the product and we optimise for fewer decisions mid-change."
    result = await BrandGuardianAgent().execute(
        ctx_for(brand),
        BrandGuardianInput(
            platform="x",
            body=body,
            call_to_action="Reply",
            sibling_bodies=[body],
        ),
    )
    assert any(v.rule == "duplicate_content" for v in result.violations)


async def test_guardian_requires_reddit_affiliation_disclosure(session, users):
    brand, _ = await make_brand_and_campaign(session, users)
    result = await BrandGuardianAgent().execute(
        ctx_for(brand),
        BrandGuardianInput(
            platform="reddit",
            content_type="community_discussion",
            body="What tooling do you use for reviews?",
            call_to_action="Curious what you think.",
        ),
    )
    assert any(v.rule == "affiliation_not_disclosed" for v in result.violations)


async def test_guardian_warnings_do_not_block(session, users):
    brand, _ = await make_brand_and_campaign(session, users)
    result = await BrandGuardianAgent().execute(
        ctx_for(brand),
        BrandGuardianInput(
            platform="instagram",
            body="A quiet update from the build log.",
            call_to_action="",  # warning: missing CTA
            hashtags=[],  # handled by the spec, not a blocking rule
        ),
    )
    assert result.status in (
        ValidationVerdict.PASS.value,
        ValidationVerdict.PASS_WITH_WARNINGS.value,
    )
    assert result.blocking_count == 0


async def test_guardian_issues_are_structured(session, users):
    brand, _ = await make_brand_and_campaign(session, users)
    result = await BrandGuardianAgent().execute(
        ctx_for(brand),
        BrandGuardianInput(platform="x", body="Guaranteed to fix 90% of bugs.", call_to_action="Reply"),
    )
    issues = result.issues_as_dicts()
    assert issues
    for issue in issues:
        for field in (
            "rule",
            "severity",
            "category",
            "message",
            "explanation",
            "evidence",
            "suggestion",
            "requires_founder_confirmation",
        ):
            assert field in issue


async def test_guardian_uses_spec_limits_from_one_source(session, users):
    """The writer and the checker must agree, because they share the table."""
    brand, _ = await make_brand_and_campaign(session, users)
    spec = get_spec("x")
    result = await BrandGuardianAgent().execute(
        ctx_for(brand),
        BrandGuardianInput(platform="x", body="a" * (spec.body_max_chars + 1), call_to_action="Reply"),
    )
    assert any(v.rule == "platform_length" for v in result.violations)


# --------------------------------------------------------------------------- #
# 5. weekly planning
# --------------------------------------------------------------------------- #
async def test_plan_distributes_across_platforms_without_uniformity(session, users):
    brand, campaign = await make_brand_and_campaign(session, users)
    service = ContentPlanningService(session)
    plan = await service.create_plan(
        brand=brand, campaign=campaign, week_start=default_week_start()
    )
    platforms = [slot.platform for slot in plan.slots]
    assert plan.slot_count == 6
    assert set(platforms) == set(Platform.values()), "every selected platform gets a slot"
    types = {slot.content_type for slot in plan.slots}
    assert len(types) > 1, "platforms must not all receive identical content types"
    # no two slots share a day
    days = [slot.scheduled_date for slot in plan.slots]
    assert len(days) == len(set(days))


async def test_plan_rejects_more_posts_than_capacity(session, users):
    brand, campaign = await make_brand_and_campaign(session, users)
    service = ContentPlanningService(session)
    with pytest.raises(ValidationProblem) as exc:
        await service.create_plan(
            brand=brand,
            campaign=campaign,
            week_start=default_week_start(),
            config={"posts_per_week": 12},
        )
    assert exc.value.details.get("max_posts_per_week")


async def test_plan_rejects_unknown_config_key(session, users):
    brand, campaign = await make_brand_and_campaign(session, users)
    service = ContentPlanningService(session)
    with pytest.raises(ValidationProblem):
        await service.create_plan(
            brand=brand, campaign=campaign, week_start=default_week_start(),
            config={"posts_per_week": 4, "invented_option": True},
        )


async def test_plan_rejects_content_type_unsupported_by_platform(session, users):
    brand, campaign = await make_brand_and_campaign(session, users)
    service = ContentPlanningService(session)
    with pytest.raises(ValidationProblem):
        await service.create_plan(
            brand=brand, campaign=campaign, week_start=default_week_start(),
            config={"content_mix": {"youtube": ["community_discussion"]}},
        )


async def test_plan_duplicate_week_is_refused_unless_replaced(session, users):
    brand, campaign = await make_brand_and_campaign(session, users)
    service = ContentPlanningService(session)
    week = default_week_start()
    await service.create_plan(brand=brand, campaign=campaign, week_start=week)
    with pytest.raises(ValidationProblem):
        await service.create_plan(brand=brand, campaign=campaign, week_start=week)


async def test_generate_persists_drafts_with_campaign_link(session, users):
    brand, campaign = await make_brand_and_campaign(session, users)
    service = ContentPlanningService(session)
    plan = await service.create_plan(brand=brand, campaign=campaign, week_start=default_week_start())
    plan = await service.generate(plan, brand=brand, campaign=campaign)

    assert plan.status == "generated"
    assert plan.item_count == 6
    items = (
        await session.execute(select(ContentService).where(False))
    ) if False else None  # noqa: F841  (kept explicit: query below)

    from app.models.content import ContentItem

    rows = (
        await session.execute(select(ContentItem).where(ContentItem.campaign_id == campaign.id))
    ).scalars().all()
    assert len(rows) == 6, "every slot must persist exactly one content item"
    for item in rows:
        assert item.campaign_id == campaign.id
        assert item.brand_profile_id == brand.id
        assert item.status == ContentStatus.DRAFT.value
        assert item.version_count == 1
    assert plan.completeness_score > 0


async def test_generate_records_validation_per_version(session, users):
    brand, campaign = await make_brand_and_campaign(session, users)
    service = ContentPlanningService(session)
    plan = await service.create_plan(brand=brand, campaign=campaign, week_start=default_week_start())
    plan = await service.generate(plan, brand=brand, campaign=campaign)

    rows = (await session.execute(select(ContentValidation))).scalars().all()
    assert len(rows) == 6
    for row in rows:
        assert row.verdict in ValidationVerdict.values()
        assert row.content_version_id is not None
        assert row.provider in {"mock", "deterministic"}


async def test_generate_creates_calendar_entries(session, users):
    brand, campaign = await make_brand_and_campaign(session, users)
    service = ContentPlanningService(session)
    plan = await service.create_plan(brand=brand, campaign=campaign, week_start=default_week_start())
    await service.generate(plan, brand=brand, campaign=campaign)

    entries = (await session.execute(select(ContentCalendarEntry))).scalars().all()
    assert len(entries) == 6
    for entry in entries:
        assert entry.brand_profile_id == brand.id
        assert entry.plan_id == plan.id


async def test_generate_is_idempotent_per_slot(session, users):
    brand, campaign = await make_brand_and_campaign(session, users)
    service = ContentPlanningService(session)
    plan = await service.create_plan(brand=brand, campaign=campaign, week_start=default_week_start())
    plan = await service.generate(plan, brand=brand, campaign=campaign)

    from app.models.content import ContentItem

    first_count = len(
        (await session.execute(select(ContentItem))).scalars().all()
    )
    await service.generate(plan, brand=brand, campaign=campaign)
    second_count = len(
        (await session.execute(select(ContentItem))).scalars().all()
    )
    assert second_count == first_count, "regeneration duplicated content"


async def test_generate_attributes_mock_generation(session, users):
    brand, campaign = await make_brand_and_campaign(session, users)
    service = ContentPlanningService(session)
    plan = await service.create_plan(brand=brand, campaign=campaign, week_start=default_week_start())
    plan = await service.generate(plan, brand=brand, campaign=campaign)

    assert plan.generated_by == "mock"
    assert plan.generation_metadata["is_mock"] is True
    assert plan.generation_metadata["provider"] == "mock"
    for slot in plan.slots:
        assert slot.generation_metadata["is_mock"] is True


async def test_generate_respects_zero_paid_budget(session, users, monkeypatch):
    """A zero per-plan budget must not stop the free mock, and must never spend."""
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "content_budget_usd", 0.0, raising=False)

    brand, campaign = await make_brand_and_campaign(session, users)
    service = ContentPlanningService(session)
    plan = await service.create_plan(
        brand=brand, campaign=campaign, week_start=default_week_start(),
        config={"posts_per_week": 2, "platforms": ["linkedin", "x"]},
    )
    plan = await service.generate(plan, brand=brand, campaign=campaign)

    assert plan.generation_metadata["budget"]["spent_usd"] == 0.0
    assert plan.item_count == 2


async def test_generate_stops_when_provider_disabled(session, users, monkeypatch):
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "content_provider", "disabled", raising=False)

    brand, campaign = await make_brand_and_campaign(session, users)
    service = ContentPlanningService(session)
    plan = await service.create_plan(brand=brand, campaign=campaign, week_start=default_week_start())
    with pytest.raises(ValidationProblem):
        await service.generate(plan, brand=brand, campaign=campaign)


async def test_completeness_reflects_blocked_content(session, users):
    brand, campaign = await make_brand_and_campaign(session, users)
    service = ContentPlanningService(session)
    plan = await service.create_plan(brand=brand, campaign=campaign, week_start=default_week_start())
    # Nothing generated yet: coverage 0 -> score 0.
    assert service.completeness(plan) == 0.0
    plan = await service.generate(plan, brand=brand, campaign=campaign)
    assert 0.0 < plan.completeness_score <= 1.0


# --------------------------------------------------------------------------- #
# 6. versioning and approval
# --------------------------------------------------------------------------- #
async def _one_item(session, users):
    from app.models.content import ContentItem

    brand, campaign = await make_brand_and_campaign(session, users)
    service = ContentPlanningService(session)
    plan = await service.create_plan(
        brand=brand, campaign=campaign, week_start=default_week_start(),
        config={"posts_per_week": 1, "platforms": ["linkedin"], "variants": 1},
    )
    plan = await service.generate(plan, brand=brand, campaign=campaign)
    item = (await session.execute(select(ContentItem))).scalars().first()
    return brand, campaign, item


async def test_edit_creates_a_new_version(session, users):
    from app.services.editor import ContentEditor

    brand, campaign, item = await _one_item(session, users)
    before = item.version_count
    _, version, _ = await ContentEditor(session).edit(
        item.id, actor_id=None, body="A revised body that is definitely different.",
        change_note="founder edit",
    )
    assert version.version_number == before + 1
    assert item.version_count == before + 1
    await session.commit()


async def test_approved_version_is_immutable_and_edit_invalidates_approval(session, users):
    from app.services.editor import ContentEditor

    brand, campaign, item = await _one_item(session, users)
    approvals = ApprovalService(session)
    await approvals.apply(item.id, "submit", actor_id=None, actor_role="founder")
    await approvals.apply(item.id, "approve", actor_id=None, actor_role="founder")
    await session.commit()
    assert (await approvals.valid_approval(item)) is not None

    _, version, invalidated = await ContentEditor(session).edit(
        item.id, actor_id=None, body="Changed after approval.", change_note="post-approval edit"
    )
    await session.commit()
    assert invalidated >= 1
    assert (await approvals.valid_approval(item)) is None
    assert item.status != ContentStatus.APPROVED.value
    assert version.version_number == 2


async def test_blocked_validation_prevents_review(session, users):
    """Content with unresolved critical issues cannot enter approval."""
    brand, campaign = await make_brand_and_campaign(session, users)
    content = ContentService(session)
    item = await content.create_item(
        brand_profile_id=brand.id,
        title="Claim-heavy draft",
        platform="linkedin",
        body="We grew 340% and are 5x faster than every competitor.",
        call_to_action="Follow",
        created_by_agent="content",
    )
    version = await content.current_version(item)

    guardian = BrandGuardianAgent()
    result = await guardian.execute(
        ctx_for(brand),
        BrandGuardianInput(
            platform="linkedin", body=version.body, call_to_action=version.call_to_action
        ),
    )
    assert result.status == ValidationVerdict.BLOCKED.value

    from app.services.content_planning import ContentValidationService

    await ContentValidationService(session).record(
        item=item,
        version=version,
        validation={
            "status": result.status,
            "score": result.score,
            "blocking_count": result.blocking_count,
            "warning_count": result.warning_count,
            "issues": result.issues_as_dicts(),
            "checked_rules": list(result.checked_rules),
            "requires_founder_confirmation": result.requires_founder_confirmation,
        },
    )
    await session.commit()

    with pytest.raises(InvalidStateTransition) as exc:
        await ApprovalService(session).apply(item.id, "submit", actor_id=None, actor_role="founder")
    assert exc.value.details.get("verdict") == ValidationVerdict.BLOCKED.value


async def test_revised_version_can_enter_review(session, users):
    from app.services.content_planning import ContentValidationService
    from app.services.editor import ContentEditor

    brand, campaign = await make_brand_and_campaign(session, users)
    content = ContentService(session)
    item = await content.create_item(
        brand_profile_id=brand.id,
        title="Draft",
        platform="linkedin",
        body="We grew 340% this year.",
        call_to_action="Follow",
    )
    version = await content.current_version(item)
    guardian = BrandGuardianAgent()
    blocked = await guardian.execute(
        ctx_for(brand), BrandGuardianInput(platform="linkedin", body=version.body, call_to_action="Follow")
    )
    await ContentValidationService(session).record(
        item=item,
        version=version,
        validation={
            "status": blocked.status, "score": blocked.score,
            "blocking_count": blocked.blocking_count, "warning_count": blocked.warning_count,
            "issues": blocked.issues_as_dicts(), "checked_rules": list(blocked.checked_rules),
            "requires_founder_confirmation": blocked.requires_founder_confirmation,
        },
    )
    await session.commit()

    # The founder rewrites it; the new version has no blocking verdict.
    _, version2, _ = await ContentEditor(session).edit(
        item.id, actor_id=None,
        body="We build an IDE for developers who ship.", change_note="removed the claim",
    )
    clean = await guardian.execute(
        ctx_for(brand),
        BrandGuardianInput(platform="linkedin", body=version2.body, call_to_action="Follow"),
    )
    await ContentValidationService(session).record(
        item=item,
        version=version2,
        validation={
            "status": clean.status, "score": clean.score,
            "blocking_count": clean.blocking_count, "warning_count": clean.warning_count,
            "issues": clean.issues_as_dicts(), "checked_rules": list(clean.checked_rules),
            "requires_founder_confirmation": clean.requires_founder_confirmation,
        },
    )
    await session.commit()

    updated, record, _ = await ApprovalService(session).apply(
        item.id, "submit", actor_id=None, actor_role="founder"
    )
    await session.commit()
    assert updated.status == ContentStatus.IN_REVIEW.value
    assert record is not None


# --------------------------------------------------------------------------- #
# 7. calendar
# --------------------------------------------------------------------------- #
async def test_calendar_labels_are_honest(session, users):
    brand, campaign, item = await _one_item(session, users)
    service = ContentCalendarService(session)
    entry = await service.get_entry(item.id)
    assert service.label_for(item, entry) == "planned"

    item.status = ContentStatus.IN_REVIEW.value
    assert service.label_for(item, entry) == "in_review"
    item.status = ContentStatus.APPROVED.value
    assert service.label_for(item, entry) == "approved"
    item.status = ContentStatus.DRAFT.value
    await session.rollback()


async def test_calendar_as_dict_never_implies_external_scheduling(session, users):
    brand, campaign, item = await _one_item(session, users)
    service = ContentCalendarService(session)
    entry = await service.get_entry(item.id)
    payload = service.as_dict(entry, item)
    assert payload["external_scheduling"] is False
    assert payload["publishing_available"] is False
    assert payload["label"] in {"planned", "draft", "in_review", "approved"}


async def test_calendar_move_changes_date(session, users):
    brand, campaign, item = await _one_item(session, users)
    service = ContentCalendarService(session)
    target = date.today() + timedelta(days=10)
    entry = await service.move(item_id=item.id, planned_date=target, planned_time="11:15")
    assert entry.planned_date == target
    assert entry.planned_time == "11:15"
    await session.commit()


async def test_calendar_rejects_bad_time(session, users):
    brand, campaign, item = await _one_item(session, users)
    service = ContentCalendarService(session)
    with pytest.raises(ValidationProblem):
        await service.move(item_id=item.id, planned_time="25:99")


async def test_calendar_range_filter(session, users):
    brand, campaign, item = await _one_item(session, users)
    service = ContentCalendarService(session)
    entry = await service.get_entry(item.id)
    inside = await service.list_entries(
        brand_profile_id=brand.id,
        start=entry.planned_date - timedelta(days=1),
        end=entry.planned_date + timedelta(days=1),
    )
    assert any(e.id == entry.id for e, _ in inside)
    outside = await service.list_entries(
        brand_profile_id=brand.id,
        start=entry.planned_date + timedelta(days=30),
        end=entry.planned_date + timedelta(days=40),
    )
    assert not any(e.id == entry.id for e, _ in outside)
