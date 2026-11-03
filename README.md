# AI-ad-manager

An autonomous **AI Marketing Director** for Crusor AI — not a caption generator.

Phase 1 (Foundation & Architecture) is implemented: a stateful LangGraph director
delegates to six specialist agents, persists content as immutable versions, and
routes every publishable asset through a founder-only approval gate that has no
bypass. **Nothing is published to a social network in Phase 1**, by design.

```
Research → Strategy → Campaign Planning → Content → Creative → QA
        → Human Approval → (Scheduling) → Publishing* → Analytics* → Optimization*

* Phase 2 — the interfaces exist, the transports do not.
```

---

## Stack

| Layer | Choice |
| --- | --- |
| Frontend | React 18 + TypeScript + Tailwind + Vite (`frontend/`) |
| Backend | Python 3.11 + FastAPI + Pydantic v2 (`backend/`) |
| Orchestration | LangGraph 0.2 with a database-backed checkpointer |
| Database | PostgreSQL 15 + pgvector (Supabase-compatible); SQLite for dev/test |
| Async tasks | Celery + Redis, with an `inline` dispatcher fallback |
| Storage | Any S3-compatible bucket; filesystem backend for dev |
| LLM | Provider-independent gateway (OpenAI / Anthropic / OpenRouter) with routing and cost tracking |

---

## Quick start (development, SQLite, no external services)

```bash
# ---------- backend ----------
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt

export DATABASE_URL="sqlite+aiosqlite:///./.data/crusor.db"
export JWT_SECRET="dev-only-jwt-secret-please-rotate-32chars"
export BOOTSTRAP_FOUNDER_EMAIL="founder@crusor.ai"
export BOOTSTRAP_FOUNDER_PASSWORD="crusor-dev-password-1"

cd backend
../.venv/bin/alembic upgrade head          # create the schema
../.venv/bin/python -m app.seed.brand_crusor   # load the Crusor brand knowledge base
../.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

# ---------- frontend (second terminal) ----------
cd frontend
npm install
npm run dev            # http://localhost:5173, proxies /api → :8000
```

Open <http://localhost:5173>, sign in with the founder credentials above.
API docs are at <http://localhost:8000/docs>.

The first boot against an empty `users` table creates the founder account from
`BOOTSTRAP_FOUNDER_*`. Change those values anywhere real.

---

## The approval guarantee

The requirement was blunt: *the AI must never publish without explicit founder
approval, ever, no bypass.* Four independent mechanisms enforce it:

1. **One writer.** `PublishService.publish()` is the only code path that can
   create a `published_posts` row, and it validates approval inside the same
   transaction.
2. **Approval is version-bound.** An approval references an exact
   `content_version_id` and its content hash. Editing the copy creates version
   N+1 and invalidates the approval; replacing a creative asset does the same.
3. **Role-bound.** Only the `founder` role may approve or publish. Marketers
   write, viewers read.
4. **Honest failure.** With no platform transport configured, even a fully
   approved item is refused with `409 publishing_disabled` /
   `503 integration_unavailable` and stays `APPROVED`. Nothing is simulated.

`tests/test_publish_blocked.py` attempts six distinct bypasses, including a
stale approval hash, a draft with a hand-crafted approval, and an item-scoped
asset swap. The audit trail (`approval_records`) is append-only.

---

## Repository layout

```
backend/
  app/
    core/        config, structured logging with secret redaction, error envelope,
                 security, rate limiting, idempotency, audit
    db/          engine/session, cross-dialect column types (GUID, JSONB, Vector)
    models/      21 tables — the 16 required plus users, audit, idempotency,
                 api keys, workflow checkpoints
    services/    brand, campaign, content, approval, publishing, creative,
                 social, analytics, usage, audit
    agents/      research, strategy, content, creative, brand_guardian,
                 growth_analyst + registry and a shared base contract
    director/    LangGraph state, checkpointer, nodes, graph, runtime
    llm/         provider-independent gateway with routing, budgets, cost
    publishing/  platform adapters (Phase 1: transport unavailable)
    api/v1/      auth, brand, campaigns, content, creative, social, analytics,
                 director, agents, system
    tasks/       Celery app + swappable dispatcher
    seed/        Crusor brand knowledge base
  alembic/       migrations
  tests/         160 tests
frontend/
  src/
    lib/         typed API client, types mirroring the backend schemas, formatters
    hooks/       useApi/useMutation, auth context, toasts
    components/  UI kit, layout shell, shared domain widgets
    pages/       Overview, Director, Campaigns, Calendar, Creative Studio,
                 Approval Centre, Social Accounts, Analytics, Brand Knowledge,
                 Settings
scripts/
  acceptance.py  proves all 13 acceptance criteria against a live server
docs/
  00-ARCHITECTURE-AUDIT.md      A. what existed, constraints found
  01-IMPLEMENTATION-PLAN.md     B. milestones, non-goals
  02-FILE-PLAN.md               C. file-level plan
  03-RISKS-AND-DEPENDENCIES.md  D. risk register, external dependencies
  04-ACCEPTANCE-EVIDENCE.md     evidence per criterion
```

---

## The dashboard — "CRUSOR MARKETING HQ"

Dark, premium, desktop-first: black and charcoal, subtle violet, metallic silver,
tight uppercase tracking. Ten routes, all wired to the real API:

| Route | Purpose |
| --- | --- |
| `/` Overview | System truth, campaign and content counters, next actions |
| `/director` | Start a workflow, watch the task DAG, decide at the interrupt |
| `/campaigns` | Campaign list and detail with plan and pillars |
| `/calendar` | Content grouped by day, filterable by status and platform |
| `/creative` | Upload assets, replace them, see the art-direction brief |
| `/approvals` | Founder review queue with version hash and approval ledger |
| `/social` | Platform readiness and account metadata (no tokens) |
| `/analytics` | Performance (needs connectors) and measured model spend |
| `/brand` | The brand knowledge base — editable without a deploy |
| `/settings` | Integration status, agent contracts, security posture |

There are no fake numbers anywhere. A panel with no data shows an empty state
that explains what would produce data; a disabled integration shows
`unavailable` and the reason.

---

## Tests and acceptance

```bash
cd backend && pytest -q                  # 160 tests
cd frontend && npm run typecheck         # tsc --noEmit
cd frontend && npm run build             # tsc -b && vite build

# end-to-end proof of the 13 acceptance criteria against a running server
.venv/bin/python scripts/acceptance.py --base-url http://127.0.0.1:8000
```

`scripts/acceptance.py` exits non-zero if any criterion fails and writes
`docs/acceptance-run.json`. See `docs/04-ACCEPTANCE-EVIDENCE.md` for the observed
output of the last full run (13/13 criteria).

---

## Configuration

All configuration is environment-based; secrets are never exposed by the API or
written to logs (a structlog processor redacts secret-bearing keys and bearer
tokens before emission). See `.env.example` for the full list.

**Required in production**

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://…` |
| `JWT_SECRET` | Token signing key (32+ bytes) |
| `BOOTSTRAP_FOUNDER_EMAIL` / `BOOTSTRAP_FOUNDER_PASSWORD` | First-run founder account |

**Optional — each reports `unavailable` with a reason until configured**

| Variable | Enables |
| --- | --- |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY` | Real LLM generation instead of deterministic agents |
| `LLM_DEFAULT_PROVIDER` / `LLM_DEFAULT_MODEL` / `LLM_ROUTING_JSON` | Per-agent model routing |
| `LLM_MONTHLY_BUDGET_USD` | Hard spend ceiling |
| `REDIS_URL` / `CELERY_BROKER_URL` | Celery workers and distributed rate limiting |
| `S3_BUCKET` + credentials | Durable creative asset storage |
| `APP_ENV=production` | Disables SQLite, requires secrets, enforces CORS allow-list |

`GET /api/v1/system/status` reports the resolved state of every one of these.

---

## What Phase 1 does not do

Deliberate absences, surfaced in the product rather than hidden:

* **No publishing.** No transport ships. Approval and scheduling are complete.
* **No LLM keys, no generation.** Agents run deterministic implementations and
  report `generation_mode: "deterministic"` with `requires_human_review: true`.
* **No image/video generation.** The Creative Agent produces a brief; a human
  uploads the bytes.
* **No analytics collection.** Needs connectors and published posts. The API
  returns `has_data: false`, `data_source: "none"`, and never estimates.
* **No OAuth token storage.** `social_accounts` keeps a vault reference only.

Phase 2 adds transports, connectors, real generation and the optimization loop.
