"""Celery application (production task system).

Run a worker with::

    celery -A app.tasks.celery_app:celery_app worker --loglevel=info

If ``CELERY_BROKER_URL``/``REDIS_URL`` is unset the app still imports cleanly;
the dispatcher simply won't route work to it.
"""

from __future__ import annotations

from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "crusor",
    broker=settings.celery_broker_url or settings.redis_url or "memory://",
    backend=settings.celery_result_backend or settings.redis_url or "cache+memory://",
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_time_limit=settings.agent_timeout_seconds * 4,
    task_soft_time_limit=settings.agent_timeout_seconds * 3,
    task_default_retry_delay=10,
    broker_connection_retry_on_startup=True,
)

celery_app.autodiscover_tasks(["app.tasks"])


@celery_app.task(name="crusor.health_check")
def health_check() -> str:
    return "ok"
