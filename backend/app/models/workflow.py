"""Durable LangGraph state.

These two tables back :class:`app.director.checkpointer.DatabaseCheckpointer`.
They are intentionally generic (a serialised checkpoint blob plus pending writes)
mirroring LangGraph's own storage contract, so upgrading LangGraph does not
require a schema migration for the graph payload itself.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import GUID, JSONB


class WorkflowCheckpoint(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workflow_checkpoints"

    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    checkpoint_ns: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    checkpoint_id: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_checkpoint_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    checkpoint_type: Mapped[str] = mapped_column(String(64), nullable=False, default="json")
    checkpoint_blob: Mapped[str] = mapped_column(Text, nullable=False, default="")
    metadata_type: Mapped[str] = mapped_column(String(64), nullable=False, default="json")
    metadata_blob: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Convenience projections for the UI (status pills, workflow list).
    run_status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint(
            "thread_id", "checkpoint_ns", "checkpoint_id",
            name="uq_workflow_checkpoints_thread_ns_id",
        ),
        Index("ix_workflow_checkpoints_thread_step", "thread_id", "step"),
    )


class WorkflowWrite(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workflow_writes"

    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    checkpoint_ns: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    checkpoint_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_path: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    idx: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    channel: Mapped[str] = mapped_column(String(128), nullable=False)
    value_type: Mapped[str] = mapped_column(String(64), nullable=False, default="json")
    value_blob: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (
        UniqueConstraint(
            "thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx",
            name="uq_workflow_writes_task_idx",
        ),
    )
