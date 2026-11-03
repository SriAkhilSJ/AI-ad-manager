"""Agent execution ledger and model usage / cost accounting."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import GUID, JSONB


class AgentRunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"

    @classmethod
    def values(cls) -> list[str]:
        return [c.value for c in cls]


class AgentRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_runs"

    agent_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AgentRunStatus.PENDING.value, index=True
    )
    # Correlates the run with a LangGraph thread so UI can show the workflow.
    workflow_thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    workflow_node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True
    )
    brand_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("brand_profiles.id", ondelete="SET NULL"), nullable=True
    )
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    output_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    trace: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status in ('pending','running','succeeded','failed','timeout',"
            "'budget_exceeded','cancelled')",
            name="agent_run_status_valid",
        ),
        Index("ix_agent_runs_agent_created", "agent_name", "created_at"),
    )


class ModelUsage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Token and cost accounting.

    Unique on ``(agent_run_id, attempt)`` so a retried agent cannot double-bill,
    and so the same logical call is only ever counted once.
    """

    __tablename__ = "model_usage"

    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(default=True, nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False, default="structured_call")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "prompt_tokens >= 0 and completion_tokens >= 0 and total_tokens >= 0",
            name="model_usage_tokens_non_negative",
        ),
        CheckConstraint("cost_usd >= 0", name="model_usage_cost_non_negative"),
        UniqueConstraint("agent_run_id", "attempt", name="uq_model_usage_run_attempt"),
        Index("ix_model_usage_provider_model", "provider", "model"),
        Index("ix_model_usage_recorded", "recorded_at"),
    )
