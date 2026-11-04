"""Celery tasks must share one event loop, or the DB pool outlives its loop.

`asyncio.run()` closes the loop after every task while the SQLAlchemy engine (a
module-level singleton) still holds pooled connections created on it. The next
task's checkout then fails inside `pool_pre_ping`. SQLite often hides this; the
PostgreSQL job in CI did not.
"""

from __future__ import annotations

import asyncio

from app.tasks import jobs


def test_run_reuses_one_event_loop_across_tasks():
    jobs._loop = None  # start from a clean process state

    async def which_loop():
        return id(asyncio.get_running_loop())

    first = jobs._run(which_loop)
    second = jobs._run(which_loop)

    assert first == second, "each task got its own loop, so pooled connections dangle"
    assert not jobs._loop.is_closed()


def test_run_recovers_from_a_closed_loop():
    jobs._loop = None
    first = jobs._run(lambda: asyncio.sleep(0, result="ok"))
    asyncio.get_event_loop_policy()  # noqa: B018 - keep the policy imported state
    jobs._loop.close()
    second = jobs._run(lambda: asyncio.sleep(0, result="ok"))
    assert (first, second) == ("ok", "ok")


def test_task_coroutines_can_touch_the_database_twice_in_a_row():
    """Two sequential tasks must both get a working session from the same pool."""
    from app.db.session import session_scope
    from sqlalchemy import text

    jobs._loop = None

    async def probe():
        async with session_scope() as session:
            value = (await session.execute(text("SELECT 1"))).scalar_one()
            return value

    assert jobs._run(probe) == 1
    assert jobs._run(probe) == 1
