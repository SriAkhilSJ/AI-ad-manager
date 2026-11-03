"""Test fixtures.

Every test runs against a real, isolated SQLite database with foreign keys ON, so
constraints are genuinely exercised. Environment is forced to ``test`` before the
settings module is imported.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import AsyncIterator

import pytest

# ---- environment must be set before app modules import settings --------------
_TMP = Path(__file__).parent / ".tmp"
_TMP.mkdir(exist_ok=True)
DB_FILE = _TMP / f"test-{uuid.uuid4().hex}.db"

os.environ.update(
    {
        "ENVIRONMENT": "test",
        "DEBUG": "false",
        "DATABASE_URL": f"sqlite+aiosqlite:///{DB_FILE}",
        "SECRET_KEY": "test-secret-key-0123456789abcdef0123456789abcdef",
        "BOOTSTRAP_FOUNDER_EMAIL": "founder@example.com",
        "BOOTSTRAP_FOUNDER_PASSWORD": "founder-test-password",
        "RATE_LIMIT_PER_MINUTE": "10000",
        "TASK_DISPATCHER": "inline",
        "STORAGE_BACKEND": "local",
        "LLM_DEFAULT_PROVIDER": "",
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "OPENROUTER_API_KEY": "",
    }
)

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.security import Role, hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import _install_sqlite_pragmas  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.user import User  # noqa: E402
import app.models  # noqa: F401,E402


@pytest.fixture(scope="session")
def event_loop():  # type: ignore[no-untyped-def]
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def engine():  # type: ignore[no-untyped-def]
    eng = create_async_engine(os.environ["DATABASE_URL"], future=True)
    _install_sqlite_pragmas(eng)
    return eng


@pytest.fixture(scope="session")
def session_factory(engine):  # type: ignore[no-untyped-def]
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@pytest.fixture(scope="session", autouse=True)
def _create_schema(engine):  # type: ignore[no-untyped-def]
    async def _create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.get_event_loop().run_until_complete(_create())


@pytest.fixture(autouse=True)
async def _clean_tables(engine):  # type: ignore[no-untyped-def]
    """Remove all rows between tests, preserving schema and FK integrity.

    ``PRAGMA foreign_keys`` cannot be toggled inside a transaction, so deletion
    runs on an AUTOCOMMIT connection with constraints disabled.
    """
    yield
    from sqlalchemy import text

    autocommit = engine.execution_options(isolation_level="AUTOCOMMIT")
    async with autocommit.connect() as conn:
        await conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(text(f'DELETE FROM "{table.name}"'))
        await conn.execute(text("PRAGMA foreign_keys=ON"))


@pytest.fixture
async def session(session_factory) -> AsyncIterator[AsyncSession]:  # type: ignore[no-untyped-def]
    async with session_factory() as s:
        yield s


@pytest.fixture
async def users(session: AsyncSession) -> dict[str, User]:
    """One user per role."""
    created: dict[str, User] = {}
    for role in Role:
        user = User(
            email=f"{role.value}@example.com",
            full_name=role.value.title(),
            hashed_password=hash_password(f"{role.value}-password-123"),
            role=role.value,
        )
        session.add(user)
        created[role.value] = user
    await session.commit()
    for user in created.values():
        await session.refresh(user)
    return created


@pytest.fixture
async def app():  # type: ignore[no-untyped-def]
    application = create_app()
    return application


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:  # type: ignore[no-untyped-def]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _token(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post(
        "/api/v1/auth/token", json={"email": email, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest.fixture
async def tokens(client: AsyncClient, users: dict[str, User]) -> dict[str, str]:
    out: dict[str, str] = {}
    for role in Role:
        out[role.value] = await _token(
            client, f"{role.value}@example.com", f"{role.value}-password-123"
        )
    return out


@pytest.fixture
def auth(tokens):  # type: ignore[no-untyped-def]
    def _headers(role: str = "founder") -> dict[str, str]:
        return {"Authorization": f"Bearer {tokens[role]}"}

    return _headers


@pytest.fixture
async def brand(client: AsyncClient, auth):  # type: ignore[no-untyped-def]
    payload = {
        "name": "Crusor AI",
        "company": "Crusor AI",
        "product": "AI-powered IDE",
        "tagline": "Build and Ship with Ease",
        "positioning": "AI-powered development environment.",
        "audience": ["software developers", "indie hackers"],
        "personality": ["technical", "premium", "minimal"],
        "visual_direction": {"accent": "#8B5CF6", "backgrounds": "dark"},
        "keywords": ["AIdev", "DevTools"],
        "prohibited_phrases": ["10x", "revolutionary"],
    }
    resp = await client.post("/api/v1/brand", json=payload, headers=auth("marketer"))
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
async def brand_row(session, users) -> "object":
    """Brand profile created directly through the ORM (for model/service tests)."""
    from app.models.brand import BrandProfile

    row = BrandProfile(
        name="Crusor AI",
        company="Crusor AI",
        product="AI-powered IDE",
        tagline="Build and Ship with Ease",
        positioning="AI-powered development environment.",
        audience=["software developers", "indie hackers"],
        personality=["technical", "premium"],
        visual_direction={"accent": "#8B5CF6"},
        keywords=["AIdev"],
        prohibited_phrases=["10x", "revolutionary"],
        do_not_claim=["customer or user counts"],
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@pytest.fixture
async def content_item(client: AsyncClient, auth, brand):
    resp = await client.post(
        "/api/v1/content",
        json={
            "brand_profile_id": brand["id"],
            "title": "Crusor launch post",
            "platform": "linkedin",
            "body": "Build and ship with ease.\n\nA practical note on shipping velocity.",
            "headline": "Build and Ship with Ease",
            "hashtags": ["#AIdev"],
            "call_to_action": "Follow for build notes.",
        },
        headers=auth("marketer"),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()
