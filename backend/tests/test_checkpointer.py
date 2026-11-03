"""Checkpointer durability: writes survive process restarts and re-instantiation."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.db.session import get_session_factory
from app.director.checkpointer import DatabaseCheckpointer
from app.models.workflow import WorkflowCheckpoint, WorkflowWrite


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


def _checkpoint(thread_id: str, version: int) -> dict:
    return {
        "v": 1,
        "id": f"cp-{version}",
        "ts": f"2026-11-02T00:00:0{version}+00:00",
        "channel_values": {"counter": version, "thread": thread_id},
        "channel_versions": {"counter": version},
        "versions_seen": {},
    }


async def test_buffer_is_not_written_until_drained():
    factory = get_session_factory()
    saver = DatabaseCheckpointer(factory)
    thread_id = f"t-{uuid.uuid4()}"
    await saver.aput(_config(thread_id), _checkpoint(thread_id, 1), {"step": 1, "node": "a"}, {})

    async with factory() as session:
        count = (
            await session.execute(
                select(func.count()).select_from(WorkflowCheckpoint).where(
                    WorkflowCheckpoint.thread_id == thread_id
                )
            )
        ).scalar_one()
    assert count == 0
    assert saver.pending_count == 1


async def test_drained_checkpoint_is_readable_by_a_new_saver():
    factory = get_session_factory()
    thread_id = f"t-{uuid.uuid4()}"

    writer = DatabaseCheckpointer(factory)
    await writer.aput(_config(thread_id), _checkpoint(thread_id, 1), {"step": 1, "node": "intake"}, {})
    written = await writer.flush_standalone()
    assert written >= 1

    # Brand-new saver instance == brand-new process from the DB's perspective.
    reader = DatabaseCheckpointer(factory)
    restored = await reader.aget_tuple(_config(thread_id))
    assert restored is not None
    assert restored.checkpoint["channel_values"]["counter"] == 1
    assert restored.metadata["node"] == "intake"


async def test_latest_checkpoint_is_returned_without_an_id():
    factory = get_session_factory()
    thread_id = f"t-{uuid.uuid4()}"
    saver = DatabaseCheckpointer(factory)

    for index in range(1, 4):
        cfg = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
                "checkpoint_id": f"cp-{index - 1}" if index > 1 else None,
            }
        }
        await saver.aput(cfg, _checkpoint(thread_id, index), {"step": index, "node": "n"}, {})
    await saver.flush_standalone()

    reader = DatabaseCheckpointer(factory)
    latest = await reader.aget_tuple(_config(thread_id))
    assert latest is not None
    assert latest.checkpoint["channel_values"]["counter"] == 3
    assert latest.parent_config is not None


async def test_pending_writes_are_persisted_and_carried_forward():
    factory = get_session_factory()
    thread_id = f"t-{uuid.uuid4()}"
    saver = DatabaseCheckpointer(factory)

    cfg = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    returned = await saver.aput(cfg, _checkpoint(thread_id, 1), {"step": 1}, {})
    await saver.aput_writes(returned, [("counter", 42)], task_id="task-1")
    await saver.flush_standalone()

    reader = DatabaseCheckpointer(factory)
    tuple1 = await reader.aget_tuple(returned)
    assert tuple1 is not None
    assert ("task-1", "counter", 42) in tuple1.pending_writes

    # a child checkpoint inherits the parent's pending writes
    child_cfg = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
            "checkpoint_id": "cp-1",
        }
    }
    writer2 = DatabaseCheckpointer(factory)
    child = await writer2.aput(child_cfg, _checkpoint(thread_id, 2), {"step": 2}, {})
    await writer2.flush_standalone()

    tuple2 = await DatabaseCheckpointer(factory).aget_tuple(child)
    assert tuple2 is not None
    assert ("task-1", "counter", 42) in tuple2.pending_writes


async def test_bytes_payloads_round_trip():
    factory = get_session_factory()
    thread_id = f"t-{uuid.uuid4()}"
    saver = DatabaseCheckpointer(factory)
    checkpoint = _checkpoint(thread_id, 1)
    checkpoint["channel_values"]["blob"] = b"\x00\x01binary"
    await saver.aput(_config(thread_id), checkpoint, {"step": 1}, {})
    await saver.flush_standalone()

    restored = await DatabaseCheckpointer(factory).aget_tuple(_config(thread_id))
    assert restored is not None
    assert restored.checkpoint["channel_values"]["blob"] == b"\x00\x01binary"


async def test_delete_thread_removes_all_state():
    factory = get_session_factory()
    thread_id = f"t-{uuid.uuid4()}"
    saver = DatabaseCheckpointer(factory)
    cfg = await saver.aput(_config(thread_id), _checkpoint(thread_id, 1), {"step": 1}, {})
    await saver.aput_writes(cfg, [("counter", 1)], task_id="t1")
    await saver.flush_standalone()

    await DatabaseCheckpointer(factory).adelete_thread(thread_id)

    async with factory() as session:
        checkpoints = (
            await session.execute(
                select(func.count()).select_from(WorkflowCheckpoint).where(
                    WorkflowCheckpoint.thread_id == thread_id
                )
            )
        ).scalar_one()
        writes = (
            await session.execute(
                select(func.count()).select_from(WorkflowWrite).where(
                    WorkflowWrite.thread_id == thread_id
                )
            )
        ).scalar_one()
    assert checkpoints == 0
    assert writes == 0


async def test_alist_returns_history_newest_first():
    factory = get_session_factory()
    thread_id = f"t-{uuid.uuid4()}"
    saver = DatabaseCheckpointer(factory)
    for index in range(1, 3):
        cfg = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
                "checkpoint_id": f"cp-{index - 1}" if index > 1 else None,
            }
        }
        await saver.aput(cfg, _checkpoint(thread_id, index), {"step": index}, {})
    await saver.flush_standalone()

    reader = DatabaseCheckpointer(factory)
    history = [t async for t in reader.alist(_config(thread_id))]
    assert [h.checkpoint["channel_values"]["counter"] for h in history] == [2, 1]
