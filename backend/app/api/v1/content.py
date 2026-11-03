"""Content, versioning, approval transitions and publishing."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status

from app.api.deps import DB, CurrentUser, EditorUser, FounderUser, IdempotencyKeyDep, Pagination
from app.core.errors import NotFoundError
from app.core.idempotency import with_idempotency
from app.models.content import ContentStatus
from app.schemas.content import (
    ApprovalRecordOut,
    ContentItemCreate,
    ContentItemDetailOut,
    ContentItemOut,
    ContentVersionCreate,
    ContentVersionOut,
    PublishOut,
    PublishRequest,
    ScheduleRequest,
    TransitionRequest,
)
from app.schemas.social import PublishedPostOut, ScheduledPostOut
from app.services.approval import ApprovalService
from app.services.content import ContentService
from app.services.editor import ContentEditor
from app.services.publishing import PublishingService, SchedulingService

router = APIRouter(prefix="/content", tags=["content"])


async def _detail(session, item) -> ContentItemDetailOut:  # type: ignore[no-untyped-def]
    service = ContentService(session)
    approvals = ApprovalService(session)
    version = await service.current_version(item)
    valid = await approvals.valid_approval(item)
    records = await approvals.list_records(item.id)
    detail = ContentItemDetailOut.model_validate(item)
    detail.current_version = (
        ContentVersionOut.model_validate(version) if version else None
    )
    detail.allowed_actions = approvals.allowed_actions(item)
    detail.has_valid_approval = valid is not None
    detail.approval_count = len(records)
    return detail


@router.post(
    "", response_model=ContentItemDetailOut, status_code=status.HTTP_201_CREATED,
    summary="Create a content item (starts in DRAFT with version 1)",
)
async def create_content(
    payload: ContentItemCreate, session: DB, user: EditorUser
) -> ContentItemDetailOut:
    item = await ContentService(session).create_item(
        **payload.model_dump(), created_by=user.id
    )
    await session.commit()
    await session.refresh(item)
    return await _detail(session, item)


@router.get("", response_model=list[ContentItemOut], summary="List content items")
async def list_content(
    session: DB,
    user: CurrentUser,
    page: Pagination,
    brand_profile_id: uuid.UUID | None = None,
    campaign_id: uuid.UUID | None = None,
    status_filter: str | None = None,
    platform: str | None = None,
) -> list[ContentItemOut]:
    items, _total = await ContentService(session).list_items(
        brand_profile_id=brand_profile_id,
        campaign_id=campaign_id,
        status=status_filter,
        platform=platform,
        limit=page["limit"],
        offset=page["offset"],
    )
    return [ContentItemOut.model_validate(i) for i in items]


@router.get("/{item_id}", response_model=ContentItemDetailOut, summary="Content detail")
async def get_content(
    item_id: uuid.UUID, session: DB, user: CurrentUser
) -> ContentItemDetailOut:
    item = await ContentService(session).get_item(item_id)
    return await _detail(session, item)


# ------------------------------------------------------------------ versions
@router.get(
    "/{item_id}/versions",
    response_model=list[ContentVersionOut],
    summary="Version history (newest first)",
)
async def list_versions(
    item_id: uuid.UUID, session: DB, user: CurrentUser
) -> list[ContentVersionOut]:
    versions = await ContentService(session).list_versions(item_id)
    return [ContentVersionOut.model_validate(v) for v in versions]


@router.post(
    "/{item_id}/versions",
    response_model=ContentItemDetailOut,
    status_code=201,
    summary="Edit content: creates a new version and invalidates approvals",
)
async def create_version(
    item_id: uuid.UUID,
    payload: ContentVersionCreate,
    session: DB,
    user: EditorUser,
) -> ContentItemDetailOut:
    item, _version, _invalidated = await ContentEditor(session).edit(
        item_id, actor_id=user.id, **payload.model_dump()
    )
    await session.commit()
    await session.refresh(item)
    return await _detail(session, item)


# ----------------------------------------------------------------- approvals
@router.get(
    "/{item_id}/approvals",
    response_model=list[ApprovalRecordOut],
    summary="Approval ledger for an item",
)
async def list_approvals(
    item_id: uuid.UUID, session: DB, user: CurrentUser
) -> list[ApprovalRecordOut]:
    await ContentService(session).get_item(item_id)
    records = await ApprovalService(session).list_records(item_id)
    return [ApprovalRecordOut.model_validate(r) for r in records]


@router.post(
    "/{item_id}/transition",
    response_model=ContentItemDetailOut,
    summary="Apply a state-machine action (submit/approve/reject/request_changes/revoke/cancel)",
)
async def transition(
    item_id: uuid.UUID,
    payload: TransitionRequest,
    session: DB,
    user: CurrentUser,
    response: Response,
    key: IdempotencyKeyDep,
) -> ContentItemDetailOut:
    """Approve/revoke are founder-only; the service re-checks the role.

    Idempotency: a repeated request with the same ``Idempotency-Key`` replays the
    stored response and reports ``Idempotency-Replayed: true``.
    """
    approvals = ApprovalService(session)

    async def operation() -> dict:
        before = await ContentService(session).get_item(item_id)
        item, record, replayed = await approvals.apply(
            item_id,
            payload.action,
            actor_id=user.id,
            actor_role=user.role,
            reason=payload.reason,
            idempotency_key=key,
            requested_version_id=payload.content_version_id,
        )
        await session.flush()
        detail = await _detail(session, item)
        return {
            "detail": detail.model_dump(mode="json"),
            "replayed": replayed,
            "previous_status": before.status,
            "approval_record_id": str(record.id) if record else None,
        }

    result, replayed = await with_idempotency(
        session,
        key=key,
        user_id=user.id,
        endpoint=f"transition:{payload.action}",
        payload={"item": str(item_id), **payload.model_dump(mode="json")},
        operation=operation,
    )
    await session.commit()
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return ContentItemDetailOut.model_validate(result["detail"])


# ----------------------------------------------------------------- publishing
@router.post(
    "/{item_id}/schedule",
    response_model=ScheduledPostOut,
    status_code=201,
    summary="Schedule an approved item (requires a live approval)",
)
async def schedule_content(
    item_id: uuid.UUID,
    payload: ScheduleRequest,
    session: DB,
    user: EditorUser,
    response: Response,
    key: IdempotencyKeyDep,
) -> ScheduledPostOut:
    service = SchedulingService(session)

    async def operation() -> dict:
        item, post = await service.schedule(
            item_id,
            social_account_id=payload.social_account_id,
            scheduled_for=payload.scheduled_for,
            actor_id=user.id,
            actor_role=user.role,
            reason="Scheduled via API",
        )
        await session.flush()
        return {"post": ScheduledPostOut.model_validate(post).model_dump(mode="json")}

    result, replayed = await with_idempotency(
        session,
        key=key,
        user_id=user.id,
        endpoint="schedule",
        payload={"item": str(item_id), **payload.model_dump(mode="json")},
        operation=operation,
    )
    await session.commit()
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return ScheduledPostOut.model_validate(result["post"])


@router.post(
    "/{item_id}/publish",
    response_model=PublishOut,
    summary="Publish (requires approval bound to the current version)",
    responses={
        409: {"description": "approval_required / publishing_disabled / invalid_state_transition"},
        503: {"description": "integration_unavailable (no connected account)"},
    },
)
async def publish_content(
    item_id: uuid.UUID,
    session: DB,
    user: EditorUser,
    response: Response,
    payload: PublishRequest | None = None,
    key: IdempotencyKeyDep = None,
) -> PublishOut:
    """The only endpoint that can lead to ``PUBLISHED``.

    Order of enforcement: approval → connected account → platform transport →
    optimistic state claim. Any failure leaves the content untouched.
    """
    service = PublishingService(session)
    request_payload = (payload or PublishRequest()).model_dump(mode="json")

    async def operation() -> dict:
        item, published = await service.publish(
            item_id,
            actor_id=user.id,
            actor_role=user.role,
            social_account_id=(payload.social_account_id if payload else None),
            scheduled_post_id=(payload.scheduled_post_id if payload else None),
            idempotency_key=key,
        )
        await session.flush()
        return {
            "content_item_id": str(item.id),
            "status": item.status,
            "published_post_id": str(published.id) if published else None,
            "external_post_id": published.external_post_id if published else None,
            "approval_record_id": str(published.approval_record_id) if published else None,
            "message": "Published and recorded.",
        }

    result, replayed = await with_idempotency(
        session,
        key=key,
        user_id=user.id,
        endpoint="publish",
        payload={"item": str(item_id), **request_payload},
        operation=operation,
    )
    await session.commit()
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    if result.get("published_post_id"):
        result["published_post_id"] = uuid.UUID(result["published_post_id"])
    if result.get("approval_record_id"):
        result["approval_record_id"] = uuid.UUID(result["approval_record_id"])
    result["content_item_id"] = uuid.UUID(result["content_item_id"])
    return PublishOut.model_validate(result)


@router.get(
    "/{item_id}/publications",
    response_model=list[PublishedPostOut],
    summary="Publication records for an item",
)
async def list_publications(
    item_id: uuid.UUID, session: DB, user: CurrentUser
) -> list[PublishedPostOut]:
    rows = await PublishingService(session).get_published(item_id)
    return [PublishedPostOut.model_validate(r) for r in rows]


@router.delete(
    "/{item_id}",
    status_code=204,
    summary="Cancel a content item (audited; published items are immutable)",
)
async def cancel_content(
    item_id: uuid.UUID, session: DB, user: EditorUser
) -> Response:
    service = ContentService(session)
    item = await service.get_item(item_id)
    if item.status == ContentStatus.PUBLISHED.value:
        from app.core.errors import InvalidStateTransition

        raise InvalidStateTransition("Published content cannot be cancelled.")
    await ApprovalService(session).apply(
        item_id,
        "cancel",
        actor_id=user.id,
        actor_role=user.role,
        reason="Cancelled via API",
    )
    await session.commit()
    return Response(status_code=204)
