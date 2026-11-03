"""Structured logging with mandatory secret redaction.

Every log event passes through :func:`_redact_processor`, which recursively
masks values whose *key* looks secret-bearing and scrubs bearer tokens out of
free-form strings. This is a hard requirement: agent payloads and HTTP metadata
frequently contain credentials.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

# Matches credential-bearing *keys*, but deliberately not metrics about tokens:
# ``tokens``, ``prompt_tokens``, ``total_tokens`` and ``max_tokens`` are counts and
# must survive redaction, or cost telemetry becomes useless.
SECRET_KEY_PATTERN = re.compile(
    r"(authorization|api[_-]?key|secret|password|passwd|credential|"
    r"private[_-]?key|access[_-]?key|session[_-]?id|"
    r"^token$|_token$|^tokens?_(?!count)|bearer)",
    re.IGNORECASE,
)
TOKEN_METRIC_PATTERN = re.compile(
    r"^(tokens|prompt_tokens|completion_tokens|total_tokens|max_tokens|"
    r"tokens_used|token_count|token_budget|reasoning_tokens)$",
    re.IGNORECASE,
)
BEARER_PATTERN = re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]{8,}=*", re.IGNORECASE)
REDACTED = "***REDACTED***"
_MAX_DEPTH = 6


def _redact_value(key: str, value: Any, depth: int = 0) -> Any:
    if key and TOKEN_METRIC_PATTERN.match(key):
        return value
    if SECRET_KEY_PATTERN.search(key or ""):
        return REDACTED
    if depth > _MAX_DEPTH:
        return "<max-depth>"
    if isinstance(value, dict):
        return {k: _redact_value(str(k), v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_redact_value(key, v, depth + 1) for v in value]
    if isinstance(value, str):
        return BEARER_PATTERN.sub(r"\1" + REDACTED, value)
    return value


def _redact_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return {k: _redact_value(str(k), v) for k, v in event_dict.items()}


def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    # ``force=True`` matters: a library (aiosqlite in particular) can install a
    # root handler before the app configures logging, which would otherwise leave
    # raw driver DEBUG lines bypassing the redaction processor below.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )
    for noisy in ("aiosqlite", "sqlalchemy.engine.Engine", "sqlalchemy.pool", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_processor,
    ]
    if json_logs:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "crusor") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
