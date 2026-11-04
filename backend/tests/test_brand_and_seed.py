"""Brand knowledge base, goal/pillar APIs and the Crusor seed data."""

from __future__ import annotations

import pytest

from app.seed.brand_crusor import CRUSOR_BRAND, CRUSOR_PILLARS, seed


async def test_create_and_retrieve_brand_profile(client, auth, brand):
    """Acceptance criterion 4: brand profile can be created and retrieved."""
    got = await client.get(f"/api/v1/brand/{brand['id']}", headers=auth("viewer"))
    assert got.status_code == 200
    body = got.json()
    assert body["company"] == "Crusor AI"
    assert body["tagline"] == "Build and Ship with Ease"
    assert "software developers" in body["audience"]
    assert body["version"] == 1


async def test_brand_update_bumps_version_and_audits(client, auth, brand):
    resp = await client.patch(
        f"/api/v1/brand/{brand['id']}",
        json={"audience": ["software developers", "startup founders"]},
        headers=auth("marketer"),
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == 2
    assert "startup founders" in resp.json()["audience"]


async def test_viewer_cannot_update_brand(client, auth, brand):
    resp = await client.patch(
        f"/api/v1/brand/{brand['id']}", json={"tagline": "x"}, headers=auth("viewer")
    )
    assert resp.status_code == 403


async def test_goals_and_pillars(client, auth, brand):
    goal = await client.post(
        f"/api/v1/brand/{brand['id']}/goals",
        json={"objective": "Increase developer signups", "kpi": "signups", "priority": 1},
        headers=auth("marketer"),
    )
    assert goal.status_code == 201

    goals = await client.get(f"/api/v1/brand/{brand['id']}/goals", headers=auth("viewer"))
    assert len(goals.json()) == 1

    pillar = await client.post(
        f"/api/v1/brand/{brand['id']}/pillars",
        json={"name": "Build & Ship", "platforms": ["linkedin"], "weight": 1.0},
        headers=auth("marketer"),
    )
    assert pillar.status_code == 201

    bad_pillar = await client.post(
        f"/api/v1/brand/{brand['id']}/pillars",
        json={"name": "Nope", "platforms": ["myspace"]},
        headers=auth("marketer"),
    )
    assert bad_pillar.status_code == 422


async def test_competitor_requires_provenance(client, auth, brand):
    resp = await client.post(
        f"/api/v1/brand/{brand['id']}/competitors",
        json={"name": "Competitor X", "positioning": "general purpose editor", "source": "manual"},
        headers=auth("marketer"),
    )
    assert resp.status_code == 201
    assert resp.json()["source"] == "manual"


async def test_active_brand_404_when_none(client, auth):
    resp = await client.get("/api/v1/brand/active", headers=auth("viewer"))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_seed_creates_crusor_profile(session):
    brand = await seed()
    await session.commit()
    assert brand.company == "Crusor AI"
    assert brand.tagline == "Build and Ship with Ease"
    assert brand.product == "AI-powered IDE"
    assert "software developers" in brand.audience
    assert "indie hackers" in brand.audience
    assert "premium" in brand.personality
    assert brand.visual_direction["accent"] == "subtle purple"
    assert brand.do_not_claim


async def test_seed_is_idempotent(session):
    first = await seed()
    await session.commit()
    second = await seed()
    await session.commit()
    assert first.id == second.id


async def test_seed_data_contains_no_invented_claims():
    """Guard rail for the brand rules in the brief.

    Only *claim-bearing* fields are scanned. ``prohibited_phrases`` and
    ``do_not_claim`` necessarily name the phrasing they forbid, so including them
    would be a false positive.
    """
    import json
    import re

    claim_fields = {
        key: value
        for key, value in CRUSOR_BRAND.items()
        if key not in {"prohibited_phrases", "do_not_claim", "keywords"}
    }
    blob = json.dumps(claim_fields) + json.dumps(CRUSOR_PILLARS)
    forbidden = [
        r"\b\d{2,}\s*%",              # percentages
        r"\b\d+[km]\s+developers\b",  # user counts
        r"\$\s?\d+",                  # pricing
        r"\btrusted by\b",
        r"\b\d+x faster\b",
    ]
    for pattern in forbidden:
        assert not re.search(pattern, blob, re.IGNORECASE), f"invented claim: {pattern}"

    # And the do-not-claim guard list is populated.
    assert len(CRUSOR_BRAND["do_not_claim"]) >= 5
    assert "pricing or plan tiers" in CRUSOR_BRAND["do_not_claim"]


async def test_seed_visual_direction_matches_brief():
    visual = CRUSOR_BRAND["visual_direction"]
    assert visual["backgrounds"] == "dark"
    assert "purple" in visual["accent"]
    assert "silver" in visual["metallic"]
    assert "minimal" in visual["aesthetic"]


async def test_brand_guardian_uses_seeded_rules(session):
    from app.agents.base import AgentContext
    from app.agents.registry import get_registry

    brand = await seed()
    await session.commit()
    ctx = AgentContext(session=session, brand=brand)
    result = await get_registry().get("brand_guardian").run(
        ctx,
        {
            "platform": "linkedin",
            "body": "Our IDE is 10x faster and revolutionary. Trusted by 5000 developers.",
            "call_to_action": "Try it",
        },
    )
    assert result.verdict == "blocked"
    assert len(result.violations) >= 2
