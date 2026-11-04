"""phase3a content intelligence

Phase 3A planning layer: weekly content plans, their proposed slots, an internal
planning calendar, and Brand Guardian verdicts bound to immutable content
versions.

This revision is purely additive. It does not alter ``content_items``,
``content_versions``, ``approval_records`` or any Phase 1/2 table, so the publish
gate, the approval state machine and the budget ledger are untouched.

Portability: ``GUID``/``JSONB`` decorators as in earlier revisions. Tables are
created without the circular ``content_items.current_version_id`` problem because
these tables only *reference* existing tables.

Revision ID: 7c1f9a2d4e83
Revises: 60ed69ffc987
Create Date: 2026-11-03 10:05:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import app.db.types


revision: str = '7c1f9a2d4e83'
down_revision: Union[str, None] = '60ed69ffc987'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PLAN_STATUSES = "'draft','generating','generated','failed','archived'"
SLOT_STATUSES = "'proposed','generated','skipped','failed'"
VERDICTS = "'PASS','PASS_WITH_WARNINGS','NEEDS_REVISION','BLOCKED'"
PLATFORMS = "'instagram','linkedin','youtube','x','facebook','reddit'"


def upgrade() -> None:
    op.create_table(
        'content_plans',
        sa.Column('brand_profile_id', app.db.types.GUID(), nullable=False),
        sa.Column('campaign_id', app.db.types.GUID(), nullable=True),
        sa.Column('strategy_id', app.db.types.GUID(), nullable=True),
        sa.Column('research_project_id', app.db.types.GUID(), nullable=True),
        sa.Column('week_start', sa.Date(), nullable=False),
        sa.Column('week_end', sa.Date(), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('objective', sa.Text(), nullable=False),
        sa.Column('platforms', app.db.types.JSONB(), nullable=False),
        sa.Column('config', app.db.types.JSONB(), nullable=False),
        sa.Column('slot_count', sa.Integer(), nullable=False),
        sa.Column('item_count', sa.Integer(), nullable=False),
        sa.Column('completeness_score', sa.Float(), nullable=False),
        sa.Column('generation_metadata', app.db.types.JSONB(), nullable=False),
        sa.Column('generated_by', sa.String(length=32), nullable=False),
        sa.Column('created_by', app.db.types.GUID(), nullable=True),
        sa.Column('id', app.db.types.GUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.CheckConstraint(f'status in ({PLAN_STATUSES})', name=op.f('ck_content_plans_content_plan_status_valid')),
        sa.CheckConstraint('week_end >= week_start', name=op.f('ck_content_plans_content_plan_week_order')),
        sa.CheckConstraint('slot_count >= 0', name=op.f('ck_content_plans_content_plan_slot_count_non_negative')),
        sa.ForeignKeyConstraint(['brand_profile_id'], ['brand_profiles.id'], name=op.f('fk_content_plans_brand_profile_id_brand_profiles'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], name=op.f('fk_content_plans_campaign_id_campaigns'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['strategy_id'], ['marketing_strategies.id'], name=op.f('fk_content_plans_strategy_id_marketing_strategies'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['research_project_id'], ['research_projects.id'], name=op.f('fk_content_plans_research_project_id_research_projects'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_content_plans_created_by_users'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_content_plans')),
        sa.UniqueConstraint('brand_profile_id', 'campaign_id', 'week_start', name='uq_content_plans_brand_campaign_week'),
    )
    with op.batch_alter_table('content_plans', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_content_plans_brand_profile_id'), ['brand_profile_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_content_plans_campaign_id'), ['campaign_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_content_plans_week_start'), ['week_start'], unique=False)
        batch_op.create_index(batch_op.f('ix_content_plans_status'), ['status'], unique=False)
        batch_op.create_index('ix_content_plans_brand_week', ['brand_profile_id', 'week_start'], unique=False)

    op.create_table(
        'content_plan_slots',
        sa.Column('plan_id', app.db.types.GUID(), nullable=False),
        sa.Column('content_item_id', app.db.types.GUID(), nullable=True),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('platform', sa.String(length=32), nullable=False),
        sa.Column('content_type', sa.String(length=48), nullable=False),
        sa.Column('scheduled_date', sa.Date(), nullable=False),
        sa.Column('scheduled_time', sa.String(length=5), nullable=False),
        sa.Column('audience', sa.String(length=120), nullable=False),
        sa.Column('pillar', sa.String(length=160), nullable=False),
        sa.Column('objective', sa.String(length=120), nullable=False),
        sa.Column('primary_message', sa.Text(), nullable=False),
        sa.Column('rationale', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('variant_index', sa.Integer(), nullable=False),
        sa.Column('generation_metadata', app.db.types.JSONB(), nullable=False),
        sa.Column('id', app.db.types.GUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.CheckConstraint(f'platform in ({PLATFORMS})', name=op.f('ck_content_plan_slots_content_plan_slot_platform_valid')),
        sa.CheckConstraint(f'status in ({SLOT_STATUSES})', name=op.f('ck_content_plan_slots_content_plan_slot_status_valid')),
        sa.ForeignKeyConstraint(['plan_id'], ['content_plans.id'], name=op.f('fk_content_plan_slots_plan_id_content_plans'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['content_item_id'], ['content_items.id'], name=op.f('fk_content_plan_slots_content_item_id_content_items'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_content_plan_slots')),
    )
    with op.batch_alter_table('content_plan_slots', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_content_plan_slots_plan_id'), ['plan_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_content_plan_slots_content_item_id'), ['content_item_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_content_plan_slots_scheduled_date'), ['scheduled_date'], unique=False)
        batch_op.create_index('ix_content_plan_slots_plan_platform', ['plan_id', 'platform'], unique=False)

    op.create_table(
        'content_calendar_entries',
        sa.Column('content_item_id', app.db.types.GUID(), nullable=False),
        sa.Column('brand_profile_id', app.db.types.GUID(), nullable=False),
        sa.Column('campaign_id', app.db.types.GUID(), nullable=True),
        sa.Column('plan_id', app.db.types.GUID(), nullable=True),
        sa.Column('planned_date', sa.Date(), nullable=False),
        sa.Column('planned_time', sa.String(length=5), nullable=False),
        sa.Column('note', sa.String(length=500), nullable=False),
        sa.Column('created_by', app.db.types.GUID(), nullable=True),
        sa.Column('id', app.db.types.GUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['content_item_id'], ['content_items.id'], name=op.f('fk_content_calendar_entries_content_item_id_content_items'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['brand_profile_id'], ['brand_profiles.id'], name=op.f('fk_content_calendar_entries_brand_profile_id_brand_profiles'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], name=op.f('fk_content_calendar_entries_campaign_id_campaigns'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['plan_id'], ['content_plans.id'], name=op.f('fk_content_calendar_entries_plan_id_content_plans'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_content_calendar_entries_created_by_users'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_content_calendar_entries')),
        sa.UniqueConstraint('content_item_id', name='uq_content_calendar_entries_item'),
    )
    with op.batch_alter_table('content_calendar_entries', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_content_calendar_entries_content_item_id'), ['content_item_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_content_calendar_entries_brand_profile_id'), ['brand_profile_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_content_calendar_entries_campaign_id'), ['campaign_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_content_calendar_entries_planned_date'), ['planned_date'], unique=False)
        batch_op.create_index('ix_content_calendar_brand_date', ['brand_profile_id', 'planned_date'], unique=False)
        batch_op.create_index('ix_content_calendar_campaign_date', ['campaign_id', 'planned_date'], unique=False)

    op.create_table(
        'content_validations',
        sa.Column('content_item_id', app.db.types.GUID(), nullable=False),
        sa.Column('content_version_id', app.db.types.GUID(), nullable=False),
        sa.Column('verdict', sa.String(length=32), nullable=False),
        sa.Column('score', sa.Float(), nullable=False),
        sa.Column('issues', app.db.types.JSONB(), nullable=False),
        sa.Column('checked_rules', app.db.types.JSONB(), nullable=False),
        sa.Column('blocking_count', sa.Integer(), nullable=False),
        sa.Column('warning_count', sa.Integer(), nullable=False),
        sa.Column('requires_founder_confirmation', sa.Boolean(), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('generation_mode', sa.String(length=32), nullable=False),
        sa.Column('created_by_agent', sa.String(length=64), nullable=True),
        sa.Column('id', app.db.types.GUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.CheckConstraint(f'verdict in ({VERDICTS})', name=op.f('ck_content_validations_content_validation_verdict_valid')),
        sa.ForeignKeyConstraint(['content_item_id'], ['content_items.id'], name=op.f('fk_content_validations_content_item_id_content_items'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['content_version_id'], ['content_versions.id'], name=op.f('fk_content_validations_content_version_id_content_versions'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_content_validations')),
    )
    with op.batch_alter_table('content_validations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_content_validations_content_item_id'), ['content_item_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_content_validations_content_version_id'), ['content_version_id'], unique=False)
        batch_op.create_index('ix_content_validations_version', ['content_version_id', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_table('content_validations')
    op.drop_table('content_calendar_entries')
    op.drop_table('content_plan_slots')
    op.drop_table('content_plans')
