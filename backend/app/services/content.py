"""Content domain service: creation, immutable versioning, hashing.

Every mutation that changes what would be published creates a **new version**.
There is no in-place edit of a version's publishable fields — that is what makes
approval binding meaningful.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationProblem
from app.models.content import ContentItem, ContentStatus, ContentVersion
from app.models.creative import CreativeAsset
from app.models.social import Platform

PUBLISHABLE_FIELDS = ("headline", "body", "hashtags", "call_to_action", "media_refs")


def compute_content_hash(
    *,
    headline: str,
    body: str,
    hashtags: Sequence[str],
    call_to_action: str,
    media_refs: Sequence[str],
) -> str:
    """Deterministic fingerprint of the publishable payload.

    Stored on both the version and any approval that authorises it, so a mismatch
    at publish time proves the approved artefact is not the artefact being
    published.
    """
    payload = {
        "headline": headline,
        "body": body,
        "hashtags": list(hashtags),
        "call_to_action": call_to_action,
        "media_refs": list(media_refs),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ContentService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ read
    async def get_item(self, item_id: uuid.UUID) -> ContentItem:
        item = await self.session.get(ContentItem, item_id)
        if item is None:
            raise NotFoundError(f"Content item {item_id} not found.")
        return item

    async def get_version(self, version_id: uuid.UUID) -> ContentVersion:
        version = await self.session.get(ContentVersion, version_id)
        if version is None:
            raise NotFoundError(f"Content version {version_id} not found.")
        return version

    async def current_version(self, item: ContentItem) -> ContentVersion | None:
        if item.current_version_id is None:
            return None
        return await self.session.get(ContentVersion, item.current_version_id)

    async def list_versions(self, item_id: uuid.UUID) -> list[ContentVersion]:
        stmt = (
            select(ContentVersion)
            .where(ContentVersion.content_item_id == item_id)
            .order_by(ContentVersion.version_number.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_items(
        self,
        *,
        brand_profile_id: uuid.UUID | None = None,
        campaign_id: uuid.UUID | None = None,
        status: str | None = None,
        platform: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ContentItem], int]:
        conditions = []
        if brand_profile_id:
            conditions.append(ContentItem.brand_profile_id == brand_profile_id)
        if campaign_id:
            conditions.append(ContentItem.campaign_id == campaign_id)
        if status:
            conditions.append(ContentItem.status == status)
        if platform:
            conditions.append(ContentItem.platform == platform)

        base = select(ContentItem)
        count_stmt = select(func.count()).select_from(ContentItem)
        for cond in conditions:
            base = base.where(cond)
            count_stmt = count_stmt.where(cond)
        stmt = base.order_by(ContentItem.updated_at.desc()).limit(limit).offset(offset)
        items = list((await self.session.execute(stmt)).scalars().all())
        total = int((await self.session.execute(count_stmt)).scalar_one())
        return items, total

    # ----------------------------------------------------------------- write
    async def create_item(
        self,
        *,
        brand_profile_id: uuid.UUID,
        title: str,
        platform: str,
        format: str = "post",
        campaign_id: uuid.UUID | None = None,
        pillar_id: uuid.UUID | None = None,
        created_by: uuid.UUID | None = None,
        created_by_agent: str | None = None,
        body: str = "",
        headline: str = "",
        hashtags: Sequence[str] | None = None,
        call_to_action: str = "",
        media_refs: Sequence[str] | None = None,
        generation_metadata: dict[str, Any] | None = None,
        tags: Sequence[str] | None = None,
    ) -> ContentItem:
        if platform not in Platform.values():
            raise ValidationProblem(
                f"Unsupported platform '{platform}'.",
                details={"allowed": Platform.values()},
            )
        item = ContentItem(
            brand_profile_id=brand_profile_id,
            campaign_id=campaign_id,
            pillar_id=pillar_id,
            platform=platform,
            format=format,
            title=title,
            status=ContentStatus.DRAFT.value,
            created_by_agent=created_by_agent,
            tags=list(tags or []),
        )
        self.session.add(item)
        await self.session.flush()

        version = await self._append_version(
            item,
            headline=headline,
            body=body,
            hashtags=list(hashtags or []),
            call_to_action=call_to_action,
            media_refs=list(media_refs or []),
            change_note="Initial draft",
            created_by=created_by,
            created_by_agent=created_by_agent,
            generation_metadata=generation_metadata or {},
        )
        item.current_version_id = version.id
        item.version_count = 1
        await self.session.flush()
        return item

    async def _append_version(
        self,
        item: ContentItem,
        *,
        headline: str,
        body: str,
        hashtags: list[str],
        call_to_action: str,
        media_refs: list[str],
        change_note: str,
        created_by: uuid.UUID | None,
        created_by_agent: str | None = None,
        generation_metadata: dict[str, Any] | None = None,
    ) -> ContentVersion:
        next_number = await self._next_version_number(item.id)
        version = ContentVersion(
            content_item_id=item.id,
            version_number=next_number,
            headline=headline,
            body=body,
            hashtags=hashtags,
            call_to_action=call_to_action,
            media_refs=media_refs,
            content_hash=compute_content_hash(
                headline=headline,
                body=body,
                hashtags=hashtags,
                call_to_action=call_to_action,
                media_refs=media_refs,
            ),
            change_note=change_note,
            created_by=created_by,
            created_by_agent=created_by_agent,
            generation_metadata=generation_metadata or {},
        )
        self.session.add(version)
        await self.session.flush()
        return version

    async def _next_version_number(self, item_id: uuid.UUID) -> int:
        stmt = select(func.max(ContentVersion.version_number)).where(
            ContentVersion.content_item_id == item_id
        )
        current = (await self.session.execute(stmt)).scalar_one_or_none()
        return int(current or 0) + 1

    async def add_version(
        self,
        item: ContentItem,
        *,
        headline: str | None = None,
        body: str | None = None,
        hashtags: Sequence[str] | None = None,
        call_to_action: str | None = None,
        media_refs: Sequence[str] | None = None,
        change_note: str = "",
        created_by: uuid.UUID | None = None,
        created_by_agent: str | None = None,
        generation_metadata: dict[str, Any] | None = None,
    ) -> ContentVersion:
        """Append a new version, inheriting unspecified fields from the previous one.

        Callers are responsible for invalidating approvals; :class:`ContentEditor`
        wraps both steps in one transaction so the invalidation cannot be skipped.
        """
        previous = await self.current_version(item)
        if previous is None:
            raise ValidationProblem("Content item has no version to derive from.")

        new_headline = headline if headline is not None else previous.headline
        new_body = body if body is not None else previous.body
        new_hashtags = list(hashtags) if hashtags is not None else list(previous.hashtags)
        new_cta = call_to_action if call_to_action is not None else previous.call_to_action
        new_media = list(media_refs) if media_refs is not None else list(previous.media_refs)

        changed = any(
            [
                new_headline != previous.headline,
                new_body != previous.body,
                new_hashtags != list(previous.hashtags),
                new_cta != previous.call_to_action,
                new_media != list(previous.media_refs),
            ]
        )
        if not changed:
            raise ValidationProblem(
                "The new version is identical to the current version; nothing to review.",
                code="no_change",
            )

        version = await self._append_version(
            item,
            headline=new_headline,
            body=new_body,
            hashtags=new_hashtags,
            call_to_action=new_cta,
            media_refs=new_media,
            change_note=change_note or f"Revision {item.version_count + 1}",
            created_by=created_by,
            created_by_agent=created_by_agent,
            generation_metadata=generation_metadata or {},
        )
        item.current_version_id = version.id
        item.version_count = version.version_number
        await self.session.flush()
        return version

    async def version_media_checksums(self, version: ContentVersion) -> dict[str, str]:
        """Map ``media_ref`` → checksum for assets attached to this version."""
        if not version.media_refs:
            return {}
        stmt = select(CreativeAsset).where(
            CreativeAsset.content_item_id == version.content_item_id,
            CreativeAsset.storage_key.in_(version.media_refs),
        )
        assets = (await self.session.execute(stmt)).scalars().all()
        return {a.storage_key: a.checksum for a in assets if a.storage_key}
