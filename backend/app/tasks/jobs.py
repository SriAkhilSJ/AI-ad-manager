"""Celery job definitions.

Each job opens its own session scope and reuses the same services the API uses,
so retries are safe and idempotent (services are themselves idempotent).
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine

from app.core.logging import get_logger
from app.tasks.celery_app import celery_app

logger = get_logger("crusor.tasks.jobs")


def _run(coro_factory: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    return asyncio.run(coro_factory())


async def _refresh_analytics(brand_profile_id: str | None = None) -> dict[str, Any]:
    """Placeholder connector refresh.

    Phase 1 has no analytics connector, so this reports ``skipped`` rather than
    inventing metrics.
    """
    logger.info("analytics_refresh_skipped", reason="no_connector_configured")
    return {"status": "skipped", "reason": "no analytics connector configured"}


@celery_app.task(name="crusor.run_job", bind=True, max_retries=3)
def run_job(self, *, job: str, **kwargs: Any) -> Any:  # type: ignore[no-untyped-def]
    handlers: dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {
        "refresh_analytics": _refresh_analytics,
    }
    handler = handlers.get(job)
    if handler is None:
        raise ValueError(f"Unknown job '{job}'")
    return _run(lambda: handler(**kwargs))
