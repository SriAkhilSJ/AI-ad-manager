# Phase 2 — Research & Strategy Intelligence: Final Report

**Product:** Crusor AI Marketing Director
**Baseline:** Phase 1 at commit `43d633e`
**Phase 2 head:** `dfa7e44`
**Date:** 2026-11-03
**Honesty note:** every result below was produced by running the code in this
repository. Mocked tests are labelled as such. No claim of production readiness
is made anywhere — see [§11 Limitations](#11-known-limitations).

---

## 1. Architecture audit (summary)

The full audit is `docs/05-PHASE2-ARCHITECTURE-AUDIT.md`; the build plan is
`docs/06-PHASE2-IMPLEMENTATION-PLAN.md`. Both were written **before** any Phase 2
code. What the audit found, and what it forced:

| Finding | Consequence for Phase 2 |
| --- | --- |
| LangGraph director existed but had a single graph tuned for content production | A **second** graph was added rather than overloading the first. Phase 1's graph is untouched. |
| `ModelUsage` recorded tokens/cost but nothing checked a budget *before* a paid call | `app/llm/routing.py` gained `BudgetGuard`, `CostEstimate`, `RequestDeduplicator`. Enforcement happens pre-call. |
| `BrandProfile` had `goals` and `CompetitorProfile` but no product facts, segments, objectives or pain points | New `research.py` models; the audit explicitly rejected inventing `website` on `BrandProfile`. |
| Approval enforcement was real and tested (founder-only, version-bound) | Phase 2 reuses the same discipline: a new append-only `strategy_approvals` table, same approver role. |
| `ContentPillar` already existed | Reused, not duplicated — the plan's candidate list was corrected after inspecting the schema. |
| No research provider abstraction | `app/research/base.py` with a deterministic, an HTTP and a disabled provider behind one protocol. |
| No cost ceiling concept anywhere | Run-level `max_cost_usd` + `max_tokens` + `max_tool_calls`, checked per request. |

Two audit conclusions drove the whole phase:

1. **Honesty over optimism.** A ₹5 envelope cannot fund a real competitive
   research task against a live search API. The system therefore estimates cost
   *before* running, refuses to start when the estimate exceeds the budget, and
   reports what it did not do instead of overspending silently.
2. **Evidence before prose.** A strategy may only assert what a finding can
   support. A finding may only be `verified` if it carries a source URL — enforced
   by a database `CHECK` constraint, not by convention.

---

## 2. Files created and modified

52 files, +15,294 / −68 lines relative to `43d633e`.

### Created (32)

**Backend — models, schema, migration**
```
backend/alembic/versions/60ed69ffc987_phase2_research_strategy_intelligence.py
backend/app/models/research.py
backend/app/models/strategy.py
```

**Backend — model gateway, research, services**
```
backend/app/llm/routing.py
backend/app/research/__init__.py
backend/app/research/base.py
backend/app/services/research.py
backend/app/services/strategy.py
backend/app/agents/limits.py
```

**Backend — strategy workflow**
```
backend/app/director/strategy_state.py
backend/app/director/strategy_nodes.py
backend/app/director/strategy_graph.py
backend/app/director/strategy_runtime.py
```

**Backend — API surface**
```
backend/app/schemas/research.py
backend/app/schemas/strategy.py
backend/app/api/v1/research.py
backend/app/api/v1/strategy.py
```

**Backend — tests (10 new files, 181 Phase 2 tests)**
```
backend/tests/test_model_routing.py         33
backend/tests/test_research_providers.py    23
backend/tests/test_research_persistence.py  19
backend/tests/test_strategy_versioning.py   22
backend/tests/test_strategy_agent.py        16
backend/tests/test_strategy_workflow.py     19
backend/tests/test_research_agent.py        11
backend/tests/test_research_api.py          10
backend/tests/test_strategy_api.py          19
backend/tests/test_brand_knowledge.py        8
```

**Frontend — four surfaces**
```
frontend/src/pages/ResearchCentre.tsx
frontend/src/pages/StrategyStudio.tsx
frontend/src/pages/StrategyDirector.tsx
```

**Docs**
```
docs/05-PHASE2-ARCHITECTURE-AUDIT.md
docs/06-PHASE2-IMPLEMENTATION-PLAN.md
docs/07-PHASE2-FINAL-REPORT.md        (this file)
```

### Modified (20)

```
backend/app/models/__init__.py            export the new models
backend/app/models/agent.py               run-level budget fields
backend/app/core/config.py                budgets, FX rate, limits, model defaults
backend/app/core/errors.py                ConflictError and friends
backend/app/llm/gateway.py                pre-call budget checks, aux tracking
backend/app/services/usage.py             auxiliary vs reasoning usage split
backend/app/agents/base.py                AgentContext carries budget/limits
backend/app/agents/research.py            sets project.provider before running
backend/app/agents/strategy.py            strategy-mode generation
backend/app/api/v1/router.py              mounts 23 endpoints
backend/app/api/v1/system.py              research + strategy blocks, v1.1.0-phase2
backend/app/seed/brand_crusor.py          KB seed: facts, segments, competitors, objectives
frontend/src/App.tsx                      3 new routes
frontend/src/components/layout/AppShell.tsx   3 new nav entries
frontend/src/components/layout/icons.tsx      IconResearch, IconStrategy
frontend/src/lib/api.ts                   researchApi, strategyApi, strategyDirectorApi
frontend/src/lib/types.ts                 52 Phase 2 types
frontend/src/pages/BrandKnowledge.tsx     facts, audiences, objectives, pending claims
scripts/acceptance.py                     14 -> 23 criteria
docs/acceptance-run.json                  regenerated evidence
```

---

## 3. Migrations

One new migration, building on Phase 1's `02cf853b2147`:

**`60ed69ffc987_phase2_research_strategy_intelligence.py`** — 33 tables total.

Tables added (11), following existing naming conventions (`snake_case`, UUID
primary keys, `created_at`/`updated_at`, `brand_profile_id` FK):

| Table | Purpose |
| --- | --- |
| `product_facts` | Editable factual claims; `dedupe_key` for idempotent seeding |
| `audience_segments` | The five founder-brief audiences |
| `audience_pain_points` | Evidence-backed pains per segment |
| `marketing_objectives` | The five approved business outcomes |
| `research_projects` | One research job: objective, categories, provider, budget |
| `research_findings` | Findings with category, confidence, evidence, verification |
| `research_sources` | Source URL, title, retrieval date, per finding |
| `marketing_strategies` | Strategy container with status and current version |
| `strategy_versions` | Immutable snapshots with `content_hash` |
| `strategy_approvals` | Append-only decision ledger with `invalidated_at` |
| `growth_experiments` | Materialised only when a strategy is approved |

Verified round-trip on both engines:

- **SQLite** — `alembic upgrade head` on an empty file: `02cf853b2147` → `60ed69ffc987`, 33 tables, exit 0. Retrospectively confirmed clean by the acceptance run's *Migrations execute against the live database* criterion.
- **PostgreSQL** — `alembic upgrade head --sql` generates valid DDL (no server available to apply it, so this is offline SQL generation, **not** a live PostgreSQL run).

Integrity is enforced in the database, not only in Python:

```sql
CHECK (verification_status != 'verified' OR source_url IS NOT NULL)
```

A finding claiming verification without a citation cannot be inserted.

---

## 4. Agent architecture

Three layers, deliberately separated so each can be tested alone.

```
┌──────────────────────────────────────────────────────────────────┐
│ Marketing Director  (app/director/strategy_*.py)                  │
│ LangGraph state machine · decides, delegates, evaluates, gates    │
└───────────────┬──────────────────────────────┬───────────────────┘
                │ delegates                    │ delegates
┌───────────────▼──────────────┐  ┌────────────▼──────────────────┐
│ Research Agent               │  │ Strategy Agent                │
│ app/agents/research.py       │  │ app/agents/strategy.py        │
│ executes a research round    │  │ generates a strategy version  │
└───────────────┬──────────────┘  └────────────┬──────────────────┘
                │                              │
┌───────────────▼──────────────┐  ┌────────────▼──────────────────┐
│ Services                     │  │ Services                      │
│ app/services/research.py     │  │ app/services/strategy.py      │
│ persistence, dedup,          │  │ versioning, hashing,          │
│ verification, provenance     │  │ brand check, approvals        │
└───────────────┬──────────────┘  └────────────┬──────────────────┘
                │ provider protocol             │ routing
┌───────────────▼──────────────┐  ┌────────────▼──────────────────┐
│ Research providers           │  │ Model gateway                 │
│ app/research/base.py         │  │ app/llm/routing.py            │
│ Deterministic / HTTP /       │  │ BudgetGuard, CostEstimate,    │
│ Disabled — one protocol      │  │ RequestDeduplicator, prices   │
└──────────────────────────────┘  └───────────────────────────────┘
```

### Pluggable research providers

One protocol, three implementations, selected by configuration:

- **`DeterministicProvider`** — the default. Returns the same findings for the
  same input, reaches no network, spends nothing. It is *not* a stub that
  fabricates: for a brand with no knowledge base it correctly returns zero
  findings and reports every category as a gap.
- **`HttpSearchProvider`** — enabled only when `RESEARCH_HTTP_ENDPOINT` is set.
  **It has never been pointed at a live search vendor in this phase**, because no
  vendor credentials were available. Its request/response contract is unit-tested
  against a mocked transport only.
- **`DisabledProvider`** — reports unavailability honestly.

`research_status(settings)` exposes which is active and whether it reaches the
network; this is what `/system/status` reports.

### Crusor knowledge base

Seeded from the founder brief and **nothing else** — `seed_knowledge_base()` is
idempotent (a second call reports zeros).

| Kind | Count | State |
| --- | --- | --- |
| Product facts | 10 | 4 verified (identity, tagline, website `https://crusor.in`, positioning) each with `source_url`; 6 `needs_confirmation` with `is_public=False` — pricing, integrations, capability, performance, social proof, availability |
| Audience segments | 5 | students/engineering students, software developers, indie hackers, startup founders, AI enthusiasts — `needs_confirmation`, `source="founder brief"` |
| Competitors | 4 | Cursor, GitHub Copilot, Windsurf, VS Code with AI extensions — **names only**, `source="founder brief (name only)"`, no positioning invented |
| Marketing objectives | 5 | `developer_acquisition` at priority 1, `target_value=None` (no invented targets) |

No feature, price, integration, benchmark or testimonial was invented. A test
(`test_brand_knowledge.py`, "no invented pricing/benchmarks") token-scans the seed
to make that a guarantee rather than an intention.

---

## 5. Workflow diagram

`GET /api/v1/director/strategy/topology` returns this graph; the diagram matches
it exactly.

```
                          START
                            │
                            ▼
                  understand_objective
                            │
                            ▼
                retrieve_brand_knowledge
                            │
                            ▼
                     decide_research ──────────────┐
                       │        │                  │
        research needed │        │ not needed       │
                        ▼        ▼                  │
                  run_research  evaluate_evidence ◄─┘   ← reuse path also judged
                        │              │
                        └──────────────┤
                                       │
                     needs_more_research? ──yes──► run_research
                                       │                (bounded loop)
                                       │ no
                                       ▼
                              generate_strategy
                                       │
                                       ▼
                               check_consistency
                                       │
                         violations? ──yes──► fail
                                       │ no
                                       ▼
                               prepare_approval
                                       │
                                       ▼
                          human_strategy_approval   ◄── interrupt()
                                       │
                         ┌─────────────┴─────────────┐
                    approve                    request_changes
                         │                           │
                         ▼                           ▼
                      finalize                   finalize
                         │                           │
                         └─────────► settle ◄────────┘
                                       │
                            fail ──────┘
                                       ▼
                                      END
```

12 nodes. Exactly **one** cycle: `evaluate_evidence → run_research`. Exactly
**one** interrupt: `human_strategy_approval`.

### Bounded execution

The loop is closed four different ways. All four are checked, not documented:

| Bound | Mechanism | Default |
| --- | --- | --- |
| `max_iterations` | One full research round consumes one iteration | 4 |
| Budget | Loop exits when `budget_remaining_usd ≤ 0` | `strategy_budget_usd = 0.06` |
| Provider capability | Only a provider that reaches the network can return something *different* on a second pass | deterministic ⇒ never loops |
| No progress | A round that returns 0 new findings ends the loop | — |

Plus `max_tool_calls = 12` and `max_wall_seconds = 180`. Loop detection is
explicit: `no_progress` and `live_provider` are both recomputed per round, so the
graph cannot spin on a provider that returns identical results. Structured failure
handling routes every failure mode (`failed`, `budget_exhausted`, `cancelled`)
through `fail → settle`, which records a `stop_reason` rather than throwing.

**There is no path from any node to a side effect.** No node publishes, sends
email, spends ad money, or touches a live social account.

---

## 6. API endpoints

23 endpoints. All require authentication; role checks are enforced server-side.

### Research (9)

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/research/capabilities` | Categories, active provider, whether it reaches the network |
| `GET` | `/research/projects` | List, filterable by brand/status |
| `POST` | `/research/projects` | **Creates and executes** a job; optional `max_cost_usd` |
| `GET` | `/research/projects/{id}` | Project + findings + sources + gaps |
| `POST` | `/research/projects/{id}/run` | Re-run / continue |
| `GET` | `/research/findings` | Filter by category, verification status, min confidence |
| `POST` | `/research/findings/{id}/verify` | **Founder only**; URL required (422 without) |
| `GET` | `/research/competitors` | Brand-scoped (nested under `/brand/{brand_id}/competitors` for writes) |
| — | competitor create/update | Nested under `/brand/{brand_id}/competitors` |

### Strategy (11)

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/strategy/capabilities` | Categories, objectives, models, budget in USD and INR |
| `POST` | `/strategy/generate` | Generates a strategy + v1 |
| `GET` | `/strategy` | List |
| `GET` | `/strategy/{id}` | Strategy + current version + approval + history |
| `POST` | `/strategy/{id}/versions` | Edit ⇒ v(n+1), **invalidates approval** |
| `GET` | `/strategy/{id}/versions/{vid}` | Exact snapshot |
| `GET` | `/strategy/{id}/diff?left&right` | Field-level version diff |
| `POST` | `/strategy/{id}/decision` | **Founder only** approve / request changes |
| `GET` | `/strategy/{id}/execution-status` | "Planning only" scope |
| `GET` | `/strategy/{id}/approval-history` | Append-only ledger with invalidation reasons |

### Director (7)

`POST /director/strategy/start` · `GET .../estimate` · `GET .../topology` ·
`GET .../workflows` · `GET .../workflows/{id}` · `POST .../workflows/{id}/decision` ·
`DELETE .../workflows/{id}`

**Authorization proven by test, not by inspection:**

- marketer → `403` on strategy approve and on director decision
- viewer → `403` on research start and strategy generate
- non-URL verification payload → `422`
- unknown category / objective → `422`
- unknown brand → `404`

---

## 7. Frontend pages

Four surfaces, built on the existing design system (`Card`, `Badge`, `Banner`,
`Tabs`, `EmptyState`, `KeyValue`, `Mono`). No new dependency, no new design
language, no second icon set.

| Route | Page | Contents |
| --- | --- | --- |
| `/research` | **Research Centre** | Start a job with objective + categories; findings with category, verification, confidence, evidence, citation, retrieval date; filters for category / verification / min confidence; founder-only "verify with source"; jobs tab with findings/sources/gaps/provider/spend/tokens/iterations; competitors tab with evidence counts and an explicit warning when no positioning is recorded |
| `/strategy` | **Strategy Studio** | Generate from objective + business objectives (optionally scoped to a research project), budget shown in USD **and** INR; version list; overview (positioning, objective alignment, segments, differentiation); full plan (KPIs flagged vanity vs acquisition, measurement disclaimer, pillars, concepts, per-platform strategy, weekly objectives, lead gen, experiments, open questions); versions with hash and field-level diff; approval panel with history and invalidation reasons |
| `/strategy-director` | **Marketing Director** | Objective input, business-objective toggles, budget; **pre-flight cost estimate rendered before running** with its assumptions; live status (phase stepper, iterations/rounds/evidence/spend/findings/verified/version/checkpoints); agent activity timeline from the real graph trace; research rounds with per-round gaps; approval prompt only when the workflow is genuinely interrupted, with an explanation for non-founders; sidebar listing unconfirmed claims excluded from the strategy |
| `/brand` | **Brand Knowledge** (extended) | Product facts split confirmed vs needs-confirmation; competitors with evidence counts; audience segments; the five business objectives; pending-confirmation claims list |

Nav gained three entries: Marketing Director, Research Centre, Strategy Studio,
with `IconResearch` and `IconStrategy` added to the inline icon set.

---

## 8. Model routing configuration

Models are **configuration**, not code. No agent names a model.

```bash
LLM_REASONING_PROVIDER=openrouter     LLM_REASONING_MODEL=deepseek-v4.1-flash
LLM_AUXILIARY_PROVIDER=openrouter     LLM_AUXILIARY_MODEL=qwen-3.7-flash
LLM_TASK_ROUTING_JSON={}              # per-task overrides
LLM_PRICING_JSON={}                   # per-model price overrides
```

| Class | Tasks | Model |
| --- | --- | --- |
| Cheap / mechanical | `classification`, `extraction`, `summarisation`, `formatting`, `deduplication`, `tagging` | Qwen 3.7 Flash (auxiliary) |
| Reasoning | `competitor_analysis`, `positioning`, `strategic_decision`, `strategy`, `research_synthesis`, `brand_consistency`, `evaluation` | DeepSeek V4.1 Flash (reasoning) |

Auxiliary usage is tracked **separately** from reasoning usage in
`app/services/usage.py`.

### Honest limitation: pricing is unverified

Neither `deepseek-v4.1-flash` nor `qwen-3.7-flash` has a price this repository can
verify, so both are reported as `price_verified: false` and
`/system/status` lists them under `unverified_models`. When a model has no known
price, the router substitutes a **deliberately expensive** placeholder
(`$0.004 / 1k` input, `$0.016 / 1k` output) so that an unknown model fails a
budget check *before* it can spend money. Cost figures in this report are
therefore **estimates under placeholder pricing, not measured bills.**

---

## 9. Cost control implementation

### Enforcement is pre-call

```
request → ModelRouter.resolve(task) → CostEstimate
        → BudgetGuard.check(estimate)   ← refuses here if over
        → RequestDeduplicator           ← refuses duplicate here
        → provider call
        → actual usage recorded (prompt tokens, completion tokens, cost)
```

`BudgetGuard.check()` takes a single `CostEstimate` and tracks three ceilings per
run: `max_cost_usd`, `max_tokens`, `max_tool_calls`. `remaining_usd` is exposed so
the Director can stop early and *report* why, instead of dying mid-call.

### The ₹5 question, stated plainly

The spec asked for a default budget around ₹5 for a simple task, and asked for
recognition that ₹5 is not achievable for complex research. Both are honoured:

- `strategy_budget_usd = 0.06`, `usd_inr_rate = 83.0` ⇒ **$0.06 ≈ ₹4.98**.
- A measured estimate for a simple objective: **$0.0195 ≈ ₹1.62**, within budget.
- `CostEstimate` carries an explicit assumption:
  *"A complex research task can exceed a ₹5-equivalent envelope; the run stops
  rather than overspending, and reports what it did not do."*
- `research_budget_usd = 0.12` is separate and larger, because research needs more
  calls than a single strategy task.

The estimate is returned **before** execution via
`GET /director/strategy/estimate` and rendered in the Marketing Director page
before the run button is usable.

### Other guarantees

- **Duplicate paid requests** — `RequestDeduplicator` keys on a logical key plus a
  SHA-256 of the prompt, so a retry loop cannot re-bill an identical prompt.
- **Cached research reuse** — the Director reuses existing findings when enough
  verified evidence exists. Observed live: 72 existing findings reused with zero
  new spend (`§12`).
- **Cancellation** — run state accepts `cancelled`, which routes to `fail → settle`.
- **Auxiliary usage** tracked separately from reasoning usage.
- **Never silently exceeded** — verified by test at a zero budget: a run with
  `max_cost_usd: 0.0` spends `$0.0000`.

---

## 10. Test results

```
$ cd backend && pytest tests/ -q
353 passed, 0 failed, 0 skipped, 0 errors          (junit: /tmp/j9.xml)
```

Phase 2 contributes 181 of those:

| File | Tests | Covers |
| --- | --- | --- |
| `test_model_routing.py` | 33 | Routing, cheap vs reasoning classes, pricing, estimates, budget guard, dedup, timeouts |
| `test_research_providers.py` | 23 | All three providers, status reporting, disabled path, HTTP contract (mocked transport) |
| `test_research_persistence.py` | 19 | Findings/sources persistence, dedup, verification, invalid/missing sources, competitor dedup |
| `test_strategy_versioning.py` | 22 | Immutability, `content_hash` recomputation, diff, approval binding, invalidation, tamper detection |
| `test_strategy_agent.py` | 16 | Generation, brand consistency validation, objective mapping, vanity-KPI rejection |
| `test_strategy_workflow.py` | 19 | Full graph, both research branches, evidence verdict, approval gate, resumption, missing brand, blank objective, topology |
| `test_research_agent.py` | 11 | Agent execution, iteration counting, limits, budget exhaustion, duplicate-task prevention |
| `test_strategy_api.py` | 19 | All strategy endpoints, authz, 403/404/422 paths |
| `test_research_api.py` | 10 | All research endpoints, authz, provenance via HTTP |
| `test_brand_knowledge.py` | 8 | Seed idempotency, no invented pricing/benchmarks, unverified flags |

**Mocking policy:** no test reaches the network. The research provider is the
deterministic one; the HTTP provider is exercised against a mocked transport. No
provider credits were spent at any point in this phase. Consequently these tests
prove the *logic*, not that a real vendor integration works — see §11.

### Bug found by acceptance, not by unit tests

Running the new acceptance criteria surfaced a genuine defect:

> When `decide_research` concluded existing findings were sufficient, the graph
> routed **straight to `generate_strategy`**, bypassing `evaluate_evidence`. The
> result was a strategy approved on evidence that had never been assessed, and a
> Director page showing an empty evidence panel (`findings_summary = {}`,
> `evidence_verdict = null`) while the run reported success.

All 18 workflow tests passed while this was live. The acceptance harness caught it
because it asserted on the *outcome* of a realistic run rather than on node-level
behaviour.

Fixed by routing the reuse branch through `evaluate_evidence`, so both branches of
the research decision pass the same judgement; `evaluate_evidence` now describes
the evidence it is judging when no round ran. Covered two ways:

1. a workflow test asserting the reuse path records `evidence_evaluated` with
   `reused_existing_findings = true`;
2. a routing-contract test asserting **neither** branch can reach
   `generate_strategy` directly.

This is the clearest evidence for why the acceptance harness exists separately
from the unit suite.

### Frontend checks

```
$ npx tsc --noEmit      → exit 0, no diagnostics
$ npm run build         → exit 0
  dist/assets/index-B4TmxVNb.js   332.31 kB │ gzip: 94.00 kB
  dist/assets/index-dirCqeBQ.css   32.05 kB │ gzip:  6.48 kB
```

Bundle assertions confirm the four surfaces are present. Preview sanity: SPA route
`/research` → `200`, `/strategy` → `200`, API proxy through Vite → `200`,
and a `Host: <preview-id>.e2b.app` request → `200` (no host/origin rejection).

No headless browser was available, so **no visual or interactive verification was
performed**. Type-checking and bundle contents are not a substitute.

---

## 11. Phase 1 regression results

`scripts/acceptance.py` was extended from 14 to 23 criteria. **All 14 Phase 1
criteria still pass, unmodified**, alongside the 9 new Phase 2 criteria:

```
$ .venv/bin/python scripts/acceptance.py
RESULT 23/23 criteria passed

  PASS   1. Backend starts
  PASS   1b. /system/status reports every integration honestly
  PASS   2. Frontend builds and serves a real SPA
  PASS   3. Migrations execute against the live database
  PASS   4. Brand profile create + retrieve
  PASS   5. Director initialises a stateful workflow
  PASS   6. All six agents invocable through structured interfaces
  PASS   7. Workflow state persists across restarts
  PASS   8. Content create + immutable version history
  PASS   9. Approval state machine + invalidation on edit
  PASS  10. Unapproved publication is blocked (and approved is not faked)
  PASS  11. Model usage and cost are recordable
  PASS  12. Automated test suite passes
  PASS  13. Architecture documentation is complete
  PASS  14. Research jobs create and execute
  PASS  15. Findings carry source metadata
  PASS  16. Phase 2 schema is migrated (not create_all)
  PASS  17. Strategy generation works
  PASS  18. Versioning + approval invalidation
  PASS  19. Director orchestrates agents and gates on approval
  PASS  20. Usage, cost and budget enforcement
  PASS  21. Execution limits are enforced
  PASS  22. Phase 2 never publishes, sends or spends
```

Machine-readable evidence: `docs/acceptance-run.json`.

### Live HTTP verification (not mocked)

Against a real uvicorn server on a freshly migrated database with the seeded
Crusor brand — **29/29 checks passed**, including:

- 18 findings / 10 sources persisted by a real research run
- 4 verified findings, **all** with a source URL and retrieval date
- 5 uncited findings, **none** claiming verification
- strategy v1 DRAFT, mapping to `developer_acquisition`, 4 KPIs containing an
  acquisition metric
- pre-flight estimate `$0.0195` within a `$0.06` budget
- Director reached `awaiting_approval` with a real interrupt, then approved
- edit created v2 and invalidated the v1 approval ("superseded by version 2")
- unverified pricing disclosed for both models

### Phase 1 behaviour deliberately preserved

- Publishing still reports `unavailable` — Phase 2 changed nothing about it.
- The Phase 1 director graph and all its nodes are untouched.
- Approval remains founder-only and version-bound.

---

## 12. Known limitations

Stated plainly, because the alternative is a false claim.

1. **No live research vendor has been contacted.** The HTTP provider is
   contract-tested against a mock only. Whether it works against a real search API
   is **unknown**. All research findings in every demonstration above came from the
   deterministic provider.
2. **No live LLM has been called.** `LLM_PROVIDER=none` throughout. All strategies
   were produced deterministically. Token counts in demonstration runs are `0`.
   Whether DeepSeek V4.1 Flash or Qwen 3.7 Flash behave as the routing table
   assumes is **unverified**.
3. **Model pricing is a placeholder.** Both models report `price_verified: false`.
   All cost figures are estimates under deliberately conservative placeholder
   prices, not measured bills.
4. **PostgreSQL was never actually run.** Only offline `--sql` DDL generation was
   verified. All runtime evidence is SQLite.
5. **The frontend has never been visually verified.** No browser was available.
   Type-checks and bundle assertions only.
6. **The research quality of the deterministic provider is bounded by the knowledge
   base.** It cannot discover anything new; it structures and reports what is
   already known, and honestly reports gaps.
7. **`strategy_max_wall_seconds` is checked between nodes,** not mid-call. A single
   long provider call can exceed it.
8. **Cost estimates use a character-ratio token estimate** (`3.6 chars/token`).
   Real provider tokenisation will differ.
9. **The Director reuses existing findings aggressively.** With 72 findings present
   it skips research entirely. This is correct for cost but means a stale evidence
   base can persist. The verdict is still computed and surfaced; there is no
   automatic staleness expiry.
10. **No concurrency control on a strategy.** Two simultaneous edits could race;
    version numbers are computed in application code, not by a database sequence.

---

## 13. Security risks

| Risk | Severity | Mitigation present | Residual |
| --- | --- | --- | --- |
| Prompt injection via research content | **High** | Research text is data, never instructions; the deterministic provider is closed | A live search provider could return adversarial text. Not tested against hostile input. |
| Hallucinated citations | **High** | DB `CHECK`: `verified` requires `source_url`; confidence and verification are separate fields | A *real but irrelevant* URL could be attached. Only a founder's verify action can promote a finding, and it is audited. |
| Approval bypass | Medium | Founder-only, version-bound, append-only ledger, `content_hash` **recomputed from payload** on read (fails closed) | — |
| Budget bypass | Medium | Pre-call `BudgetGuard` on cost, tokens and tool calls; duplicate suppression | Placeholder pricing means a real bill may differ from the estimate. |
| Credential leakage | Medium | Secrets from env only; no key ever logged or persisted | Dev JWT secret in `/tmp/env.sh` is a local placeholder, not a production value. |
| Unauthorised strategy creation | Low | Role checks proven by test (viewer → 403) | — |
| Data exfiltration via research URLs | Low | URLs stored as references, never fetched by the deterministic provider | The HTTP provider would fetch configured endpoints. |
| Denial of service | Low | `max_iterations`, `max_tool_calls`, `max_wall_seconds`, budget | Wall-clock is per-node (see §12.7). |

**Nothing in Phase 2 can publish, send, spend, or alter a live account.** This was
verified three ways: five publish/execute/apply routes return 404/405; the
execution-status endpoint scopes approval to "Planning only"; `/system/status`
continues to report publishing as `unavailable`.

---

## 14. Git commits

| Commit | Milestone |
| --- | --- |
| `43d633e` | **Phase 1 baseline** — 14/14 acceptance, 160 tests |
| `7cc23e1` | M1–M3: Phase 2 schema, migration, model routing, budgets |
| `8d81b36` | M4–M5: research providers, research agent |
| `cc523f6` | M6: strategy service, versioning, approval, strategy agent |
| `569ba36` | M8: strategy orchestrator (12-node graph) |
| `c58ba0f` | M9: 23 API endpoints + Crusor knowledge base seed |
| `fc5cb5d` | M10: four frontend surfaces |
| **`dfa7e44`** | **M11 — Phase 2 head:** evidence-evaluation fix, Phase 2 acceptance criteria, 353 tests |

52 files changed, +15,294 / −68.

---

## 15. Verdict

Phase 2 delivers research, strategy and orchestration with approval and cost
control that are enforced rather than described.

What is **proven by execution** in this environment: the full research → findings →
strategy → version → approval → invalidation lifecycle; budget and iteration limits
that stop runs; a database constraint that refuses an uncited "verified" finding;
role-based authorization returning the right status codes; 353 tests; 23/23
acceptance criteria; a clean frontend type-check and production build.

What is **not** proven and must not be claimed: that any real search vendor works,
that any real LLM produces usable output, that the prices are accurate, that
PostgreSQL runs, that the UI looks right, or that this is production-ready.

The default ₹5-equivalent budget funds a simple task (estimated ₹1.62) but is
explicitly **not** assumed sufficient for complex competitive research — the system
says so before it starts, and stops rather than overspending.
