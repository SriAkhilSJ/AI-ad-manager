# Deployment guide

How to run Crusor AI Marketing Director in development, staging and production,
including the migration/rollback procedure and what "healthy" looks like.

> **Honesty note.** The Docker images, compose stack and CI workflow in this
> repository were written during Phase 4 but **have not been executed** in the
> environment they were authored in (no Docker daemon available there). Treat
> them as reviewed-but-unproven until a `docker compose up --build` succeeds on
> your machine. Everything else in this guide — migrations, test suite,
> acceptance run, source audit — has been executed and its output is recorded in
> `docs/22-PHASE4-PRODUCTION-INTEGRATIONS.md`.

---

## 1. Prerequisites

| Component | Minimum | Notes |
| --- | --- | --- |
| Python | 3.11 | 3.12 untested |
| PostgreSQL | 15 with `pgvector` | `pgvector/pgvector:pg16` is used in compose |
| Redis | 7 | optional; without it the inline dispatcher and in-memory rate limiter are used |
| Node | 20 | frontend build |
| S3-compatible storage | any | optional; filesystem storage is the fallback |

SQLite is supported for development and the test suite only. `APP_ENV=production`
refuses to start on SQLite.

---

## 2. Development (no external services)

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt

export DATABASE_URL="sqlite+aiosqlite:///./.data/crusor.db"
export SECRET_KEY="dev-only-secret-key-at-least-32-characters"
export BOOTSTRAP_FOUNDER_EMAIL="founder@crusor.ai"
export BOOTSTRAP_FOUNDER_PASSWORD="change-me-dev-password"

cd backend
../.venv/bin/alembic upgrade head
../.venv/bin/python -m app.seed.brand_crusor
../.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

In a second terminal:

```bash
cd frontend && npm install && npm run dev   # http://localhost:5173
```

---

## 3. Docker compose (production-shaped)

```bash
cp .env.example .env
# Fill in: SECRET_KEY, POSTGRES_PASSWORD, BOOTSTRAP_FOUNDER_*, and any
# integration credentials you actually hold. Leave the rest empty — an empty
# value means "unavailable", which the UI reports honestly.

docker compose up --build
docker compose exec backend alembic current
```

Services:

| Service | Port | Purpose |
| --- | --- | --- |
| `db` | 5432 | PostgreSQL 16 + pgvector |
| `redis` | 6379 | broker + rate limiter |
| `backend` | 8000 | FastAPI; runs `alembic upgrade head` then uvicorn |
| `worker` | — | Celery worker |
| `frontend` | 8080 | nginx serving the SPA and proxying `/api` |

The frontend proxies `/api` to the backend, so the browser talks to one origin and
no CORS relaxation is required in production.

---

## 4. Migrations

```bash
cd backend
alembic upgrade head        # apply
alembic current             # show the head that is applied
alembic downgrade -1        # roll back one revision
alembic upgrade head        # forward again
```

Current head: `e6f7a8b9c0d1` (Phase 4: `oauth_tokens`, `oauth_flows`,
`ad_campaigns`, `ad_budget_authorizations`, `ad_operations`).

**Procedure for a production deploy:**

1. Take a backup (§7).
2. Run `alembic upgrade head` as a **single** short-lived job, not from every
   replica. In compose the backend container does it on start; with multiple
   replicas, run it separately and start the replicas afterwards.
3. Verify `alembic current` prints the expected revision.
4. Start the application.

Every Phase 3C/4 migration is additive. The Phase 4 downgrade drops five tables
and loses their rows — acceptable because they hold credentials (re-connectable)
and ad proposals (re-creatable), never published content.

---

## 5. Rollback

| Situation | Action |
| --- | --- |
| Bad application deploy, schema compatible | redeploy the previous image; no migration needed |
| Bad migration, data must survive | `alembic downgrade -1`, redeploy previous image |
| Corrupt data | restore the backup, then `alembic upgrade head` |
| Publishing misbehaving | remove a platform's app credentials or `TOKEN_VAULT_KEY`; publishing becomes unavailable within one request and every publish is refused before the network |
| Ads misbehaving | delete the active `ad_budget_authorizations` rows; every launch then refuses for lack of authorisation |

The last two are the designed kill switches: they do not require a deploy.

---

## 6. Health and monitoring

| Endpoint | Auth | Meaning |
| --- | --- | --- |
| `GET /api/v1/system/health` | none | liveness only; no DB, no integrations. This is the container healthcheck. |
| `GET /api/v1/system/status` | none | full integration report: database, LLM, tasks, storage, research, creative, Flow, **publishing** (per-platform vault/credential state) and **ads** (per-platform transport state) |
| `GET /api/v1/social/platforms` | user | per-platform readiness + capability matrix |
| `GET /api/v1/ads/transports` | user | ads transport availability |

Healthcheck for compose/orchestrators:

```bash
curl -fsS http://127.0.0.1:8000/api/v1/system/health
```

A `200` from `/health` means the process is alive. **It does not mean the system
can publish anything** — read `/system/status` for that, and expect `publishing`
to say `unavailable` until a vault key and platform credentials exist.

Structured logs are JSON via structlog, with a processor that redacts
secret-bearing keys and bearer tokens. Set the log level with the usual uvicorn
`--log-level` flag.

---

## 7. Backup and recovery

| Data | Frequency | Method |
| --- | --- | --- |
| PostgreSQL | daily + before each deploy | `pg_dump -Fc crusor > crusor-$(date +%F).dump` |
| `oauth_tokens` | included in the above | ciphertext only; useless without `TOKEN_VAULT_KEY` |
| `TOKEN_VAULT_KEY` | **separate secret store** | never in the database backup; losing it means every account must be reconnected |
| Object storage | bucket versioning | S3 versioning + lifecycle rules |
| `.env` / secrets | secret manager | never in git |

Restore:

```bash
pg_restore -d crusor --clean --if-exists crusor-2026-11-04.dump
cd backend && alembic upgrade head
```

Restoring a database without its `TOKEN_VAULT_KEY` leaves every credential
undecryptable. That is intentional: the key belongs beside the database, not
inside it.

---

## 8. CI

`.github/workflows/ci.yml` runs three jobs on every push and pull request:

1. **backend** — install, `alembic upgrade/downgrade/upgrade`, full pytest suite,
   and the source audit asserted to exit **3** with `SOURCE_ABSENT`. A different
   exit code fails the build, because the gate changing silently is the risk.
2. **frontend** — `tsc --noEmit` and a production build.
3. **no-secrets** — fails if a `.env` file is committed or an API-key-shaped
   string appears anywhere outside the docs.

CI runs with **no provider credentials on purpose**. The suite asserts that every
integration reports `unavailable`; a CI run that had keys would prove less, not
more.

---

## 9. Enabling a platform for real

Only after the platform's app approval exists:

1. Put the platform's own credentials in the environment (`META_APP_ID`/`SECRET`,
   `LINKEDIN_CLIENT_ID`/`SECRET`, `X_CLIENT_ID`/`SECRET`, `GOOGLE_CLIENT_ID`/`SECRET`,
   `REDDIT_CLIENT_ID`/`SECRET`).
2. Set `TOKEN_VAULT_BACKEND=db` and `TOKEN_VAULT_KEY=<32 bytes>`.
3. Set `OAUTH_REDIRECT_BASE_URL` to the public URL and register
   `<base>/api/v1/social/oauth/callback` as the redirect URI in the provider's app.
4. As a founder, call `POST /api/v1/social/oauth/{platform}/start`, open the
   returned `authorize_url`, complete consent.
5. Confirm `GET /api/v1/social/platforms` now reports `publish_enabled: true` for
   that platform and `GET /api/v1/system/status` shows `publishing.status:
   available`.
6. Publish through the normal approval flow. The first successful publish proves
   the transport; until then the capability matrix correctly says `unverified`.

For ads: nothing to enable until a transport is implemented. The governance layer
will refuse every launch with `ads transport unavailable` and record it.
