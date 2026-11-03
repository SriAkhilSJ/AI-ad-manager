"""Portable column types.

Production runs PostgreSQL (Supabase-compatible) with ``JSONB`` and native
``UUID``; the sandbox and unit tests run SQLite. Rather than maintaining two
schemas, these type decorators render the right DDL per dialect while keeping a
single set of models.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import JSON, CHAR, TypeDecorator
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import UserDefinedType


class GUID(TypeDecorator):
    """UUID stored natively on PostgreSQL, as 32-char hex on SQLite."""

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):  # type: ignore[no-untyped-def]
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(32))

    def process_bind_param(self, value: Any, dialect):  # type: ignore[no-untyped-def]
        if value is None:
            return None
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        if dialect.name == "postgresql":
            return value
        return value.hex

    def process_result_value(self, value: Any, dialect):  # type: ignore[no-untyped-def]
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


class JSONB(TypeDecorator):
    """JSONB on PostgreSQL, JSON elsewhere."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):  # type: ignore[no-untyped-def]
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.JSONB())
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value: Any, dialect):  # type: ignore[no-untyped-def]
        if value is not None and dialect.name != "postgresql":
            # SQLite cannot bind arbitrary python objects; normalise via JSON text.
            return json.loads(json.dumps(value, default=str))
        return value


class Vector(UserDefinedType):
    """pgvector column type.

    Only rendered on PostgreSQL (where the ``vector`` extension is installed by
    the migration). On other dialects the model declares the column as JSON and
    similarity is computed in Python instead.
    """

    cache_ok = True

    def __init__(self, dim: int = 1536) -> None:
        self.dim = dim

    def get_col_spec(self, **kw: Any) -> str:  # pragma: no cover - PG only
        return f"vector({self.dim})"
