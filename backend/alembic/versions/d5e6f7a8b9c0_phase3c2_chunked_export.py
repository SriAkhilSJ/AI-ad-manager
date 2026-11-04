"""phase3c2 chunked flow asset export

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-11-03

Additive only: five columns on ``flow_asset_exports`` that let a large asset arrive
as bounded, individually checksummed chunks instead of one oversized body.

* ``upload_mode`` — ``single`` (the Phase 3C.1 PUT) or ``chunked``;
* ``chunk_bytes`` — the per-chunk ceiling the ticket accepted;
* ``chunk_count`` — total chunks the companion announced;
* ``chunks_received`` — how many arrived, in order, so far;
* ``chunk_manifest`` — ``[{index, sha256, size}]`` for every accepted chunk.

Nothing is backfilled: existing tickets keep ``single`` and an empty manifest, and
the downgrade restores the Phase 3C.1 schema exactly.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "flow_asset_exports",
        sa.Column("upload_mode", sa.String(16), nullable=False, server_default="single"),
    )
    op.add_column(
        "flow_asset_exports",
        sa.Column("chunk_bytes", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "flow_asset_exports",
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "flow_asset_exports",
        sa.Column("chunks_received", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "flow_asset_exports",
        sa.Column("chunk_manifest", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    with op.batch_alter_table("flow_asset_exports") as batch:
        batch.drop_column("chunk_manifest")
        batch.drop_column("chunks_received")
        batch.drop_column("chunk_count")
        batch.drop_column("chunk_bytes")
        batch.drop_column("upload_mode")
