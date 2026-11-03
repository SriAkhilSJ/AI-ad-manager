"""Health and integration status.

Every integration reports one of: ``available``, ``degraded`` or ``unavailable``,
with a human-readable reason. This endpoint is the single source of truth the UI
uses to explain *why* something is not working, instead of showing a fake state.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import APIRouter
from sqlalchemy import func, select, text

from app.api.deps import DB
from app.core.config import get_settings
from app.director.graph import graph_topology
from app.llm.gateway import gateway_status
from app.models.user import User
from app.research.base import research_status
from app.storage.base import storage_status
from app.tasks.dispatcher import dispatcher_status

router = APIRouter(prefix="/system", tags=["system"])

START_TIME = time.time()


def _db_dialect() -> str:
    url = get_settings().database_url
    if url.startswith("postgres"):
        return "postgresql"
    if url.startswith("sqlite"):
        return "sqlite"
    return "unknown"


@router.get("/health", summary="Liveness probe (no auth, no DB)")
async def health() -> dict[str, Any]:
    return {"status": "ok", "uptime_seconds": round(time.time() - START_TIME, 1)}


@router.get("/status", summary="Detailed platform + integration status")
async def status(session: DB) -> dict[str, Any]:
    settings = get_settings()
    dialect = _db_dialect()

    db_ok = True
    db_error = ""
    user_count = 0
    try:
        await session.execute(text("SELECT 1"))
        user_count = int(
            (await session.execute(select(func.count()).select_from(User))).scalar_one()
        )
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        db_error = type(exc).__name__

    llm = gateway_status(settings)
    storage = storage_status(settings)
    research = research_status(settings)
    tasks = dispatcher_status(settings)

    def classify(available: bool, configured: bool) -> Literal["available", "degraded", "unavailable"]:
        if available:
            return "available"
        return "degraded" if configured else "unavailable"

    return {
        "app": {
            "name": settings.app_name,
            "environment": settings.environment,
            "version": "1.1.0-phase2",
            "api_prefix": settings.api_v1_prefix,
            "uptime_seconds": round(time.time() - START_TIME, 1),
            "bootstrap_required": user_count == 0,
        },
        "database": {
            "dialect": dialect,
            "status": "available" if db_ok else "unavailable",
            "error": db_error or None,
            "pgvector": "native" if dialect == "postgresql" else "json-fallback",
            "vector_search": (
                "pgvector" if dialect == "postgresql" else "python-cosine"
            ),
        },
        "llm": {
            "status": classify(llm["available"], bool(llm["routing"].get("configured"))),
            "providers": llm["providers"],
            "routing": llm["routing"],
            "monthly_budget_usd": llm["monthly_budget_usd"],
            "note": None
            if llm["available"]
            else "No LLM provider configured. Agents run deterministic paths and record no usage.",
        },
        "tasks": tasks,
        "storage": storage,
        "research": research,
        "strategy": {
            "status": "available",
            "budget_usd": settings.strategy_budget_usd,
            "budget_inr_equivalent": round(
                settings.strategy_budget_usd * settings.usd_inr_rate, 2
            ),
            "max_iterations": settings.strategy_max_iterations,
            "models": {
                "reasoning": settings.llm_reasoning_model,
                "auxiliary": settings.llm_auxiliary_model,
            },
            "price_verified": bool(llm["routing"].get("unverified_models") == []),
            "unverified_models": llm["routing"].get("unverified_models", []),
            "note": (
                "Strategy generation is available and produces founder-approvable drafts. "
                "Nothing is published, sent, spent or applied to a live account."
            ),
        },
        "publishing": {
            "status": "unavailable",
            "mode": "human_approval_required",
            "note": (
                "Phase 1 never publishes to a social network. Approvals and scheduling "
                "are fully implemented; network transport is a Phase 2 integration."
            ),
        },
        "auth": {
            "scheme": "JWT bearer",
            "roles": ["founder", "marketer", "viewer"],
            "approver_role": "founder",
        },
        "director": {
            "orchestrator": "langgraph",
            "checkpointing": "database",
            "human_in_the_loop": True,
            "nodes": len(graph_topology()["nodes"]),
        },
        "rate_limit": {
            "driver": "redis" if settings.redis_url else "memory",
            "per_minute": settings.rate_limit_per_minute,
        },
    }
