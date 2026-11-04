"""Migration tests: the Alembic chain must apply and reverse on a clean database.

Runs against its own SQLite file so it cannot interfere with the shared test DB.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def _run(args: list[str], db_url: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": db_url, "PYTHONPATH": str(BACKEND)}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_upgrade_and_downgrade(tmp_path):
    db = tmp_path / "migration.db"
    url = f"sqlite+aiosqlite:///{db}"

    up = _run(["upgrade", "head"], url)
    assert up.returncode == 0, up.stderr[-2000:]

    down = _run(["downgrade", "base"], url)
    assert down.returncode == 0, down.stderr[-2000:]


def test_upgrade_creates_every_model_table(tmp_path):
    db = tmp_path / "migration2.db"
    url = f"sqlite+aiosqlite:///{db}"

    up = _run(["upgrade", "head"], url)
    assert up.returncode == 0, up.stderr[-2000:]

    sys.path.insert(0, str(BACKEND))
    from sqlalchemy import create_engine, inspect

    sync_url = url.replace("+aiosqlite", "")
    engine = create_engine(sync_url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()

    sys.path.insert(0, str(BACKEND))
    from app.db.base import Base
    import app.models  # noqa: F401

    expected = set(Base.metadata.tables)
    missing = expected - tables
    assert not missing, f"migration is missing tables: {missing}"


def test_postgres_ddl_is_generated_offline():
    """Proves the same migration emits PostgreSQL DDL (UUID, JSONB, ALTER FK)."""
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=BACKEND,
        env={
            **os.environ,
            "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/db",
            "PYTHONPATH": str(BACKEND),
        },
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    sql = proc.stdout
    assert "CREATE TABLE content_items" in sql
    assert "CREATE TABLE approval_records" in sql
    assert "UUID NOT NULL" in sql          # native UUID, not CHAR(32)
    assert "JSONB" in sql                  # native JSONB
    # circular FK added after both tables exist
    assert "ADD CONSTRAINT fk_content_items_current_version_id" in sql
