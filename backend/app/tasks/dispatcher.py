"""Background task dispatch.

Phase 1 needs agent workflows to run without blocking an HTTP request, but Redis
is not guaranteed to exist. Rather than silently dropping work, the dispatcher
resolves a driver and reports which one is active:

* ``celery`` — production fan-out over Redis.
* ``inline`` — runs the coroutine in-process on an asyncio task (dev/test). Real
  execution, real ledger rows, no external dependency.
* ``disabled`` — explicit; submissions are rejected with a typed error.

``/api/v1/system/status`` exposes the resolved driver so nobody mistakes dev
behaviour for a distributed queue.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Coroutine

from app.core.config import Settings, get_settings
from app.core.errors import IntegrationUnavailableError
from app.core.logging import get_logger

logger = get_logger("crusor.tasks")


@dataclass
class TaskHandle:
    task_id: str
    driver: str
    accepted: bool
    detail: str = ""


@dataclass
class InlineDispatcher:
    """Runs coroutines in the current process and keeps a bounded registry."""

    driver: str = "inline"
    available: bool = True
    _tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    _results: dict[str, Any] = field(default_factory=dict)

    async def submit(
        self, name: str, coro_factory: Callable[[], Coroutine[Any, Any, Any]]
    ) -> TaskHandle:
        task_id = f"inline-{len(self._tasks) + 1}-{name}"
        task = asyncio.create_task(coro_factory(), name=task_id)
        self._tasks[task_id] = task
        task.add_done_callback(lambda t: self._results.__setitem__(task_id, t))
        return TaskHandle(task_id=task_id, driver=self.driver, accepted=True)

    def status(self, task_id: str) -> dict[str, Any]:
        task = self._tasks.get(task_id)
        if task is None:
            return {"task_id": task_id, "state": "unknown", "driver": self.driver}
        if not task.done():
            return {"task_id": task_id, "state": "running", "driver": self.driver}
        if task.cancelled():
            return {"task_id": task_id, "state": "cancelled", "driver": self.driver}
        exc = task.exception()
        return {
            "task_id": task_id,
            "state": "failed" if exc else "succeeded",
            "driver": self.driver,
            "error": str(exc)[:300] if exc else None,
        }

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)


@dataclass
class CeleryDispatcher:
    driver: str = "celery"
    available: bool = True

    async def submit(
        self, name: str, coro_factory: Callable[[], Coroutine[Any, Any, Any]]
    ) -> TaskHandle:
        from app.tasks.celery_app import celery_app

        # Celery workers execute the *sync* task which spins its own event loop;
        # the coroutine factory is therefore resolved inside the worker.
        result = celery_app.send_task("crusor.run_job", kwargs={"job": name})
        return TaskHandle(
            task_id=str(result.id), driver=self.driver, accepted=True, detail="queued"
        )

    def status(self, task_id: str) -> dict[str, Any]:
        from app.tasks.celery_app import celery_app

        result = celery_app.AsyncResult(task_id)
        return {
            "task_id": task_id,
            "state": result.state.lower(),
            "driver": self.driver,
            "error": str(result.result)[:300] if result.failed() else None,
        }


@dataclass
class DisabledDispatcher:
    driver: str = "disabled"
    available: bool = False
    reason: str = "Background tasks are disabled by configuration."

    async def submit(
        self, name: str, coro_factory: Callable[[], Coroutine[Any, Any, Any]]
    ) -> TaskHandle:
        raise IntegrationUnavailableError(self.reason)

    def status(self, task_id: str) -> dict[str, Any]:
        return {"task_id": task_id, "state": "unavailable", "driver": self.driver}


Dispatcher = InlineDispatcher | CeleryDispatcher | DisabledDispatcher

_dispatcher: Dispatcher | None = None


def get_dispatcher(settings: Settings | None = None) -> Dispatcher:
    global _dispatcher
    if _dispatcher is not None:
        return _dispatcher
    settings = settings or get_settings()
    mode = settings.task_dispatcher
    broker = settings.celery_broker_url or settings.redis_url

    if mode == "disabled":
        _dispatcher = DisabledDispatcher()
    elif mode == "celery" or (mode == "auto" and broker):
        if not broker:
            _dispatcher = InlineDispatcher()
            logger.warning("celery_requested_without_broker", fallback="inline")
        else:
            _dispatcher = CeleryDispatcher()
    else:
        _dispatcher = InlineDispatcher()
    return _dispatcher


def reset_dispatcher() -> None:
    global _dispatcher
    _dispatcher = None


def dispatcher_status(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    dispatcher = get_dispatcher(settings)
    notes = {
        "inline": "In-process execution. Suitable for development; no persistence across restarts.",
        "celery": "Celery worker over Redis.",
        "disabled": "Task submission is disabled.",
    }
    return {
        "driver": dispatcher.driver,
        "available": dispatcher.available,
        "status": "available" if dispatcher.available else "unavailable",
        "broker_configured": bool(settings.celery_broker_url or settings.redis_url),
        "requested": settings.task_dispatcher,
        "note": notes.get(dispatcher.driver, ""),
    }
