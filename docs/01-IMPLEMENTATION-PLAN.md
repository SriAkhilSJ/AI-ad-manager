# B. Proposed Implementation Plan

**Scope:** Phase 1 only — Foundation & Architecture. Phase 2 (real publishing,
real OAuth, live model routing, media generation) is explicitly **not** built.

---

## B.1 Guiding principles

1. **The approval system is the product.** Everything else exists to feed it or
   consume it. It must be impossible to publish without a valid approval bound to
   an exact content version.
2. **Structured over conversational.** Every agent boundary is a Pydantic
   contract. No agent reads another agent's prose.
3. **No fabrication.** Unconfigured integrations report `unavailable`; they never
   return plausible fake data. No invented metrics, campaigns, testimonials or
   product claims (per brand rules).
4. **Portable, production-shaped.** Postgres in production, SQLite in sandbox
   tests, one model layer for both.
5. **Durable workflows.** LangGraph state lives in the database via a custom
   `BaseCheckpointSaver`, so a process restart does not lose a workflow.

## B.2 Delivery milestones (incremental, each verifiable)

| # | Milestone | Exit check |
| --- | --- | --- |
| M0 | Repo scaffold, tooling, env config, docs skeleton | `make check` runs |
| M1 | Settings, logging, error envelope, GUID/JSONB types, DB session | `pytest tests/db` green |
| M2 | Full schema + Alembic migration (up + down) | `alembic upgrade head` on SQLite **and** SQL generation for Postgres |
| M3 | Auth (JWT + RBAC), audit log, idempotency store, rate limiting | `pytest tests/api/test_auth.py` |
| M4 | Brand knowledge base: model, seeding, CRUD API | brand create/read test |
| M5 | Marketing goals, competitor profiles, content pillars | CRUD tests |
| M6 | Campaigns, content items, **content versions** | versioning test |
| M7 | **Approval state machine** + approval records + invalidation + idempotency | state transition + bypass tests |
| M8 | Social accounts (token isolation), scheduled/published posts, analytics snapshots | API tests |
| M9 | LLM gateway, model routing, usage/cost tracking, agent run ledger | usage recording test |
| M10 | Agent base interface + 6 agents + registry | agent contract tests |
| M11 | LangGraph Marketing Director: planner, supervisor loop, HITL interrupt, checkpointer | workflow persistence + resume test |
| M12 | Async task layer (Celery + inline dispatcher + status) | dispatcher tests |
| M13 | Versioned API surface completion + OpenAPI | endpoint tests |
| M14 | Frontend: shell, 10 routes, design system, real API client, empty states | build + smoke test |
| M15 | Full test suite, acceptance script, architecture docs, README | `make verify` |

## B.3 Component architecture

```
                        ┌──────────────────────────────────────────┐
                        │  FRONTEND — "CRUSOR MARKETING HQ"        │
                        │  React 18 · TS · Tailwind · Vite         │
                        │  Overview · Director · Campaigns ·       │
                        │  Calendar · Creative · Approvals ·       │
                        │  Social · Analytics · Brand · Settings   │
                        └───────────────┬──────────────────────────┘
                                        │  REST /api/v1 (JWT)
                        ┌───────────────▼──────────────────────────┐
   API layer            │ routers/*  (thin, validated, RBAC)       │
                        ├──────────────────────────────────────────┤
   Domain services      │ ApprovalService · ContentService ·       │
                        │ CampaignService · BrandService ·         │
                        │ AnalyticsService · UsageService          │
                        ├──────────────────────────────────────────┤
   Agent layer          │ base.Agent (schema in/out, budgets,      │
                        │ retries, logging, usage) · registry ·    │
                        │ research/strategy/content/creative/       │
                        │ brand_guardian/growth_analyst            │
                        ├──────────────────────────────────────────┤
   Orchestration        │ director.graph (LangGraph StateGraph)    │
                        │ director.nodes · director.state          │
                        │ director.checkpointer (DB-backed)        │
                        ├──────────────────────────────────────────┤
   Infrastructure       │ llm gateway (providers, router, budget)  │
                        │ storage (local/S3) · tasks (celery/inline)│
                        │ db (async SQLAlchemy) · cache (memory/redis)│
                        └──────────────────────────────────────────┘
                                        │
                   PostgreSQL (+pgvector) · Redis · S3
```

### Marketing Director graph (non-sequential, supervisor-style)

```
        ┌────────────┐
        │  intake    │  load brand profile + goals + memory
        └─────┬──────┘
        ┌─────▼──────┐         (LLM structured plan, degrades to
        │   plan     │          deterministic rule-based planner)
        └─────┬──────┘
        ┌─────▼──────┐◄────────────────┐
        │  dispatch  │                 │  re-plan / next task
        └─────┬──────┘                 │
              │ conditional routing on next pending task
     ┌────────┼────────┬────────┬──────────┬─────────────┐
     ▼        ▼        ▼        ▼          ▼             ▼
 research  strategy  content  creative  brand_guardian  growth_analyst
     └────────┴────────┴───────┴──────────┴─────────────┘
              │  each writes {task result, usage, trace span}
        ┌─────▼──────┐
        │  review    │  QA gate: brand guardian verdict + schema checks
        └─────┬──────┘
        ┌─────▼──────┐
        │ interrupt  │  ⛔ founder approval (LangGraph interrupt / HITL)
        └─────┬──────┘
        ┌─────▼──────┐
        │ finalize   │  persist artifacts, approval request, memory write
        └────────────┘
```

Routing is conditional on graph state, agent outputs can send work back to
`dispatch`, and the graph **pauses** at an approval interrupt that resumes only
via `Command(resume=...)` — it is not a linear chain.

## B.4 Approval state machine

```
                    ┌─────────────── CANCELLED ◄────────────┐
                    │                                       │
  (create) ──► DRAFT ──submit──► IN_REVIEW ──approve──► APPROVED ──schedule──► SCHEDULED
                 ▲                  │  │                  │                       │
                 │                  │  └──reject──► REJECTED│                      ▼
                 │                  │                     │                 PUBLISHING
                 │                  └──request_changes──► │                      │
                 │                        CHANGES_REQUESTED                      ├──► PUBLISHED
                 │                              │         │                      └──► FAILED
                 └──────── edit (invalidate) ───┴─────────┘
```

Enforced invariants (tested):

1. `PUBLISHED` is unreachable from any state except `PUBLISHING`.
2. `PUBLISHING` requires a non-revoked `approval_records` row for the **exact
   current `content_version_id`** of the item.
3. Any content edit creates a **new version** and sets
   `status = DRAFT`, revoking prior approvals (`invalidated_at`).
4. Replacing a creative asset on a version likewise revokes approvals.
5. Every transition writes an `approval_records`/audit row with actor, from, to,
   reason, and timestamp. No transition may skip audit.
6. Repeated `submit`/`approve`/`publish` calls are idempotent — same target state
   returns the existing row, not a duplicate.
7. There is **no** code path that sets `PUBLISHED` directly; the only writer is
   `publish_service.publish()`, which validates the approval record inside the
   same DB transaction as the status change.

## B.5 Non-goals for Phase 1 (explicit)

- Real OAuth handshakes with Meta/LinkedIn/Google/X/Reddit.
- Real publishing to any network.
- Real image/video generation and S3 upload of generated media.
- Real paid-tier model routing / prompt optimisation / fine-tuning.
- Multitenant billing, team invitations beyond a two-role RBAC.
- Phase 2 optimisation loops and autonomous re-planning from live analytics.

Anything in this list that appears in the UI is rendered as an explicit
`Not configured` / empty state, never as fabricated data.

## B.6 Definition of done

Exactly the 13 acceptance criteria in the brief, each mapped to an automated
check in `scripts/acceptance.py` and/or `tests/` (mapping table in
`docs/04-ACCEPTANCE-EVIDENCE.md`).
