"""Structured error handling.

Every failure leaves the API in one shape::

    {"error": {"code": "approval_required", "message": "...",
               "details": {...}, "request_id": "..."}}

Downstream clients can branch on ``code`` without string matching, and no
exception text from a dependency is ever echoed verbatim in production.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

logger = get_logger("crusor.errors")


class AppError(Exception):
    """Base class for all expected application failures."""

    code: str = "internal_error"
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message or self.message)
        self.message = message or self.message
        self.details = details or {}
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code


class NotFoundError(AppError):
    code = "not_found"
    status_code = status.HTTP_404_NOT_FOUND
    message = "Resource not found."


class ConflictError(AppError):
    code = "conflict"
    status_code = status.HTTP_409_CONFLICT
    message = "The request conflicts with the current state."


class ValidationProblem(AppError):
    code = "validation_error"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    message = "The request payload is invalid."


class AuthenticationError(AppError):
    code = "unauthenticated"
    status_code = status.HTTP_401_UNAUTHORIZED
    message = "Authentication credentials were missing or invalid."


class PermissionDeniedError(AppError):
    code = "forbidden"
    status_code = status.HTTP_403_FORBIDDEN
    message = "You do not have permission to perform this action."


class RateLimitedError(AppError):
    code = "rate_limited"
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    message = "Too many requests."


class InvalidStateTransition(AppError):
    code = "invalid_state_transition"
    status_code = status.HTTP_409_CONFLICT
    message = "This state transition is not allowed."


class ApprovalRequiredError(AppError):
    """Raised whenever publishing is attempted without a valid approval.

    This error is the enforcement point required by the Phase 1 brief: it is
    impossible to persist ``PUBLISHED`` without passing through it.
    """

    code = "approval_required"
    status_code = status.HTTP_409_CONFLICT
    message = "Publishing requires a valid approval for the exact content version."


class PublishingDisabledError(AppError):
    code = "publishing_disabled"
    status_code = status.HTTP_409_CONFLICT
    message = (
        "Publishing is disabled: no social platform adapter is configured. "
        "Phase 1 runs in human-approval-only mode."
    )


class IntegrationUnavailableError(AppError):
    code = "integration_unavailable"
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    message = "A required external integration is not configured."


class IdempotencyConflictError(AppError):
    code = "idempotency_key_reuse"
    status_code = status.HTTP_409_CONFLICT
    message = "This Idempotency-Key was already used with a different request body."


# ---- Phase 2 -------------------------------------------------------------
class BudgetExhaustedError(AppError):
    """A run was stopped because its cost or token budget was spent.

    Distinct from a 402/429: nothing is wrong with the request, the *envelope*
    ran out. The Director reports how much was spent and what remains undone.
    """

    code = "budget_exhausted"
    status_code = status.HTTP_409_CONFLICT
    message = "The cost or token budget for this run is exhausted."


class CancelledProblem(AppError):
    """Cooperative cancellation of a long-running research/strategy run."""

    code = "cancelled"
    status_code = status.HTTP_409_CONFLICT
    message = "The run was cancelled."


class LoopDetectedError(AppError):
    """The orchestrator would repeat work it has already done."""

    code = "loop_detected"
    status_code = status.HTTP_409_CONFLICT
    message = "Execution would repeat a task that has already been attempted."


class ResearchUnavailableError(AppError):
    """No usable research provider is configured for the requested scope."""

    code = "research_unavailable"
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    message = "No research provider is configured."


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def error_payload(
    code: str, message: str, *, details: dict[str, Any] | None, request_id: str
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": request_id,
        }
    }


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    rid = _request_id(request)
    if exc.status_code >= 500:
        logger.error("app_error", code=exc.code, message=exc.message, request_id=rid)
    else:
        logger.info("app_error", code=exc.code, message=exc.message, request_id=rid)
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(exc.code, exc.message, details=exc.details, request_id=rid),
    )


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    rid = _request_id(request)
    code = {
        401: "unauthenticated",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        429: "rate_limited",
    }.get(exc.status_code, "http_error")
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(code, detail, details=None, request_id=rid),
        headers=getattr(exc, "headers", None),
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    rid = _request_id(request)
    fields = [
        {
            "loc": ".".join(str(p) for p in err.get("loc", [])),
            "message": err.get("msg", ""),
            "type": err.get("type", ""),
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=error_payload(
            "validation_error",
            "The request payload is invalid.",
            details={"fields": fields},
            request_id=rid,
        ),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    rid = _request_id(request)
    logger.exception("unhandled_exception", request_id=rid, error_type=type(exc).__name__)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=error_payload(
            "internal_error",
            "An unexpected error occurred.",
            details=None,
            request_id=rid,
        ),
    )
