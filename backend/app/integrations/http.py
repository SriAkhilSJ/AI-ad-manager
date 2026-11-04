"""Shared HTTP behaviour for platform calls.

Every platform call goes through :func:`request_json` so that timeouts, retries,
rate limits and error mapping behave the same way everywhere, and so that a
provider's error text is captured *without* the request body (which may contain a
token).

Rules:

* **Retry only what is safe.** 429 and 5xx are retried with exponential backoff
  honouring ``Retry-After``; a 4xx that is not 429 is returned immediately,
  because retrying a rejected request just multiplies the rejection.
* **Publishing is not blindly retried.** A create call that may have succeeded is
  never retried automatically — the caller verifies first. That is why
  :func:`request_json` takes ``idempotent: bool`` and refuses to retry when it is
  ``False``.
* **Timeouts are failures, not successes.** ``httpx.TimeoutException`` is mapped
  to :class:`PlatformUnavailable`, never swallowed.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any

import httpx


class PlatformError(Exception):
    """A platform call failed. ``retryable`` describes the failure class."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str = "platform_error",
        retryable: bool = False,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.payload = payload or {}


class PlatformRateLimited(PlatformError):
    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message, status=429, code="platform_rate_limited", retryable=True)
        self.retry_after = retry_after


class PlatformUnavailable(PlatformError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="platform_unavailable", retryable=True)


class PlatformRejected(PlatformError):
    """The platform refused the request and will refuse it again."""

    def __init__(self, message: str, *, status: int, payload: dict[str, Any] | None = None) -> None:
        super().__init__(
            message, status=status, code="platform_rejected", retryable=False, payload=payload
        )


class PlatformAuthError(PlatformError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status=401, code="platform_auth_failed", retryable=False)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    timeout_seconds: float = 30.0

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return min(max(retry_after, 0.0), self.max_delay)
        jitter = 0.5 + random.random() / 2
        return min(self.base_delay * (2 ** (attempt - 1)) * jitter, self.max_delay)


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("retry-after")
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        return None


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    policy: RetryPolicy | None = None,
    idempotent: bool = True,
    sleep=asyncio.sleep,
    **kwargs: Any,
) -> dict[str, Any]:
    """Perform one platform request and return decoded JSON.

    ``idempotent=False`` disables retries: used for create/publish calls where a
    retried request could produce a duplicate post.
    """
    policy = policy or RetryPolicy()
    attempts = policy.max_attempts if idempotent else 1
    last_error: PlatformError | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = await client.request(method, url, timeout=policy.timeout_seconds, **kwargs)
        except httpx.TimeoutException as exc:
            last_error = PlatformUnavailable(f"timeout calling {method} {url}: {exc}")
            if attempt >= attempts:
                raise last_error from exc
            await sleep(policy.delay_for(attempt))
            continue
        except httpx.HTTPError as exc:
            last_error = PlatformUnavailable(f"transport error calling {method} {url}: {type(exc).__name__}")
            if attempt >= attempts:
                raise last_error from exc
            await sleep(policy.delay_for(attempt))
            continue

        if response.status_code == 429:
            retry_after = _retry_after(response)
            last_error = PlatformRateLimited(
                f"rate limited by {url}", retry_after=retry_after
            )
            if attempt >= attempts:
                raise last_error
            await sleep(policy.delay_for(attempt, retry_after))
            continue

        if response.status_code in (401, 403):
            raise PlatformAuthError(
                _error_message(response) or "the platform rejected the credential or scope"
            )

        if response.status_code >= 500:
            last_error = PlatformUnavailable(f"{url} returned {response.status_code}")
            if attempt >= attempts:
                raise last_error
            await sleep(policy.delay_for(attempt, _retry_after(response)))
            continue

        if response.status_code >= 400:
            raise PlatformRejected(
                _error_message(response) or f"{url} returned {response.status_code}",
                status=response.status_code,
                payload=_json_or_empty(response),
            )

        if not response.content:
            return {}
        return _json_or_empty(response)

    raise last_error or PlatformUnavailable("request failed")


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    if isinstance(data, dict):
        return data
    return {"data": data}


def _error_message(response: httpx.Response) -> str:
    """Extract a provider error string without echoing the request."""
    data = _json_or_empty(response)
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "")
    if isinstance(error, str):
        return error
    for key in ("message", "detail", "error_description"):
        if data.get(key):
            return str(data[key])
    return ""
