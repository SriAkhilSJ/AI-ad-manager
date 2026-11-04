from __future__ import annotations

import uuid

from fastapi import APIRouter, UploadFile, File, Form, status

from app.api.deps import DB, CurrentUser, EditorUser
from app.core.errors import ValidationProblem
from app.schemas.creative import CreativeAssetCreate, CreativeAssetOut, CreativeReplaceOut
from app.services.creative import CreativeService
from app.storage.base import get_storage, storage_key

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
    if not data:
        raise ValidationProblem("Uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValidationProblem(
            f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit."
        )
    service = CreativeService(session)
    storage = get_storage()
    if replace_asset_id is not None:
        existing = await service.get_asset(replace_asset_id)
        stored = await storage.put(
            existing.storage_key or storage_key(file.filename or "asset"),
            data,
            content_type=file.content_type,
        )
        asset, _invalidated = await service.replace(
            replace_asset_id,
            actor_id=user.id,
            content_bytes=data,
            storage_key=stored.key,
            public_url=stored.public_url,
            mime_type=file.content_type,
            filename=file.filename,
        )
    else:
        key = storage_key(file.filename or "asset")
        stored = await storage.put(key, data, content_type=file.content_type)
        asset = await service.register(
            content_item_id=content_item_id,
            kind=kind,
            filename=file.filename or "asset",
            mime_type=file.content_type or "",
            content_bytes=data,
            storage_key=stored.key,
            public_url=stored.public_url,
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
    if not data:
        raise ValidationProblem("Replacement file is empty.")
    service = CreativeService(session)
    existing = await service.get_asset(asset_id)
    stored = await get_storage().put(
        existing.storage_key or storage_key(file.filename or "asset"),
        data,
        content_type=file.content_type,
    )
    asset, invalidated = await service.replace(
        asset_id,
        actor_id=user.id,
        content_bytes=data,
        storage_key=stored.key,
        public_url=stored.public_url,
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
