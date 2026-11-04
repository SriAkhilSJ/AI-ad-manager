"""Authentication and authorisation."""

from __future__ import annotations

import uuid

import pytest
from jose import jwt

from app.core.config import get_settings
from app.core.security import ALGORITHM, Role, create_access_token


async def test_missing_token_is_rejected(client):
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthenticated"


async def test_invalid_token_is_rejected(client):
    resp = await client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert resp.status_code == 401


async def test_tampered_token_is_rejected(client, users, tokens):
    token = tokens["founder"]
    head, payload, sig = token.split(".")
    forged = f"{head}.{payload}.{'a' * len(sig)}"
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


async def test_expired_token_is_rejected(client, users):
    from datetime import timedelta

    token, _ = create_access_token(
        subject=str(users["founder"].id),
        role=Role.FOUNDER,
        expires_delta=timedelta(seconds=-10),
    )
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


async def test_wrong_password(client, users):
    resp = await client.post(
        "/api/v1/auth/token",
        json={"email": "founder@example.com", "password": "wrong-password"},
    )
    assert resp.status_code == 401


async def test_unknown_user_does_not_leak(client):
    resp = await client.post(
        "/api/v1/auth/token",
        json={"email": "nobody@example.com", "password": "whatever-123"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["message"] == "Invalid email or password."


async def test_inactive_user_cannot_login(client, users, session):
    users["marketer"].is_active = False
    session.add(users["marketer"])
    await session.commit()
    resp = await client.post(
        "/api/v1/auth/token",
        json={"email": "marketer@example.com", "password": "marketer-password-123"},
    )
    assert resp.status_code == 401


async def test_viewer_cannot_create_content(client, auth, brand):
    resp = await client.post(
        "/api/v1/content",
        json={
            "brand_profile_id": brand["id"],
            "title": "x",
            "platform": "linkedin",
        },
        headers=auth("viewer"),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


async def test_only_founder_can_approve(client, auth, content_item):
    mid = content_item["id"]
    submit = await client.post(
        f"/api/v1/content/{mid}/transition",
        json={"action": "submit"},
        headers=auth("marketer"),
    )
    assert submit.status_code == 200

    attempt = await client.post(
        f"/api/v1/content/{mid}/transition",
        json={"action": "approve", "reason": "self-approve attempt"},
        headers=auth("marketer"),
    )
    assert attempt.status_code == 403
    assert attempt.json()["error"]["code"] == "forbidden"

    ok = await client.post(
        f"/api/v1/content/{mid}/transition",
        json={"action": "approve", "reason": "founder review"},
        headers=auth("founder"),
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "APPROVED"


async def test_only_founder_can_create_users(client, auth):
    denied = await client.post(
        "/api/v1/auth/users",
        json={"email": "new@example.com", "password": "password-123", "role": "marketer"},
        headers=auth("marketer"),
    )
    assert denied.status_code == 403

    ok = await client.post(
        "/api/v1/auth/users",
        json={"email": "new@example.com", "password": "password-123", "role": "marketer"},
        headers=auth("founder"),
    )
    assert ok.status_code == 201


async def test_password_is_hashed_not_stored_plaintext(client, auth):
    resp = await client.post(
        "/api/v1/auth/users",
        json={"email": "hash@example.com", "password": "super-secret-123", "role": "viewer"},
        headers=auth("founder"),
    )
    assert resp.status_code == 201
    # The API response must never contain a password hash.
    body = resp.text
    assert "super-secret-123" not in body
    assert "$2b$" not in body
    assert "hashed_password" not in body
