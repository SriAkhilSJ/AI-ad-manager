"""Analytics retrieval.

Honesty rule: when no ``analytics_snapshots`` rows exist the API says so
(``data_source: "none"``, ``has_data: false``) and returns empty series. It never
returns zero-filled or synthetic series that a chart would render as real
performance.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics import AnalyticsSnapshot
from app.models.social import Platform, PublishedPost


class AnalyticsService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def overview(
        self,
        *,
        brand_profile_id: uuid.UUID,
        days: int = 30,
        platform: str | None = None,
    ) -> dict[str, Any]:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        conds = [
            AnalyticsSnapshot.brand_profile_id == brand_profile_id,
            AnalyticsSnapshot.captured_at >= since,
        ]
        if platform:
            conds.append(AnalyticsSnapshot.platform == platform)

        rows = list(
            (
                await self.session.execute(
                    select(AnalyticsSnapshot)
                    .where(*conds)
                    .order_by(AnalyticsSnapshot.captured_at.asc())
                )
            )
            .scalars()
            .all()
        )

        published_total = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(PublishedPost)
                    .where(
                        PublishedPost.platform.in_(
                            [platform] if platform else Platform.values()
                        )
                    )
                )
            ).scalar_one()
        )

        if not rows:
            return {
                "has_data": False,
                "data_source": "none",
                "window_days": days,
                "platform": platform,
                "message": (
                    "No analytics snapshots recorded. Connect a platform connector to "
                    "collect performance data — nothing is estimated or simulated."
                ),
                "totals": {
                    "impressions": 0,
                    "reach": 0,
                    "engagements": 0,
                    "clicks": 0,
                    "likes": 0,
                    "comments": 0,
                    "shares": 0,
                    "saves": 0,
                },
                "engagement_rate": None,
                "by_platform": [],
                "series": [],
                "published_posts": published_total,
            }

        totals = {
            "impressions": sum(r.impressions for r in rows),
            "reach": sum(r.reach for r in rows),
            "engagements": sum(r.engagements for r in rows),
            "clicks": sum(r.clicks for r in rows),
            "likes": sum(r.likes for r in rows),
            "comments": sum(r.comments for r in rows),
            "shares": sum(r.shares for r in rows),
            "saves": sum(r.saves for r in rows),
        }
        engagement_rate = (
            round(totals["engagements"] / totals["impressions"], 4)
            if totals["impressions"]
            else None
        )

        by_platform: dict[str, dict[str, int]] = defaultdict(
            lambda: {"impressions": 0, "engagements": 0, "clicks": 0, "snapshots": 0}
        )
        for r in rows:
            bucket = by_platform[r.platform]
            bucket["impressions"] += r.impressions
            bucket["engagements"] += r.engagements
            bucket["clicks"] += r.clicks
            bucket["snapshots"] += 1

        daily: dict[date, dict[str, int]] = defaultdict(
            lambda: {"impressions": 0, "engagements": 0}
        )
        for r in rows:
            captured = r.captured_at.date() if isinstance(r.captured_at, datetime) else r.captured_at
            daily[captured]["impressions"] += r.impressions
            daily[captured]["engagements"] += r.engagements

        return {
            "has_data": True,
            "data_source": sorted({r.source for r in rows}),
            "window_days": days,
            "platform": platform,
            "message": None,
            "totals": totals,
            "engagement_rate": engagement_rate,
            "by_platform": [
                {"platform": p, **v} for p, v in sorted(by_platform.items())
            ],
            "series": [
                {"date": d.isoformat(), **v} for d, v in sorted(daily.items())
            ],
            "published_posts": published_total,
        }

    async def record_snapshot(
        self,
        *,
        brand_profile_id: uuid.UUID,
        platform: str,
        published_post_id: uuid.UUID | None = None,
        captured_at: datetime | None = None,
        impressions: int = 0,
        reach: int = 0,
        engagements: int = 0,
        clicks: int = 0,
        likes: int = 0,
        comments: int = 0,
        shares: int = 0,
        saves: int = 0,
        followers_delta: int = 0,
        metrics: dict[str, Any] | None = None,
        source: str = "manual",
        window: str = "lifetime",
    ) -> AnalyticsSnapshot:
        snapshot = AnalyticsSnapshot(
            brand_profile_id=brand_profile_id,
            platform=platform,
            published_post_id=published_post_id,
            captured_at=captured_at or datetime.now(timezone.utc),
            window=window,
            impressions=impressions,
            reach=reach,
            engagements=engagements,
            clicks=clicks,
            likes=likes,
            comments=comments,
            shares=shares,
            saves=saves,
            followers_delta=followers_delta,
            metrics=metrics or {},
            source=source,
        )
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot
