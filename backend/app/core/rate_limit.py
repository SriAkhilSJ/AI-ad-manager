"""Sliding-window rate limiting.

Two drivers:

* ``memory`` — process-local token buckets, used in dev/tests when Redis is absent.
* ``redis``  — fixed-window counters shared across processes (when ``REDIS_URL``
  is configured).

The active driver is reported by ``/api/v1/system/status`` so operators are never
misled into believing a distributed limit is in force when it is not.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Protocol

from app.core.config import get_settings


class RateLimiter(Protocol):
    driver: str

    def hit(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]: ...

    def reset(self) -> None: ...


class MemoryRateLimiter:
    driver = "memory"

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        bucket = self._hits[key]
        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = int(window_seconds - (now - bucket[0])) + 1
            return False, max(retry_after, 1)
        bucket.append(now)
        return True, 0

    def reset(self) -> None:
        self._hits.clear()


class RedisRateLimiter:
    driver = "redis"

    def __init__(self, url: str) -> None:
        import redis  # local import: optional dependency at runtime

        self._client = redis.Redis.from_url(url, decode_responses=True)

    def hit(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        window = int(time.time()) // window_seconds
        composite = f"rl:{key}:{window}"
        pipe = self._client.pipeline()
        pipe.incr(composite, 1)
        pipe.expire(composite, window_seconds + 1)
        count, _ = pipe.execute()
        if int(count) > limit:
            return False, window_seconds
        return True, 0

    def reset(self) -> None:  # pragma: no cover - destructive; test-only
        self._client.flushdb()


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is not None:
        return _limiter
    settings = get_settings()
    if settings.redis_url:
        try:
            _limiter = RedisRateLimiter(settings.redis_url)
            return _limiter
        except Exception:  # noqa: BLE001 - degrade, never crash on limiter init
            pass
    _limiter = MemoryRateLimiter()
    return _limiter


def reset_rate_limiter() -> None:
    global _limiter
    if _limiter is not None:
        _limiter.reset()
    _limiter = None
