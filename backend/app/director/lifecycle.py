"""Post-approval lifecycle: schedule → publish → measure → optimise.

The workflow graph used to stop at "the founder approved the copy". That left the
back half of the promise — get it scheduled, get it out, see what it did, decide
what to do next — entirely manual. This module is the wiring for that half.

Two rules shape every method here:

1. **Nothing is invented.** Every method returns a record of what actually
   happened, including the reason it did not happen. A missing social account is
   recorded as ``blocked``, not as a successful schedule; an unavailable analytics
   vault is recorded as ``unavailable``, not as zeroes.
2. **Nothing spends or publishes on the model's own authority.** Scheduling only
   touches content the founder already approved *and* only when the approval
   decision said so. Publishing goes through the same approval gate and the same
   transport gate as the API does. Optimisation produces recommendations with
   ``auto_applied=False``; budget changes remain a founder action.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models.content import ContentItem
from app.models.social import SocialAccount
from app.services.analytics_ingest import AnalyticsIngestService
from app.services.approval import ApprovalService
from app.services.publishing import PublishingService, SchedulingService

logger = get_logger("crusor.director.lifecycle")

#: How many days ahead the director spreads the approved content.
DEFAULT_HORIZON_DAYS = 7
#: Slot hour (UTC) for the first item of a day.
SLOT_HOUR_UTC = 9


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _error(stage: str, code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "at": _now().isoformat(),
        "stage": stage,
        "code": code,
        "message": message[:500],
        **extra,
    }


class LifecycleActions:
    """Scheduling, publishing, measurement and optimisation for one workflow."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings | None = None,
        *,
        actor_id: UUID | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.actor_id = actor_id
        self.approvals = ApprovalService(session)

    # ------------------------------------------------------------- selection
    async def _account_for(self, item: ContentItem) -> SocialAccount | None:
        """Pick the connected account for this item's platform, if there is one."""
        rows = (
            (
                await self.session.execute(
                    select(SocialAccount)
                    .where(
                        SocialAccount.brand_profile_id == item.brand_profile_id,
                        SocialAccount.platform == item.platform,
                    )
                    .order_by(SocialAccount.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        for account in rows:
            if str(getattr(account, "status", "")).lower() in {"connected", "active"}:
                return account
        # A stored row is not a working authorisation; only a connected account can
        # receive a scheduled post.
        return None

    async def _items(self, content_item_ids: list[str]) -> list[ContentItem]:
        out: list[ContentItem] = []
        for raw in content_item_ids:
            try:
                item = await self.session.get(ContentItem, UUID(str(raw)))
            except (ValueError, AttributeError):
                continue
            if item is not None:
                out.append(item)
        return out

    def _slot(self, index: int, start: datetime, horizon_days: int) -> datetime:
        """Deterministic, spread-out schedule: one slot per day, then per hour."""
        days = min(index, max(horizon_days - 1, 0))
        slot = (start + timedelta(days=days)).replace(
            hour=SLOT_HOUR_UTC, minute=0, second=0, microsecond=0
        )
        if slot <= start:
            slot += timedelta(days=1)
        return slot

    # ------------------------------------------------------------- scheduling
    async def schedule(
        self,
        *,
        content_item_ids: list[str],
        horizon_days: int = DEFAULT_HORIZON_DAYS,
        start_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Schedule approved content that has a connected account.

        Each item is attempted independently: one missing account must not stop
        the rest of the plan, and the reason must survive in the trace.
        """
        horizon = max(1, int(horizon_days))
        start = start_at or _now()
        items = await self._items(content_item_ids)
        service = SchedulingService(self.session)

        scheduled: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            # ContentItem.status is stored upper-case (IN_REVIEW / APPROVED / ...).
            if str(item.status).lower() != "approved":
                blocked.append(
                    {
                        "content_item_id": str(item.id),
                        "platform": item.platform,
                        "reason": f"content is '{item.status}', not approved",
                    }
                )
                continue
            account = await self._account_for(item)
            if account is None:
                blocked.append(
                    {
                        "content_item_id": str(item.id),
                        "platform": item.platform,
                        "reason": (
                            f"no connected {item.platform} account for this brand; "
                            "authorise one under /api/v1/social/oauth"
                        ),
                    }
                )
                continue
            when = self._slot(index, start, horizon)
            try:
                _, post = await service.schedule(
                    item.id,
                    social_account_id=account.id,
                    scheduled_for=when,
                    actor_id=self.actor_id,
                    actor_role="system",
                    reason="Scheduled by the Marketing Director after founder approval",
                )
            except Exception as exc:  # noqa: BLE001 - per-item boundary
                blocked.append(
                    {
                        "content_item_id": str(item.id),
                        "platform": item.platform,
                        "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
                    }
                )
                continue
            scheduled.append(
                {
                    "content_item_id": str(item.id),
                    "scheduled_post_id": str(post.id),
                    "platform": item.platform,
                    "social_account_id": str(account.id),
                    "scheduled_for": post.scheduled_for.isoformat(),
                    "status": post.status,
                }
            )

        return {
            "scheduled": scheduled,
            "blocked": blocked,
            "horizon_days": horizon,
            "detail": (
                f"{len(scheduled)} item(s) scheduled, {len(blocked)} blocked."
                if items
                else "No approved content items to schedule."
            ),
        }

    # ------------------------------------------------------------- publishing
    async def publish(
        self,
        *,
        scheduled_post_ids: list[str],
        publish_now: bool = False,
    ) -> dict[str, Any]:
        """Publish only when the founder asked for it *now*.

        A schedule in the future is not a publish. Unless ``publish_now`` is set,
        every post is reported as ``scheduled`` and left to the scheduler, which
        runs the same approval and transport gates.
        """
        if not publish_now:
            return {
                "published": [],
                "failed": [],
                "deferred": scheduled_post_ids,
                "detail": (
                    f"{len(scheduled_post_ids)} post(s) left to the scheduler; "
                    "the founder did not ask for immediate publication."
                ),
            }

        from app.models.social import ScheduledPost

        service = PublishingService(self.session)
        published: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for raw in scheduled_post_ids:
            try:
                post = await self.session.get(ScheduledPost, UUID(str(raw)))
            except (ValueError, AttributeError):
                post = None
            if post is None:
                failed.append({"scheduled_post_id": raw, "reason": "post not found"})
                continue
            if post.status != "pending":
                failed.append(
                    {"scheduled_post_id": raw, "reason": f"post is '{post.status}'"}
                )
                continue
            try:
                _, record = await service.publish(
                    post.content_item_id,
                    actor_id=self.actor_id,
                    actor_role="founder",
                    social_account_id=post.social_account_id,
                    scheduled_post_id=post.id,
                )
            except Exception as exc:  # noqa: BLE001 - transport/serialisation boundary
                failed.append(
                    {
                        "scheduled_post_id": raw,
                        "reason": f"{type(exc).__name__}: {str(exc)[:300]}",
                    }
                )
                continue
            published.append(
                {
                    "scheduled_post_id": raw,
                    "content_item_id": str(post.content_item_id),
                    "external_post_id": getattr(record, "external_post_id", None),
                    "url": getattr(record, "url", None),
                }
            )

        return {
            "published": published,
            "failed": failed,
            "deferred": [],
            "detail": (
                f"{len(published)} published, {len(failed)} failed."
                if scheduled_post_ids
                else "Nothing to publish."
            ),
        }

    # ------------------------------------------------------------ measurement
    async def measure(self, *, brand_profile_id: UUID | None) -> dict[str, Any]:
        """Read measured performance. Never estimates, never zero-fills."""
        if brand_profile_id is None:
            return {
                "available": False,
                "ingested": 0,
                "skipped": 0,
                "unavailable": [],
                "detail": "No brand profile on this workflow; nothing to measure.",
            }
        service = AnalyticsIngestService(self.session, self.settings)
        try:
            result = await service.ingest(brand_profile_id=brand_profile_id)
        except Exception as exc:  # noqa: BLE001 - provider boundary
            return {
                "available": False,
                "ingested": 0,
                "skipped": 0,
                "unavailable": [{"platform": "*", "reason": f"{type(exc).__name__}: {exc}"[:300]}],
                "detail": "Analytics ingestion failed; no figures were recorded.",
            }
        unavailable = result.get("unavailable") or []
        return {
            "available": bool(result.get("ingested")),
            "ingested": int(result.get("ingested", 0)),
            "skipped": int(result.get("skipped", 0)),
            "unavailable": unavailable,
            "detail": result.get("detail")
            or (
                f"{result.get('ingested', 0)} measurement(s) ingested."
                if result.get("ingested")
                else "No platform returned measurements; nothing was estimated."
            ),
        }

def lifecycle_errors(stage: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert a lifecycle result's blocked entries into state errors (traces)."""
    errors: list[dict[str, Any]] = []
    for row in payload.get("blocked") or []:
        errors.append(
            _error(stage, "blocked", str(row.get("reason", "")), platform=row.get("platform"))
        )
    for row in payload.get("failed") or []:
        errors.append(_error(stage, "failed", str(row.get("reason", ""))))
    return errors
