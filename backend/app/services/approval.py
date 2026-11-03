"""The approval state machine — the integrity core of the system.

Design rules encoded here (and enforced by tests):

1. Transitions are declared in one table; there is no other writer of
   ``ContentItem.status``.
2. ``PUBLISHING`` may only be entered with a live approval bound to the item's
   *current* version and matching ``content_hash``.
3. Any change to the publishable payload or attached creative invalidates
   approvals, moving the item back to ``DRAFT``.
4. Repeating a request is idempotent: it returns the existing record rather than
   appending a duplicate or raising.
5. Every transition writes an :class:`ApprovalRecord` and an :class:`AuditLog`
   row inside the same transaction.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_audit
from app.core.errors import (
    ApprovalRequiredError,
    InvalidStateTransition,
    NotFoundError,
    PermissionDeniedError,
)
from app.models.approval import ApprovalAction, ApprovalRecord
from app.models.content import ContentItem, ContentStatus, ContentVersion
from app.models.creative import CreativeAsset

F = ContentStatus

# from_status -> {action: to_status}
TRANSITIONS: dict[ContentStatus, dict[str, ContentStatus]] = {
    F.DRAFT: {
        ApprovalAction.SUBMIT.value: F.IN_REVIEW,
        ApprovalAction.CANCEL.value: F.CANCELLED,
    },
    F.IN_REVIEW: {
        ApprovalAction.APPROVE.value: F.APPROVED,
        ApprovalAction.REJECT.value: F.REJECTED,
        ApprovalAction.REQUEST_CHANGES.value: F.CHANGES_REQUESTED,
        ApprovalAction.CANCEL.value: F.CANCELLED,
    },
    F.APPROVED: {
        "schedule": F.SCHEDULED,
        "publish": F.PUBLISHING,
        ApprovalAction.REVOKE.value: F.DRAFT,
        ApprovalAction.CANCEL.value: F.CANCELLED,
    },
    F.SCHEDULED: {
        "publish": F.PUBLISHING,
        "unschedule": F.APPROVED,
        ApprovalAction.CANCEL.value: F.CANCELLED,
    },
    F.PUBLISHING: {
        "published": F.PUBLISHED,
        "fail": F.FAILED,
    },
    F.FAILED: {
        "retry": F.APPROVED,
        ApprovalAction.CANCEL.value: F.CANCELLED,
    },
    F.CHANGES_REQUESTED: {
        ApprovalAction.SUBMIT.value: F.IN_REVIEW,
        ApprovalAction.CANCEL.value: F.CANCELLED,
    },
    F.REJECTED: {ApprovalAction.CANCEL.value: F.CANCELLED},
    F.PUBLISHED: {},
    F.CANCELLED: {},
}

# Actions that require the approval to already exist and be valid.
GATED_ACTIONS = {"schedule", "publish", "published"}

# Actions only an approver (founder) may perform.
APPROVER_ACTIONS = {ApprovalAction.APPROVE.value, ApprovalAction.REVOKE.value}

# Which approval action name is persisted for a given transition action.
PERSISTED_ACTION = {
    ApprovalAction.SUBMIT.value: ApprovalAction.SUBMIT.value,
    ApprovalAction.APPROVE.value: ApprovalAction.APPROVE.value,
    ApprovalAction.REJECT.value: ApprovalAction.REJECT.value,
    ApprovalAction.REQUEST_CHANGES.value: ApprovalAction.REQUEST_CHANGES.value,
    ApprovalAction.REVOKE.value: ApprovalAction.REVOKE.value,
    ApprovalAction.CANCEL.value: ApprovalAction.CANCEL.value,
}

APPROVER_ROLE_NAMES = {"founder"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class ApprovalService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---------------------------------------------------------------- lookups
    async def get_item(self, item_id: uuid.UUID) -> ContentItem:
        item = await self.session.get(ContentItem, item_id)
        if item is None:
            raise NotFoundError(f"Content item {item_id} not found.")
        return item

    async def list_records(self, item_id: uuid.UUID) -> list[ApprovalRecord]:
        stmt = (
            select(ApprovalRecord)
            .where(ApprovalRecord.content_item_id == item_id)
            .order_by(ApprovalRecord.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def valid_approval(
        self, item: ContentItem, version: ContentVersion | None = None
    ) -> ApprovalRecord | None:
        """The live approval authorising ``item.current_version_id``, if any."""
        version_id = item.current_version_id if version is None else version.id
        if version_id is None:
            return None
        stmt = (
            select(ApprovalRecord)
            .where(
                ApprovalRecord.content_item_id == item.id,
                ApprovalRecord.content_version_id == version_id,
                ApprovalRecord.action == ApprovalAction.APPROVE.value,
                ApprovalRecord.invalidated_at.is_(None),
            )
            .order_by(ApprovalRecord.created_at.desc())
            .limit(1)
        )
        record = (await self.session.execute(stmt)).scalar_one_or_none()
        if record is None:
            return None
        # Hash mismatch means the approved artefact is no longer the artefact on
        # disk (e.g. an asset swap that bypassed the normal path).
        current_hash = await self.current_content_hash(item)
        if record.content_hash and current_hash and record.content_hash != current_hash:
            return None
        # Phase 3B: the selected creative must still be the one that was approved.
        binding = await self.creative_binding(item)
        approved_asset = str(record.creative_asset_id) if record.creative_asset_id else ""
        if approved_asset != binding["asset_id"]:
            return None
        if (record.creative_binding_hash or "") and record.creative_binding_hash != binding["hash"]:
            return None
        return record

    async def require_valid_approval(
        self, item: ContentItem, version: ContentVersion | None = None
    ) -> ApprovalRecord:
        record = await self.valid_approval(item, version)
        if record is None:
            raise ApprovalRequiredError(
                "Publishing requires a valid approval for the exact current content version.",
                details={
                    "content_item_id": str(item.id),
                    "content_version_id": str(item.current_version_id),
                    "status": item.status,
                },
            )
        return record

    async def _require_validation_clear(self, item: ContentItem) -> None:
        """Refuse to start review while the current version has critical issues."""
        from app.models.content_plan import ContentValidation, ValidationVerdict

        if item.current_version_id is None:
            return
        row = (
            await self.session.execute(
                select(ContentValidation)
                .where(ContentValidation.content_version_id == item.current_version_id)
                .order_by(ContentValidation.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return
        if row.verdict in (
            ValidationVerdict.BLOCKED.value,
            ValidationVerdict.NEEDS_REVISION.value,
        ):
            raise InvalidStateTransition(
                "This version cannot enter review: the Brand Guardian found "
                "unresolved critical issues.",
                details={
                    "verdict": row.verdict,
                    "blocking_count": row.blocking_count,
                    "requires_founder_confirmation": row.requires_founder_confirmation,
                    "issues": row.issues[:5],
                },
            )

    async def selected_creative(self, item: ContentItem) -> "CreativeAsset | None":
        """The creative asset currently bound to the item's live content version.

        Phase 3B: an approval covers a *combination* — content version, creative
        brief version and selected asset version. This is the lookup that makes the
        last two checkable.
        """
        from app.models.creative import CreativeAssetStatus

        if item.current_version_id is None:
            return None
        stmt = (
            select(CreativeAsset)
            .where(
                CreativeAsset.content_item_id == item.id,
                CreativeAsset.content_version_id == item.current_version_id,
                CreativeAsset.selected.is_(True),
                CreativeAsset.status == CreativeAssetStatus.READY.value,
            )
            .order_by(CreativeAsset.asset_version.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def creative_binding(self, item: ContentItem) -> dict[str, Any]:
        """Everything an approval must pin, in comparable string form."""
        asset = await self.selected_creative(item)
        if asset is None:
            return {
                "brief_id": "",
                "brief_version": 0,
                "asset_id": "",
                "asset_version": 0,
                "asset_checksum": "",
                "hash": "",
            }
        binding = {
            "brief_id": str(asset.brief_id) if asset.brief_id else "",
            "brief_version": int(asset.brief_version or 0),
            "asset_id": str(asset.id),
            "asset_version": int(asset.asset_version or 0),
            "asset_checksum": asset.checksum or "",
        }
        import hashlib
        import json as _json

        binding["hash"] = hashlib.sha256(
            _json.dumps(
                {
                    "content_version_id": str(item.current_version_id or ""),
                    "brief_id": binding["brief_id"],
                    "brief_version": binding["brief_version"],
                    "asset_id": binding["asset_id"],
                    "asset_version": binding["asset_version"],
                    "asset_checksum": binding["asset_checksum"],
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return binding

    async def current_content_hash(self, item: ContentItem) -> str:
        """Hash of the item's *current* version (authoritative, DB-backed).

        Always resolved from the database rather than cached on the instance: the
        same ``ContentItem`` object is used across a multi-step transition, and a
        stale cache here would silently authorise an outdated payload.
        """
        if item.current_version_id is None:
            return ""
        version = await self.session.get(ContentVersion, item.current_version_id)
        return version.content_hash if version else ""

    # ------------------------------------------------------------- transitions
    async def apply(
        self,
        item_id: uuid.UUID,
        action: str,
        *,
        actor_id: uuid.UUID | None,
        actor_role: str = "system",
        reason: str = "",
        idempotency_key: str | None = None,
        requested_version_id: uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[ContentItem, ApprovalRecord | None, bool]:
        """Apply ``action`` and return ``(item, record, was_replay)``."""
        item = await self.get_item(item_id)

        # --- idempotency: same terminal outcome, no duplicate ledger rows ----
        replay_record = await self._find_replay(item, action, idempotency_key)
        if replay_record is not None:
            return item, replay_record, True

        # --- permission gate -------------------------------------------------
        if action in APPROVER_ACTIONS and actor_role not in APPROVER_ROLE_NAMES:
            raise PermissionDeniedError(
                f"Action '{action}' requires the founder role.",
                details={"actor_role": actor_role},
            )

        # --- version binding --------------------------------------------------
        if requested_version_id is not None and requested_version_id != item.current_version_id:
            raise InvalidStateTransition(
                "This action targets a stale content version.",
                details={
                    "requested_version_id": str(requested_version_id),
                    "current_version_id": str(item.current_version_id),
                },
            )

        # --- transition legality ----------------------------------------------
        current = ContentStatus(item.status)
        allowed = TRANSITIONS.get(current, {})
        if action not in allowed:
            raise InvalidStateTransition(
                f"Action '{action}' is not allowed from status '{current.value}'.",
                details={
                    "status": current.value,
                    "action": action,
                    "allowed_actions": sorted(allowed.keys()),
                },
            )
        target = allowed[action]

        # --- Phase 3A validation gate -----------------------------------------
        # Content whose latest Brand Guardian verdict is BLOCKED or NEEDS_REVISION
        # may not enter review. This only *adds* a precondition: items with no
        # validation record (everything created before Phase 3A, and manual drafts)
        # behave exactly as before.
        if action == ApprovalAction.SUBMIT.value:
            await self._require_validation_clear(item)

        # --- approval gate -----------------------------------------------------
        approval: ApprovalRecord | None = None
        if action in GATED_ACTIONS:
            approval = await self.require_valid_approval(item)

        # --- write -------------------------------------------------------------
        before = {"status": current.value}
        item.status = target.value
        await self.session.flush()

        record: ApprovalRecord | None = None
        persisted_action = PERSISTED_ACTION.get(action)
        creative = await self.creative_binding(item) if persisted_action == (
            ApprovalAction.APPROVE.value
        ) else {
            "brief_id": "",
            "brief_version": 0,
            "asset_id": "",
            "asset_version": 0,
            "asset_checksum": "",
            "hash": "",
        }
        if persisted_action is not None:
            record = ApprovalRecord(
                content_item_id=item.id,
                content_version_id=item.current_version_id,
                action=persisted_action,
                actor_id=actor_id,
                actor_role=actor_role,
                reason=reason,
                content_hash=await self.current_content_hash(item),
                idempotency_key=idempotency_key,
                request_payload=metadata or {},
                creative_brief_id=(
                    uuid.UUID(creative["brief_id"]) if creative["brief_id"] else None
                ),
                creative_brief_version=creative["brief_version"] or None,
                creative_asset_id=(
                    uuid.UUID(creative["asset_id"]) if creative["asset_id"] else None
                ),
                creative_asset_version=creative["asset_version"] or None,
                creative_binding_hash=creative["hash"],
            )
            self.session.add(record)
            if persisted_action == ApprovalAction.APPROVE.value:
                # Only one live approval per version.
                await self._invalidate_other_approvals(item, keep=None, reason="superseded")
            await self.session.flush()

        await record_audit(
            self.session,
            action=f"content.{action}",
            entity_type="content_item",
            entity_id=item.id,
            actor_id=actor_id,
            before=before,
            after={
                "status": target.value,
                "approval_record_id": str(record.id) if record else None,
                "approval_record_id_used": str(approval.id) if approval else None,
            },
            metadata={"reason": reason, "actor_role": actor_role, **(
                {"extra": metadata} if metadata else {}
            )},
        )
        return item, record, False

    async def _find_replay(
        self, item: ContentItem, action: str, idempotency_key: str | None
    ) -> ApprovalRecord | None:
        """Detect a repeat request.

        Two signals: an explicit idempotency key, or the action already producing
        the target state (e.g. approving an already-approved item).
        """
        if idempotency_key:
            stmt = select(ApprovalRecord).where(
                ApprovalRecord.content_item_id == item.id,
                ApprovalRecord.idempotency_key == idempotency_key,
            )
            existing = (await self.session.execute(stmt)).scalar_one_or_none()
            if existing is not None:
                return existing

        persisted = PERSISTED_ACTION.get(action)
        if persisted is None:
            return None
        if persisted == ApprovalAction.APPROVE.value:
            return await self.valid_approval(item)
        if persisted == ApprovalAction.SUBMIT.value and item.status == ContentStatus.IN_REVIEW.value:
            return await self._latest(item, ApprovalAction.SUBMIT.value)
        return None

    async def _latest(self, item: ContentItem, action: str) -> ApprovalRecord | None:
        stmt = (
            select(ApprovalRecord)
            .where(
                ApprovalRecord.content_item_id == item.id,
                ApprovalRecord.action == action,
                ApprovalRecord.content_version_id == item.current_version_id,
            )
            .order_by(ApprovalRecord.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def _invalidate_other_approvals(
        self,
        item: ContentItem,
        *,
        keep: uuid.UUID | None,
        reason: str,
    ) -> int:
        stmt = (
            update(ApprovalRecord)
            .where(
                ApprovalRecord.content_item_id == item.id,
                ApprovalRecord.action == ApprovalAction.APPROVE.value,
                ApprovalRecord.invalidated_at.is_(None),
            )
            .values(invalidated_at=now_utc(), invalidated_reason=reason)
        )
        if keep is not None:
            stmt = stmt.where(ApprovalRecord.id != keep)
        result = await self.session.execute(stmt)
        await self.session.flush()
        return int(result.rowcount or 0)

    # ------------------------------------------------------------- invalidation
    async def invalidate_for_new_version(
        self,
        item: ContentItem,
        *,
        actor_id: uuid.UUID | None,
        reason: str,
    ) -> int:
        """Revoke all live approvals because the content payload changed.

        Editing approved content must not leave the item publishable, so the item
        is also returned to ``DRAFT``.
        """
        count = await self._invalidate_other_approvals(item, keep=None, reason=reason)
        before = {"status": item.status}
        if ContentStatus(item.status) is not ContentStatus.DRAFT:
            item.status = ContentStatus.DRAFT.value
        await self.session.flush()
        await record_audit(
            self.session,
            action="content.approval_invalidated",
            entity_type="content_item",
            entity_id=item.id,
            actor_id=actor_id,
            before=before,
            after={"status": item.status, "invalidated_approvals": count},
            metadata={"reason": reason},
        )
        return count

    async def invalidate_for_asset_change(
        self,
        *,
        item: ContentItem,
        asset: CreativeAsset,
        actor_id: uuid.UUID | None,
        reason: str = "Creative asset replaced",
    ) -> int:
        """Replacing a creative invalidates approval of the version referencing it.

        An asset counts as *in scope* when it is explicitly bound to the current
        version, is referenced by the version's ``media_refs``, or is attached to
        the content item itself (item-scoped creatives are publishable, so
        swapping their bytes must revoke approval just the same).
        """
        in_scope = bool(
            item.current_version_id
            and (
                asset.content_version_id == item.current_version_id
                or asset.content_item_id == item.id
                or (asset.storage_key and await self._version_references(item, asset.storage_key))
            )
        )
        if not in_scope:
            return 0
        count = await self._invalidate_other_approvals(item, keep=None, reason=reason)
        before = {"status": item.status}
        if ContentStatus(item.status) in {
            ContentStatus.APPROVED,
            ContentStatus.SCHEDULED,
            ContentStatus.PUBLISHING,
        }:
            item.status = ContentStatus.CHANGES_REQUESTED.value
        await self.session.flush()
        await record_audit(
            self.session,
            action="creative.asset_replaced",
            entity_type="creative_asset",
            entity_id=asset.id,
            actor_id=actor_id,
            before=before,
            after={"status": item.status, "invalidated_approvals": count},
            metadata={"reason": reason, "checksum": asset.checksum},
        )
        return count

    async def _version_references(self, item: ContentItem, storage_key: str) -> bool:
        if item.current_version_id is None:
            return False
        version = await self.session.get(ContentVersion, item.current_version_id)
        return bool(version and storage_key in (version.media_refs or []))

    # --------------------------------------------------------------- queries
    def allowed_actions(self, item: ContentItem) -> list[str]:
        return sorted(TRANSITIONS.get(ContentStatus(item.status), {}).keys())

    async def audit_trail(self, item_id: uuid.UUID) -> list[dict[str, Any]]:
        records = await self.list_records(item_id)
        return [
            {
                "id": str(r.id),
                "action": r.action,
                "content_version_id": str(r.content_version_id),
                "actor_id": str(r.actor_id) if r.actor_id else None,
                "actor_role": r.actor_role,
                "reason": r.reason,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "invalidated_at": r.invalidated_at.isoformat() if r.invalidated_at else None,
                "invalidated_reason": r.invalidated_reason,
                "valid": r.is_valid,
            }
            for r in records
        ]
