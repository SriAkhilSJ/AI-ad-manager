"""Creative asset registration and replacement.

Replacing an asset that a live-approved version references revokes that approval
in the same transaction — the second invalidation rule in the brief.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationProblem
from app.models.content import ContentVersion
from app.models.creative import CreativeAsset, CreativeAssetStatus, CreativeKind
from app.services.approval import ApprovalService
from app.services.content import ContentService


class CreativeService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.content = ContentService(session)
        self.approvals = ApprovalService(session)

    async def list_assets(
        self, item_id: uuid.UUID | None = None, *, limit: int = 100
    ) -> list[CreativeAsset]:
        stmt = select(CreativeAsset)
        if item_id:
            stmt = stmt.where(CreativeAsset.content_item_id == item_id)
        stmt = stmt.order_by(CreativeAsset.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_asset(self, asset_id: uuid.UUID) -> CreativeAsset:
        asset = await self.session.get(CreativeAsset, asset_id)
        if asset is None:
            raise NotFoundError(f"Creative asset {asset_id} not found.")
        return asset

    async def register(
        self,
        *,
        content_item_id: uuid.UUID | None,
        kind: str,
        filename: str,
        mime_type: str = "",
        content_bytes: bytes | None = None,
        storage_key: str | None = None,
        public_url: str | None = None,
        prompt: str = "",
        provider: str = "",
        width: int | None = None,
        height: int | None = None,
        metadata: dict[str, Any] | None = None,
        generated_by_agent: str | None = None,
        actor_id: uuid.UUID | None = None,
    ) -> CreativeAsset:
        if kind not in {k.value for k in CreativeKind}:
            raise ValidationProblem(f"Unsupported creative kind '{kind}'.")

        checksum = ""
        size = 0
        if content_bytes is not None:
            checksum = hashlib.sha256(content_bytes).hexdigest()
            size = len(content_bytes)
            if not storage_key:
                storage_key = f"creatives/{uuid.uuid4().hex}/{filename}"

        asset = CreativeAsset(
            content_item_id=content_item_id,
            kind=kind,
            status=CreativeAssetStatus.READY.value if checksum else CreativeAssetStatus.PENDING.value,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size,
            storage_key=storage_key,
            public_url=public_url,
            checksum=checksum,
            width=width,
            height=height,
            prompt=prompt,
            provider=provider,
            metadata_json=metadata or {},
            generated_by_agent=generated_by_agent,
        )
        self.session.add(asset)
        await self.session.flush()
        return asset

    async def replace(
        self,
        asset_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None,
        content_bytes: bytes | None = None,
        storage_key: str | None = None,
        public_url: str | None = None,
        mime_type: str | None = None,
        filename: str | None = None,
    ) -> tuple[CreativeAsset, int]:
        """Replace the bytes behind an asset, invalidating any dependent approval."""
        asset = await self.get_asset(asset_id)
        new_checksum = hashlib.sha256(content_bytes).hexdigest() if content_bytes else ""
        checksum_changed = bool(new_checksum) and new_checksum != asset.checksum

        if content_bytes is not None:
            asset.size_bytes = len(content_bytes)
            asset.checksum = new_checksum
            asset.status = CreativeAssetStatus.READY.value
        if storage_key:
            asset.storage_key = storage_key
        if public_url is not None:
            asset.public_url = public_url
        if mime_type:
            asset.mime_type = mime_type
        if filename:
            asset.filename = filename
        self.session.add(asset)
        await self.session.flush()

        invalidated = 0
        if checksum_changed and asset.content_item_id is not None:
            item = await self.content.get_item(asset.content_item_id)
            invalidated = await self.approvals.invalidate_for_asset_change(
                item=item, asset=asset, actor_id=actor_id
            )
            asset.content_version_id = await self._link_version(item)
            await self.session.flush()
        return asset, invalidated

    async def _link_version(self, item) -> uuid.UUID | None:  # type: ignore[no-untyped-def]
        if item.current_version_id is None:
            return None
        version = await self.session.get(ContentVersion, item.current_version_id)
        return version.id if version else None

    async def attach_to_version(
        self, asset_id: uuid.UUID, version_id: uuid.UUID, *, actor_id: uuid.UUID | None
    ) -> CreativeAsset:
        asset = await self.get_asset(asset_id)
        version = await self.content.get_version(version_id)
        asset.content_version_id = version.id
        asset.content_item_id = version.content_item_id
        self.session.add(asset)
        await self.session.flush()
        return asset
