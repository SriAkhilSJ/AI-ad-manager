"""Celery job definitions.

Each job opens its own session scope and reuses the same services the API uses,
so retries are safe and idempotent (services are themselves idempotent).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Callable, Coroutine

from app.core.logging import get_logger
from app.tasks.celery_app import celery_app

logger = get_logger("crusor.tasks.jobs")


def _run(coro_factory: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    return asyncio.run(coro_factory())


async def _refresh_analytics(brand_profile_id: str | None = None, days: int = 30) -> dict[str, Any]:
    """Pull measured post metrics from the platform APIs.

    With no credential vault every brand reports ``unavailable`` and nothing is
    written — the honest outcome, not a zero-filled series.
    """
    from app.core.config import get_settings
    from app.db.session import session_scope
    from app.services.scheduler import refresh_analytics

    async with session_scope() as session:
        result = await refresh_analytics(
            session,
            get_settings(),
            brand_profile_id=uuid.UUID(brand_profile_id) if brand_profile_id else None,
            days=days,
        )
        await session.commit()
    logger.info("analytics_refreshed", **{k: v for k, v in result.items() if k != "unavailable"})
    return result


async def _publish_due(limit: int = 50) -> dict[str, Any]:
    """Publish every scheduled post whose time has arrived.

    Each publish goes through the normal approval + transport gates, so an
    approval invalidated by an edit fails the scheduled post instead of shipping
    a stale version.
    """
    from app.core.config import get_settings
    from app.db.session import session_scope
    from app.services.scheduler import SchedulerService

    async with session_scope() as session:
        result = await SchedulerService(session, get_settings()).run_due(limit=limit)
        await session.commit()
    logger.info(
        "publish_due_run",
        due=result["due"],
        published=result["published"],
        failed=result["failed"],
    )
    return result


@celery_app.task(name="crusor.run_job", bind=True, max_retries=3)
def run_job(self, *, job: str, **kwargs: Any) -> Any:  # type: ignore[no-untyped-def]
    handlers: dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {
        "refresh_analytics": _refresh_analytics,
        "publish_due": _publish_due,
    }
    handler = handlers.get(job)
    if handler is None:
        raise ValueError(f"Unknown job '{job}'")
    return _run(lambda: handler(**kwargs))
