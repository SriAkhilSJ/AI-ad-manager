"""phase3c1 flow bridge and asset export

Revision ID: c4d5e6f7a8b9
Revises: 9b4c7d1e5a02
Create Date: 2026-11-03

Additive only:

* ``flow_companion_sessions`` — which local companions have authenticated;
* ``flow_asset_exports`` — one-time, checksum-pinned upload tickets;
* six ``flow_*`` binding columns plus ``flow_binding_hash`` on ``approval_records``.

No existing column is dropped or re-typed, so the downgrade restores the Phase 3B
schema exactly. Server defaults matter here: the tables are inserted from services
that do not set ``created_at``/``updated_at`` themselves.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "9b4c7d1e5a02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TIMESTAMP_SERVER_DEFAULT = sa.text("(CURRENT_TIMESTAMP)")


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_TIMESTAMP_SERVER_DEFAULT,
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=_TIMESTAMP_SERVER_DEFAULT,
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "flow_companion_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        *_timestamps(),
        sa.Column("companion_id", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("protocol", sa.String(length=32), nullable=False, server_default="flow.companion.v1"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="connected"),
        sa.Column("extension_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("extension_version", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("flow_tab_present", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("bridge_paired", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("live_generation_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("client_host", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("secret_fingerprint", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("capabilities", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status in ('connected','stale','disconnected')",
            name="ck_flow_session_status",
        ),
    )
    op.create_index("ix_flow_companion_sessions_companion_id", "flow_companion_sessions", ["companion_id"])
    op.create_index("ix_flow_companion_sessions_status", "flow_companion_sessions", ["status"])
    op.create_index(
        "ix_flow_sessions_companion_seen",
        "flow_companion_sessions",
        ["companion_id", "last_seen_at"],
    )

    op.create_table(
        "flow_asset_exports",
        sa.Column("id", sa.String(length=36), primary_key=True),
        *_timestamps(),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("companion_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("generation_job_id", sa.String(length=36), nullable=True),
        sa.Column("campaign_id", sa.String(length=36), nullable=True),
        sa.Column("content_item_id", sa.String(length=36), nullable=True),
        sa.Column("content_version_id", sa.String(length=36), nullable=True),
        sa.Column("brief_id", sa.String(length=36), nullable=True),
        sa.Column("brief_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expected_sha256", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("declared_content_type", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("filename", sa.String(length=255), nullable=False, server_default="flow-asset"),
        sa.Column("max_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("received_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checksum", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("storage_key", sa.Text(), nullable=False, server_default=""),
        sa.Column("asset_id", sa.String(length=36), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.CheckConstraint(
            "status in ('pending','consumed','expired','failed','cancelled')",
            name="ck_flow_export_status",
        ),
        sa.ForeignKeyConstraint(["generation_job_id"], ["creative_generation_jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["content_item_id"], ["content_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["content_version_id"], ["content_versions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["brief_id"], ["creative_briefs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["asset_id"], ["creative_assets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_flow_asset_exports_token_hash", "flow_asset_exports", ["token_hash"], unique=True)
    op.create_index("ix_flow_asset_exports_status", "flow_asset_exports", ["status"])
    op.create_index("ix_flow_exports_status_expires", "flow_asset_exports", ["status", "expires_at"])

    with op.batch_alter_table("approval_records") as batch:
        batch.add_column(sa.Column("flow_prompt_hash", sa.String(length=64), nullable=False, server_default=""))
        batch.add_column(sa.Column("flow_provider", sa.String(length=64), nullable=False, server_default=""))
        batch.add_column(sa.Column("flow_model", sa.String(length=64), nullable=False, server_default=""))
        batch.add_column(sa.Column("flow_aspect_ratio", sa.String(length=16), nullable=False, server_default=""))
        batch.add_column(sa.Column("flow_duration_seconds", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("flow_output_count", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("flow_binding_hash", sa.String(length=64), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("approval_records") as batch:
        batch.drop_column("flow_binding_hash")
        batch.drop_column("flow_output_count")
        batch.drop_column("flow_duration_seconds")
        batch.drop_column("flow_aspect_ratio")
        batch.drop_column("flow_model")
        batch.drop_column("flow_provider")
        batch.drop_column("flow_prompt_hash")

    op.drop_index("ix_flow_exports_status_expires", table_name="flow_asset_exports")
    op.drop_index("ix_flow_asset_exports_status", table_name="flow_asset_exports")
    op.drop_index("ix_flow_asset_exports_token_hash", table_name="flow_asset_exports")
    op.drop_table("flow_asset_exports")

    op.drop_index("ix_flow_sessions_companion_seen", table_name="flow_companion_sessions")
    op.drop_index("ix_flow_companion_sessions_status", table_name="flow_companion_sessions")
    op.drop_index("ix_flow_companion_sessions_companion_id", table_name="flow_companion_sessions")
    op.drop_table("flow_companion_sessions")
