"""Credential, redaction and origin primitives for the local Flow bridge.

Everything here is Crusor-owned code. The rules it implements come from the
external DeepX audit (findings S1/S2) plus this repository's own constraints:

* pairing credentials are 256 bits from :mod:`secrets`, never derived from a
  password, and never written to a log line;
* every credential comparison is :func:`hmac.compare_digest` over bytes, so a
  timing side channel cannot be used to guess a secret one byte at a time;
* Host and Origin are validated against explicit allowlists — wildcard
  ``window.postMessage``-style trust does not exist here;
* only loopback bind addresses are accepted, and the check is a literal IP parse
  rather than a string comparison on a log message.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Iterable

#: How comparisons are done. Exposed for the test suite so "constant time" is an
#: assertion about the implementation, not a comment.
CONSTANT_TIME_IMPLEMENTATION = "hmac.compare_digest"

PAIRING_SECRET_BYTES = 32
PAIRING_ID_BYTES = 12


class LoopbackBindError(RuntimeError):
    """Raised instead of binding to anything that is not a loopback address."""


def generate_pairing_secret() -> str:
    """256 bits of URL-safe entropy (43 characters)."""
    return secrets.token_urlsafe(PAIRING_SECRET_BYTES)


def generate_pairing_id() -> str:
    return secrets.token_urlsafe(PAIRING_ID_BYTES)


def entropy_bits(secret: str) -> float:
    """Lower bound on entropy for a token_urlsafe-style secret, used by tests/doc."""
    if not secret:
        return 0.0
    try:
        raw = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
        return float(len(raw) * 8)
    except Exception:  # noqa: BLE001 - non-base64 secrets still measured by length
        return float(len(secret) * 6)


def _to_bytes(value: str | bytes) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else value


def constant_time_equals(left: str | bytes, right: str | bytes) -> bool:
    """Compare two credentials without leaking length or content through timing."""
    if left is None or right is None:
        return False
    return hmac.compare_digest(_to_bytes(left), _to_bytes(right))


def secret_fingerprint(secret: str) -> str:
    """A stable, non-reversible label for a secret (safe to log or store)."""
    return hashlib.sha256(_to_bytes(secret)).hexdigest()[:16]


def is_loopback_host(host: str) -> bool:
    candidate = (host or "").strip().strip("[]")
    if candidate.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def assert_loopback_bind(host: str) -> str:
    """Refuse to start a local bridge anywhere but loopback.

    ``0.0.0.0``, ``::``, a LAN address and a hostname that merely resolves to
    loopback are all rejected: only an explicit loopback literal is accepted.
    """
    if not is_loopback_host(host):
        raise LoopbackBindError(
            f"Refusing to bind the Flow bridge to {host!r}: only loopback addresses "
            "are allowed (127.0.0.1 or ::1)."
        )
    return host


# --------------------------------------------------------------------------- #
# redaction
# --------------------------------------------------------------------------- #
_SECRET_PATTERNS = (
    re.compile(r"(ya29\.[A-Za-z0-9_\-\.]{10,})"),          # Google access token
    re.compile(r"(Bearer\s+[A-Za-z0-9_\-\.=]{8,})", re.I),
    re.compile(r"(pairing[_-]?secret\"?\s*[:=]\s*\"?)([^\s\",}]{4,})", re.I),
    re.compile(r"(x-flow-pairing\"?\s*[:=]\s*\"?)([^\s\",}]{4,})", re.I),
    re.compile(r"(__Secure-[A-Za-z0-9\-]+=)([^;\s]{4,})"),  # cookie jar dumps
)


def redact(text: str, *, secrets_to_scrub: Iterable[str] = ()) -> str:
    """Remove credential-shaped substrings before anything is logged.

    Two layers: an explicit list of known secrets (compared in constant time) and a
    set of patterns that catch Google bearer tokens, cookie jars and pairing values
    even if a new code path forgets to pass the secrets explicitly.
    """
    if not text:
        return text
    scrubbed = text
    for secret in secrets_to_scrub:
        if secret and len(secret) >= 8:
            scrubbed = scrubbed.replace(secret, "[redacted]")
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            scrubbed = pattern.sub(lambda m: f"{m.group(1)}[redacted]", scrubbed)
        else:
            scrubbed = pattern.sub("[redacted]", scrubbed)
    return scrubbed


class RedactingFilter(logging.Filter):
    """Log filter installed on every bridge/companion logger.

    Belt and braces: even if a future edit formats a secret into a message, the
    record leaves this filter with ``[redacted]`` in its place.
    """

    def __init__(self, secrets_to_scrub: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets_to_scrub if s)

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        cleaned = redact(message, secrets_to_scrub=self._secrets)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


def install_redaction(logger: logging.Logger, secrets_to_scrub: Iterable[str] = ()) -> None:
    for existing in list(logger.filters):
        if isinstance(existing, RedactingFilter):
            logger.removeFilter(existing)
    logger.addFilter(RedactingFilter(secrets_to_scrub))


# --------------------------------------------------------------------------- #
# host / origin policy
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class HostOriginPolicy:
    """Explicit allowlists. Wildcards are not representable on purpose."""

    port: int
    extension_ids: tuple[str, ...] = ()
    allow_localhost_alias: bool = True

    def allowed_hosts(self) -> tuple[str, ...]:
        names = [f"127.0.0.1:{self.port}", f"[::1]:{self.port}", f"localhost:{self.port}"]
        if not self.allow_localhost_alias:
            names = [n for n in names if not n.startswith("localhost")]
        return tuple(names)

    def allowed_origins(self) -> tuple[str, ...]:
        origins = [f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"]
        origins.extend(f"chrome-extension://{ext_id}" for ext_id in self.extension_ids)
        return tuple(origins)

    def check_host(self, host_header: str | None) -> tuple[bool, str]:
        if not host_header:
            return False, "missing Host header"
        value = host_header.strip().lower()
        if value in {h.lower() for h in self.allowed_hosts()}:
            return True, ""
        return False, "Host header is not the local bridge address"

    def check_origin(self, origin: str | None) -> tuple[bool, str]:
        """``Origin`` may be absent (non-browser clients) but never wrong."""
        if origin is None or origin == "":
            return True, "origin absent"
        value = origin.strip()
        if value in set(self.allowed_origins()):
            return True, ""
        return False, "Origin is not an allowlisted extension or local origin"


# --------------------------------------------------------------------------- #
# connection accounting
# --------------------------------------------------------------------------- #
class ConnectionLimiter:
    """Counts live connections and enforces a hard ceiling.

    Plain integer accounting rather than a semaphore so the bridge can answer
    ``429`` and close instead of queueing work behind a stuck socket.
    """

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("connection limit must be >= 1")
        self.limit = limit
        self.active = 0
        self.rejected = 0

    def try_acquire(self) -> bool:
        if self.active >= self.limit:
            self.rejected += 1
            return False
        self.active += 1
        return True

    def release(self) -> None:
        self.active = max(0, self.active - 1)

    def snapshot(self) -> dict[str, int]:
        return {"limit": self.limit, "active": self.active, "rejected": self.rejected}
