"""Database-backed LangGraph checkpoint saver.

Why this is not just ``MemorySaver`` or a thin wrapper over a vendor saver:

* Workflow state must survive a process restart, so checkpoints live in
  PostgreSQL/SQLite alongside the rest of the domain data.
* LangGraph writes checkpoints from *inside* its execution loop, concurrently
  with node code. Sharing one ``AsyncSession`` across those tasks is unsafe
  (concurrent flush), and opening a second write transaction on SQLite while the
  graph's own transaction is open deadlocks.

Design: **write-behind buffering**.

1. ``aput`` / ``aput_writes`` append the serialised checkpoint to an in-memory
   buffer. They never touch the database, so they are safe to call from any task.
2. The runtime calls :meth:`DatabaseCheckpointer.drain` at a safe point — after
   each graph step — which writes the buffered checkpoints *through the caller's
   session*, in the same transaction as the node's domain writes. Checkpoints and
   the data they describe therefore commit atomically, and durability is per
   superstep, which is standard LangGraph semantics.
3. Reads (``aget_tuple`` / ``alist``) go to committed rows, so a resumed workflow
   in a brand-new process rehydrates from the database with nothing in memory.

The buffer is bounded; if it is not drained (misconfigured embedder), writes are
lost rather than silently growing without limit.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Sequence

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.models.workflow import WorkflowCheckpoint, WorkflowWrite

logger = get_logger("crusor.director.checkpointer")

ENCODING = "json+base64"
MAX_BUFFER = 500


# --------------------------------------------------------------------- codec
def _json_default(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__bytes__": base64.b64encode(value).decode("ascii")}
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return str(value)


def _restore_bytes(obj: dict[str, Any]) -> Any:
    if set(obj.keys()) == {"__bytes__"}:
        return base64.b64decode(obj["__bytes__"])
    return obj


def _encode(payload: Any) -> str:
    serialised = json.dumps(payload, default=_json_default)
    return base64.b64encode(serialised.encode("utf-8")).decode("ascii")


def _decode(blob: str | None) -> Any:
    if not blob:
        return None
    raw = base64.b64decode(blob.encode("ascii")).decode("utf-8")
    return json.loads(raw, object_hook=_restore_bytes)


# ------------------------------------------------------------------- buffer
@dataclass
class _CheckpointWrite:
    thread_id: str
    checkpoint_ns: str
    checkpoint_id: str
    parent_checkpoint_id: str | None
    checkpoint_blob: str
    metadata_blob: str
    run_status: str
    node: str | None
    step: int


@dataclass
class _WriteRecord:
    thread_id: str
    checkpoint_ns: str
    checkpoint_id: str
    task_id: str
    task_path: str
    idx: int
    channel: str
    value_blob: str


@dataclass
class _Buffer:
    checkpoints: list[_CheckpointWrite] = field(default_factory=list)
    writes: list[_WriteRecord] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.checkpoints and not self.writes

    @property
    def size(self) -> int:
        return len(self.checkpoints) + len(self.writes)

    def clear(self) -> None:
        self.checkpoints.clear()
        self.writes.clear()


# -------------------------------------------------------------------- saver
class DatabaseCheckpointer(BaseCheckpointSaver):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._buffer = _Buffer()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ config util
    @staticmethod
    def _config_values(config: dict[str, Any]) -> tuple[str, str, str | None]:
        configurable = config.get("configurable") or {}
        thread_id = configurable.get("thread_id")
        if not thread_id:
            raise ValueError("checkpointer requires config['configurable']['thread_id']")
        return (
            str(thread_id),
            str(configurable.get("checkpoint_ns", "")),
            configurable.get("checkpoint_id"),
        )

    # ------------------------------------------------------------------ reads
    async def aget_tuple(self, config: dict[str, Any]) -> CheckpointTuple | None:
        thread_id, ns, checkpoint_id = self._config_values(config)
        async with self._session_factory() as session:
            stmt = select(WorkflowCheckpoint).where(
                WorkflowCheckpoint.thread_id == thread_id,
                WorkflowCheckpoint.checkpoint_ns == ns,
            )
            if checkpoint_id:
                stmt = stmt.where(WorkflowCheckpoint.checkpoint_id == checkpoint_id)
            else:
                stmt = stmt.order_by(WorkflowCheckpoint.step.desc()).limit(1)
            row = (await session.execute(stmt)).scalars().first()
            if row is None:
                return None
            writes = await self._load_writes(session, thread_id, ns, row.checkpoint_id)
        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": ns,
                    "checkpoint_id": row.checkpoint_id,
                }
            },
            checkpoint=_decode(row.checkpoint_blob) or {},
            metadata=_decode(row.metadata_blob) or {},
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": ns,
                        "checkpoint_id": row.parent_checkpoint_id,
                    }
                }
                if row.parent_checkpoint_id
                else None
            ),
            pending_writes=writes,
        )

    async def _load_writes(
        self, session: AsyncSession, thread_id: str, ns: str, checkpoint_id: str
    ) -> list[tuple[str, str, Any]]:
        stmt = (
            select(WorkflowWrite)
            .where(
                WorkflowWrite.thread_id == thread_id,
                WorkflowWrite.checkpoint_ns == ns,
                WorkflowWrite.checkpoint_id == checkpoint_id,
            )
            .order_by(WorkflowWrite.idx.asc())
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [(r.task_id, r.channel, _decode(r.value_blob)) for r in rows]

    async def alist(
        self,
        config: dict[str, Any] | None,
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        thread_id, ns, _ = self._config_values(config or {})
        async with self._session_factory() as session:
            stmt = (
                select(WorkflowCheckpoint)
                .where(
                    WorkflowCheckpoint.thread_id == thread_id,
                    WorkflowCheckpoint.checkpoint_ns == ns,
                )
                .order_by(WorkflowCheckpoint.step.desc())
            )
            if limit:
                stmt = stmt.limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            tuples: list[CheckpointTuple] = []
            for row in rows:
                writes = await self._load_writes(session, thread_id, ns, row.checkpoint_id)
                tuples.append(
                    CheckpointTuple(
                        config={
                            "configurable": {
                                "thread_id": thread_id,
                                "checkpoint_ns": ns,
                                "checkpoint_id": row.checkpoint_id,
                            }
                        },
                        checkpoint=_decode(row.checkpoint_blob) or {},
                        metadata=_decode(row.metadata_blob) or {},
                        parent_config=None,
                        pending_writes=writes,
                    )
                )
        for item in tuples:
            yield item

    # ----------------------------------------------------------- buffered puts
    async def aput(
        self,
        config: dict[str, Any],
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> dict[str, Any]:
        thread_id, ns, _ = self._config_values(config)
        checkpoint_id = checkpoint["id"]
        parent_id = (config.get("configurable") or {}).get("checkpoint_id")
        meta = metadata or {}
        async with self._lock:
            self._buffer.checkpoints.append(
                _CheckpointWrite(
                    thread_id=thread_id,
                    checkpoint_ns=ns,
                    checkpoint_id=checkpoint_id,
                    parent_checkpoint_id=parent_id,
                    checkpoint_blob=_encode(checkpoint),
                    metadata_blob=_encode(meta),
                    run_status=str(
                        meta.get("run_status") or meta.get("status") or "running"
                    ),
                    node=meta.get("node"),
                    step=int(meta.get("step", 0) or 0),
                )
            )
            self._enforce_bound()
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    async def aput_writes(
        self,
        config: dict[str, Any],
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id, ns, checkpoint_id = self._config_values(config)
        if not checkpoint_id:
            raise ValueError("aput_writes requires a checkpoint_id in config")
        async with self._lock:
            for idx, (channel, value) in enumerate(writes):
                self._buffer.writes.append(
                    _WriteRecord(
                        thread_id=thread_id,
                        checkpoint_ns=ns,
                        checkpoint_id=checkpoint_id,
                        task_id=task_id,
                        task_path=task_path,
                        idx=idx,
                        channel=channel,
                        value_blob=_encode(value),
                    )
                )
            self._enforce_bound()

    def _enforce_bound(self) -> None:
        if self._buffer.size > MAX_BUFFER:
            dropped = self._buffer.size - MAX_BUFFER
            # Drop oldest checkpoints; the newest checkpoint is the one that matters.
            del self._buffer.checkpoints[: max(0, dropped)]
            logger.warning("checkpoint_buffer_overflow", dropped=dropped)

    # ---------------------------------------------------------------- draining
    async def drain(self, session: AsyncSession) -> int:
        """Persist buffered checkpoints through ``session`` (no commit).

        Returns the number of rows written. Call this, then commit the session.
        """
        async with self._lock:
            if self._buffer.empty:
                return 0
            pending_cp = list(self._buffer.checkpoints)
            pending_wr = list(self._buffer.writes)
            self._buffer.clear()

        # Deduplicate: the newest buffered state per (thread, ns, id) wins.
        latest: dict[tuple[str, str, str], _CheckpointWrite] = {}
        for cp in pending_cp:
            latest[(cp.thread_id, cp.checkpoint_ns, cp.checkpoint_id)] = cp

        written = 0
        for cp in latest.values():
            existing = (
                await session.execute(
                    select(WorkflowCheckpoint).where(
                        WorkflowCheckpoint.thread_id == cp.thread_id,
                        WorkflowCheckpoint.checkpoint_ns == cp.checkpoint_ns,
                        WorkflowCheckpoint.checkpoint_id == cp.checkpoint_id,
                    )
                )
            ).scalars().first()
            if existing is None:
                session.add(
                    WorkflowCheckpoint(
                        thread_id=cp.thread_id,
                        checkpoint_ns=cp.checkpoint_ns,
                        checkpoint_id=cp.checkpoint_id,
                        parent_checkpoint_id=cp.parent_checkpoint_id,
                        checkpoint_type=ENCODING,
                        checkpoint_blob=cp.checkpoint_blob,
                        metadata_type=ENCODING,
                        metadata_blob=cp.metadata_blob,
                        run_status=cp.run_status,
                        node=cp.node,
                        step=cp.step,
                    )
                )
                written += 1
            else:
                existing.checkpoint_blob = cp.checkpoint_blob
                existing.metadata_blob = cp.metadata_blob
                existing.run_status = cp.run_status
                existing.node = cp.node
                existing.step = cp.step
                session.add(existing)

            # Carry the parent's pending writes onto the new checkpoint, matching
            # LangGraph's reference saver behaviour.
            if cp.parent_checkpoint_id:
                parent_rows = (
                    await session.execute(
                        select(WorkflowWrite).where(
                            WorkflowWrite.thread_id == cp.thread_id,
                            WorkflowWrite.checkpoint_ns == cp.checkpoint_ns,
                            WorkflowWrite.checkpoint_id == cp.parent_checkpoint_id,
                        )
                    )
                ).scalars().all()
                for row in parent_rows:
                    session.add(
                        WorkflowWrite(
                            thread_id=row.thread_id,
                            checkpoint_ns=row.checkpoint_ns,
                            checkpoint_id=cp.checkpoint_id,
                            task_id=row.task_id,
                            task_path=row.task_path,
                            idx=row.idx,
                            channel=row.channel,
                            value_type=row.value_type,
                            value_blob=row.value_blob,
                        )
                    )
                await session.execute(
                    delete(WorkflowWrite).where(
                        WorkflowWrite.thread_id == cp.thread_id,
                        WorkflowWrite.checkpoint_ns == cp.checkpoint_ns,
                        WorkflowWrite.checkpoint_id == cp.parent_checkpoint_id,
                    )
                )

        for wr in pending_wr:
            exists = (
                await session.execute(
                    select(WorkflowWrite.id).where(
                        WorkflowWrite.thread_id == wr.thread_id,
                        WorkflowWrite.checkpoint_ns == wr.checkpoint_ns,
                        WorkflowWrite.checkpoint_id == wr.checkpoint_id,
                        WorkflowWrite.task_id == wr.task_id,
                        WorkflowWrite.idx == wr.idx,
                    )
                )
            ).first()
            if exists:
                continue
            session.add(
                WorkflowWrite(
                    thread_id=wr.thread_id,
                    checkpoint_ns=wr.checkpoint_ns,
                    checkpoint_id=wr.checkpoint_id,
                    task_id=wr.task_id,
                    task_path=wr.task_path,
                    idx=wr.idx,
                    channel=wr.channel,
                    value_type=ENCODING,
                    value_blob=wr.value_blob,
                )
            )
            written += 1

        await session.flush()
        return written

    async def flush_standalone(self) -> int:
        """Drain using a private session + commit (for scripts and tests)."""
        async with self._session_factory() as session:
            written = await self.drain(session)
            await session.commit()
            return written

    @property
    def pending_count(self) -> int:
        return self._buffer.size

    # ------------------------------------------------------------- thread ops
    async def adelete_thread(self, thread_id: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                delete(WorkflowWrite).where(WorkflowWrite.thread_id == thread_id)
            )
            await session.execute(
                delete(WorkflowCheckpoint).where(WorkflowCheckpoint.thread_id == thread_id)
            )
            await session.commit()
