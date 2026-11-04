"""Regressions found while running the live system, not by unit tests.

Each case here was a real defect observed in a server log during acceptance:

1. Redaction masked token *counts* (``tokens=***REDACTED***``) which silently
   destroyed cost telemetry in logs.
2. A second director workflow with the same objective derived the same campaign
   name and then failed with "validation" while the workflow carried on without
   a campaign — a silent partial failure.
3. ``PUT /creative/upload`` originally discarded the bytes; storage is now real.
"""

from __future__ import annotations

import pytest


# --------------------------------------------------------------------------- #
# 1. redaction precision
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "key",
    [
        "tokens",
        "tokens_used",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "max_tokens",
        "token_count",
        "token_budget",
    ],
)
def test_token_counts_survive_redaction(key):
    from app.core.logging import _redact_value

    assert _redact_value(key, 1234) == 1234


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "access_token",
        "refresh_token",
        "idempotency_token",
        "password",
        "hashed_password",
        "api_key",
        "openai_api_key",
        "authorization",
        "secret",
        "client_secret",
        "session_id",
        "private_key",
        "credentials",
    ],
)
def test_credential_fields_are_redacted(key):
    from app.core.logging import REDACTED, _redact_value

    assert _redact_value(key, "supersecretvalue") == REDACTED


def test_bearer_tokens_are_scrubbed_from_free_text():
    from app.core.logging import _redact_value

    out = _redact_value("message", "calling with Authorization: Bearer abcdefghijklmnop")
    assert "abcdefghijklmnop" not in out


def test_redaction_is_recursive():
    from app.core.logging import REDACTED, _redact_value

    payload = {"outer": {"api_key": "k-123", "tokens": 42}, "list": [{"secret": "s"}]}
    out = _redact_value("payload", payload)
    assert out["outer"]["api_key"] == REDACTED
    assert out["outer"]["tokens"] == 42  # metric preserved
    assert out["list"][0]["secret"] == REDACTED


# --------------------------------------------------------------------------- #
# 2. campaign reuse
# --------------------------------------------------------------------------- #
async def test_campaign_service_finds_existing_by_name(session, brand_row):
    from app.services.campaign import CampaignService

    service = CampaignService(session)
    created = await service.create(
        brand_profile_id=brand_row.id, name="Awareness Push", objective="be seen"
    )
    await session.flush()

    found = await service.find_by_name(brand_profile_id=brand_row.id, name="Awareness Push")
    assert found is not None
    assert found.id == created.id

    assert (
        await service.find_by_name(brand_profile_id=brand_row.id, name="Something Else")
        is None
    )


async def test_duplicate_campaign_name_still_rejected_at_service_layer(session, brand_row):
    """The service keeps its uniqueness contract; only the director reuses."""
    from app.core.errors import ValidationProblem
    from app.services.campaign import CampaignService

    service = CampaignService(session)
    await service.create(brand_profile_id=brand_row.id, name="Same Name")
    await session.flush()
    with pytest.raises(ValidationProblem):
        await service.create(brand_profile_id=brand_row.id, name="Same Name")


async def test_director_reuses_campaign_across_workflows(client, auth, brand_row):
    """Two workflows with the same objective must share one campaign, not fail."""
    headers = auth("founder")
    payload = {
        "objective": "Explain the AI-powered IDE to developers",
        "platforms": ["linkedin"],
    }
    first = await client.post("/api/v1/director/workflows", json=payload, headers=headers)
    assert first.status_code in (200, 201), first.text
    second = await client.post("/api/v1/director/workflows", json=payload, headers=headers)
    assert second.status_code in (200, 201), second.text

    states = []
    for response in (first, second):
        workflow_id = response.json()["workflow_id"]
        status = await client.get(
            f"/api/v1/director/workflows/{workflow_id}", headers=headers
        )
        states.append(status.json())

    campaign_ids = {state["created_campaign_id"] for state in states}
    assert len(campaign_ids) == 1, f"workflows forked separate campaigns: {campaign_ids}"
    assert campaign_ids != {None}

    # The second workflow must not report a campaign failure.
    for state in states:
        assert not [
            error for error in state["errors"] if error.get("code") == "campaign_create_failed"
        ], state["errors"]


# --------------------------------------------------------------------------- #
# 3. creative uploads are persisted through the storage seam
# --------------------------------------------------------------------------- #
async def test_upload_writes_bytes_through_storage(client, auth):
    headers = auth("founder")
    files = {"file": ("hero.png", b"\x89PNG\r\n\x1a\nreal-bytes-here", "image/png")}
    response = await client.post(
        "/api/v1/creative/upload",
        files=files,
        data={"kind": "image", "prompt": "dark metallic hero"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    asset = response.json()
    assert asset["size_bytes"] == len(b"\x89PNG\r\n\x1a\nreal-bytes-here")
    assert len(asset["checksum"]) == 64
    assert asset["storage_key"].endswith("hero.png")
    assert asset["status"] == "ready"

    from app.storage.base import get_storage

    stored = await get_storage().get(asset["storage_key"])
    assert stored == b"\x89PNG\r\n\x1a\nreal-bytes-here"


async def test_upload_rejects_empty_file(client, auth):
    headers = auth("founder")
    response = await client.post(
        "/api/v1/creative/upload",
        files={"file": ("empty.png", b"", "image/png")},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
