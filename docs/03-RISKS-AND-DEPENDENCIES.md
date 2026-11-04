# D. Identified Risks and Dependencies

## D.1 Risk register

| ID | Risk | Likelihood | Impact | Mitigation (implemented in Phase 1) |
| --- | --- | --- | --- | --- |
| R1 | No PostgreSQL in the build environment; pgvector behaviour unverified | High | Medium | Dual-dialect SQLAlchemy types (`GUID`, `JSONB`→`JSON`, `Vector`→JSON). Alembic migration guarded by dialect. Tests run `upgrade head` and `downgrade base` on SQLite. `Makefile` target emits Postgres DDL for review. Documented residual risk. |
| R2 | **Approval bypass** — a code path publishes without approval | Medium | **Critical** | Single writer: `PublishService.publish()`. Approval validated inside the same transaction. DB constraint `published_requires_approval` style checks at service level + append-only approval ledger. Regression tests attempt 6 distinct bypasses. |
| R3 | Stale approval after content edit or asset swap | Medium | **Critical** | Every edit creates a new `content_version`; `ApprovalService.invalidate_for_version_change()` revokes rows. Assets carry a checksum; swap compares checksum and revokes. `content_items.current_version_id` FK is the only publishable target. |
| R4 | LangGraph 0.2.x API drift breaks the custom checkpointer | Medium | High | Pin exact versions in `requirements.txt`. Checkpointer implemented strictly against `BaseCheckpointSaver` ABC; `test_checkpointer_persistence.py` re-instantiates the saver to prove durability. |
| R5 | Duplicate model-usage rows inflate cost reporting on retries | Medium | Medium | `model_usage` unique on `(agent_run_id, attempt)`; gateway writes via upsert. |
| R6 | Idempotency implemented as a no-op / fake | Medium | High | `Idempotency-Key` handled by a DB table with `request_hash`; mismatch → `409 idempotency_key_reuse`. Tests assert a repeated approve/publish returns the same record id. |
| R7 | Tokens or secrets leak into frontend responses or logs | Medium | **Critical** | Tokens never stored in `social_accounts` (only `token_ref` pointing to a vault). Response schemas are explicit allow-lists. structlog processor redacts `authorization`, `token`, `secret`, `password`, `api_key` patterns. Test asserts no `token`/`secret` field in social account responses and no secret in log capture. |
| R8 | Fabricated data presented as real (brand/analytics/agents) | Medium | High | Every integration has `available: false` + reason. Empty states in UI. Brand seed asserts no invented claims (`do_not_claim` list). Analytics API returns `data_source: "none"` when no snapshots exist rather than zeros-as-truth. |
| R9 | Agent layer becomes a hidden sequential chain | Medium | High | Graph routing is conditional (`route_after_review`, `route_next_task`) with a supervisor `dispatch` node; planner emits a task DAG with dependencies; cycle guard (`max_iterations`) prevents runaway loops. Test asserts non-linear routing. |
| R10 | Scope creep into Phase 2 | High | Medium | Non-goals enumerated in `docs/01-IMPLEMENTATION-PLAN.md` §B.5; publishing always returns `publishing_disabled` in Phase 1 unless a platform adapter is configured. |
| R11 | Rate limiting is in-process only → not distributed | Medium | Low | `RateLimiter` protocol with memory driver; Redis driver selected when `REDIS_URL` is set; `/system/status` reports which driver is active. |
| R12 | Migration drift between models and migrations | Medium | Medium | `test_migrations.py` asserts every model table exists after `upgrade head`; autogenerate instructions documented. |
| R13 | Celery/Redis absent in dev → agent execution silently never runs | Medium | High | `TaskDispatcher` resolves to `inline` (with warning) when broker absent, so workflows still execute and are recorded; status endpoint exposes `tasks: {driver, available}`. |
| R14 | SQLite can't do `FOR UPDATE` → race on publish | Low (dev) / n/a (prod) | Medium | Publish path uses `SELECT ... FOR UPDATE` on Postgres and an optimistic `UPDATE ... WHERE status = 'PUBLISHING'` guard so exactly one caller wins on both dialects. |
| R15 | bcrypt/passlib version incompatibility | Known | Low | `bcrypt==4.2.1` pinned with `passlib==1.7.4`; if passlib warns, `SecurityService` uses bcrypt directly. |

## D.2 External dependencies

| Dependency | Required for | State in Phase 1 | Behaviour when absent |
| --- | --- | --- | --- |
| PostgreSQL 15+ | production DB | not provisioned | SQLite driver for dev/test; `system.status` = `unavailable` |
| pgvector extension | semantic memory search | not provisioned | JSON embedding + cosine fallback in Python |
| Redis | Celery broker, distributed rate limit, cache | not provisioned | `inline` dispatcher, memory limiter |
| S3-compatible storage | creative asset bytes | not provisioned | `LocalFilesystemStorage` in `.data/storage`, or `unavailable` |
| LLM provider keys (OpenAI/Anthropic/OpenRouter) | model-backed agents | not provisioned | provider returns `ProviderUnavailable`; deterministic offline agent paths execute and record **zero** invented usage |
| Social platform apps (Meta, LinkedIn, X, Google) | publishing | not provisioned | adapters report `not_configured`; publish endpoint returns `publishing_disabled` |
| SMTP | notifications | not provisioned | notifications disabled, logged |

## D.3 Dependency policy

- Every runtime dependency is pinned in `backend/requirements.txt`.
- No dependency is added for something achievable in <100 lines of stdlib
  (e.g. idempotency store, rate limiter, redaction processor).
- Frontend keeps a minimal tree: `react`, `react-dom`, `react-router-dom`,
  `clsx`, plus dev-only Vite/TS/Tailwind/PostCSS. Charts are hand-built SVG to
  avoid a heavy charting dependency for empty datasets.
- No dependency directories are modified by hand; `node_modules` and `.venv` are
  gitignored.
