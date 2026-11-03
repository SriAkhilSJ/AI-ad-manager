from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter

from app.api.deps import DB, CurrentUser
from app.schemas.analytics import AnalyticsOverviewOut, UsageSummaryOut
from app.services.analytics import AnalyticsService
from app.services.brand import BrandService
from app.services.usage import UsageService

router = APIRouter(tags=["analytics"])


@router.get(
    "/analytics/overview",
    response_model=AnalyticsOverviewOut,
    summary="Measured performance (empty when no snapshots exist)",
)
async def analytics_overview(
    session: DB,
    user: CurrentUser,
    brand_profile_id: uuid.UUID | None = None,
    days: int = 30,
    platform: str | None = None,
) -> Any:
    brand_id = brand_profile_id
    if brand_id is None:
        brand = await BrandService(session).get_active()
        if brand is None:
            return {
                "has_data": False,
                "data_source": "none",
                "window_days": days,
                "platform": platform,
                "message": "No active brand profile yet.",
                "totals": {
                    "impressions": 0, "reach": 0, "engagements": 0, "clicks": 0,
                    "likes": 0, "comments": 0, "shares": 0, "saves": 0,
                },
                "engagement_rate": None,
                "by_platform": [],
                "series": [],
                "published_posts": 0,
            }
        brand_id = brand.id
    return await AnalyticsService(session).overview(
        brand_profile_id=brand_id, days=days, platform=platform
    )


@router.get(
    "/usage/summary",
    response_model=UsageSummaryOut,
    summary="Model token usage and cost (empty when no provider is configured)",
)
async def usage_summary(session: DB, user: CurrentUser, days: int = 30) -> Any:
    return await UsageService(session).summary(days=days)


@router.get("/usage/agent-runs", summary="Recent agent executions")
async def agent_runs(session: DB, user: CurrentUser, limit: int = 20) -> Any:
    return await UsageService(session).agent_run_summary(limit=limit)
