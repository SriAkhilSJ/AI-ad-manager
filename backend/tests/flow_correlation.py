"""Phase 3C.2.3.4 test support: correlation for scripted answers.

The coordinator now refuses to act on a result that is not bound to the exact command
that awaits it — a missing command id, job id or request hash is a rejection, not a
wildcard. Fixtures written before that rule built answers with placeholder ids, so
they describe an extension that never echoes correlation at all.

``correlated()`` fills in **only the values the fixture left empty**. A deliberately
wrong value is preserved, because the tests that hand the coordinator a foreign id,
a foreign job id or a mismatched hash must keep failing correlation.
"""

from __future__ import annotations

from typing import Any


def correlated(envelope: Any, result: Any) -> Any:
    """Return *result* with absent correlation values taken from *envelope*."""

    if not hasattr(result, "model_copy") or envelope is None:
        return result
    update: dict[str, Any] = {}
    if not getattr(result, "id", ""):
        update["id"] = getattr(envelope, "id", "")
    if not getattr(result, "job_id", ""):
        update["job_id"] = getattr(envelope, "job_id", "")
    if not getattr(result, "request_hash", ""):
        update["request_hash"] = getattr(envelope, "request_hash", "")
    return result.model_copy(update=update) if update else result
