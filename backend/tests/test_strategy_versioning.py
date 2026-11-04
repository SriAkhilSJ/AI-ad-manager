"""Strategy versioning, approval binding and edit-invalidation.

The invariant under test throughout: an approval authorises one exact version,
and changing the strategy invalidates it. A strategy can never be executed
without a live approval for the current version.
"""

from __future__ import annotations

import pytest

from app.core.errors import ConflictError, NotFoundError, ValidationProblem
from app.models.strategy import GrowthExperiment, StrategyStatus, StrategyVersion
from app.services.strategy import (
    VERSION_PAYLOAD_FIELDS,
    StrategyService,
    compute_strategy_hash,
)


def payload(**overrides) -> dict:
    base = {
        "positioning_recommendations": [{"statement": "Ship faster, not louder."}],
        "audience_segmentation": [{"segment": "software developers", "priority": 1}],
        "competitor_differentiation": [{"competitor": "Cursor", "differentiation": "x"}],
        "content_pillars": [{"name": "Build logs", "objective": "developer_acquisition"}],
        "campaign_concepts": [{"name": "Build in public", "objective": "developer_acquisition"}],
        "platform_strategies": [
            {"platform": "linkedin", "frequency": "3/week", "objective": "developer_acquisition"}
        ],
        "weekly_objectives": [{"week": 1, "focus": "Build logs"}],
        "publishing_frequency": {"cadence": "3/week"},
        "lead_generation": [{"mechanism": "Content CTA", "metric": "signup_starts"}],
        "growth_experiments": [
            {
                "name": "Build-log attribution",
                "objective_type": "developer_acquisition",
                "primary_metric": "signup_starts",
                "expected_effort": "medium",
            }
        ],
        "kpis": [
            {"name": "signup_starts", "objective": "developer_acquisition"},
            {"name": "activation_rate", "objective": "product_adoption"},
        ],
        "measurement_plan": {"disclaimer": "definitions only"},
        "business_objectives": ["developer_acquisition"],
        "objective_alignment": {"map": {"developer_acquisition": ["Build logs"]}},
        "research_summary": "3 findings",
        "rationale": "Evidence supports developer-first messaging.",
    }
    base.update(overrides)
    return base


@pytest.fixture()
def founder_user(users):
    return users["founder"]


@pytest.fixture()
def marketer_user(users):
    return users["marketer"]


async def make_strategy(session, brand_row, **kwargs):  # type: ignore[no-untyped-def]
    service = StrategyService(session)
    return await service.create_strategy(
        brand_profile_id=brand_row.id,
        name=kwargs.pop("name", "Crusor developer growth"),
        objective=kwargs.pop("objective", "Acquire developers"),
        payload=kwargs.pop("payload", payload()),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# creation and validation
# --------------------------------------------------------------------------- #
async def test_create_strategy_creates_first_version(session, brand_row):
    strategy, version = await make_strategy(session, brand_row)

    assert strategy.status == StrategyStatus.DRAFT.value
    assert strategy.version_count == 1
    assert strategy.current_version_id == version.id
    assert version.version_number == 1
    assert version.content_hash == compute_strategy_hash(payload())
    assert version.business_objectives == ["developer_acquisition"]


async def test_strategy_requires_a_business_objective(session, brand_row):
    with pytest.raises(ValidationProblem, match="at least one business objective"):
        await make_strategy(session, brand_row, payload=payload(business_objectives=[]))


async def test_strategy_rejects_unknown_objective(session, brand_row):
    with pytest.raises(ValidationProblem, match="Unknown business objective"):
        await make_strategy(session, brand_row, payload=payload(business_objectives=["vibes"]))


async def test_strategy_requires_kpis(session, brand_row):
    with pytest.raises(ValidationProblem, match="must define KPIs"):
        await make_strategy(session, brand_row, payload=payload(kpis=[]))


async def test_empty_name_rejected(session, brand_row):
    with pytest.raises(ValidationProblem, match="name is required"):
        await make_strategy(session, brand_row, name="   ")


async def test_missing_strategy_raises_not_found(session):
    import uuid

    with pytest.raises(NotFoundError):
        await StrategyService(session).get_strategy(uuid.uuid4())


# --------------------------------------------------------------------------- #
# immutability and versioning
# --------------------------------------------------------------------------- #
async def test_new_version_supersedes_without_mutating_the_old_one(session, brand_row, founder_user):
    service = StrategyService(session)
    strategy, v1 = await make_strategy(session, brand_row)
    v1_hash = v1.content_hash
    v1_positioning = list(v1.positioning_recommendations)

    v2 = await service.add_version(
        strategy,
        payload=payload(positioning_recommendations=[{"statement": "Different angle."}]),
        change_note="Sharper positioning",
        created_by=founder_user.id,
    )

    await session.refresh(v1)
    assert v1.content_hash == v1_hash, "the old version must not be rewritten"
    assert v1.positioning_recommendations == v1_positioning
    assert v2.version_number == 2
    assert v2.content_hash != v1_hash
    assert strategy.current_version_id == v2.id
    assert strategy.version_count == 2


async def test_version_numbers_are_unique_per_strategy(session, brand_row):
    service = StrategyService(session)
    strategy, _ = await make_strategy(session, brand_row)
    await service.add_version(strategy, payload=payload(rationale="second"))
    versions = await service.list_versions(strategy.id)
    assert [v.version_number for v in versions] == [2, 1]


async def test_diff_reports_changed_fields(session, brand_row):
    service = StrategyService(session)
    strategy, v1 = await make_strategy(session, brand_row)
    v2 = await service.add_version(
        strategy, payload=payload(rationale="New reasoning", kpis=[{"name": "trial_starts"}])
    )
    diff = await service.diff_versions(v1, v2)
    assert diff["identical"] is False
    assert "rationale" in diff["changed_fields"]
    assert "kpis" in diff["changed_fields"]
    assert diff["changes"]["rationale"]["after"] == "New reasoning"


async def test_diff_of_identical_versions_is_empty(session, brand_row):
    service = StrategyService(session)
    strategy, v1 = await make_strategy(session, brand_row)
    v2 = await service.add_version(strategy, payload=payload())
    diff = await service.diff_versions(v1, v2)
    assert diff["identical"] is True
    assert diff["changed_fields"] == []


# --------------------------------------------------------------------------- #
# approval binding
# --------------------------------------------------------------------------- #
async def test_founder_approval_authorises_the_exact_version(session, brand_row, founder_user):
    service = StrategyService(session)
    strategy, version = await make_strategy(session, brand_row)

    await service.decide(
        strategy,
        action="submit",
        actor_id=founder_user.id,
        actor_role="marketer",
    )
    assert strategy.status == StrategyStatus.IN_REVIEW.value

    record = await service.decide(
        strategy,
        action="approve",
        actor_id=founder_user.id,
        actor_role="founder",
        reason="Aligned with Q4 developer acquisition",
    )
    assert strategy.status == StrategyStatus.APPROVED.value
    assert record.strategy_version_id == version.id
    assert record.content_hash == version.content_hash
    assert record.is_valid is True

    approved_strategy, approved_version, approval = await service.require_approved(strategy.id)
    assert approved_version.id == version.id
    assert approval.id == record.id


async def test_non_founder_cannot_approve(session, brand_row, marketer_user):
    service = StrategyService(session)
    strategy, _ = await make_strategy(session, brand_row)
    with pytest.raises(ConflictError, match="Only the founder"):
        await service.decide(
            strategy, action="approve", actor_id=marketer_user.id, actor_role="marketer"
        )


async def test_unapproved_strategy_cannot_be_executed(session, brand_row):
    service = StrategyService(session)
    strategy, _ = await make_strategy(session, brand_row)
    with pytest.raises(ConflictError, match="no valid approval"):
        await service.require_approved(strategy.id)


async def test_editing_an_approved_strategy_invalidates_approval(session, brand_row, founder_user):
    service = StrategyService(session)
    strategy, v1 = await make_strategy(session, brand_row)
    await service.decide(
        strategy, action="approve", actor_id=founder_user.id, actor_role="founder"
    )
    assert await service.valid_approval(strategy) is not None

    v2 = await service.add_version(
        strategy, payload=payload(rationale="Changed after approval")
    )

    assert strategy.status == StrategyStatus.DRAFT.value
    assert strategy.current_version_id == v2.id
    assert await service.valid_approval(strategy) is None, (
        "approval of the old version must not survive an edit"
    )
    with pytest.raises(ConflictError):
        await service.require_approved(strategy.id)

    history = await service.approval_history(strategy.id)
    approval_row = next(r for r in history if r["action"] == "approve")
    assert approval_row["is_valid"] is False
    assert "superseded" in approval_row["invalidated_reason"]


async def test_approval_of_a_superseded_version_is_refused(session, brand_row, founder_user):
    service = StrategyService(session)
    strategy, v1 = await make_strategy(session, brand_row)
    await service.add_version(strategy, payload=payload(rationale="v2"))

    with pytest.raises(ConflictError, match="Only the current version can be approved"):
        await service.decide(
            strategy,
            action="approve",
            actor_id=founder_user.id,
            actor_role="founder",
            version_id=v1.id,
        )


async def test_hash_mismatch_is_detected(session, brand_row, founder_user):
    """Tampering with a stored version must break the approval."""
    service = StrategyService(session)
    strategy, version = await make_strategy(session, brand_row)
    await service.decide(
        strategy, action="approve", actor_id=founder_user.id, actor_role="founder"
    )

    # Simulate out-of-band modification of the stored payload.
    version.rationale = "Tampered after approval"
    session.add(version)
    await session.flush()

    with pytest.raises(ConflictError, match="no longer matches|stale"):
        await service.require_approved(strategy.id)


async def test_reject_and_request_changes_invalidate(session, brand_row, founder_user):
    service = StrategyService(session)
    strategy, _ = await make_strategy(session, brand_row)
    await service.decide(
        strategy, action="approve", actor_id=founder_user.id, actor_role="founder"
    )
    await service.decide(
        strategy,
        action="request_changes",
        actor_id=founder_user.id,
        actor_role="founder",
        reason="Needs a sharper developer hook",
    )
    assert strategy.status == StrategyStatus.CHANGES_REQUESTED.value
    assert await service.valid_approval(strategy) is None


async def test_unknown_action_is_refused(session, brand_row, founder_user):
    service = StrategyService(session)
    strategy, _ = await make_strategy(session, brand_row)
    with pytest.raises(ValidationProblem, match="Unknown strategy decision"):
        await service.decide(
            strategy, action="publish", actor_id=founder_user.id, actor_role="founder"
        )


async def test_approval_history_is_append_only_and_ordered(session, brand_row, founder_user):
    service = StrategyService(session)
    strategy, _ = await make_strategy(session, brand_row)
    for action in ("submit", "approve", "request_changes"):
        await service.decide(
            strategy,
            action=action,
            actor_id=founder_user.id,
            actor_role="founder",
            reason=f"{action} reason",
        )
    history = await service.approval_history(strategy.id)
    assert [h["action"] for h in history] == ["submit", "approve", "request_changes"]
    assert all(h["actor_role"] == "founder" for h in history)


# --------------------------------------------------------------------------- #
# growth experiments materialise only on approval
# --------------------------------------------------------------------------- #
async def test_experiments_materialise_only_after_approval(session, brand_row, founder_user):
    service = StrategyService(session)
    strategy, _ = await make_strategy(session, brand_row)

    assert await service.list_experiments(strategy.id) == [], (
        "an unapproved idea is not a commitment"
    )

    await service.decide(
        strategy, action="approve", actor_id=founder_user.id, actor_role="founder"
    )
    experiments = await service.list_experiments(strategy.id)
    assert len(experiments) == 1
    assert experiments[0].name == "Build-log attribution"
    assert experiments[0].objective_type == "developer_acquisition"
    assert experiments[0].status == "proposed"

    # approving again must not duplicate them
    await service.decide(
        strategy, action="approve", actor_id=founder_user.id, actor_role="founder"
    )
    assert len(await service.list_experiments(strategy.id)) == 1


# --------------------------------------------------------------------------- #
# every payload field participates in the hash
# --------------------------------------------------------------------------- #
def test_hash_covers_every_payload_field():
    base = payload()
    base_hash = compute_strategy_hash(base)
    for field in VERSION_PAYLOAD_FIELDS:
        changed = dict(base)
        changed[field] = {"mutated": True}
        assert compute_strategy_hash(changed) != base_hash, f"{field} is not hashed"


def test_hash_ignores_non_payload_keys():
    base = payload()
    base_hash = compute_strategy_hash(base)
    extra = dict(base)
    extra["created_by"] = "someone-else"
    extra["unrelated"] = 123
    assert compute_strategy_hash(extra) == base_hash
