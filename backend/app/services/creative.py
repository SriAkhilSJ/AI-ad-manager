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


def _aspect_from(width: int | None, height: int | None) -> str | None:
    if not width or not height:
        return None
    for ratio, (rw, rh) in {
        "1:1": (1, 1),
        "4:5": (4, 5),
        "9:16": (9, 16),
        "16:9": (16, 9),
        "1.91:1": (191, 100),
    }.items():
        if abs((width / height) - (rw / rh)) < 0.02:
            return ratio
    return f"{width}:{height}"


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
        brand_profile_id: uuid.UUID | None = None,
        campaign_id: uuid.UUID | None = None,
        brief_id: uuid.UUID | None = None,
        brief_version: int = 0,
        content_version_id: uuid.UUID | None = None,
        generation_job_id: uuid.UUID | None = None,
        aspect_ratio: str = "",
        duration_seconds: float | None = None,
        is_mock: bool = False,
        detected_mime: str = "",
        metadata_verified: bool = False,
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
            content_version_id=content_version_id,
            brand_profile_id=brand_profile_id,
            campaign_id=campaign_id,
            brief_id=brief_id,
            brief_version=brief_version,
            generation_job_id=generation_job_id,
            aspect_ratio=aspect_ratio,
            duration_seconds=duration_seconds,
            is_mock=is_mock,
            detected_mime=detected_mime,
            metadata_verified=metadata_verified,
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


    # ------------------------------------------------------- Phase 3B: versions
    async def list_versions(self, asset: CreativeAsset) -> list[CreativeAsset]:
        """Every version in this asset's chain, newest first."""
        root = asset.root_asset_id or asset.id
        stmt = (
            select(CreativeAsset)
            .where(
                (CreativeAsset.id == root)
                | (CreativeAsset.root_asset_id == root)
                | (CreativeAsset.supersedes_asset_id == root)
            )
            .order_by(CreativeAsset.asset_version.desc())
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        # Include intermediate links (v3 superseding v2, where root is v1).
        if asset.root_asset_id:
            chain = (
                await self.session.execute(
                    select(CreativeAsset)
                    .where(CreativeAsset.root_asset_id == asset.root_asset_id)
                    .order_by(CreativeAsset.asset_version.desc())
                )
            ).scalars().all()
            known = {row.id for row in rows}
            rows.extend(row for row in chain if row.id not in known)
            rows.sort(key=lambda row: row.asset_version, reverse=True)
        return rows

    async def create_version(
        self,
        asset: CreativeAsset,
        *,
        data: bytes,
        mime_type: str,
        filename: str,
        storage_key: str,
        actor_id: uuid.UUID | None,
        metadata: dict[str, Any] | None = None,
        public_url: str | None = None,
        width: int | None = None,
        height: int | None = None,
        aspect_ratio: str | None = None,
        is_mock: bool = False,
        selected: bool = False,
    ) -> tuple[CreativeAsset, int]:
        """Add a new version of an asset without touching the previous one.

        The old row is marked ``superseded``; its bytes stay in storage and its
        checksum is unchanged, so an approval that referenced it can still be
        audited against exactly what was approved. The caller must supply a fresh
        ``storage_key``: reusing the previous key would overwrite the approved bytes.
        """
        checksum = hashlib.sha256(data).hexdigest()
        root = asset.root_asset_id or asset.id
        newest = (
            await self.session.execute(
                select(CreativeAsset)
                .where((CreativeAsset.id == root) | (CreativeAsset.root_asset_id == root))
                .order_by(CreativeAsset.asset_version.desc())
                .limit(1)
            )
        ).scalar_one_or_none() or asset

        new_asset = CreativeAsset(
            content_item_id=asset.content_item_id,
            content_version_id=asset.content_version_id,
            brand_profile_id=asset.brand_profile_id,
            campaign_id=asset.campaign_id,
            brief_id=asset.brief_id,
            brief_version=asset.brief_version,
            generation_job_id=None,
            kind=asset.kind,
            status=CreativeAssetStatus.READY.value,
            filename=filename,
            mime_type=mime_type,
            detected_mime=mime_type,
            metadata_verified=True,
            size_bytes=len(data),
            storage_key=storage_key,
            public_url=public_url,
            checksum=checksum,
            width=width if width is not None else asset.width,
            height=height if height is not None else asset.height,
            duration_seconds=asset.duration_seconds,
            aspect_ratio=(
                aspect_ratio
                if aspect_ratio is not None
                else (_aspect_from(width, height) or asset.aspect_ratio)
            ),
            is_mock=is_mock,
            asset_version=int(newest.asset_version) + 1,
            supersedes_asset_id=newest.id,
            root_asset_id=root,
            # Selection is never inherited: the new bytes have not been chosen or
            # approved by anyone yet, and the approval on the old bytes is void.
            selected=selected,
            prompt=asset.prompt,
            provider=asset.provider,
            generated_by_agent=asset.generated_by_agent,
            metadata_json={**(asset.metadata_json or {}), **(metadata or {}), "replacement": True},
        )
        self.session.add(new_asset)
        await self.session.flush()

        if newest.status != CreativeAssetStatus.SUPERSEDED.value:
            newest.status = CreativeAssetStatus.SUPERSEDED.value
            newest.selected = False
            self.session.add(newest)
            await self.session.flush()

        invalidated = 0
        if asset.content_item_id is not None:
            item = await self.content.get_item(asset.content_item_id)
            invalidated = await self.approvals.invalidate_for_asset_change(
                item=item,
                asset=new_asset,
                actor_id=actor_id,
                reason=f"Creative asset replaced (version {new_asset.asset_version})",
            )
        await self.session.flush()
        return new_asset, invalidated

    async def select(
        self,
        asset: CreativeAsset,
        *,
        actor_id: uuid.UUID | None,
        content_version_id: uuid.UUID | None = None,
    ) -> tuple[CreativeAsset, int]:
        """Bind an asset to its content version and invalidate any live approval."""
        if asset.status != CreativeAssetStatus.READY.value:
            raise ValidationProblem(
                "Only a ready asset can be selected.", code="asset_not_ready"
            )
        version_id = content_version_id or asset.content_version_id
        if version_id is None and asset.content_item_id is not None:
            item = await self.content.get_item(asset.content_item_id)
            version_id = item.current_version_id
        if version_id is None:
            raise ValidationProblem(
                "This asset is not attached to a content version.", code="asset_unattached"
            )

        previous = (
            await self.session.execute(
                select(CreativeAsset).where(
                    CreativeAsset.content_version_id == version_id,
                    CreativeAsset.kind == asset.kind,
                    CreativeAsset.selected.is_(True),
                    CreativeAsset.id != asset.id,
                )
            )
        ).scalars().all()
        for row in previous:
            row.selected = False
            self.session.add(row)

        asset.selected = True
        asset.content_version_id = version_id
        self.session.add(asset)
        await self.session.flush()

        invalidated = 0
        if asset.content_item_id is not None:
            item = await self.content.get_item(asset.content_item_id)
            invalidated = await self.approvals.invalidate_for_asset_change(
                item=item,
                asset=asset,
                actor_id=actor_id,
                reason="Creative asset selection changed",
            )
        await self.session.flush()
        return asset, invalidated
