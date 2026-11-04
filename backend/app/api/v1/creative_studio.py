"""Creative Studio API (Phase 3B).

Everything here is additive. Content editing, versioning and approval stay on the
Phase 1/3A routes; this module produces briefs, runs generation jobs, stores assets
and validates them.

Deliberately absent: any endpoint that publishes, schedules externally, or spends
money without authorisation. ``/creative-studio/flow/status`` reports the Flow
Connector as unavailable with the exact information still required.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status
from sqlalchemy import func, or_, select

from app.api.deps import DB, CurrentUser, EditorUser, FounderUser, IdempotencyKeyDep
from app.core.audit import record_audit
from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationProblem
from app.core.idempotency import with_idempotency
from app.creative.media import validate_upload
from app.creative.providers import FlowCreativeProvider, creative_provider_status
from app.models.approval import ApprovalAction, ApprovalRecord
from app.models.campaign import Campaign
from app.models.content import ContentItem, ContentVersion
from app.models.creative import CreativeAsset, CreativeAssetStatus
from app.models.creative_brief import CreativeBrief
from app.models.creative_job import CreativeGenerationJob
from app.schemas.creative_studio import (
    AssetDetailOut,
    AssetOut,
    AssetSelectIn,
    AssetSelectOut,
    BriefCreateIn,
    BriefOut,
    CreativeOverviewOut,
    JobCreateIn,
    JobOut,
    JobRetryIn,
)
from app.services.creative import CreativeService
from app.services.creative_briefs import CreativeBriefService
from app.services.creative_generation import CreativeGenerationService
from app.services.creative_qa import CreativeQAService
from app.storage.base import get_storage, storage_key, storage_status

router = APIRouter(prefix="/creative-studio", tags=["creative-studio"])


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _storage():
    return get_storage()


async def _asset_out(asset: CreativeAsset) -> AssetOut:
    return AssetOut.model_validate(asset)


async def _approval_for(session, asset: CreativeAsset) -> tuple[str, dict[str, Any] | None]:
    """Asset approval status, derived from the single approval ledger."""
    if asset.content_item_id is None:
        return "unapproved", None
    row = (
        await session.execute(
            select(ApprovalRecord)
            .where(
                ApprovalRecord.content_item_id == asset.content_item_id,
                ApprovalRecord.action == ApprovalAction.APPROVE.value,
                ApprovalRecord.creative_asset_id == asset.id,
            )
            .order_by(ApprovalRecord.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return ("selected" if asset.selected else "unapproved"), None
    if row.invalidated_at is not None:
        return "invalidated", {
            "approval_id": str(row.id),
            "invalidated_at": row.invalidated_at.isoformat(),
            "reason": row.invalidated_reason,
        }
    return "approved", {
        "approval_id": str(row.id),
        "approved_at": row.created_at.isoformat(),
        "content_version_id": str(row.content_version_id),
        "brief_version": row.creative_brief_version,
        "asset_version": row.creative_asset_version,
    }


# --------------------------------------------------------------------------- #
# capability surface
# --------------------------------------------------------------------------- #
@router.get("/capabilities", summary="What creative generation can and cannot do right now")
async def capabilities(user: CurrentUser) -> dict[str, Any]:
    settings = get_settings()
    provider_status = creative_provider_status(settings)
    from app.creative.providers import get_creative_provider

    provider = get_creative_provider(settings)
    caps = await provider.get_capabilities()
    return {
        **caps.as_dict(),
        "provider": caps.provider,
        "available": bool(provider_status.get("available")),
        "live_generation_enabled": bool(provider_status.get("live_generation_enabled")),
        "configured_provider": str(provider_status.get("provider", "")),
        "provider_status": provider_status,
        "storage": storage_status(settings),
        "paid_generation_enabled": bool(settings.creative_allow_paid_generation),
        "campaign_budget_usd": settings.creative_campaign_budget_usd,
        "max_upload_bytes": settings.creative_max_upload_bytes,
        "publishing_available": False,
        "external_scheduling": False,
        "qa_note": (
            "Creative QA is automated only: no OCR, no aesthetic judgement, no human "
            "review. Every asset still requires a human look before it ships."
        ),
    }


@router.get("/flow/status", summary="Flow Connector integration readiness")
async def flow_status(user: CurrentUser) -> dict[str, Any]:
    settings = get_settings()
    return {
        "integration": "google_flow_connector",
        "status": "not_available",
        "available": False,
        "enabled": False,
        "configured": (settings.creative_provider or "").lower() == "flow",
        "source_found": False,
        "adapters_implemented": ["creative_provider_boundary"],
        "required_information": FlowCreativeProvider.required_information(),
        "note": (
            "The Flow Connector source is absent from this repository, so its messaging "
            "protocol cannot be implemented from evidence. No MCP tool, browser "
            "automation or invented bridge has been added."
        ),
    }


@router.get("/overview", response_model=CreativeOverviewOut, summary="Creative Studio metrics")
async def overview(session: DB, user: CurrentUser) -> CreativeOverviewOut:
    settings = get_settings()

    assets = list((await session.execute(select(CreativeAsset))).scalars().all())
    jobs = list((await session.execute(select(CreativeGenerationJob))).scalars().all())

    by_campaign: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    by_validation: dict[str, int] = {}
    for asset in assets:
        if asset.campaign_id:
            key = str(asset.campaign_id)
            by_campaign[key] = by_campaign.get(key, 0) + 1
        platform = str((asset.metadata_json or {}).get("platform") or "unassigned")
        by_platform[platform] = by_platform.get(platform, 0) + 1
        by_validation[asset.validation_status] = by_validation.get(asset.validation_status, 0) + 1

    approved_assets = 0
    if assets:
        approved_rows = (
            await session.execute(
                select(func.count(func.distinct(ApprovalRecord.creative_asset_id))).where(
                    ApprovalRecord.action == ApprovalAction.APPROVE.value,
                    ApprovalRecord.invalidated_at.is_(None),
                    ApprovalRecord.creative_asset_id.is_not(None),
                )
            )
        ).scalar()
        approved_assets = int(approved_rows or 0)

    terminal_running = ("queued", "submitted", "running")
    provider_status = creative_provider_status(settings)
    return CreativeOverviewOut(
        total_assets=len(assets),
        mock_assets=sum(1 for a in assets if a.is_mock),
        pending_jobs=sum(1 for j in jobs if j.status in terminal_running),
        failed_jobs=sum(1 for j in jobs if j.status in ("failed", "timed_out")),
        succeeded_jobs=sum(1 for j in jobs if j.status == "succeeded"),
        approved_assets=approved_assets,
        needs_review_assets=sum(
            1
            for a in assets
            if a.validation_status in ("unverified", "needs_revision", "blocked")
        ),
        by_campaign=by_campaign,
        by_platform=by_platform,
        by_validation=by_validation,
        provider=str(provider_status.get("provider", "")),
        is_mock_provider=bool(provider_status.get("is_mock")),
        paid_generation_enabled=bool(settings.creative_allow_paid_generation),
        flow_connector={
            "status": "not_available",
            "source_found": False,
            "required_information": FlowCreativeProvider.required_information(),
        },
        storage=storage_status(settings),
    )


# --------------------------------------------------------------------------- #
# briefs
# --------------------------------------------------------------------------- #
@router.post(
    "/briefs",
    response_model=BriefOut,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a creative brief for an exact content version",
)
async def create_brief(
    payload: BriefCreateIn,
    session: DB,
    user: EditorUser,
    response: Response,
) -> BriefOut:
    service = CreativeBriefService(session)
    version_id = payload.content_version_id
    if version_id is None:
        if payload.content_item_id is None:
            raise ValidationProblem("Provide content_version_id or content_item_id.")
        item = await session.get(ContentItem, payload.content_item_id)
        if item is None:
            raise NotFoundError(f"Content item {payload.content_item_id} not found.")
        if item.current_version_id is None:
            raise ValidationProblem(
                "That content item has no versions yet.", code="no_content_version"
            )
        version_id = item.current_version_id

    brief, created = await service.create(
        content_version_id=version_id,
        asset_type=payload.asset_type,
        actor_id=user.id,
        creative_direction=payload.creative_direction,
        preferred_aspect_ratio=payload.aspect_ratio,
        duration_seconds=payload.duration_seconds,
        strategy_reference=payload.strategy_reference,
        force_new_version=payload.force_new_version,
    )
    await session.commit()
    await session.refresh(brief)
    if not created:
        response.status_code = status.HTTP_200_OK
    return BriefOut.model_validate(brief)


@router.get("/briefs", response_model=list[BriefOut], summary="List creative briefs")
async def list_briefs(
    session: DB,
    user: CurrentUser,
    campaign_id: uuid.UUID | None = None,
    content_item_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[BriefOut]:
    rows = await CreativeBriefService(session).list(
        campaign_id=campaign_id, content_item_id=content_item_id, limit=limit
    )
    return [BriefOut.model_validate(row) for row in rows]


@router.get("/briefs/{brief_id}", response_model=BriefOut, summary="Read one creative brief")
async def get_brief(brief_id: uuid.UUID, session: DB, user: CurrentUser) -> BriefOut:
    return BriefOut.model_validate(await CreativeBriefService(session).get(brief_id))


# --------------------------------------------------------------------------- #
# generation jobs
# --------------------------------------------------------------------------- #
@router.post(
    "/jobs",
    response_model=JobOut,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a generation job for a brief (mock provider by default)",
)
async def create_job(
    payload: JobCreateIn,
    session: DB,
    user: EditorUser,
    response: Response,
    idempotency_key: IdempotencyKeyDep = None,
) -> JobOut:
    brief = await CreativeBriefService(session).get(payload.brief_id)
    service = CreativeGenerationService(session, storage=_storage())

    async def operation() -> dict[str, Any]:
        job, created = await service.submit(
            brief=brief,
            job_type=payload.job_type or brief.asset_type,
            parameters=payload.parameters,
            actor_id=user.id,
            idempotency_key=idempotency_key,
            auto_advance=payload.auto_advance,
        )
        if not created:
            response.status_code = status.HTTP_200_OK
        await session.flush()
        return JobOut.model_validate(job).model_dump(mode="json")

    if idempotency_key:
        result, replayed = await with_idempotency(
            session,
            key=idempotency_key,
            user_id=user.id,
            endpoint="creative.job",
            payload=payload.model_dump(mode="json"),
            operation=operation,
        )
        await session.commit()
        if replayed:
            response.status_code = status.HTTP_200_OK
        return JobOut.model_validate(result)

    result = await operation()
    await session.commit()
    return JobOut.model_validate(result)


@router.get("/jobs", response_model=list[JobOut], summary="List generation jobs")
async def list_jobs(
    session: DB,
    user: CurrentUser,
    brief_id: uuid.UUID | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    campaign_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[JobOut]:
    rows = await CreativeGenerationService(session, storage=_storage()).list(
        brief_id=brief_id, status=status_filter, campaign_id=campaign_id, limit=limit
    )
    return [JobOut.model_validate(row) for row in rows]


@router.get("/jobs/{job_id}", response_model=JobOut, summary="Read one generation job")
async def get_job(job_id: uuid.UUID, session: DB, user: CurrentUser) -> JobOut:
    job = await CreativeGenerationService(session, storage=_storage()).get(job_id)
    return JobOut.model_validate(job)


@router.post("/jobs/{job_id}/poll", response_model=JobOut, summary="Poll a job's provider state")
async def poll_job(job_id: uuid.UUID, session: DB, user: EditorUser) -> JobOut:
    service = CreativeGenerationService(session, storage=_storage())
    job = await service.get(job_id)
    await service.advance(job, actor_id=user.id)
    await session.commit()
    await session.refresh(job)
    return JobOut.model_validate(job)


@router.post("/jobs/{job_id}/retry", response_model=JobOut, summary="Retry an eligible failure")
async def retry_job(
    job_id: uuid.UUID,
    payload: JobRetryIn,
    session: DB,
    user: EditorUser,
) -> JobOut:
    if payload.authorize_ambiguous:
        # Only a founder may accept the risk of paying twice.
        from app.core.errors import PermissionDeniedError

        if user.role != "founder":
            raise PermissionDeniedError(
                "Authorising a retry of an ambiguous timeout requires the founder role.",
                details={"actor_role": user.role},
            )
    service = CreativeGenerationService(session, storage=_storage())
    job = await service.get(job_id)
    await service.retry(
        job, actor_id=user.id, authorize_ambiguous=payload.authorize_ambiguous
    )
    await session.commit()
    await session.refresh(job)
    return JobOut.model_validate(job)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut, summary="Cancel a running job")
async def cancel_job(job_id: uuid.UUID, session: DB, user: EditorUser) -> JobOut:
    service = CreativeGenerationService(session, storage=_storage())
    job = await service.get(job_id)
    await service.cancel(job, actor_id=user.id)
    await session.commit()
    await session.refresh(job)
    return JobOut.model_validate(job)


# --------------------------------------------------------------------------- #
# assets
# --------------------------------------------------------------------------- #
@router.get("/assets", response_model=list[AssetOut], summary="List creative assets")
async def list_assets(
    session: DB,
    user: CurrentUser,
    campaign_id: uuid.UUID | None = None,
    content_item_id: uuid.UUID | None = None,
    platform: str | None = None,
    kind: str | None = None,
    validation_status: str | None = None,
    q: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=100, ge=1, le=200),
) -> list[AssetOut]:
    stmt = select(CreativeAsset)
    if campaign_id:
        stmt = stmt.where(CreativeAsset.campaign_id == campaign_id)
    if content_item_id:
        stmt = stmt.where(CreativeAsset.content_item_id == content_item_id)
    if kind:
        stmt = stmt.where(CreativeAsset.kind == kind)
    if validation_status:
        stmt = stmt.where(CreativeAsset.validation_status == validation_status)
    if platform:
        stmt = stmt.where(CreativeAsset.metadata_json["platform"].as_string() == platform)
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                CreativeAsset.filename.ilike(pattern),
                CreativeAsset.prompt.ilike(pattern),
            )
        )
    stmt = stmt.order_by(CreativeAsset.created_at.desc()).limit(limit)
    rows = list((await session.execute(stmt)).scalars().all())
    return [AssetOut.model_validate(row) for row in rows]


@router.get("/assets/{asset_id}", response_model=AssetDetailOut, summary="Asset detail")
async def get_asset(asset_id: uuid.UUID, session: DB, user: CurrentUser) -> AssetDetailOut:
    asset = await CreativeService(session).get_asset(asset_id)
    approval_status, approval = await _approval_for(session, asset)
    brief = await session.get(CreativeBrief, asset.brief_id) if asset.brief_id else None
    job = (
        await session.get(CreativeGenerationJob, asset.generation_job_id)
        if asset.generation_job_id
        else None
    )
    versions = await CreativeService(session).list_versions(asset)
    return AssetDetailOut(
        **AssetOut.model_validate(asset).model_dump(),
        approval_status=approval_status,
        approval=approval,
        brief=BriefOut.model_validate(brief) if brief else None,
        job=JobOut.model_validate(job) if job else None,
        versions=[
            {
                "id": str(row.id),
                "asset_version": row.asset_version,
                "status": row.status,
                "checksum": row.checksum,
                "size_bytes": row.size_bytes,
                "width": row.width,
                "height": row.height,
                "is_mock": row.is_mock,
                "created_at": row.created_at.isoformat(),
                "selected": row.selected,
            }
            for row in versions
        ],
    )


@router.get("/assets/{asset_id}/bytes", summary="Download the stored bytes of an asset")
async def get_asset_bytes(asset_id: uuid.UUID, session: DB, user: CurrentUser) -> Response:
    asset = await CreativeService(session).get_asset(asset_id)
    if not asset.storage_key:
        raise NotFoundError("This asset has no stored bytes.")
    data = await _storage().get(asset.storage_key)
    return Response(
        content=data,
        media_type=asset.mime_type or "application/octet-stream",
        headers={
            "Cache-Control": "private, max-age=60",
            "X-Asset-Checksum": asset.checksum,
            "X-Asset-Mock": "true" if asset.is_mock else "false",
        },
    )


@router.post(
    "/assets/{asset_id}/validate",
    summary="Re-run the automated Creative QA checks",
)
async def validate_asset(asset_id: uuid.UUID, session: DB, user: EditorUser) -> dict[str, Any]:
    asset = await CreativeService(session).get_asset(asset_id)
    brief = await session.get(CreativeBrief, asset.brief_id) if asset.brief_id else None
    report = await CreativeQAService(session, _storage()).evaluate(
        asset=asset, brief=brief, actor_id=user.id
    )
    await session.commit()
    await session.refresh(asset)
    return {"asset": AssetOut.model_validate(asset).model_dump(mode="json"), "qa_report": report}


@router.post(
    "/assets/{asset_id}/select",
    response_model=AssetSelectOut,
    summary="Select an asset for its content version (invalidates any approval)",
)
async def select_asset(
    asset_id: uuid.UUID, payload: AssetSelectIn, session: DB, user: EditorUser
) -> AssetSelectOut:
    service = CreativeService(session)
    asset = await service.get_asset(asset_id)
    asset, invalidated = await service.select(
        asset, actor_id=user.id, content_version_id=payload.content_version_id
    )
    await session.commit()
    await session.refresh(asset)
    return AssetSelectOut(
        asset=AssetOut.model_validate(asset),
        invalidated_approvals=invalidated,
        message=(
            f"Asset selected. {invalidated} approval(s) invalidated."
            if invalidated
            else "Asset selected. No live approval referenced the previous selection."
        ),
    )


@router.post(
    "/assets/{asset_id}/versions",
    response_model=AssetSelectOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a new version of an asset without overwriting the previous one",
)
async def upload_asset_version(
    asset_id: uuid.UUID,
    session: DB,
    user: EditorUser,
    file: UploadFile = File(...),
) -> AssetSelectOut:
    settings = get_settings()
    data = await file.read()
    info, _warnings, error = validate_upload(
        data,
        declared_mime=file.content_type,
        filename=file.filename,
        max_bytes=settings.creative_max_upload_bytes,
        strict=True,
    )
    if error:
        raise ValidationProblem(error, code="invalid_upload")

    service = CreativeService(session)
    existing = await service.get_asset(asset_id)
    # A new object key every time: the previous version's bytes are never touched.
    key = storage_key(file.filename or "asset.png", prefix="creatives")
    stored = await _storage().put(key, data, content_type=info.mime_type or "")
    asset, invalidated = await service.create_version(
        existing,
        data=data,
        mime_type=info.mime_type or "",
        filename=file.filename or "asset",
        storage_key=stored.key,
        actor_id=user.id,
        metadata={"uploaded": True, "detected_mime": info.mime_type},
        public_url=stored.public_url,
        width=info.width,
        height=info.height,
        is_mock=False,
    )
    await CreativeQAService(session, _storage()).evaluate(
        asset=asset, brief=None, actor_id=user.id
    )
    await session.commit()
    await session.refresh(asset)
    return AssetSelectOut(
        asset=AssetOut.model_validate(asset),
        invalidated_approvals=invalidated,
        message=(
            f"New asset version {asset.asset_version} stored. "
            f"{invalidated} approval(s) invalidated."
        ),
    )


@router.post(
    "/assets/upload",
    response_model=AssetOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload an external asset with strict type and size validation",
)
async def upload_asset(
    session: DB,
    user: EditorUser,
    file: UploadFile = File(...),
    content_item_id: uuid.UUID | None = Form(default=None),
    campaign_id: uuid.UUID | None = Form(default=None),
    kind: str = Form(default="image"),
) -> AssetOut:
    settings = get_settings()
    data = await file.read()
    info, _warnings, error = validate_upload(
        data,
        declared_mime=file.content_type,
        filename=file.filename,
        max_bytes=settings.creative_max_upload_bytes,
        strict=True,
    )
    if error:
        raise ValidationProblem(error, code="invalid_upload")

    key = storage_key(file.filename or "asset.png", prefix="creatives")
    stored = await _storage().put(key, data, content_type=info.mime_type or "")

    version_id = None
    brand_id = None
    if content_item_id is not None:
        item = await session.get(ContentItem, content_item_id)
        if item is None:
            raise NotFoundError(f"Content item {content_item_id} not found.")
        version_id = item.current_version_id
        brand_id = item.brand_profile_id
        campaign_id = campaign_id or item.campaign_id

    asset = await CreativeService(session).register(
        content_item_id=content_item_id,
        content_version_id=version_id,
        brand_profile_id=brand_id,
        campaign_id=campaign_id,
        kind=kind,
        filename=file.filename or "asset",
        mime_type=info.mime_type or "",
        detected_mime=info.mime_type or "",
        metadata_verified=True,
        content_bytes=data,
        storage_key=stored.key,
        public_url=stored.public_url,
        width=info.width,
        height=info.height,
        metadata={"uploaded": True, "source": "external_upload"},
        actor_id=user.id,
    )
    await CreativeQAService(session, _storage()).evaluate(asset=asset, brief=None, actor_id=user.id)
    await session.commit()
    await session.refresh(asset)
    return AssetOut.model_validate(asset)


@router.get("/assets/{asset_id}/versions", summary="Asset version history")
async def asset_versions(asset_id: uuid.UUID, session: DB, user: CurrentUser) -> list[dict[str, Any]]:
    asset = await CreativeService(session).get_asset(asset_id)
    rows = await CreativeService(session).list_versions(asset)
    return [
        {
            "id": str(row.id),
            "asset_version": row.asset_version,
            "status": row.status,
            "selected": row.selected,
            "checksum": row.checksum,
            "size_bytes": row.size_bytes,
            "created_at": row.created_at.isoformat(),
            "is_mock": row.is_mock,
        }
        for row in rows
    ]
