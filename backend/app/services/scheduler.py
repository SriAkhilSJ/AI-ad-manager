"""Executing due scheduled posts and refreshing analytics.

Until Phase 4 a scheduled post was a row that nothing ever acted on: the calendar
was honest about it (status stayed ``pending``) but the product could not actually
publish on a schedule. This module closes that.

The dispatch path deliberately reuses :class:`PublishingService`, so a scheduled
publish passes through exactly the same gates as a manual one — approval bound to
the current version, connected account, live transport, verified result. A
schedule created before the content was edited therefore **fails** rather than
publishing a stale version.

Claiming is optimistic: ``UPDATE … WHERE status='pending' AND scheduled_for <= now``
with a rowcount check, so two workers polling the same queue cannot both publish
one post. A failed attempt records the error and increments ``attempts``; after
``max_attempts`` the row is parked as ``failed`` and left for a human, because an
automatic retry loop against a refused approval would never succeed and would only
hide the cause.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.models.social import ScheduledPost
from app.services.analytics_ingest import AnalyticsIngestService
from app.services.publishing import PublishingService

logger = get_logger("crusor.scheduler")

DEFAULT_MAX_ATTEMPTS = 3


class SchedulerService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self.session = session
        self.settings = settings
        self.max_attempts = max_attempts
        self.publishing = PublishingService(session)

    async def due(self, *, now: datetime | None = None, limit: int = 50) -> list[ScheduledPost]:
        moment = now or datetime.now(timezone.utc)
        rows = (
            await self.session.execute(
                select(ScheduledPost)
                .where(
                    ScheduledPost.status == "pending",
                    ScheduledPost.scheduled_for <= moment,
                    ScheduledPost.attempts < self.max_attempts,
                )
                .order_by(ScheduledPost.scheduled_for.asc())
                .limit(limit)
            )
        ).scalars().all()
        return list(rows)

    async def run_due(self, *, now: datetime | None = None, limit: int = 50) -> dict[str, Any]:
        """Publish every due post. Returns a per-post outcome summary."""
        moment = now or datetime.now(timezone.utc)
        candidates = await self.due(now=moment, limit=limit)
        results: list[dict[str, Any]] = []

        for post in candidates:
            claimed = await self.session.execute(
                update(ScheduledPost)
                .where(
                    ScheduledPost.id == post.id,
                    ScheduledPost.status == "pending",
                )
                .values(status="queued", attempts=ScheduledPost.attempts + 1)
            )
            if int(claimed.rowcount or 0) != 1:
                # Another worker took it between the read and the claim.
                results.append({"id": str(post.id), "outcome": "skipped", "detail": "claimed elsewhere"})
                continue

            try:
                item, published = await self.publishing.publish(
                    post.content_item_id,
                    actor_id=None,
                    actor_role="scheduler",
                    social_account_id=post.social_account_id,
                    scheduled_post_id=post.id,
                )
            except AppError as exc:
                await self._record_failure(post, f"{getattr(exc, 'code', 'error')}: {exc}")
                results.append(
                    {"id": str(post.id), "outcome": "failed", "detail": f"{exc.code}: {exc}"}
                )
                continue
            except Exception as exc:  # noqa: BLE001 - a transport boundary
                await self._record_failure(post, f"{type(exc).__name__}: {exc}")
                results.append(
                    {"id": str(post.id), "outcome": "failed", "detail": type(exc).__name__}
                )
                continue

            await self.session.refresh(post)
            results.append(
                {
                    "id": str(post.id),
                    "outcome": "published",
                    "detail": (
                        f"published as {published.external_post_id}"
                        if published is not None
                        else "already published"
                    ),
                }
            )

        await self.session.flush()
        published_count = sum(1 for r in results if r["outcome"] == "published")
        failed_count = sum(1 for r in results if r["outcome"] == "failed")
        if results:
            logger.info(
                "scheduler_run",
                due=len(candidates),
                published=published_count,
                failed=failed_count,
            )
        return {
            "due": len(candidates),
            "published": published_count,
            "failed": failed_count,
            "results": results,
            "detail": (
                "Nothing was due." if not candidates else
                f"{published_count} published, {failed_count} failed of {len(candidates)} due."
            ),
        }

    async def _record_failure(self, post: ScheduledPost, reason: str) -> None:
        await self.session.refresh(post)
        attempts = int(post.attempts or 0)
        post.last_error = reason[:2000]
        post.status = "failed" if attempts >= self.max_attempts else "pending"
        self.session.add(post)
        await self.session.flush()


async def refresh_analytics(
    session: AsyncSession,
    settings: Settings,
    *,
    brand_profile_id: uuid.UUID | None = None,
    days: int = 30,
) -> dict[str, Any]:
    """Ingest measured post metrics for every brand (or one) with a vault."""
    from app.models.brand import BrandProfile

    if brand_profile_id is not None:
        brands = [brand_profile_id]
    else:
        brands = [
            row
            for row in (
                await session.execute(select(BrandProfile.id))
            ).scalars().all()
        ]

    service = AnalyticsIngestService(session, settings)
    totals = {"brands": 0, "requested": 0, "ingested": 0, "skipped": 0}
    unavailable: list[dict[str, str]] = []
    for brand_id in brands:
        result = await service.ingest(brand_profile_id=brand_id, days=days)
        totals["brands"] += 1
        totals["requested"] += int(result["requested"])
        totals["ingested"] += int(result["ingested"])
        totals["skipped"] += int(result["skipped"])
        unavailable.extend(result["unavailable"])
    await session.flush()
    return {**totals, "unavailable": unavailable}
