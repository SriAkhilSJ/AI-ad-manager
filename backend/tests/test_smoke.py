from __future__ import annotations


async def test_health(client):
    resp = await client.get("/api/v1/system/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_login_and_me(client, users, auth):
    resp = await client.get("/api/v1/auth/me", headers=auth("founder"))
    assert resp.status_code == 200
    assert resp.json()["role"] == "founder"


async def test_brand_create_and_get(client, auth, brand):
    got = await client.get(f"/api/v1/brand/{brand['id']}", headers=auth("viewer"))
    assert got.status_code == 200
    assert got.json()["tagline"] == "Build and Ship with Ease"
    active = await client.get("/api/v1/brand/active", headers=auth("viewer"))
    assert active.status_code == 200
    assert active.json()["id"] == brand["id"]
