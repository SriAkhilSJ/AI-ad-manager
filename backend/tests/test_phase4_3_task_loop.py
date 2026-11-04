"""Celery tasks must share one event loop, or the DB pool outlives its loop.

``asyncio.run()`` closes the loop after every task while the SQLAlchemy engine (a
module-level singleton) still holds pooled connections created on it; the next
task's checkout then fails inside ``pool_pre_ping``. SQLite often hides this, the
PostgreSQL job in CI did not.

These tests purposefully restore every piece of global asyncio state they touch:
the engine and the event-loop policy are process-wide, and a test that leaves a
closed loop installed breaks unrelated tests in the same worker.
"""

from __future__ import annotations

import asyncio

import pytest

from app.tasks import jobs


@pytest.fixture()
def restored_loop_state():
    """Undo anything a test does to the worker loop, whatever it is."""
    previous_jobs_loop = jobs._loop
    try:
        current = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        current = None
    try:
        yield
    finally:
        jobs._loop = previous_jobs_loop
        if current is not None and not current.is_closed():
            asyncio.set_event_loop(current)
        elif previous_jobs_loop is not None and not previous_jobs_loop.is_closed():
            asyncio.set_event_loop(previous_jobs_loop)


def test_run_reuses_one_event_loop_across_tasks(restored_loop_state):
    jobs._loop = None

    async def which_loop():
        return id(asyncio.get_running_loop())

    first = jobs._run(which_loop)
    second = jobs._run(which_loop)

    assert first == second, "each task got its own loop, so pooled connections dangle"
    assert not jobs._loop.is_closed()


def test_run_recovers_when_the_loop_was_closed(restored_loop_state):
    jobs._loop = None
    assert jobs._run(lambda: asyncio.sleep(0, result="ok")) == "ok"
    jobs._loop.close()
    # the next task must not inherit the dead loop
    assert jobs._run(lambda: asyncio.sleep(0, result="ok")) == "ok"
    assert not jobs._loop.is_closed()
