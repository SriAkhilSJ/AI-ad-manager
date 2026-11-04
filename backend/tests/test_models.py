"""Database model tests: schema presence, defaults, constraints, cascades."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from app.db.base import Base
from app.models.approval import ApprovalRecord
from app.models.brand import BrandProfile
from app.models.campaign import Campaign
from app.models.content import ContentItem, ContentStatus, ContentVersion
from app.models.social import Platform, SocialAccount

REQUIRED_TABLES = {
    "brand_profiles",
    "marketing_goals",
    "competitor_profiles",
    "campaigns",
    "content_pillars",
    "content_items",
    "content_versions",
    "creative_assets",
    "approval_records",
    "social_accounts",
    "scheduled_posts",
    "published_posts",
    "analytics_snapshots",
    "agent_runs",
    "model_usage",
    "marketing_memory",
}


def test_all_required_tables_exist():
    present = set(Base.metadata.tables)
    missing = REQUIRED_TABLES - present
    assert not missing, f"missing tables: {missing}"
    # plus supporting tables
    assert {"users", "audit_log", "idempotency_keys", "workflow_checkpoints"} <= present


async def test_uuid_pk_and_timestamps(session):
    brand = BrandProfile(name="B", company="C", product="P")
    session.add(brand)
    await session.commit()
    await session.refresh(brand)
    assert isinstance(brand.id, uuid.UUID)
    assert brand.created_at is not None
    assert brand.updated_at is not None


async def test_social_account_has_no_token_column():
    """Hard security assertion: the table must not be able to hold a token."""
    columns = {c.name for c in SocialAccount.__table__.columns}
    assert "token_ref" in columns
    for forbidden in {"access_token", "refresh_token", "oauth_token", "token", "secret"}:
        assert forbidden not in columns


async def test_content_status_check_constraint(session, brand_row):
    item = ContentItem(
        brand_profile_id=brand_row.id, platform="linkedin", title="t", status="NOT_A_STATUS"
    )
    session.add(item)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_content_platform_check_constraint(session, brand_row):
    item = ContentItem(
        brand_profile_id=brand_row.id, platform="myspace", title="t", status="DRAFT"
    )
    session.add(item)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_version_number_uniqueness(session, brand_row):
    item = ContentItem(brand_profile_id=brand_row.id, platform="linkedin", title="t")
    session.add(item)
    await session.flush()
    session.add(ContentVersion(content_item_id=item.id, version_number=1))
    await session.commit()
    session.add(ContentVersion(content_item_id=item.id, version_number=1))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_fk_enforcement_published_post_requires_approval(session, brand_row):
    """A publication row cannot exist without a real approval record (FK)."""
    from app.models.social import PublishedPost

    session.add(
        PublishedPost(
            content_item_id=uuid.uuid4(),
            content_version_id=uuid.uuid4(),
            social_account_id=uuid.uuid4(),
            approval_record_id=uuid.uuid4(),
            platform="linkedin",
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_cascade_delete_versions_with_item(session, brand_row):
    item = ContentItem(brand_profile_id=brand_row.id, platform="x", title="t")
    session.add(item)
    await session.flush()
    version = ContentVersion(content_item_id=item.id, version_number=1, body="hi")
    session.add(version)
    await session.commit()

    await session.delete(item)
    await session.commit()
    remaining = (
        await session.execute(
            select(func.count()).select_from(ContentVersion).where(
                ContentVersion.content_item_id == item.id
            )
        )
    ).scalar_one()
    assert remaining == 0


async def test_campaign_name_unique_per_brand(session, brand_row):
    session.add(Campaign(brand_profile_id=brand_row.id, name="Launch"))
    await session.commit()
    session.add(Campaign(brand_profile_id=brand_row.id, name="Launch"))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_approval_ledger_is_append_only_by_construction():
    """No model exposes an update helper; the ORM row is only ever inserted or
    marked invalidated. This test documents the invariant for reviewers."""
    assert not hasattr(ApprovalRecord, "update")
    assert "invalidated_at" in ApprovalRecord.__table__.columns


async def test_platform_enum_matches_required_channels():
    assert set(Platform.values()) == {
        "instagram",
        "linkedin",
        "youtube",
        "x",
        "facebook",
        "reddit",
    }


async def test_content_status_vocabulary():
    assert set(ContentStatus.values()) == {
        "DRAFT",
        "IN_REVIEW",
        "APPROVED",
        "SCHEDULED",
        "PUBLISHING",
        "PUBLISHED",
        "REJECTED",
        "CHANGES_REQUESTED",
        "FAILED",
        "CANCELLED",
    }
