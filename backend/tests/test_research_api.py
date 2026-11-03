"""Research Centre API: authz, provider honesty, provenance, verification."""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.models.research import ResearchFinding, VerificationStatus


async def make_brand(client, auth):  # type: ignore[no-untyped-def]
    payload = {
        "name": "Crusor AI",
        "company": "Crusor AI",
        "product": "AI-powered IDE",
        "tagline": "Build and Ship with Ease",
        "positioning": "AI-powered development environment.",
        "audience": ["software developers"],
        "prohibited_phrases": ["10x"],
    }
    resp = await client.post("/api/v1/brand", json=payload, headers=auth("marketer"))
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# capabilities
# --------------------------------------------------------------------------- #
async def test_capabilities_are_honest_about_the_provider(client, auth):
    resp = await client.get("/api/v1/research/capabilities", headers=auth("viewer"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"]["status"] in ("available", "degraded", "unavailable")
    assert len(body["categories"]) == 9
    # With no HTTP endpoint configured the provider must say it is offline.
    assert body["provider"]["network"] is False
    assert "RESEARCH_HTTP_ENDPOINT" in body["provider"]["note"]


async def test_capabilities_requires_auth(client):
    resp = await client.get("/api/v1/research/capabilities")
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# create + run
# --------------------------------------------------------------------------- #
async def seed_kb_for(brand_id):
    """Load the Crusor knowledge base for a brand created through the API."""
    from app.db.session import get_session_factory
    from app.models.brand import BrandProfile
    from app.seed import brand_crusor

    async with get_session_factory()() as s:
        brand = await s.get(BrandProfile, __import__("uuid").UUID(brand_id))
        counts = await brand_crusor.seed_knowledge_base(brand)
        await s.commit()
    return counts


async def test_create_and_run_project_persists_sourced_findings(client, auth):
    brand = await make_brand(client, auth)
    await seed_kb_for(brand["id"])
    resp = await client.post(
        "/api/v1/research/projects",
        json={
            "brand_profile_id": brand["id"],
            "objective": "Map the AI IDE market",
            "categories": ["competitor_identification", "developer_pain_points"],
        },
        headers=auth("marketer"),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["project"]["status"] in ("succeeded", "partial")
    assert body["project"]["provider"] == "deterministic"
    assert body["findings_created"] > 0
    assert body["provider_status"] in ("degraded", "available")

    project_id = body["project"]["id"]
    detail = await client.get(
        f"/api/v1/research/projects/{project_id}", headers=auth("viewer")
    )
    assert detail.status_code == 200
    data = detail.json()

    assert data["findings"]
    assert data["sources"], "findings must link to persisted sources"
    for finding in data["findings"]:
        if finding["verification_status"] == "verified":
            assert finding["source_url"], "verified findings must cite a source"
        assert finding["retrieved_at"] or finding["source_url"] == ""


async def test_viewer_cannot_start_research(client, auth):
    brand = await make_brand(client, auth)
    resp = await client.post(
        "/api/v1/research/projects",
        json={"brand_profile_id": brand["id"], "objective": "Try to run research"},
        headers=auth("viewer"),
    )
    assert resp.status_code == 403


async def test_unknown_category_is_rejected_with_the_allowed_list(client, auth):
    brand = await make_brand(client, auth)
    resp = await client.post(
        "/api/v1/research/projects",
        json={"brand_profile_id": brand["id"], "objective": "x", "categories": ["astrology"]},
        headers=auth("marketer"),
    )
    assert resp.status_code == 422


async def test_unknown_brand_is_rejected(client, auth):
    import uuid

    resp = await client.post(
        "/api/v1/research/projects",
        json={"brand_profile_id": str(uuid.uuid4()), "objective": "Map the market"},
        headers=auth("marketer"),
    )
    assert resp.status_code == 404


async def test_bare_brand_yields_gaps_not_invention(client, auth):
    """With no stored knowledge, research must report gaps rather than invent data."""
    brand = await make_brand(client, auth)
    resp = await client.post(
        "/api/v1/research/projects",
        json={"brand_profile_id": brand["id"], "objective": "Map the market"},
        headers=auth("marketer"),
    )
    assert resp.status_code == 201
    body = resp.json()
    # With no stored knowledge the provider may still record a handful of
    # *explicitly labelled* gaps, but it must declare gaps and never let an
    # unsourced finding claim to be verified.
    assert body["gaps"], "an empty knowledge base must produce declared gaps"
    assert any("no verifiable findings" in gap for gap in body["gaps"])

    detail = await client.get(
        f"/api/v1/research/projects/{body['project']['id']}", headers=auth("viewer")
    )
    findings = detail.json()["findings"]
    assert findings, "gap records are persisted, not merely counted"
    for finding in findings:
        assert finding["verification_status"] != "verified"
        assert finding["source_url"] == ""
        assert finding["source_title"] == ""


async def test_project_can_be_created_without_running(client, auth):
    brand = await make_brand(client, auth)
    resp = await client.post(
        "/api/v1/research/projects",
        json={
            "brand_profile_id": brand["id"],
            "objective": "Deferred research",
            "run_now": False,
        },
        headers=auth("marketer"),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["project"]["status"] == "queued"
    assert body["findings_created"] == 0
    assert body["stop_reason"] == "not_started"


# --------------------------------------------------------------------------- #
# findings search + filters
# --------------------------------------------------------------------------- #
async def test_findings_filter_by_category_and_confidence(client, auth):
    brand = await make_brand(client, auth)
    await seed_kb_for(brand["id"])
    await client.post(
        "/api/v1/research/projects",
        json={"brand_profile_id": brand["id"], "objective": "Map the market"},
        headers=auth("marketer"),
    )

    resp = await client.get(
        "/api/v1/research/findings",
        params={"brand_profile_id": brand["id"], "category": "developer_pain_points"},
        headers=auth("viewer"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 0
    for item in body["items"]:
        assert item["category"] == "developer_pain_points"

    high = await client.get(
        "/api/v1/research/findings",
        params={"brand_profile_id": brand["id"], "min_confidence": 0.9},
        headers=auth("viewer"),
    )
    for item in high.json()["items"]:
        assert item["confidence"] >= 0.9


async def test_findings_are_paginated(client, auth):
    brand = await make_brand(client, auth)
    await seed_kb_for(brand["id"])
    await client.post(
        "/api/v1/research/projects",
        json={"brand_profile_id": brand["id"], "objective": "Map the market"},
        headers=auth("marketer"),
    )
    resp = await client.get(
        "/api/v1/research/findings",
        params={"brand_profile_id": brand["id"], "limit": 2, "offset": 0},
        headers=auth("viewer"),
    )
    body = resp.json()
    assert len(body["items"]) <= 2
    assert body["limit"] == 2


# --------------------------------------------------------------------------- #
# verification requires a real source and founder role
# --------------------------------------------------------------------------- #
async def test_verify_requires_founder_and_a_url(client, auth, session):
    brand = await make_brand(client, auth)
    await seed_kb_for(brand["id"])
    run = await client.post(
        "/api/v1/research/projects",
        json={"brand_profile_id": brand["id"], "objective": "Map the market"},
        headers=auth("marketer"),
    )
    project_id = run.json()["project"]["id"]

    detail = await client.get(
        f"/api/v1/research/projects/{project_id}", headers=auth("viewer")
    )
    uncited = next(
        (f for f in detail.json()["findings"] if not f["source_url"]), None
    )
    assert uncited is not None, "the offline provider must produce uncited gap findings"

    # marketer cannot verify
    resp = await client.patch(
        f"/api/v1/research/findings/{uncited['id']}/verify",
        params={"source_url": "https://example.com/evidence"},
        headers=auth("marketer"),
    )
    assert resp.status_code == 403

    # a non-URL is refused
    bad = await client.patch(
        f"/api/v1/research/findings/{uncited['id']}/verify",
        params={"source_url": "trust me"},
        headers=auth("founder"),
    )
    assert bad.status_code == 422

    ok = await client.patch(
        f"/api/v1/research/findings/{uncited['id']}/verify",
        params={"source_url": "https://example.com/evidence"},
        headers=auth("founder"),
    )
    assert ok.status_code == 200
    assert ok.json()["verification_status"] == VerificationStatus.VERIFIED.value


# --------------------------------------------------------------------------- #
# competitors
# --------------------------------------------------------------------------- #
async def test_competitor_snapshot_reports_evidence_counts(client, auth):
    brand = await make_brand(client, auth)
    await seed_kb_for(brand["id"])
    await client.post(
        f"/api/v1/brand/{brand['id']}/competitors",
        json={
            "name": "Cursor",
            "website": "https://cursor.com",
            "positioning": "AI-first editor",
        },
        headers=auth("marketer"),
    )
    await client.post(
        "/api/v1/research/projects",
        json={
            "brand_profile_id": brand["id"],
            "objective": "Map competitors",
            "categories": ["competitor_identification"],
        },
        headers=auth("marketer"),
    )

    resp = await client.get(
        "/api/v1/research/competitors",
        params={"brand_profile_id": brand["id"]},
        headers=auth("viewer"),
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert rows and rows[0]["name"] == "Cursor"
    assert rows[0]["findings"] >= 1


# --------------------------------------------------------------------------- #
# provider disabled => honest refusal
# --------------------------------------------------------------------------- #
async def test_disabled_provider_reports_unavailable_project(client, auth, monkeypatch):
    brand = await make_brand(client, auth)
    settings = get_settings()
    monkeypatch.setattr(settings, "research_provider", "disabled", raising=False)

    resp = await client.post(
        "/api/v1/research/projects",
        json={"brand_profile_id": brand["id"], "objective": "Map the market"},
        headers=auth("marketer"),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["project"]["status"] == "unavailable"
    assert body["provider_status"] == "unavailable"
    assert body["findings_created"] == 0
