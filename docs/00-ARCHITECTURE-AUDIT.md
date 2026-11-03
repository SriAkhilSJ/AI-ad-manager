# A. Existing Architecture Audit

**Repository:** `SriAkhilSJ/AI-ad-manager`
**Audited commit:** `449018d` (`main`) — branch `arena/c7e4be51-ai-ad-manager`
**Audit date:** 2026-11-02
**Auditor role:** Principal AI Systems Architect

---

## A.1 Method

1. `git log --all`, `git status`, `git branch -a` to establish history and branch state.
2. Full recursive file inventory of the working tree (`find`, excluding `.git`).
3. Manual read of every non-binary file found.
4. Toolchain probe of the sandbox (Python, Node, Postgres, Redis, Docker availability).

## A.2 Findings

| Area | Finding |
| --- | --- |
| Tracked files | **1** — `README.md` (16 bytes, content: `# AI-ad-manager`) |
| Commit history | **1** commit — `449018d Initial commit` |
| Existing application code | **None** |
| Existing config | **None** (no `package.json`, `pyproject.toml`, `requirements.txt`, `docker-compose.yml`, CI config, `.gitignore`) |
| Existing tests | **None** |
| Existing database schema / migrations | **None** |
| Existing integrations (social, LLM, storage, queue) | **None** |
| Existing UI components / design system | **None** |

```
$ find . -path ./.git -prune -o -type f -print
./README.md
```

### Verdict

This is a **greenfield repository**. There is no existing stack, no working
feature, no reusable component, and no integration to preserve. Section 12 of
the brief ("avoid unnecessary rewrites", "keep existing functionality intact",
"do not delete existing integrations") is therefore satisfied trivially: the
only pre-existing artifact is the `README.md`, which this phase **keeps and
appends to** rather than replaces.

## A.3 Environment probe (sandbox)

| Component | Available | Impact on design |
| --- | --- | --- |
| Python | 3.11.2 | Backend target: `>=3.11` |
| Node.js / npm | 22.22.3 / 10.9.8 | Frontend via Vite 5 + React 18 + TS 5 |
| PostgreSQL | **not installed** | Schema must be written for Postgres, but migrations/tests must also run on **SQLite (aiosqlite)** so CI/sandbox can verify migrations execute |
| Redis | **not installed** | Queue layer must degrade gracefully to an in-process dispatcher and report `unavailable` rather than crash |
| Docker | not installed | Provide `docker-compose.yml` as deployment artifact only; not required for Phase 1 acceptance |
| S3 / MinIO | **not configured** | Storage must be an explicit interface + `unavailable` status, with a local filesystem driver for dev |
| Outbound pip/network | available (PyPI + npm registry) | Dependencies installable |

## A.4 Audit consequences (design decisions forced by the audit)

1. **Greenfield → no integration risk, but a real risk of over-scoping.** Phase 1
   is deliberately bounded: foundation + approval integrity + workflow
   persistence. No publishing, no real OAuth, no real model calls required.
2. **Postgres is the production target but is absent in the sandbox.** Therefore:
   - All models use portable SQLAlchemy 2.0 typed constructs.
   - UUID primary keys are stored via a `GUID` `TypeDecorator` (`native_uuid`
     on Postgres, `CHAR(32)` on SQLite) so the *same* models serve both.
   - JSON payloads use `JSONB` on Postgres and `JSON` on SQLite.
   - A `pgvector` column is added **only** on Postgres (`Vector`), guarded by
     dialect detection; on SQLite the embedding is stored as JSON plus a
     cosine-similarity fallback search.
   - Alembic migrations are dialect-aware (`CREATE EXTENSION IF NOT EXISTS vector`
     guarded by `op.get_bind().dialect.name`).
3. **No Redis.** Celery is configured as the production task system, but the
   application uses a `TaskDispatcher` abstraction with three drivers:
   `celery` (Redis broker), `inline` (in-process, for tests/dev), and
   `unavailable` (explicit status surfaced through `/api/v1/system/status`).
   This satisfies "if an external integration is not configured, provide an
   explicit interface and a clear unavailable status".
4. **No S3.** Same pattern: `StorageBackend` protocol + `LocalFilesystemStorage`
   (dev) + `S3Storage` (configured but unverified unless credentials present) and
   an `unavailable` status when neither is usable.
5. **No LLM keys.** The provider-independent gateway must not fabricate model
   output. Unconfigured providers return a **structured** `ProviderUnavailable`
   error, and every agent has a deterministic **offline** implementation that
   performs real, useful work (planning, validation, templating) without model
   calls. Model usage is only recorded when a provider actually returns tokens —
   never invented.

## A.5 Reuse assessment

Nothing to reuse. However, the following **public-specification conventions** are
adopted rather than invented, to keep the codebase idiomatic and reviewable:

- FastAPI + Pydantic v2 patterns (dependency injection, `APIRouter`, exception handlers).
- SQLAlchemy 2.0 declarative `Mapped`/`mapped_column` style.
- Alembic autogenerate-compatible migration layout.
- LangGraph `StateGraph` + `interrupt`/`Command` for human-in-the-loop and
  `BaseCheckpointSaver` for durable state.
- Tailwind + Vite + React Router conventions.

## A.6 Risks carried into the plan

See `docs/03-RISKS-AND-DEPENDENCIES.md` for the full register. Headline risks:

- **R1** Postgres/pgvector unverifiable in sandbox → mitigated by dual-dialect
  migrations + CI matrix note.
- **R2** Approval bypass is the highest-severity product risk → mitigated by a
  single authoritative state-transition service, DB-level constraints, and a
  regression test suite that attempts bypasses.
- **R3** LangGraph API drift (0.2.x) → pinned versions, custom checkpointer
  written against the documented `BaseCheckpointSaver` contract and covered by
  a persistence-across-restart test.
- **R4** Cost/token accounting can silently double count on retries → usage rows
  keyed by `agent_run_id` + `attempt` with a unique constraint.
- **R5** Over-scoped Phase 1 → explicit non-goals enumerated in the plan.
