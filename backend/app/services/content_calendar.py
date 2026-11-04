"""Internal content calendar (Phase 3A).

**This is a planning calendar, not a scheduler.** Nothing here talks to Instagram,
LinkedIn or any other platform, and nothing here creates a ``scheduled_posts`` row.
The Phase 1 external scheduling gate (which requires a live social account and a
valid approval) is untouched and still the only route to a real schedule.

Labels are derived from the content item's real approval state, so the calendar
can never show "Approved" for a draft that is not approved.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationProblem
from app.models.content import ContentItem, ContentStatus
from app.models.content_plan import ContentCalendarEntry
from app.models.social import Platform

#: Display labels. ``planned`` is the calendar's own word for "in the internal
#: plan but not yet in review"; every other label comes from the real state.
PLANNED_LABEL = "planned"

STATUS_LABELS: dict[str, str] = {
    ContentStatus.DRAFT.value: "draft",
    ContentStatus.IN_REVIEW.value: "in_review",
    ContentStatus.APPROVED.value: "approved",
    ContentStatus.CHANGES_REQUESTED.value: "changes_requested",
    ContentStatus.REJECTED.value: "rejected",
    ContentStatus.SCHEDULED.value: "scheduled_internally",
    ContentStatus.CANCELLED.value: "cancelled",
    ContentStatus.FAILED.value: "failed",
}


def _validate_clock(value: str) -> str:
    try:
        return datetime.strptime(str(value), "%H:%M").strftime("%H:%M")
    except (TypeError, ValueError) as exc:
        raise ValidationProblem(f"planned_time '{value}' must be HH:MM (24h).") from exc


class ContentCalendarService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ reads
    async def get_entry(self, item_id: uuid.UUID) -> ContentCalendarEntry | None:
        return (
            await self.session.execute(
                select(ContentCalendarEntry).where(
                    ContentCalendarEntry.content_item_id == item_id
                )
            )
        ).scalar_one_or_none()

    async def list_entries(
        self,
        *,
        brand_profile_id: uuid.UUID,
        start: date | None = None,
        end: date | None = None,
        platform: str | None = None,
        campaign_id: uuid.UUID | None = None,
        limit: int = 500,
    ) -> list[tuple[ContentCalendarEntry, ContentItem]]:
        stmt = (
            select(ContentCalendarEntry, ContentItem)
            .join(ContentItem, ContentItem.id == ContentCalendarEntry.content_item_id)
            .where(ContentCalendarEntry.brand_profile_id == brand_profile_id)
        )
        if start is not None:
            stmt = stmt.where(ContentCalendarEntry.planned_date >= start)
        if end is not None:
            stmt = stmt.where(ContentCalendarEntry.planned_date <= end)
        if platform:
            stmt = stmt.where(ContentItem.platform == platform)
        if campaign_id is not None:
            stmt = stmt.where(ContentCalendarEntry.campaign_id == campaign_id)
        stmt = stmt.order_by(
            ContentCalendarEntry.planned_date, ContentCalendarEntry.planned_time
        ).limit(min(max(limit, 1), 1000))
        rows = (await self.session.execute(stmt)).all()
        return [(row[0], row[1]) for row in rows]

    # ----------------------------------------------------------------- writes
    async def upsert(
        self,
        *,
        item: ContentItem,
        planned_date: date,
        planned_time: str,
        actor_id: uuid.UUID | None = None,
        note: str = "",
        plan_id: uuid.UUID | None = None,
    ) -> ContentCalendarEntry:
        entry = await self.get_entry(item.id)
        if entry is None:
            entry = ContentCalendarEntry(
                content_item_id=item.id,
                brand_profile_id=item.brand_profile_id,
                campaign_id=item.campaign_id,
                plan_id=plan_id,
                planned_date=planned_date,
                planned_time=_validate_clock(planned_time),
                note=note,
                created_by=actor_id,
            )
            self.session.add(entry)
        else:
            entry.planned_date = planned_date
            entry.planned_time = _validate_clock(planned_time)
            if note:
                entry.note = note[:500]
        await self.session.flush()
        return entry

    async def move(
        self,
        *,
        item_id: uuid.UUID,
        planned_date: date | None = None,
        planned_time: str | None = None,
        note: str | None = None,
        brand_profile_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
    ) -> ContentCalendarEntry:
        item = await self.session.get(ContentItem, item_id)
        if item is None:
            raise NotFoundError("Content item not found.")
        if brand_profile_id is not None and item.brand_profile_id != brand_profile_id:
            raise NotFoundError("Content item not found.")
        entry = await self.get_entry(item_id)
        if entry is None:
            entry = await self.upsert(
                item=item,
                planned_date=planned_date or date.today(),
                planned_time=planned_time or "09:00",
                actor_id=actor_id,
                note=note or "",
            )
            return entry
        if planned_date is not None:
            entry.planned_date = planned_date
        if planned_time is not None:
            entry.planned_time = _validate_clock(planned_time)
        if note is not None:
            entry.note = note[:500]
        if actor_id is not None:
            entry.created_by = entry.created_by or actor_id
        await self.session.flush()
        return entry

    async def delete_for_item(self, item_id: uuid.UUID) -> None:
        entry = await self.get_entry(item_id)
        if entry is not None:
            await self.session.delete(entry)
            await self.session.flush()

    # -------------------------------------------------------------- rendering
    def label_for(self, item: ContentItem, entry: ContentCalendarEntry | None) -> str:
        """A label that reflects reality, never optimism.

        Draft items that carry a calendar entry read ``planned``; everything else
        maps from the approval state machine. External scheduling is a *different*
        concept and is labelled ``scheduled_internally`` so it cannot be confused
        with a real platform schedule.
        """
        if item.status == ContentStatus.DRAFT.value and entry is not None:
            return PLANNED_LABEL
        return STATUS_LABELS.get(item.status, item.status.lower())

    def as_dict(
        self, entry: ContentCalendarEntry, item: ContentItem
    ) -> dict[str, Any]:
        return {
            "entry_id": str(entry.id),
            "content_item_id": str(item.id),
            "title": item.title,
            "platform": item.platform,
            "content_type": item.format,
            "campaign_id": str(entry.campaign_id) if entry.campaign_id else None,
            "plan_id": str(entry.plan_id) if entry.plan_id else None,
            "status": item.status,
            "label": self.label_for(item, entry),
            "planned_date": entry.planned_date.isoformat(),
            "planned_time": entry.planned_time,
            "note": entry.note,
            "approval_required": True,
            # Explicit, so no client can read this as "posted":
            "external_scheduling": False,
            "publishing_available": False,
        }

    # ------------------------------------------------------------ aggregation
    def month_bounds(anchor: date) -> tuple[date, date]:  # type: ignore[misc]
        first = anchor.replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        return first, nxt - timedelta(days=1)


def week_bounds(anchor: date) -> tuple[date, date]:
    start = anchor - timedelta(days=anchor.weekday())
    return start, start + timedelta(days=6)
