from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int = 50
    offset: int = 0


class MessageResponse(BaseModel):
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class IdResponse(BaseModel):
    id: uuid.UUID


class TimestampsOut(BaseModel):
    created_at: datetime
    updated_at: datetime | None = None
