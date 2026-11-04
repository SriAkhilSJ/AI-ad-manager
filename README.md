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
| **Director lifecycle (goal → … → approval → schedule → publish → measure → optimise)** | implemented, mock-tested end-to-end, durable across restarts |
| **Paid-media governance (proposal → authorisation → ledger)** | implemented, mock-tested |
| **Paid-media transports (Meta Graph v21.0 / Google Ads REST v17 / LinkedIn 202405)** | implemented with preview + dry-run + read-back; mock-tested, **never live-verified** |
| **Creative providers (OpenAI Images, Replicate video)** | implemented behind price/budget/authorisation gates; mock-tested, **never live-verified** |
| Google Flow / DeepX bridge | implemented behind provenance gates; **blocked** (source, release and installed artifact unverifiable) |
| **PostgreSQL 16 + pgvector + Redis + Celery** | verified locally (migrations, full suite, broker round-trip) and in CI: run 41870446667, `backend-postgres` job green — full suite on PostgreSQL, then three real tasks dispatched over Redis, all `SUCCESS` |
| **Integration canaries** (`scripts/integration_canary.py`) | implemented and tested; **every provider blocked** — no credentials here, and this environment closes every provider TLS handshake |
| **Lifecycle evidence** (`scripts/lifecycle_evidence.py`) | executed end-to-end against a live server: 25 trace entries, 26 checkpoints, approval boundary enforced |
| Docker, compose | written and reviewed; **not executed** — Docker is unavailable in the authoring environment |

The full five-column matrix — implemented / mock-tested / sandbox-verified /
production-verified / blocked — lives in
[`docs/25-platform-readiness.md`](docs/25-platform-readiness.md), which is
generated from the code and the live environment by
`scripts/platform_readiness.py` (so it cannot claim a verification the build has
not performed). The Phase 4.1 matrix and its narrative are in
[`docs/22-PHASE4-PRODUCTION-INTEGRATIONS.md`](docs/22-PHASE4-PRODUCTION-INTEGRATIONS.md);
the Phase 4.2 record is
[`docs/26-PHASE4.2-RELEASE-READINESS.md`](docs/26-PHASE4.2-RELEASE-READINESS.md).

```text
RELEASE ANCHOR:          MISSING
INSTALLED EXTENSION:     NOT FOUND
OVERALL PROVENANCE:      UNVERIFIED
LIVE FLOW GENERATION:    DISABLED
U1–U7:                   UNRESOLVED
PHASE 3C.3:              NO-GO — NOT STARTED
DOCKER COMPOSE:          NOT EXECUTED (docker unavailable in the authoring sandbox)
CI (PG + Redis + Celery): GREEN — run 41870446667
DIRECTOR LIFECYCLE:      EXECUTED, GOVERNANCE CHECKS PASS
PROVIDER CANARIES:       BLOCKED (no credentials, no provider egress)
PRODUCTION READINESS:    NOT CLAIMED
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

### 6. The Director's autonomy stops at the founder's signature

* The graph halts on a real LangGraph `interrupt`; resuming without a decision is
  impossible and only a founder may resume it.
* Approving the copy is **not** consent to schedule or publish. The decision body
  carries `schedule` (default true) and `publish_now` (default false), both of
  which are persisted with the decision.
* Scheduling only touches content the approval service has already moved to
  `APPROVED`, and only on an account that is *connected* — a stored row is not an
  authorisation.
* Measurement reports `unavailable` when no platform returned numbers, and
  optimisation produces recommendations with `auto_applied: false`; anything that
  would move money is flagged `requires_founder_approval`.
* Cancelling a workflow makes it unresumable without deleting the checkpoints that
  record what it had already done.

### 7. Capabilities are never overstated

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
    director/      LangGraph state, checkpointer, nodes, graphs, runtimes,
                   lifecycle (schedule → publish → measure → optimise)
    creative/      contracts, media inspection, providers (mock, Flow boundary,
                   OpenAI images, Replicate video)
    integrations/  OAuth, token vault, social transports, ads transports
                   (Meta Graph v2x, Google Ads REST, LinkedIn Marketing)
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
../.venv/bin/python -m pytest tests          # 1274 tests
npx tsc --noEmit --project ../frontend       # or: cd ../frontend && npx tsc --noEmit
cd ../frontend && npm run build

# end-to-end against a running server
cd ../backend && ../.venv/bin/python ../scripts/acceptance.py --base-url http://127.0.0.1:8000

# integration canaries: report only, then reachability, then read-only live calls
../.venv/bin/python ../scripts/integration_canary.py
../.venv/bin/python ../scripts/integration_canary.py --network
../.venv/bin/python ../scripts/integration_canary.py --network --live --write

# one complete Director lifecycle through the API, with governance assertions
LIFECYCLE_EMAIL=… LIFECYCLE_PASSWORD=… \
  ../.venv/bin/python ../scripts/lifecycle_evidence.py --base-url http://127.0.0.1:8000 --write
```

The acceptance script exits non-zero if any criterion fails and writes
`docs/acceptance-run.json`. Its last full run against a live server was
**65/65 PASS** (which itself includes a 1274-test, 0-failure suite run).

Recorded on 2026-11-04 (UTC), Phase 4.2:

| Run | Result |
| --- | --- |
| Backend suite, SQLite | 1274 tests, 0 failures, 0 errors |
| Backend suite, PostgreSQL 16.2 + pgvector, `-n 4` | 1274 tests, 0 failures, 0 errors (832.7 s) |
| Acceptance harness, live server | 65/65 PASS |
| Frontend `tsc --noEmit` / `npm run build` | exit 0 / build OK |
| Broker round-trip (Redis + Celery) | 3/3 tasks SUCCESS |

What those runs do **not** prove: that Meta, Google Ads, LinkedIn, OpenAI or
Replicate accept our requests. Every one of those is mocked here and marked
`never live-verified` until someone runs it with real credentials.

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
LLM provider keys, Redis/Celery, S3 storage, paid-media credentials
(`META_ADS_*`, `GOOGLE_ADS_*`, `LINKEDIN_ADS_*`), creative provider credentials
(`OPENAI_API_KEY` / `REPLICATE_API_TOKEN` with pinned pricing), Flow provenance
material.

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
