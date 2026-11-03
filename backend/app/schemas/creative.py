from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CreativeAssetCreate(BaseModel):
    content_item_id: uuid.UUID | None = None
    kind: str = Field(default="image", pattern="^(image|video|carousel|thumbnail|document)$")
    filename: str = Field(min_length=1, max_length=300)
    mime_type: str = Field(default="", max_length=128)
    storage_key: str | None = None
    public_url: str | None = None
    prompt: str = ""
    provider: str = ""
    width: int | None = None
    height: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreativeAssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content_item_id: uuid.UUID | None
    content_version_id: uuid.UUID | None
    kind: str
    status: str
    filename: str
    mime_type: str
    size_bytes: int
    storage_key: str | None
    public_url: str | None
    checksum: str
    width: int | None
    height: int | None
    prompt: str
    provider: str
    metadata_json: dict[str, Any]
    generated_by_agent: str | None
    created_at: datetime


class CreativeReplaceOut(BaseModel):
    asset: CreativeAssetOut
    invalidated_approvals: int
    message: str
