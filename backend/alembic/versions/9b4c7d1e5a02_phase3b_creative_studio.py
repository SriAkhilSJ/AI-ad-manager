"""phase3b creative studio

Revision ID: 9b4c7d1e5a02
Revises: 7c1f9a2d4e83
Create Date: 2026-11-03

Additive only: two new tables (creative briefs, generation jobs), extra columns on
``creative_assets``, and the creative binding columns on ``approval_records``.
No existing column is dropped or re-typed, so a downgrade restores the Phase 3A
schema exactly.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "9b4c7d1e5a02"
down_revision: Union[str, None] = "7c1f9a2d4e83"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "creative_briefs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.Column("brand_profile_id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=36), nullable=True),
        sa.Column("content_item_id", sa.String(length=36), nullable=True),
        sa.Column("content_version_id", sa.String(length=36), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("strategy_version_id", sa.String(length=36), nullable=True),
        sa.Column("version_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("brief_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("platform", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("asset_type", sa.String(length=32), nullable=False, server_default="image"),
        sa.Column("creative_objective", sa.Text(), nullable=False, server_default=""),
        sa.Column("visual_concept", sa.Text(), nullable=False, server_default=""),
        sa.Column("scenes", sa.JSON(), nullable=False),
        sa.Column("subjects", sa.JSON(), nullable=False),
        sa.Column("background", sa.Text(), nullable=False, server_default=""),
        sa.Column("lighting", sa.Text(), nullable=False, server_default=""),
        sa.Column("composition", sa.JSON(), nullable=False),
        sa.Column("colour_palette", sa.JSON(), nullable=False),
        sa.Column("typography", sa.JSON(), nullable=False),
        sa.Column("brand_asset_requirements", sa.JSON(), nullable=False),
        sa.Column("image_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("negative_prompt", sa.JSON(), nullable=False),
        sa.Column("aspect_ratio", sa.String(length=16), nullable=False, server_default="4:5"),
        sa.Column("resolution", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("reference_assets", sa.JSON(), nullable=False),
        sa.Column("validation_constraints", sa.JSON(), nullable=False),
        sa.Column("generation_mode", sa.String(length=32), nullable=False, server_default="deterministic"),
        sa.Column("created_by_agent", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("generation_metadata", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["brand_profile_id"], ["brand_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["content_item_id"], ["content_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["content_version_id"], ["content_versions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "content_version_id", "asset_type", "version_number", name="uq_creative_brief_version"
        ),
        sa.CheckConstraint("version_number >= 1", name="creative_brief_version_positive"),
        sa.CheckConstraint(
            "asset_type in ('image','video','carousel','thumbnail','document')",
            name="creative_brief_asset_type_valid",
        ),
    )
    op.create_index("ix_creative_briefs_brand_profile_id", "creative_briefs", ["brand_profile_id"])
    op.create_index("ix_creative_briefs_campaign_id", "creative_briefs", ["campaign_id"])
    op.create_index("ix_creative_briefs_content_item_id", "creative_briefs", ["content_item_id"])
    op.create_index(
        "ix_creative_briefs_content_version_id", "creative_briefs", ["content_version_id"]
    )
    op.create_index("ix_creative_briefs_brief_hash", "creative_briefs", ["brief_hash"])
    op.create_index("ix_creative_briefs_campaign_created", "creative_briefs", ["campaign_id", "created_at"])

    op.create_table(
        "creative_generation_jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.Column("brief_id", sa.String(length=36), nullable=False),
        sa.Column("brief_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("brand_profile_id", sa.String(length=36), nullable=True),
        sa.Column("campaign_id", sa.String(length=36), nullable=True),
        sa.Column("content_item_id", sa.String(length=36), nullable=True),
        sa.Column("content_version_id", sa.String(length=36), nullable=True),
        sa.Column("job_type", sa.String(length=16), nullable=False, server_default="image"),
        sa.Column("provider", sa.String(length=64), nullable=False, server_default="mock"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="queued"),
        sa.Column("external_job_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("submitted_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("negative_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("error_code", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("error_retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ambiguous", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_mock", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("cost_estimate_usd", sa.Float(), nullable=True),
        sa.Column("cost_actual_usd", sa.Float(), nullable=True),
        sa.Column("price_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("asset_id", sa.String(length=36), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("audit_trail", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["brief_id"], ["creative_briefs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["brand_profile_id"], ["brand_profiles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["content_item_id"], ["content_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["content_version_id"], ["content_versions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["asset_id"], ["creative_assets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("idempotency_key", name="uq_creative_job_idempotency"),
        sa.CheckConstraint(
            "status in ('queued','submitted','running','succeeded','failed',"
            "'cancelled','timed_out')",
            name="creative_job_status_valid",
        ),
        sa.CheckConstraint("job_type in ('image','video')", name="creative_job_type_valid"),
        sa.CheckConstraint("attempts >= 0", name="creative_job_attempts_non_negative"),
    )
    op.create_index("ix_creative_jobs_brief_id", "creative_generation_jobs", ["brief_id"])
    op.create_index("ix_creative_jobs_status", "creative_generation_jobs", ["status"])
    op.create_index(
        "ix_creative_jobs_brand_profile_id", "creative_generation_jobs", ["brand_profile_id"]
    )
    op.create_index("ix_creative_jobs_campaign_id", "creative_generation_jobs", ["campaign_id"])
    op.create_index(
        "ix_creative_jobs_content_item_id", "creative_generation_jobs", ["content_item_id"]
    )
    op.create_index("ix_creative_jobs_asset_id", "creative_generation_jobs", ["asset_id"])
    op.create_index("ix_creative_jobs_brief_status", "creative_generation_jobs", ["brief_id", "status"])
    op.create_index("ix_creative_jobs_created", "creative_generation_jobs", ["created_at"])

    with op.batch_alter_table("creative_assets") as batch:
        batch.add_column(sa.Column("brand_profile_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("campaign_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("brief_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("brief_version", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("generation_job_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("asset_version", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("supersedes_asset_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("root_asset_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("duration_seconds", sa.Float(), nullable=True))
        batch.add_column(sa.Column("aspect_ratio", sa.String(length=16), nullable=False, server_default=""))
        batch.add_column(sa.Column("is_mock", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("detected_mime", sa.String(length=128), nullable=False, server_default=""))
        batch.add_column(sa.Column("metadata_verified", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(
            sa.Column(
                "validation_status", sa.String(length=32), nullable=False, server_default="unverified"
            )
        )
        batch.add_column(sa.Column("validation_checked_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("qa_report", sa.JSON(), nullable=False, server_default="{}"))
        batch.create_foreign_key(
            "fk_creative_assets_brand_profile", "brand_profiles", ["brand_profile_id"], ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_creative_assets_campaign", "campaigns", ["campaign_id"], ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_creative_assets_brief", "creative_briefs", ["brief_id"], ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_creative_assets_supersedes", "creative_assets", ["supersedes_asset_id"], ["id"],
            ondelete="SET NULL",
        )
        batch.create_check_constraint(
            "creative_asset_validation_valid",
            "validation_status in ('unverified','pass','pass_with_warnings',"
            "'needs_revision','blocked')",
        )
    op.create_index("ix_creative_assets_brand_profile_id", "creative_assets", ["brand_profile_id"])
    op.create_index("ix_creative_assets_campaign_id", "creative_assets", ["campaign_id"])
    op.create_index("ix_creative_assets_brief_id", "creative_assets", ["brief_id"])
    op.create_index("ix_creative_assets_generation_job_id", "creative_assets", ["generation_job_id"])
    op.create_index("ix_creative_assets_root_asset_id", "creative_assets", ["root_asset_id"])

    with op.batch_alter_table("approval_records") as batch:
        batch.add_column(sa.Column("creative_brief_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("creative_brief_version", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("creative_asset_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("creative_asset_version", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("creative_binding_hash", sa.String(length=64), nullable=False, server_default="")
        )
        batch.create_foreign_key(
            "fk_approvals_creative_brief", "creative_briefs", ["creative_brief_id"], ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_approvals_creative_asset", "creative_assets", ["creative_asset_id"], ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("approval_records") as batch:
        batch.drop_constraint("fk_approvals_creative_asset", type_="foreignkey")
        batch.drop_constraint("fk_approvals_creative_brief", type_="foreignkey")
        batch.drop_column("creative_binding_hash")
        batch.drop_column("creative_asset_version")
        batch.drop_column("creative_asset_id")
        batch.drop_column("creative_brief_version")
        batch.drop_column("creative_brief_id")

    op.drop_index("ix_creative_assets_root_asset_id", table_name="creative_assets")
    op.drop_index("ix_creative_assets_generation_job_id", table_name="creative_assets")
    op.drop_index("ix_creative_assets_brief_id", table_name="creative_assets")
    op.drop_index("ix_creative_assets_campaign_id", table_name="creative_assets")
    op.drop_index("ix_creative_assets_brand_profile_id", table_name="creative_assets")
    with op.batch_alter_table("creative_assets") as batch:
        batch.drop_constraint("creative_asset_validation_valid", type_="check")
        batch.drop_constraint("fk_creative_assets_supersedes", type_="foreignkey")
        batch.drop_constraint("fk_creative_assets_brief", type_="foreignkey")
        batch.drop_constraint("fk_creative_assets_campaign", type_="foreignkey")
        batch.drop_constraint("fk_creative_assets_brand_profile", type_="foreignkey")
        for column in (
            "qa_report",
            "validation_checked_at",
            "validation_status",
            "metadata_verified",
            "detected_mime",
            "is_mock",
            "aspect_ratio",
            "duration_seconds",
            "root_asset_id",
            "supersedes_asset_id",
            "asset_version",
            "selected",
            "generation_job_id",
            "brief_version",
            "brief_id",
            "campaign_id",
            "brand_profile_id",
        ):
            batch.drop_column(column)

    op.drop_index("ix_creative_jobs_created", table_name="creative_generation_jobs")
    op.drop_index("ix_creative_jobs_brief_status", table_name="creative_generation_jobs")
    op.drop_index("ix_creative_jobs_asset_id", table_name="creative_generation_jobs")
    op.drop_index("ix_creative_jobs_content_item_id", table_name="creative_generation_jobs")
    op.drop_index("ix_creative_jobs_campaign_id", table_name="creative_generation_jobs")
    op.drop_index("ix_creative_jobs_brand_profile_id", table_name="creative_generation_jobs")
    op.drop_index("ix_creative_jobs_status", table_name="creative_generation_jobs")
    op.drop_index("ix_creative_jobs_brief_id", table_name="creative_generation_jobs")
    op.drop_table("creative_generation_jobs")

    op.drop_index("ix_creative_briefs_campaign_created", table_name="creative_briefs")
    op.drop_index("ix_creative_briefs_brief_hash", table_name="creative_briefs")
    op.drop_index("ix_creative_briefs_content_version_id", table_name="creative_briefs")
    op.drop_index("ix_creative_briefs_content_item_id", table_name="creative_briefs")
    op.drop_index("ix_creative_briefs_campaign_id", table_name="creative_briefs")
    op.drop_index("ix_creative_briefs_brand_profile_id", table_name="creative_briefs")
    op.drop_table("creative_briefs")
