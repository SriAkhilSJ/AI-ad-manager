"""The single writer of ``PUBLISHED``.

Invariant chain enforced here, in this order:

1. **Approval** — a live approval bound to the current version and matching the
   payload hash must exist. Absent → ``approval_required`` (never reaches the
   network, never changes state).
2. **Channel** — a connected social account for the platform must exist.
3. **Transport** — a platform adapter with a live transport must be available.
   Absent → ``publishing_disabled`` (state unchanged, no fake success).
4. **Concurrency** — an optimistic ``UPDATE ... WHERE status IN
   ('APPROVED','SCHEDULED')`` ensures exactly one caller transitions the item
   and a second concurrent publish cannot double-post.
5. **Audit** — the ``PublishedPost`` row names the exact ``approval_record_id``
   that authorised it (DB ``NOT NULL`` + ``RESTRICT`` FK).

There is no other code path in the application that writes
``ContentStatus.PUBLISHED``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_audit
from app.core.errors import (
    ApprovalRequiredError,
    IntegrationUnavailableError,
    InvalidStateTransition,
    NotFoundError,
)
from app.models.approval import ApprovalRecord
from app.models.content import ContentItem, ContentStatus, ContentVersion
from app.models.social import (
    Platform,
    PublishedPost,
    ScheduledPost,
    SocialAccount,
    SocialAccountStatus,
)
from app.publishing.adapters import PublishPayload, get_adapter_registry
from app.services.approval import ApprovalService


class PublishingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.approvals = ApprovalService(session)

    # ------------------------------------------------------------------ read
    async def get_published(self, item_id: uuid.UUID) -> list[PublishedPost]:
        stmt = select(PublishedPost).where(PublishedPost.content_item_id == item_id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def resolve_account(
        self, item: ContentItem, social_account_id: uuid.UUID | None
    ) -> SocialAccount:
        if social_account_id is not None:
            account = await self.session.get(SocialAccount, social_account_id)
            if account is None:
                raise NotFoundError(f"Social account {social_account_id} not found.")
            if account.platform != item.platform:
                raise InvalidStateTransition(
                    "The social account platform does not match the content platform.",
                    details={"account": account.platform, "content": item.platform},
                )
            if account.status != SocialAccountStatus.CONNECTED.value:
                raise IntegrationUnavailableError(
                    f"Social account is '{account.status}', not connected.",
                    details={"social_account_id": str(account.id)},
                )
            return account

        stmt = select(SocialAccount).where(
            SocialAccount.brand_profile_id == item.brand_profile_id,
            SocialAccount.platform == item.platform,
            SocialAccount.status == SocialAccountStatus.CONNECTED.value,
        )
        account = (await self.session.execute(stmt)).scalars().first()
        if account is None:
            raise IntegrationUnavailableError(
                f"No connected {item.platform} account for this brand. "
                "Link an account before publishing.",
                details={"platform": item.platform},
            )
        return account

    # --------------------------------------------------------------- publish
    async def publish(
        self,
        item_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None,
        actor_role: str = "system",
        social_account_id: uuid.UUID | None = None,
        scheduled_post_id: uuid.UUID | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[ContentItem, PublishedPost | None]:
        item = await self.approvals.get_item(item_id)

        if item.status == ContentStatus.PUBLISHED.value:
            existing = await self.get_published(item.id)
            if existing:
                return item, existing[0]

        # 1. approval gate — evaluated before anything else
        approval = await self.approvals.require_valid_approval(item)
        version = await self.session.get(ContentVersion, item.current_version_id)
        if version is None:
            raise ApprovalRequiredError("Content item has no current version to publish.")

        # idempotency by (version, account)
        account = await self.resolve_account(item, social_account_id)
        existing = await self._find_published(version.id, account.id)
        if existing is not None:
            return item, existing

        # 2/3. transport gate — credentials, a usable token vault and a stored
        # credential for this account must all exist. Otherwise this refuses
        # before any network call and the item keeps its state.
        registry = get_adapter_registry()
        advertised = registry.get(item.platform)
        if not (advertised.credentials_configured and registry.vault_available):
            raise _publishing_disabled(advertised)
        adapter = await registry.resolve(item.platform, session=self.session, account=account)

        # 4. concurrency guard + state transition.
        #
        # ``_transition_to_publishing`` performs the atomic
        # ``UPDATE … WHERE status IN ('APPROVED','SCHEDULED')`` claim, so exactly
        # one caller moves the item to PUBLISHING. Calling
        # ``approvals.apply(..., "publish")`` as well was a bug: that path
        # validates the transition *from the item's current status*, which by then
        # is already PUBLISHING, so every publish died with
        # "Action 'publish' is not allowed from status 'PUBLISHING'". It was never
        # visible while no transport existed, because the request was refused
        # before this point. The audit entry is written explicitly instead.
        await self._transition_to_publishing(item, approval)
        await record_audit(
            self.session,
            action="content.publish",
            entity_type="content_item",
            entity_id=item.id,
            actor_id=actor_id,
            before={"status": ContentStatus.APPROVED.value},
            after={
                "status": item.status,
                "approval_record_id": str(approval.id),
                "content_version_id": str(version.id),
            },
            metadata={"reason": "Approved content released to platform adapter"},
        )

        payload = PublishPayload(
            content_item_id=item.id,
            content_version_id=version.id,
            approval_record_id=approval.id,
            platform=item.platform,
            body=version.body,
            headline=version.headline,
            hashtags=list(version.hashtags),
            call_to_action=version.call_to_action,
            media_urls=list(version.media_refs),
        )
        try:
            result = await adapter.publish(payload, account)
        except Exception as exc:  # noqa: BLE001 - adapter boundary
            await self.approvals.apply(
                item.id,
                "fail",
                actor_id=actor_id,
                actor_role=actor_role,
                reason=f"adapter_error: {type(exc).__name__}",
            )
            raise

        # A result without a remote id is not a publication. The transport is
        # required to verify the object before returning, and this is the last
        # line of defence against a fabricated success.
        if not result.external_post_id:
            await self.approvals.apply(
                item.id,
                "fail",
                actor_id=actor_id,
                actor_role=actor_role,
                reason="adapter_returned_no_external_id",
            )
            raise IntegrationUnavailableError(
                "The platform adapter returned no external post id, so the post is not "
                "considered published.",
                details={"platform": item.platform},
            )

        published = PublishedPost(
            content_item_id=item.id,
            content_version_id=version.id,
            social_account_id=account.id,
            approval_record_id=approval.id,
            platform=item.platform,
            external_post_id=result.external_post_id,
            permalink=result.permalink,
            published_at=datetime.now(timezone.utc),
            status="published",
            publish_metadata=result.metadata,
        )
        self.session.add(published)
        await self.session.flush()

        # only legal transition into PUBLISHED
        await self.approvals.apply(
            item.id,
            "published",
            actor_id=actor_id,
            actor_role=actor_role,
            reason="Platform adapter accepted the post",
        )

        if scheduled_post_id is not None:
            scheduled = await self.session.get(ScheduledPost, scheduled_post_id)
            if scheduled is not None:
                scheduled.status = "published"
                await self.session.flush()

        await record_audit(
            self.session,
            action="content.publish_committed",
            entity_type="published_post",
            entity_id=published.id,
            actor_id=actor_id,
            after={
                "content_item_id": str(item.id),
                "content_version_id": str(version.id),
                "approval_record_id": str(approval.id),
                "platform": item.platform,
            },
        )
        return item, published

    async def _transition_to_publishing(
        self, item: ContentItem, approval: ApprovalRecord
    ) -> None:
        """
        Optimistic state claim. On PostgreSQL the row is also locked, so two
        concurrent publishes cannot both observe ``APPROVED``.
        """
        stmt = (
            update(ContentItem)
            .where(
                ContentItem.id == item.id,
                ContentItem.status.in_(
                    [ContentStatus.APPROVED.value, ContentStatus.SCHEDULED.value]
                ),
                ContentItem.current_version_id == approval.content_version_id,
            )
            .values(status=ContentStatus.PUBLISHING.value)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        if int(result.rowcount or 0) != 1:
            await self.session.refresh(item)
            if item.status == ContentStatus.PUBLISHED.value:
                return
            raise InvalidStateTransition(
                "Another publish is already in progress for this content item.",
                details={"status": item.status},
            )
        await self.session.refresh(item)

    async def _find_published(
        self, version_id: uuid.UUID, account_id: uuid.UUID
    ) -> PublishedPost | None:
        stmt = select(PublishedPost).where(
            PublishedPost.content_version_id == version_id,
            PublishedPost.social_account_id == account_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


def _publishing_disabled(adapter) -> IntegrationUnavailableError:  # type: ignore[no-untyped-def]
    """Refusal raised when no live transport exists for this platform.

    The message names the missing input rather than pretending the feature is
    unimplemented: credentials, a token vault and a connected account are three
    distinct prerequisites.
    """
    from app.core.errors import PublishingDisabledError

    return PublishingDisabledError(
        adapter.reason,
        details={"platform": adapter.platform.value, "stage": "preflight"},
    )


class SchedulingService:
    """Schedules an approved item. Requires a live approval, like publishing."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.approvals = ApprovalService(session)
        self.publishing = PublishingService(session)

    async def schedule(
        self,
        item_id: uuid.UUID,
        *,
        social_account_id: uuid.UUID,
        scheduled_for: datetime,
        actor_id: uuid.UUID | None,
        actor_role: str = "system",
        reason: str = "",
    ) -> tuple[ContentItem, ScheduledPost]:
        item = await self.approvals.get_item(item_id)
        approval = await self.approvals.require_valid_approval(item)

        account = await self.publishing.resolve_account(item, social_account_id)
        if account.platform != item.platform:
            raise InvalidStateTransition("Account platform does not match content platform.")

        existing = await self._existing(item.id, account.id, scheduled_for)
        if existing is not None:
            return item, existing

        item, _, _ = await self.approvals.apply(
            item.id,
            "schedule",
            actor_id=actor_id,
            actor_role=actor_role,
            reason=reason or "Scheduled by user",
        )
        post = ScheduledPost(
            content_item_id=item.id,
            content_version_id=approval.content_version_id,
            social_account_id=account.id,
            approval_record_id=approval.id,
            scheduled_for=scheduled_for,
            status="pending",
        )
        self.session.add(post)
        await self.session.flush()
        return item, post

    async def _existing(
        self, item_id: uuid.UUID, account_id: uuid.UUID, when: datetime
    ) -> ScheduledPost | None:
        stmt = select(ScheduledPost).where(
            ScheduledPost.content_item_id == item_id,
            ScheduledPost.social_account_id == account_id,
            ScheduledPost.scheduled_for == when,
            ScheduledPost.status != "cancelled",
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()
