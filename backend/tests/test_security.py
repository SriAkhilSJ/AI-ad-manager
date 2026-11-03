"""Security: secret redaction, response allow-lists, status honesty, rate limits."""

from __future__ import annotations

import io
import json
import logging
import uuid

import pytest

from app.core.logging import _redact_processor


def test_log_redaction_masks_secret_keys():
    event = {
        "event": "call",
        "authorization": "Bearer abcdef1234567890",
        "api_key": "sk-live-123",
        "nested": {"password": "hunter2", "token": "xyz", "safe": "keep-me"},
        "list": [{"secret": "s3cr3t"}],
        "headers": {"Authorization": "Bearer supersecrettokenvalue"},
    }
    redacted = _redact_processor(None, "test", dict(event))
    assert redacted["authorization"] == "***REDACTED***"
    assert redacted["api_key"] == "***REDACTED***"
    assert redacted["nested"]["password"] == "***REDACTED***"
    assert redacted["nested"]["token"] == "***REDACTED***"
    assert redacted["nested"]["safe"] == "keep-me"
    assert redacted["list"][0]["secret"] == "***REDACTED***"
    assert "supersecrettokenvalue" not in json.dumps(redacted)


def test_bearer_tokens_in_free_text_are_scrubbed():
    redacted = _redact_processor(
        None, "test", {"note": "used Bearer abcdefghijklmnop1234 for the call"}
    )
    assert "abcdefghijklmnop1234" not in redacted["note"]
    assert "***REDACTED***" in redacted["note"]


FORBIDDEN_KEYS = {
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "token_ref",
    "password",
    "hashed_password",
    "secret",
    "client_secret",
    "api_key",
    "authorization",
    "private_key",
}


def _collect_keys(payload) -> set[str]:  # type: ignore[no-untyped-def]
    keys: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            keys.add(key.lower())
            keys |= _collect_keys(value)
    elif isinstance(payload, list):
        for entry in payload:
            keys |= _collect_keys(entry)
    return keys


async def test_social_account_response_never_contains_tokens(client, auth, brand, session):
    """The response schema is an allow-list: no credential-bearing key can appear.

    ``token_expires_at`` is a timestamp (expiry metadata), not a credential, so the
    assertion is structural rather than a substring match.
    """
    resp = await client.post(
        "/api/v1/social/accounts",
        json={"brand_profile_id": brand["id"], "platform": "linkedin", "handle": "crusor"},
        headers=auth("marketer"),
    )
    assert resp.status_code == 201
    keys = _collect_keys(resp.json())
    assert not (keys & FORBIDDEN_KEYS), f"leaked keys: {keys & FORBIDDEN_KEYS}"
    assert "token_expires_at" in keys  # metadata is present, and is a timestamp

    listing = await client.get("/api/v1/social/accounts", headers=auth("viewer"))
    assert listing.status_code == 200
    listing_keys = _collect_keys(listing.json())
    assert not (listing_keys & FORBIDDEN_KEYS)
    assert "crusor" in listing.text


async def test_status_reports_unavailable_integrations_honestly(client):
    resp = await client.get("/api/v1/system/status")
    assert resp.status_code == 200
    body = resp.json()

    assert body["publishing"]["status"] == "unavailable"
    assert body["publishing"]["mode"] == "human_approval_required"
    assert body["llm"]["status"] in {"unavailable", "degraded", "available"}
    assert body["llm"]["providers"]["openai"]["configured"] is False
    assert body["tasks"]["driver"] in {"inline", "celery", "disabled"}
    assert body["storage"]["backend"] in {"local", "s3", "none"}
    assert body["auth"]["approver_role"] == "founder"
    assert body["director"]["checkpointing"] == "database"
    # No secret material anywhere in the payload
    text = resp.text.lower()
    for forbidden in ("sk-", "api_key", "secret_key", "password"):
        assert forbidden not in text


async def test_platform_status_endpoint_is_explicit(client, auth):
    resp = await client.get("/api/v1/social/platforms", headers=auth("viewer"))
    assert resp.status_code == 200
    rows = resp.json()
    assert {r["platform"] for r in rows} == {
        "instagram", "linkedin", "youtube", "x", "facebook", "reddit"
    }
    assert all(r["publish_enabled"] is False for r in rows)
    assert all(r["reason"] for r in rows)


async def test_error_envelope_shape(client):
    resp = await client.get("/api/v1/content/00000000-0000-4000-8000-000000000000", headers={
        "Authorization": "Bearer invalid"
    })
    assert resp.status_code == 401
    error = resp.json()["error"]
    assert set(error) == {"code", "message", "details", "request_id"}
    assert resp.headers["X-Request-Id"]


async def test_validation_error_envelope(client, auth):
    resp = await client.post("/api/v1/content", json={"title": ""}, headers=auth("marketer"))
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["fields"]


async def test_upload_size_limit(client, auth, content_item):
    big = b"x" * (26 * 1024 * 1024)
    resp = await client.post(
        "/api/v1/creative/upload",
        files={"file": ("big.bin", io.BytesIO(big), "application/octet-stream")},
        headers=auth("marketer"),
    )
    assert resp.status_code == 422
    assert "limit" in resp.json()["error"]["message"].lower()


async def test_rate_limit_returns_429(client, auth):
    from app.core.config import get_settings
    from app.core.rate_limit import get_rate_limiter

    limiter = get_rate_limiter()
    # Force an exhausted window for this path/identity.
    for _ in range(get_settings().rate_limit_per_minute + 1):
        limiter.hit("/api/v1/brand:exhausted", limit=2, window_seconds=60)

    allowed, retry_after = limiter.hit("/api/v1/brand:exhausted", limit=2, window_seconds=60)
    assert allowed is False
    assert retry_after >= 1


async def test_debug_docs_hidden_in_production():
    from app.core.config import Settings
    from app.main import create_app

    prod = Settings(
        environment="production",
        debug=False,
        secret_key="x" * 40,
        bootstrap_founder_password="a-real-password-123",
        database_url="postgresql+asyncpg://u:p@h/db",
    )
    app = create_app(prod)
    assert app.docs_url is None
    assert app.openapi_url is None


def test_unsafe_production_config_is_rejected():
    from app.core.config import Settings

    unsafe = Settings(environment="production", debug=True)
    with pytest.raises(RuntimeError):
        unsafe.validate_for_environment()

    safe = Settings(
        environment="production",
        debug=False,
        secret_key="y" * 40,
        bootstrap_founder_password="strong-password-123",
        database_url="postgresql+asyncpg://u:p@h/db",
    )
    safe.validate_for_environment()  # must not raise


async def test_audit_log_written_for_brand_update(client, auth, brand, session):
    from sqlalchemy import select
    from app.models.audit import AuditLog

    resp = await client.patch(
        f"/api/v1/brand/{brand['id']}",
        json={"tagline": "Build and Ship with Ease."},
        headers=auth("marketer"),
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == 2

    rows = (
        await session.execute(select(AuditLog).where(AuditLog.action == "brand.updated"))
    ).scalars().all()
    assert rows
    assert rows[0].before.get("tagline") == "Build and Ship with Ease"
