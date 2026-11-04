"""phase4 social oauth and paid ads

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-11-04

Five additive tables, no changes to existing ones:

* ``oauth_tokens`` — AES-256-GCM ciphertext for platform credentials, in a table
  no API schema maps to. ``social_accounts`` still has no token column.
* ``oauth_flows`` — single-use authorisation attempts (signed state + sealed PKCE
  verifier), so a replayed callback cannot re-exchange a code.
* ``ad_campaigns`` / ``ad_budget_authorizations`` / ``ad_operations`` — the paid
  advertising gate: a proposal, a founder authorisation bound to one exact
  proposal hash, and an append-only ledger of every attempted external mutation.

Nothing is backfilled. Existing installations gain empty tables; publishing stays
disabled until platform credentials *and* ``TOKEN_VAULT_KEY`` are configured, and
ads stay inert until a founder authorises a specific proposal.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PLATFORMS = "'instagram','linkedin','youtube','x','facebook','reddit'"
AD_PLATFORMS = "'meta_ads','google_ads','linkedin_ads'"


def upgrade() -> None:
    op.create_table(
        "oauth_tokens",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "brand_profile_id",
            GUID(),
            sa.ForeignKey("brand_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("account_key", sa.String(255), nullable=False, server_default=""),
        sa.Column("sealed_access", sa.Text(), nullable=False),
        sa.Column("sealed_refresh", sa.Text(), nullable=True),
        sa.Column("token_type", sa.String(32), nullable=False, server_default="Bearer"),
        sa.Column("scopes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(f"platform in ({PLATFORMS})", name="oauth_token_platform_valid"),
        sa.UniqueConstraint(
            "brand_profile_id", "platform", "account_key", name="uq_oauth_tokens_owner"
        ),
    )
    op.create_index("ix_oauth_tokens_brand_profile_id", "oauth_tokens", ["brand_profile_id"])
    op.create_index("ix_oauth_tokens_brand_platform", "oauth_tokens", ["brand_profile_id", "platform"])

    op.create_table(
        "oauth_flows",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "brand_profile_id",
            GUID(),
            sa.ForeignKey("brand_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("state", sa.String(1024), nullable=False),
        sa.Column("sealed_code_verifier", sa.Text(), nullable=True),
        sa.Column("redirect_uri", sa.String(1000), nullable=False, server_default=""),
        sa.Column("requested_scopes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("created_by", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("pkce_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(f"platform in ({PLATFORMS})", name="oauth_flow_platform_valid"),
        sa.UniqueConstraint("state", name="uq_oauth_flows_state"),
    )
    op.create_index("ix_oauth_flows_brand_profile_id", "oauth_flows", ["brand_profile_id"])
    op.create_index("ix_oauth_flows_brand_platform", "oauth_flows", ["brand_profile_id", "platform"])

    op.create_table(
        "ad_campaigns",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "brand_profile_id",
            GUID(),
            sa.ForeignKey("brand_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("campaign_id", GUID(), sa.ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("objective", sa.String(64), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="proposed"),
        sa.Column("plan", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("targeting", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("creatives", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("daily_budget_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_budget_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column("proposal_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("external_campaign_id", sa.String(255), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_agent", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(f"platform in ({AD_PLATFORMS})", name="ad_campaign_platform_valid"),
        sa.CheckConstraint(
            "status in ('proposed','authorized','creating','active','paused','completed',"
            "'rejected','failed')",
            name="ad_campaign_status_valid",
        ),
        sa.CheckConstraint(
            "daily_budget_cents >= 0 and total_budget_cents >= 0",
            name="ad_campaign_budget_non_negative",
        ),
    )
    op.create_index("ix_ad_campaigns_brand_profile_id", "ad_campaigns", ["brand_profile_id"])
    op.create_index("ix_ad_campaigns_brand_status", "ad_campaigns", ["brand_profile_id", "status"])

    op.create_table(
        "ad_budget_authorizations",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "ad_campaign_id",
            GUID(),
            sa.ForeignKey("ad_campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("proposal_hash", sa.String(64), nullable=False),
        sa.Column("max_spend_cents", sa.Integer(), nullable=False),
        sa.Column("allowed_operations", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authorized_by", GUID(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by_operation_id", GUID(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status in ('active','consumed','revoked','expired')",
            name="ad_authorization_status_valid",
        ),
        sa.CheckConstraint("max_spend_cents > 0", name="ad_authorization_spend_positive"),
    )
    op.create_index(
        "ix_ad_budget_authorizations_ad_campaign_id", "ad_budget_authorizations", ["ad_campaign_id"]
    )
    op.create_index(
        "ix_ad_authorizations_campaign_status",
        "ad_budget_authorizations",
        ["ad_campaign_id", "status"],
    )

    op.create_table(
        "ad_operations",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "ad_campaign_id",
            GUID(),
            sa.ForeignKey("ad_campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "authorization_id",
            GUID(),
            sa.ForeignKey("ad_budget_authorizations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("request_fingerprint", sa.String(64), nullable=False, server_default=""),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("actor_id", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_role", sa.String(32), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "kind in ('create','launch','pause','resume','budget_change','read')",
            name="ad_operation_kind_valid",
        ),
        sa.CheckConstraint(
            "outcome in ('requested','refused','succeeded','failed','unverified')",
            name="ad_operation_outcome_valid",
        ),
        sa.UniqueConstraint(
            "ad_campaign_id", "kind", "request_fingerprint", name="uq_ad_operations_idempotent"
        ),
    )
    op.create_index("ix_ad_operations_ad_campaign_id", "ad_operations", ["ad_campaign_id"])
    op.create_index(
        "ix_ad_operations_campaign_created", "ad_operations", ["ad_campaign_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_ad_operations_campaign_created", table_name="ad_operations")
    op.drop_index("ix_ad_operations_ad_campaign_id", table_name="ad_operations")
    op.drop_table("ad_operations")
    op.drop_index("ix_ad_authorizations_campaign_status", table_name="ad_budget_authorizations")
    op.drop_index(
        "ix_ad_budget_authorizations_ad_campaign_id", table_name="ad_budget_authorizations"
    )
    op.drop_table("ad_budget_authorizations")
    op.drop_index("ix_ad_campaigns_brand_status", table_name="ad_campaigns")
    op.drop_index("ix_ad_campaigns_brand_profile_id", table_name="ad_campaigns")
    op.drop_table("ad_campaigns")
    op.drop_index("ix_oauth_flows_brand_platform", table_name="oauth_flows")
    op.drop_index("ix_oauth_flows_brand_profile_id", table_name="oauth_flows")
    op.drop_table("oauth_flows")
    op.drop_index("ix_oauth_tokens_brand_platform", table_name="oauth_tokens")
    op.drop_index("ix_oauth_tokens_brand_profile_id", table_name="oauth_tokens")
    op.drop_table("oauth_tokens")
