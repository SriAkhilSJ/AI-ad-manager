from __future__ import annotations

import uuid

from fastapi import APIRouter, UploadFile, File, Form, status

from app.api.deps import DB, CurrentUser, EditorUser
from app.core.errors import ValidationProblem
from app.schemas.creative import CreativeAssetCreate, CreativeAssetOut, CreativeReplaceOut
from app.services.creative import CreativeService

router = APIRouter(prefix="/creative", tags=["creative"])

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


@router.post(
    "", response_model=CreativeAssetOut, status_code=status.HTTP_201_CREATED,
    summary="Register creative asset metadata",
)
async def create_asset(
    payload: CreativeAssetCreate, session: DB, user: EditorUser
) -> CreativeAssetOut:
    asset = await CreativeService(session).register(
        **payload.model_dump(), actor_id=user.id
    )
    await session.commit()
    await session.refresh(asset)
    return CreativeAssetOut.model_validate(asset)


@router.post(
    "/upload",
    response_model=CreativeAssetOut,
    status_code=201,
    summary="Upload creative bytes (checksum stored; approval invalidated on replace)",
)
async def upload_asset(
    session: DB,
    user: EditorUser,
    file: UploadFile = File(...),
    content_item_id: uuid.UUID | None = Form(default=None),
    kind: str = Form(default="image"),
    prompt: str = Form(default=""),
    replace_asset_id: uuid.UUID | None = Form(default=None),
) -> CreativeAssetOut:
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValidationProblem(
            f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit."
        )
    service = CreativeService(session)
    if replace_asset_id is not None:
        asset, _invalidated = await service.replace(
            replace_asset_id,
            actor_id=user.id,
            content_bytes=data,
            mime_type=file.content_type,
            filename=file.filename,
        )
    else:
        asset = await service.register(
            content_item_id=content_item_id,
            kind=kind,
            filename=file.filename or "asset",
            mime_type=file.content_type or "",
            content_bytes=data,
            prompt=prompt,
            actor_id=user.id,
        )
    await session.commit()
    await session.refresh(asset)
    return CreativeAssetOut.model_validate(asset)


@router.get("", response_model=list[CreativeAssetOut], summary="List creative assets")
async def list_assets(
    session: DB, user: CurrentUser, content_item_id: uuid.UUID | None = None
) -> list[CreativeAssetOut]:
    rows = await CreativeService(session).list_assets(content_item_id)
    return [CreativeAssetOut.model_validate(a) for a in rows]


@router.post(
    "/{asset_id}/replace",
    response_model=CreativeReplaceOut,
    summary="Replace asset bytes; invalidates approvals of the referencing version",
)
async def replace_asset(
    asset_id: uuid.UUID,
    session: DB,
    user: EditorUser,
    file: UploadFile = File(...),
) -> CreativeReplaceOut:
    data = await file.read()
    asset, invalidated = await CreativeService(session).replace(
        asset_id,
        actor_id=user.id,
        content_bytes=data,
        mime_type=file.content_type,
        filename=file.filename,
    )
    await session.commit()
    await session.refresh(asset)
    return CreativeReplaceOut(
        asset=CreativeAssetOut.model_validate(asset),
        invalidated_approvals=invalidated,
        message=(
            f"Asset replaced. {invalidated} approval(s) invalidated."
            if invalidated
            else "Asset replaced. No live approval referenced it."
        ),
    )
