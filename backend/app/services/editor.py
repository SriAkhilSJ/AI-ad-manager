"""The only supported way to change publishable content.

`ContentEditor.edit()` does three things in a single transaction:

1. append a new immutable version,
2. point the item at it,
3. invalidate every live approval and return the item to ``DRAFT``.

Because step 3 lives here, and no service exposes step 1 in isolation to the API
layer, "editing approved content invalidates its approval" is structural rather
than a convention someone must remember.
"""

from __future__ import annotations

import uuid
from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_audit
from app.services.approval import ApprovalService
from app.services.content import ContentService


class ContentEditor:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.content = ContentService(session)
        self.approvals = ApprovalService(session)

    async def edit(
        self,
        item_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None,
        headline: str | None = None,
        body: str | None = None,
        hashtags: Sequence[str] | None = None,
        call_to_action: str | None = None,
        media_refs: Sequence[str] | None = None,
        change_note: str = "",
        created_by_agent: str | None = None,
        generation_metadata: dict[str, Any] | None = None,
    ) -> tuple[Any, Any, int]:
        """Returns ``(item, new_version, invalidated_approval_count)``."""
        item = await self.content.get_item(item_id)
        version = await self.content.add_version(
            item,
            headline=headline,
            body=body,
            hashtags=hashtags,
            call_to_action=call_to_action,
            media_refs=media_refs,
            change_note=change_note,
            created_by=actor_id,
            created_by_agent=created_by_agent,
            generation_metadata=generation_metadata,
        )
        invalidated = await self.approvals.invalidate_for_new_version(
            item,
            actor_id=actor_id,
            reason=f"Content edited to version {version.version_number}",
        )
        await record_audit(
            self.session,
            action="content.version_created",
            entity_type="content_version",
            entity_id=version.id,
            actor_id=actor_id,
            after={
                "content_item_id": str(item.id),
                "version_number": version.version_number,
                "content_hash": version.content_hash,
                "invalidated_approvals": invalidated,
            },
        )
        return item, version, invalidated
