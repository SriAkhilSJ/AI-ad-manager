"""Model usage and cost reporting.

Only *observed* usage is recorded. When no LLM provider is configured there are
simply no rows — the dashboard shows "no model usage recorded" rather than an
invented spend figure.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import AgentRun, AgentRunStatus, ModelUsage


class UsageService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(
        self,
        *,
        agent_run_id: uuid.UUID | None,
        agent_name: str,
        provider: str,
        model: str,
        attempt: int,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        latency_ms: int | None = None,
        success: bool = True,
        operation: str = "structured_call",
        metadata: dict[str, Any] | None = None,
    ) -> ModelUsage:
        row = ModelUsage(
            agent_run_id=agent_run_id,
            agent_name=agent_name,
            provider=provider,
            model=model,
            attempt=attempt,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cost_usd=round(cost_usd, 6),
            latency_ms=latency_ms,
            success=success,
            operation=operation,
            metadata_json=metadata or {},
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def summary(self, *, days: int = 30) -> dict[str, Any]:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        base = select(ModelUsage).where(ModelUsage.recorded_at >= since)
        rows = list((await self.session.execute(base)).scalars().all())

        if not rows:
            return {
                "has_data": False,
                "window_days": days,
                "message": (
                    "No model usage recorded. Agents run in deterministic offline mode "
                    "until an LLM provider is configured."
                ),
                "totals": {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                },
                "by_provider": [],
                "by_agent": [],
                "by_day": [],
            }

        totals = {
            "calls": len(rows),
            "prompt_tokens": sum(r.prompt_tokens for r in rows),
            "completion_tokens": sum(r.completion_tokens for r in rows),
            "total_tokens": sum(r.total_tokens for r in rows),
            "cost_usd": round(sum(r.cost_usd for r in rows), 6),
        }

        by_provider: dict[tuple[str, str], dict[str, Any]] = {}
        by_agent: dict[str, dict[str, Any]] = {}
        by_day: dict[str, dict[str, Any]] = {}

        for r in rows:
            key = (r.provider, r.model)
            bucket = by_provider.setdefault(
                key,
                {
                    "provider": r.provider,
                    "model": r.model,
                    "calls": 0,
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                },
            )
            bucket["calls"] += 1
            bucket["total_tokens"] += r.total_tokens
            bucket["cost_usd"] = round(bucket["cost_usd"] + r.cost_usd, 6)

            agent_bucket = by_agent.setdefault(
                r.agent_name or "unknown",
                {"agent": r.agent_name or "unknown", "calls": 0, "total_tokens": 0, "cost_usd": 0.0},
            )
            agent_bucket["calls"] += 1
            agent_bucket["total_tokens"] += r.total_tokens
            agent_bucket["cost_usd"] = round(agent_bucket["cost_usd"] + r.cost_usd, 6)

            day = r.recorded_at.date().isoformat() if r.recorded_at else ""
            day_bucket = by_day.setdefault(
                day, {"date": day, "total_tokens": 0, "cost_usd": 0.0, "calls": 0}
            )
            day_bucket["total_tokens"] += r.total_tokens
            day_bucket["cost_usd"] = round(day_bucket["cost_usd"] + r.cost_usd, 6)
            day_bucket["calls"] += 1

        return {
            "has_data": True,
            "window_days": days,
            "message": None,
            "totals": totals,
            "by_provider": sorted(by_provider.values(), key=lambda x: -x["cost_usd"]),
            "by_agent": sorted(by_agent.values(), key=lambda x: -x["total_tokens"]),
            "by_day": sorted(by_day.values(), key=lambda x: x["date"]),
        }

    async def month_spend(self) -> float:
        start = datetime.now(timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        stmt = select(func.coalesce(func.sum(ModelUsage.cost_usd), 0.0)).where(
            ModelUsage.recorded_at >= start
        )
        return float((await self.session.execute(stmt)).scalar_one() or 0.0)

    async def agent_run_summary(self, *, limit: int = 20) -> list[dict[str, Any]]:
        stmt = select(AgentRun).order_by(AgentRun.created_at.desc()).limit(limit)
        runs = list((await self.session.execute(stmt)).scalars().all())
        return [
            {
                "id": str(r.id),
                "agent": r.agent_name,
                "status": r.status,
                "workflow_thread_id": r.workflow_thread_id,
                "workflow_node": r.workflow_node,
                "attempts": r.attempts,
                "latency_ms": r.latency_ms,
                "error_code": r.error_code,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            }
            for r in runs
        ]

    async def runs_by_status(self) -> dict[str, int]:
        stmt = select(AgentRun.status, func.count()).group_by(AgentRun.status)
        rows = (await self.session.execute(stmt)).all()
        result = {s.value: 0 for s in AgentRunStatus}
        result.update({status: int(count) for status, count in rows})
        return result
