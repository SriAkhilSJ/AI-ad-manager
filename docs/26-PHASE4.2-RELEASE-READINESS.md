# 26 — Phase 4.2: production validation, autonomous Director, real providers

Phase 4.1 made the platform's gates real: nothing publishes without a read-back,
nothing spends without a founder authorisation bound to the exact proposal hash.
Phase 4.2 closes the three holes that remained, and it does so **without**
claiming a single verification this repository has not performed:

1. **Infrastructure** — the suite now runs against PostgreSQL + pgvector in CI,
   not only SQLite, and a real broker round-trip is part of the pipeline.
2. **Director** — one founder instruction now drives the lifecycle *past* the
   approval interrupt: schedule → publish → measure → optimise → hand-off.
3. **Providers** — Meta/Google/LinkedIn ads transports and real image/video
   creative providers exist, are previewable, and stay refused until the founder
   supplies exactly what they need.

---

## 1. What was actually executed here (and what was not)

| Check | Result | Evidence |
| --- | --- | --- |
| Docker / `docker compose up` | **not executed** — `docker: command not found` in the authoring sandbox | blocker reported, covered by CI services instead |
| PostgreSQL 16.2 + `vector` 0.6.2 (local `pgserver`) | migrations `02cf853b2147 → e6f7a8b9c0d1` apply clean; full suite green | §3 |
| Alembic up → down → up on PostgreSQL | passed after fixing three PG-only defects (§2) | §3 |
| Redis 6.2.14 + Celery 5.4.0 worker (local `redislite`) | `publish_due` + `refresh_analytics` + `health_check` all `SUCCESS` through the broker | `scripts/celery_roundtrip.py` |
| Redis rate limiter | limit 3 → `[True, True, True, False, False]` | §3 |
| Dispatcher resolution with `REDIS_URL` | driver `celery`, `accepted: True` | §3 |
| Frontend typecheck + build | see §4 | §4 |
| Acceptance suite (live API) | see §4 | §4 |
| Live platform calls (Meta/Google/LinkedIn/X/YouTube/Reddit/OpenAI/Replicate) | **none** — no credentials exist in this environment | every transport reports `unverified` |

**Docker Compose has not been run.** It is written, reviewed and unchanged in
behaviour, but "written" is not "executed", and this document does not claim
otherwise. CI now runs the same PostgreSQL/Redis pair the compose file declares,
which is the substitute evidence available from this environment.

---

## 2. Defects found by running on the production dialect

All three were invisible on SQLite and would have failed at deploy time:

| # | Defect | Fix |
| --- | --- | --- |
| 1 | Three migrations declared entity ids as `String(36)`/`CHAR(32)` while the models use `GUID()`; PostgreSQL rejected the foreign keys | migrations rewritten to `GUID()` |
| 2 | `oauth_flows.state` was `String(255)` but the signed state is **317 characters** → `StringDataRightTruncationError` during OAuth | widened to `String(1024)` |
| 3 | `celery_app.autodiscover_tasks` looks for `app.tasks.tasks`; this package keeps them in `app.tasks.jobs`, so the worker started and then rejected `crusor.run_job` | explicit `conf.imports = ("app.tasks.jobs",)` |

Two test-harness defects were fixed with them: asyncpg binds a connection to the
loop that created it (⇒ `NullPool`), and per-test cleanup on PostgreSQL must be
`TRUNCATE … RESTART IDENTITY CASCADE`, not a SQLite pragma. Parallel workers get
their own database (`PYTEST_XDIST_WORKER` suffix) because they each create the
schema.

---

## 3. Director: the lifecycle now continues past approval

Before this phase the graph stopped at *"the founder approved the copy"*. The
back half of the product promise — schedule it, get it out, see what it did,
decide what to do next — was manual. The graph now runs:

```text
START → intake → plan → dispatch ─┬→ research ─┐
                                  ├→ strategy ─┤
                                  ├→ content ──┤→ dispatch (loop)
                                  ├→ creative ─┤
                                  └→ growth ───┘
dispatch → review → ensure_campaign → persist_drafts → request_approval
        → human_approval (interrupt) → apply_decision
        → schedule → publish → measure → optimize → finalize → END   (approved)
        → finalize → END                                            (anything else)
```

`apply_decision` is a new node and it fixes a real ordering bug the lifecycle
tests caught: the approval transitions must be applied **before** scheduling,
because the scheduler only accepts content the approval service has actually
moved to `APPROVED`. Applying them inside `finalize` (at the end) meant every
lifecycle node ran against content still `IN_REVIEW`.

What each stage does, and refuses to do:

| Stage | Does | Refuses |
| --- | --- | --- |
| `schedule` | creates a `ScheduledPost` per approved item on a **connected** account, spread across a horizon | never schedules content that is not approved, or on an account that exists but is not connected |
| `publish` | publishes only when the founder set `publish_now` | a future slot is not a publish; everything else is reported as `deferred` with the count |
| `measure` | ingests measured analytics through the platform APIs | never zero-fills, never estimates; unavailable is reported as unavailable |
| `optimize` | asks the growth analyst over measured data | never proposes an optimisation without measurements; never applies one (`auto_applied: false`), and anything touching spend is flagged `requires_founder_approval` |

Durability, duplicate prevention and cancellation:

* **Durable execution** — every superstep writes a `workflow_checkpoints` row in
  the same transaction as its domain writes, so a crash cannot record a checkpoint
  describing rolled-back work.
* **Duplicate prevention** — the thread id is derived from
  `brand | objective | campaign`, so a repeat request finds and continues the open
  workflow (`reused: true`) instead of planning and spending twice. A *finished*
  workflow is never resurrected; a new request gets a new thread.
* **Cancellation** — `POST /api/v1/director/workflows/{id}/cancel` (founder) writes
  a terminal state patch. The workflow cannot be resumed afterwards, and the
  checkpoints that record what it *had* done are kept.
* **Approval of content ≠ consent to publish** — the decision body carries
  `schedule` (default true) and `publish_now` (default false), and both are
  recorded in the state the resume produces.

---

## 4. Verification (local, 2026-11-04 UTC)

| Check | Result |
| --- | --- |
| Full backend suite (SQLite, `pytest -q`) | **1274 tests, exit 0** (run 13:33–13:47Z) |
| Full backend suite (PostgreSQL 16.2 + pgvector, `pytest -n 4`) | **1274 tests, 0 failures, 0 errors, 832.7s** (13:52–14:06Z, `/tmp/pg-final.xml`) |
| Alembic on PostgreSQL | `02cf853b2147 → e6f7a8b9c0d1` upgrade → downgrade → upgrade, head `e6f7a8b9c0d1` |
| Ads transports (`test_phase4_ads_transports.py`) | **30 passed** |
| Director lifecycle (`test_phase4_director_lifecycle.py`) | **14 passed** |
| Creative providers (`test_phase4_creative_providers.py`) | **23 passed** |
| E2E lifecycle (`test_phase4_e2e_lifecycle.py`) | **3 passed** |
| Phase 4.2 modules together | **114 tests, 0 failures, 0 errors** (14 + 30 + 23 + 3 + 44) |
| Broker round-trip (`scripts/celery_roundtrip.py`) | exit 0 — `publish_due`, `refresh_analytics`, `health_check` all `SUCCESS` |
| Readiness report (`scripts/platform_readiness.py`) | writes `docs/25-platform-readiness.md`; every `sandbox-verified` / `production-verified` column is `no` |
| Acceptance harness (`scripts/acceptance.py` against a live server) | **65/65 PASS, exit 0** — includes "1274 tests, 0 failures, 0 errors"; evidence in `docs/acceptance-run.json` |
| Frontend typecheck (`tsc --noEmit`) | exit 0 |
| Frontend production build (`npm run build`) | OK — `index-Bqzo970z.js` 411.77 kB (gzip 112.31 kB), CSS 33.30 kB |

Reproduce the acceptance run (the harness needs prepared state — it does not
migrate its own database, and two of its checks require a configured Flow
companion):

```bash
# 1. a database that is migrated and has a founder whose password the harness knows
export DATABASE_URL="sqlite+aiosqlite:////tmp/acceptance.db"
cd backend && ../.venv/bin/alembic upgrade head

# 2. the server, with the companion identity the harness sends
ENVIRONMENT=development SECRET_KEY=... DATABASE_URL=... \
  BOOTSTRAP_FOUNDER_EMAIL=founder@crusor.ai BOOTSTRAP_FOUNDER_PASSWORD=crusor-dev-password-1 \
  TASK_DISPATCHER=inline RATE_LIMIT_PER_MINUTE=10000 \
  FLOW_COMPANION_ID=acceptance-companion FLOW_COMPANION_SECRET=<32+ chars> \
  ../.venv/bin/python -m uvicorn app.main:app --port 8000

# 3. the brand used by criterion 15 needs competitor records: the offline
#    research provider draws sources from stored brand knowledge and refuses to
#    invent external ones, so an empty brand legitimately yields `sources: []`.

# 4. the harness itself
FLOW_COMPANION_ID=acceptance-companion FLOW_COMPANION_SECRET=<same> \
  DATABASE_URL=... ../.venv/bin/python ../scripts/acceptance.py --base-url http://127.0.0.1:8000
```

Two defects in the harness itself were found and fixed while running it:

| Check | Defect | Fix |
| --- | --- | --- |
| 19 | asserted a `research_run` trace entry the runtime never writes, so it could only pass in the "reuse existing evidence" branch | assert `research_completed`, the entry the graph actually emits |
| 19 | read `research_rounds[0]["findings"]`; the workflow records `findings_created` | read the field the API returns, accepting either name |

---

## 5. Paid media: transports, and every guard that still holds

`app/integrations/ads.py` implements three transports against published APIs:

| Platform | API | Create is… | Idempotency | Read-back |
| --- | --- | --- | --- | --- |
| Meta | Graph `v21.0` | `status=PAUSED` for campaign and ad set | creates are never retried | `GET /{campaign_id}` |
| Google | REST `v17` | `status=PAUSED` | `request-id` header per mutate | GAQL `googleAds:search` |
| LinkedIn | `202405` | `status=DRAFT` | `X-RestLi-Method: PARTIAL_UPDATE` for updates | `GET /adCampaigns/{id}` |

Guards preserved and extended:

* the proposal hash is unchanged by execution (`test_service_marks_a_campaign_live_only_after_a_verified_read_back`);
* an unverified read-back leaves the campaign `authorized`, does **not** burn the
  authorisation, and records `unverified` (`test_service_refuses_to_go_live_when_the_platform_did_not_confirm`);
* a create that the platform rejects is never retried (`test_meta_create_error_is_not_retried`, `test_google_create_failure_is_not_retried`);
* tokens travel in `Authorization` headers, never in a URL, and provider errors
  never carry vendor bodies (`test_openai_rejection_is_typed_and_does_not_leak_the_body`);
* a created campaign lands locally as `paused`, because that is the state the
  read-back proved — a local row saying "active" while the platform holds it
  paused would be a lie the founder could act on.

New surface: `POST /ads/campaigns/{id}/preview` (dry run, `spends_money: false`),
`POST /ads/campaigns/{id}/validate-account` (founder-only, read-only), and
`GET /ads/campaigns/{id}/report` (measured only).

---

## 6. Creative: one real image provider, one real video provider

| Medium | Provider | API | Async model |
| --- | --- | --- | --- |
| image | OpenAI Images | `POST /v1/images/generations` | provider call completes inline; bytes are cached and integrity-checked before the job is marked succeeded |
| video | Replicate | `POST /v1/predictions`, poll `GET /v1/predictions/{id}`, `POST …/cancel` | real asynchronous prediction; output URL downloaded and signature-checked |

Selection is per medium (`CREATIVE_IMAGE_PROVIDER`, `CREATIVE_VIDEO_PROVIDER`);
the effective provider name is the pair (`openai+replicate`) so the service's
"is this the provider the operator configured?" check still has something exact to
compare against. `CREATIVE_PROVIDER=mock` remains the default.

Guards:

* **No invented pricing.** A billable provider is refused until
  `CREATIVE_PRICING_JSON` contains a verified price, the budget is non-zero and
  `CREATIVE_ALLOW_PAID_GENERATION=true`.
* **No shared credentials.** The image provider reads `OPENAI_API_KEY`; the video
  provider reads `REPLICATE_API_TOKEN`. Neither falls back to the other
  (`test_openai_does_not_borrow_the_replicate_token`).
* **Integrity before success.** Bytes that are not a recognisable image/video are
  errors, not assets.
* **Ambiguity is preserved.** A transport timeout on a billable request is marked
  `ambiguous`, so it can never be silently resubmitted as if it had failed.
* **Flow stays disabled.** No Flow live generation, no fabricated extension
  artifact; the readiness report says so explicitly.

---

## 7. Six-platform verification

`docs/25-platform-readiness.md` is generated by `scripts/platform_readiness.py`
from code plus the live environment. It carries the five columns the release gate
asks for, and both evidence columns are `no` for all six platforms and all three
ads platforms, because no live call has been made. The founder checklist in that
document names, per platform, the credentials, scopes and app approvals that are
still missing.

---

### What the readiness report cannot say yet

No row has a `sandbox-verified` or `production-verified` mark. Generating the
report is a statement about *code and configuration*, not about any platform's
servers. To move a row, someone must run the checklist section for that platform
with official sandbox credentials and record the request/response pairs in
`docs/04-ACCEPTANCE-EVIDENCE.md`. Until then the honest answer stays `no`.

---

## 8. Remaining external blockers

Unchanged from Phase 4.1 except where noted; none is a code defect.

| # | Blocker | What unblocks it | Owner |
| --- | --- | --- | --- |
| B1–B5 | Meta / LinkedIn / X / Google-YouTube / Reddit app approvals and scopes | developer apps + review, credentials in the secret store | Founder |
| B6 | Ads access: Meta Marketing API, Google developer token, LinkedIn Advertising API | three approvals + billing | Founder |
| B7 | `TOKEN_VAULT_KEY` in a real deployment | `openssl rand -hex 32` into the secret store | Founder |
| B8 | Flow/DeepX provenance | licensed source + signed release + installed artifact | Founder / vendor |
| B9 | Compose never executed here | run `docker compose up --build` where Docker exists; the CI `backend-postgres` job runs the same services | Operator |
| B10 | LLM pricing unverified | supply `LLM_PRICING_JSON` from the provider's price list | Founder |
| B11 | Creative pricing unverified | enter the contracted per-generation price in `CREATIVE_PRICING_JSON` | Founder |
| B12 | No pinned video model | set `REPLICATE_VIDEO_MODEL` to an approved `owner/model:version` | Founder |

---

## 9. Verdict

```text
LIVE FLOW GENERATION:    DISABLED
PHASE 3C.3:              NO-GO — NOT STARTED
DIRECTOR LIFECYCLE:      IMPLEMENTED, MOCK-VERIFIED END-TO-END
SOCIAL TRANSPORTS:       IMPLEMENTED, MOCK-VERIFIED, NEVER LIVE-VERIFIED
PAID MEDIA TRANSPORTS:   IMPLEMENTED, MOCK-VERIFIED, NEVER LIVE-VERIFIED
CREATIVE PROVIDERS:      IMPLEMENTED (OPENAI IMAGE, REPLICATE VIDEO), NEVER LIVE-VERIFIED
PRODUCTION DIALECT:      VERIFIED LOCALLY ON POSTGRESQL 16 + pgvector; CI JOB ADDED
DOCKER COMPOSE:          NOT EXECUTED (docker unavailable here)
PRODUCTION READINESS:    NOT CLAIMED — pending founder credentials and a Compose run
```

The founder can now give one instruction and watch a campaign travel from goal to
scheduled, measured, optimised hand-off, with an approval gate they cannot be
bypassed through and a spend ledger that records refusals as well as successes.
What remains is authorisation, not code.
