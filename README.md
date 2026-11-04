# Crusor AI Marketing Director

An autonomous **AI marketing director**: research → strategy → campaign planning →
platform-specific content → creative assets → brand validation → founder approval
→ publishing → analytics → optimisation, on one durable LangGraph runtime with a
founder-only approval gate that has no bypass.

The system is built to be *honest about what it cannot do*. Every integration
reports `unavailable` with a specific reason when its prerequisites are missing,
and nothing is ever simulated into a success: a post is only marked published
after the platform is read back, a metric is only stored if the platform returned
it, and an ad campaign cannot be launched without a founder authorisation bound to
its exact proposal hash.

---

## Status

| Layer | State |
| --- | --- |
| Research, strategy, content, creative, brand, growth agents | implemented, mock-tested |
| Content intelligence (weekly plans, validation) | implemented, mock-tested |
| Creative Studio (briefs, assets, immutable versions, QA) | implemented, mock-tested |
| LLM routing, per-provider keys, budgets, pricing verification | implemented, mock-tested |
| Approval gate (exact version, founder-only, append-only ledger) | implemented and exercised end-to-end |
| **OAuth connections + token vault (6 platforms)** | implemented, mock-tested, **never live-verified** |
| **Platform transports (publish + verify + metrics)** | implemented, mock-tested, **never live-verified** |
| **Analytics ingestion from platform APIs** | implemented, mock-tested, **never live-verified** |
| **Scheduled publishing (due-post runner + Celery job)** | implemented, mock-tested |
| **Paid-media governance (proposal → authorisation → ledger)** | implemented, mock-tested |
| **Paid-media transports (Meta/Google/LinkedIn Ads)** | **not implemented** — refused and recorded |
| Google Flow / DeepX bridge | implemented behind provenance gates; **blocked** (source, release and installed artifact unverifiable) |
| Docker, compose, CI | written; not executed in the authoring environment |

The full five-column matrix — implemented / mock-tested / sandbox-verified /
production-verified / blocked — is in
[`docs/22-PHASE4-PRODUCTION-INTEGRATIONS.md`](docs/22-PHASE4-PRODUCTION-INTEGRATIONS.md).

```text
RELEASE ANCHOR:          MISSING
INSTALLED EXTENSION:     NOT FOUND
OVERALL PROVENANCE:      UNVERIFIED
LIVE FLOW GENERATION:    DISABLED
U1–U7:                   UNRESOLVED
PHASE 3C.3:              NO-GO — NOT STARTED
```

---

## Stack

| Layer | Choice |
| --- | --- |
| Frontend | React 18 + TypeScript + Tailwind + Vite |
| Backend | Python 3.11 + FastAPI + Pydantic v2 |
| Orchestration | LangGraph 0.2 with a database-backed checkpointer |
| Database | PostgreSQL 15/16 + pgvector (SQLite for dev/test) |
| Async tasks | Celery + Redis, with an inline dispatcher fallback |
| Storage | any S3-compatible bucket, or the filesystem |
| LLM | provider-independent gateway (OpenAI / Anthropic / OpenRouter / verified OpenAI-compatible), task routing, per-call cost tracking |
| Credentials | AES-256-GCM token vault (`TOKEN_VAULT_KEY`), key never in the database |

---

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt

export DATABASE_URL="sqlite+aiosqlite:///./.data/crusor.db"
export SECRET_KEY="dev-only-secret-key-at-least-32-characters"
export BOOTSTRAP_FOUNDER_EMAIL="founder@crusor.ai"
export BOOTSTRAP_FOUNDER_PASSWORD="crusor-dev-password-1"

cd backend
../.venv/bin/alembic upgrade head
../.venv/bin/python -m app.seed.brand_crusor
../.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

```bash
cd frontend && npm install && npm run dev    # http://localhost:5173
```

API docs: <http://localhost:8000/docs>. Sign in with the bootstrap founder.

To enable publishing you additionally need a vault key and that platform's own app
credentials — see [`docs/24-ENVIRONMENT-CHECKLIST.md`](docs/24-ENVIRONMENT-CHECKLIST.md).

---

## The guarantees, and how they are enforced

### 1. Nothing publishes without a founder approval of that exact version

* `PublishingService.publish()` is the only writer of `PUBLISHED`, and it checks
  the approval inside the same transaction as the state change.
* An approval names the exact `content_version_id` **and** its content hash.
  Editing the copy creates version N+1 and invalidates the approval; swapping a
  creative asset does the same.
* Only the `founder` role may approve or publish.
* With no transport configured the request is refused (`publishing_disabled` /
  `integration_unavailable`) and the item keeps its state.

### 2. A publish is verified, not assumed

Every transport performs the create call, then reads the created object back. A
response that creates nothing, or a read-back that returns a different object,
raises — `PublishedPost` is never written. A final check rejects any adapter result
without an external id.

### 3. Credentials cannot leak through the API

* `social_accounts` has **no token column**; it stores `token_ref = "vault:<uuid>"`.
* Tokens live only as AES-256-GCM ciphertext in `oauth_tokens`, with associated
  data binding each record to its brand, platform and account — a copied
  ciphertext fails to decrypt in another row.
* No response schema maps to that table. A database dump alone yields nothing
  usable without `TOKEN_VAULT_KEY`.

### 4. Analytics are measured or absent

`analytics_snapshots` rows are written only when a platform actually returned a
value. Each row records `measured`, `unavailable` and `derived` sets, so a missing
metric is shown as "not reported" rather than drawn as zero.

### 5. Spending requires a founder decision about that exact proposal

* An agent may draft a paid campaign. Drafting cannot spend.
* A founder authorisation names the proposal hash, a spend ceiling, an operation
  allow-list and an expiry.
* Any edit after approval changes the hash and invalidates the authorisation.
* Consumption is a single atomic `UPDATE … WHERE status='active'`, so one approval
  funds exactly one launch; the transport is checked *before* consumption so a
  blocked launch does not burn it.
* `ad_operations` records every attempt, including refusals.

### 6. Capabilities are never overstated

`GET /api/v1/social/platforms` reports, per platform, whether credentials exist,
whether a vault exists, whether publishing is therefore enabled, and — per
operation — `documented` support, `implemented` in this build, and `verification`
(`unverified` / `sandbox` / `production`). On a fresh checkout every operation is
`unverified`, because it is.

---

## Repository layout

```
backend/
  app/
    core/          config, structured logging with redaction, error envelope,
                   security, rate limiting, idempotency, audit
    db/            engine/session, cross-dialect types (GUID, JSONB, Vector)
    models/        22 tables incl. oauth_tokens, oauth_flows, ad_campaigns,
                   ad_budget_authorizations, ad_operations
    services/      brand, campaign, content, approval, publishing, creative,
                   analytics, analytics_ingest, oauth, ads, research, strategy,
                   social, usage
    agents/        research, strategy, content, creative, brand_guardian,
                   growth_analyst + registry and a shared base contract
    director/      LangGraph state, checkpointer, nodes, graphs, runtimes
    llm/           provider-independent gateway, routing, budgets, cost
    integrations/  token vault (AES-GCM), OAuth, HTTP policy, platform
                   transports, ads transports, capability matrix
    publishing/    adapter registry: unavailable vs live, per-account resolution
    flow/          DeepX/Flow bridge with provenance gates
    api/v1/        auth, brand, campaigns, content, content_studio, creative,
                   creative_studio, flow, director, agents, social, analytics,
                   research, strategy, ads, system
    tasks/         Celery app + swappable dispatcher
  alembic/         migrations (head: e6f7a8b9c0d1)
  tests/           1204 tests
frontend/
  src/pages/       Overview, Director, Research, Strategy, Campaigns, Calendar,
                   Content Studio, Creative Studio, Approvals, Social Accounts,
                   Advertising, Analytics, Brand, Settings
scripts/
  acceptance.py        end-to-end proof of the acceptance criteria
  flow_source_audit.py provenance audit (expected: SOURCE_ABSENT, exit 3)
docker-compose.yml    PG16 + Redis + backend + worker + frontend
.github/workflows/    CI: tests, migrations, audit gate, typecheck, secret scan
```

---

## Tests and acceptance

```bash
cd backend
../.venv/bin/python -m pytest tests          # 1204 tests
npx tsc --noEmit --project ../frontend       # or: cd ../frontend && npx tsc --noEmit
cd ../frontend && npm run build

# end-to-end against a running server
cd ../backend && ../.venv/bin/python ../scripts/acceptance.py --base-url http://127.0.0.1:8000
```

The acceptance script exits non-zero if any criterion fails and writes
`docs/acceptance-run.json`.

`scripts/flow_source_audit.py` is expected to exit **3** with `SOURCE_ABSENT`. That
is the gate working: the DeepX source is not in this repository and must never be
vendored.

CI (`.github/workflows/ci.yml`) runs the suite with **no credentials at all**,
asserts the audit still exits 3, typechecks and builds the frontend, and fails if a
`.env` file or an API-key-shaped string is committed.

---

## Configuration

All configuration is environment-based; secrets are never returned by the API or
written to logs (structlog redacts secret-bearing keys and bearer tokens).

**Required in production:** `DATABASE_URL` (PostgreSQL), `SECRET_KEY`,
`BOOTSTRAP_FOUNDER_*` on first boot.

**Required to publish anything:** `TOKEN_VAULT_BACKEND=db`, `TOKEN_VAULT_KEY`,
`OAUTH_REDIRECT_BASE_URL`, plus each platform's own `*_CLIENT_*` pair.

**Optional, each reporting `unavailable` with a reason until configured:**
LLM provider keys, Redis/Celery, S3 storage, ads transport credentials (none exist
yet), Flow provenance material.

The complete table, including what each missing value disables and what the founder
must provision, is in
[`docs/24-ENVIRONMENT-CHECKLIST.md`](docs/24-ENVIRONMENT-CHECKLIST.md).

`GET /api/v1/system/status` reports the resolved state of all of it, including
`publishing.platforms[]` and `ads.transports[]`.

---

## Documentation

| Document | Contents |
| --- | --- |
| `docs/00`–`04` | Phase 1 architecture audit, plan, file plan, risks, acceptance evidence |
| `docs/05`–`07` | Phase 2 audit, plan, final report |
| `docs/08`–`10` | Phase 3 integration readiness, provider routing security, budget safety |
| `docs/11`–`12` | Phase 3A content intelligence, Phase 3B creative studio |
| `docs/13`–`21` | Phase 3C Flow security, DeepX remediation, trust pipeline, hardening, retry/delivery, cancellation semantics, correlation |
| **`docs/22`** | **Phase 4: integrations, transports, ads governance, completion matrix, security audit** |
| **`docs/23`** | **Deployment guide (dev, compose, migrations, rollback, health, backups)** |
| **`docs/24`** | **Environment and credential checklist** |

---

## What this system does not do

* It does not publish to a social network until a platform's app credentials, a
  vault key and a connected account all exist. Without them it refuses, visibly.
* It does not launch or modify a paid campaign at all: the governance is complete,
  the transports are not.
* It does not generate live Google Flow media. The bridge is implemented and gated
  on a source licence, a signed release and an installed artifact, none of which
  can be verified here.
* It does not claim a verification it has not performed. Where the code is
  mock-tested only, the matrix says so.
