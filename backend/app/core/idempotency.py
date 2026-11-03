"""Database-backed idempotency for sensitive operations.

Semantics (matching the widely used ``Idempotency-Key`` convention):

* First request with a key → executes, stores the response.
* Repeat with the *same* key and *same* request hash → replays the stored
  response (``Idempotency-Replayed: true``), without re-executing side effects.
* Repeat with the same key but a *different* body → ``409 idempotency_key_reuse``.
* A key still ``in_progress`` → ``409`` with a retry hint, so two concurrent
  approve/publish calls cannot both win.

When no key is supplied the operation is still internally idempotent (state
machine transitions are), but no replay is possible.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import IdempotencyConflictError
from app.models.idempotency import IdempotencyKey

KEY_TTL = timedelta(hours=24)


def hash_request(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def find(self, key: str, user_id: uuid.UUID | None) -> IdempotencyKey | None:
        stmt = select(IdempotencyKey).where(
            IdempotencyKey.key == key, IdempotencyKey.user_id == user_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def begin(
        self, key: str, user_id: uuid.UUID | None, endpoint: str, request_hash: str
    ) -> tuple[IdempotencyKey, dict[str, Any] | None]:
        """Claim a key. Returns the row and, if replayable, the stored body."""
        existing = await self.find(key, user_id)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise IdempotencyConflictError(
                    "This Idempotency-Key was already used with a different request body.",
                    details={"endpoint": existing.endpoint},
                )
            if existing.status == "completed":
                return existing, (existing.response or {})
            # in progress → concurrent duplicate
            raise IdempotencyConflictError(
                "A request with this Idempotency-Key is already in progress.",
                details={"status": existing.status},
            )

        row = IdempotencyKey(
            key=key,
            user_id=user_id,
            endpoint=endpoint,
            request_hash=request_hash,
            status="in_progress",
        )
        self.session.add(row)
        try:
            await self.session.flush()
        except IntegrityError:
            await self.session.rollback()
            existing = await self.find(key, user_id)
            if existing and existing.status == "completed":
                return existing, existing.response or {}
            raise IdempotencyConflictError(
                "A request with this Idempotency-Key is already in progress."
            ) from None
        return row, None

    async def complete(
        self, row: IdempotencyKey, response: dict[str, Any], status_code: int = 200
    ) -> None:
        row.status = "completed"
        row.response = response
        row.status_code = status_code
        row.completed_at = datetime.now(timezone.utc)
        self.session.add(row)
        await self.session.flush()

    async def fail(self, row: IdempotencyKey, reason: str) -> None:
        row.status = "failed"
        row.response = {"error": reason}
        await self.session.flush()

    async def purge_expired(self) -> int:
        cutoff = datetime.now(timezone.utc) - KEY_TTL
        stmt = select(IdempotencyKey).where(IdempotencyKey.created_at < cutoff)
        rows = (await self.session.execute(stmt)).scalars().all()
        for row in rows:
            await self.session.delete(row)
        return len(rows)


async def with_idempotency(
    session: AsyncSession,
    *,
    key: str | None,
    user_id: uuid.UUID | None,
    endpoint: str,
    payload: Any,
    operation: Callable[[], Awaitable[dict[str, Any]]],
) -> tuple[dict[str, Any], bool]:
    """Execute ``operation`` at most once per (user, key).

    Returns ``(body, replayed)``.
    """
    if not key:
        return await operation(), False

    service = IdempotencyService(session)
    row, replay = await service.begin(key, user_id, endpoint, hash_request(payload))
    if replay is not None:
        return replay, True
    try:
        result = await operation()
    except Exception:
        await service.fail(row, "operation_failed")
        raise
    await service.complete(row, result)
    return result, False
