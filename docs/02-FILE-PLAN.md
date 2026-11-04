# C. File-Level Implementation Plan

Legend: `[ ]` planned · `[x]` implemented in Phase 1

## C.1 Root

```
[ ] .gitignore                     python/node/env/artifacts
[ ] .env.example                   every secret, documented, no real values
[ ] Makefile                       install/db/migrate/test/lint/dev/verify
[ ] docker-compose.yml             postgres(pgvector)+redis+api+worker+web (deployment artifact)
[ ] README.md                      preserved header + quickstart + architecture summary
[ ] pytest.ini / pyproject.toml    test config, ruff config
```

## C.2 Backend — `backend/`

```
[ ] backend/Dockerfile
[ ] backend/requirements.txt
[ ] backend/alembic.ini
[ ] backend/alembic/env.py                     async engine, dialect-aware
[ ] backend/alembic/versions/0001_initial.py   full schema, upgrade + downgrade
[ ] backend/app/__init__.py
[ ] backend/app/main.py                        FastAPI app factory, lifespan, handlers
[ ] backend/app/core/config.py                 pydantic-settings, all secrets env-only
[ ] backend/app/core/logging.py                structlog, secret redaction processor
[ ] backend/app/core/security.py               JWT encode/decode, password hashing, RBAC deps
[ ] backend/app/core/errors.py                 structured error envelope + exception types
[ ] backend/app/core/rate_limit.py             sliding-window limiter (memory/redis)
[ ] backend/app/core/idempotency.py            Idempotency-Key store, replay + conflict
[ ] backend/app/core/audit.py                  audit log writer
[ ] backend/app/db/base.py                     Declarative base, GUID/JSONB/Vector types, mixins
[ ] backend/app/db/session.py                  async engine/sessionmaker, transaction helper
[ ] backend/app/models/*.py                    (16 tables, see C.3)
[ ] backend/app/schemas/*.py                   Pydantic request/response contracts
[ ] backend/app/services/*.py                  domain logic (approval, content, campaign, …)
[ ] backend/app/agents/base.py                 Agent protocol, budgets, retries, usage
[ ] backend/app/agents/registry.py             name → agent resolution
[ ] backend/app/agents/{research,strategy,content,creative,brand_guardian,growth_analyst}.py
[ ] backend/app/director/state.py              TypedDict graph state
[ ] backend/app/director/nodes.py              intake/plan/dispatch/agent/review/approval/finalize
[ ] backend/app/director/graph.py              StateGraph wiring + conditional edges
[ ] backend/app/director/checkpointer.py       DB-backed BaseCheckpointSaver
[ ] backend/app/director/runtime.py            build + start + resume + status
[ ] backend/app/llm/base.py                    provider protocol, structured call
[ ] backend/app/llm/providers.py               openai/anthropic/openrouter/mock-unavailable
[ ] backend/app/llm/gateway.py                 routing, budget guard, usage recording
[ ] backend/app/storage/base.py                StorageBackend protocol + availability
[ ] backend/app/storage/local.py / s3.py
[ ] backend/app/tasks/dispatcher.py            celery | inline | unavailable
[ ] backend/app/tasks/celery_app.py
[ ] backend/app/tasks/jobs.py                  agent run, analytics refresh
[ ] backend/app/api/v1/router.py               aggregates routers
[ ] backend/app/api/v1/{auth,brand,goals,competitors,pillars,campaigns,content,versions,
                         creative,approvals,social,analytics,usage,director,agents,
                         system,campaigns}.py
[ ] backend/app/seed/brand_crusor.py           Crusor brand profile + pillars (no invented claims)
[ ] backend/app/seed/demo.py                   optional dev seed (clearly labelled)
```

## C.3 Tables (16 required + 3 support)

| Table | Key columns | Notes |
| --- | --- | --- |
| `brand_profiles` | id, organization_id, name, product, tagline, positioning, audience[], personality[], visual{}, do_not_claim[], version | JSONB config, updateable without code change |
| `marketing_goals` | id, brand_profile_id, objective, kpi, target_value, horizon, status | FK cascade |
| `competitor_profiles` | id, brand_profile_id, name, url, positioning, notes | no scraped claims |
| `campaigns` | id, brand_profile_id, name, objective, status, start/end, plan JSONB | |
| `content_pillars` | id, brand_profile_id, name, description, weight | |
| `content_items` | id, campaign_id, pillar_id, platform, format, title, status, current_version_id | status = state machine |
| `content_versions` | id, content_item_id, version_number, body, media_refs, created_by, change_note | unique(item, version_number) |
| `creative_assets` | id, content_item_id, content_version_id, kind, storage_key, mime, checksum, status | checksum drives invalidation |
| `approval_records` | id, content_item_id, content_version_id, action, actor_id, reason, created_at | append-only |
| `social_accounts` | id, brand_profile_id, platform, handle, status, token_ref (vault ref, **not** token) | tokens never in this table |
| `scheduled_posts` | id, content_item_id, social_account_id, scheduled_for, status, approval_record_id | |
| `published_posts` | id, content_item_id, social_account_id, external_id, published_at, url | |
| `analytics_snapshots` | id, published_post_id, platform, captured_at, metrics JSONB | empty until real data |
| `agent_runs` | id, agent, status, input/output JSONB, started/finished, error, workflow_thread_id | execution ledger |
| `model_usage` | id, agent_run_id, provider, model, prompt_tokens, completion_tokens, cost_usd, attempt | unique(agent_run_id, attempt) |
| `marketing_memory` | id, brand_profile_id, kind, content, embedding (vector/json), source_ref | pgvector on PG |
| `users` | id, email, hashed_password, role, is_active | auth |
| `audit_log` | id, actor_id, action, entity_type, entity_id, before/after JSONB, ip | append-only |
| `idempotency_keys` | key, user_id, endpoint, request_hash, response JSONB, status | replay |
| `workflow_checkpoints` + `workflow_writes` | thread_id, checkpoint_id, parent_id, state JSONB, metadata | LangGraph saver |

## C.4 Frontend — `frontend/`

```
[ ] package.json, tsconfig, vite.config.ts, tailwind.config, postcss.config
[ ] index.html
[ ] src/main.tsx, src/App.tsx (router + auth guard)
[ ] src/styles/index.css               design tokens, scrollbar, focus rings
[ ] src/lib/api.ts                     typed fetch client, error envelope, idempotency header
[ ] src/lib/types.ts                   mirrors backend schemas
[ ] src/lib/format.ts
[ ] src/hooks/useApi.ts                loading/error/data, no fake fallbacks
[ ] src/components/ui/{Button,Card,Badge,Input,Select,Modal,Table,Tabs,
                        Skeleton,EmptyState,StatusBadge,Toast,Drawer,Tooltip}.tsx
[ ] src/components/layout/{AppShell,Sidebar,Topbar,PageHeader}.tsx
[ ] src/components/{ApprovalCard,ContentVersionList,CampaignCard,
                    AgentRunTimeline,UsageChart,MetricCard,PlatformIcon}.tsx
[ ] src/pages/{Overview,Director,Campaigns,ContentCalendar,CreativeStudio,
               ApprovalCentre,SocialAccounts,Analytics,BrandKnowledge,
               Settings,Login}.tsx
[ ] src/pages/campaigns/CampaignDetail.tsx
[ ] src/pages/content/ContentDetail.tsx
```

## C.5 Tests — `backend/tests/`

```
[ ] conftest.py                       temp SQLite DB, app client, auth fixtures
[ ] test_models.py                    schema/constraints/nullability/cascade
[ ] test_migrations.py                upgrade head then downgrade base on SQLite
[ ] test_auth.py                      401/403, RBAC, token tampering
[ ] test_brand.py                     create + retrieve brand profile
[ ] test_campaigns_content.py         campaign + content + versioning
[ ] test_state_machine.py             all legal transitions + illegal ones
[ ] test_approval_invalidation.py     edit / asset replace revokes approval
[ ] test_publish_blocked.py           ⚠ CRITICAL: DRAFT + no approval → rejected
[ ] test_idempotency.py               repeated approve/publish
[ ] test_agents.py                    schema in/out, budgets, usage, error mapping
[ ] test_director_workflow.py         start workflow, checkpoint, resume, HITL
[ ] test_checkpointer_persistence.py  state survives saver re-instantiation
[ ] test_usage_costs.py               aggregated token/cost accounting
[ ] test_system.py                    health + integration availability status
```

## C.6 Scripts & docs

```
[ ] scripts/acceptance.py             drives all 13 acceptance criteria against a live app
[ ] docs/00-ARCHITECTURE-AUDIT.md     [x]
[ ] docs/01-IMPLEMENTATION-PLAN.md    [x]
[ ] docs/02-FILE-PLAN.md              [x] (this file)
[ ] docs/03-RISKS-AND-DEPENDENCIES.md [x]
[ ] docs/04-ARCHITECTURE.md           deep dive: agents, graph, approval, security
[ ] docs/05-ACCEPTANCE-EVIDENCE.md    criterion → test mapping + run output
[ ] docs/06-API.md                    endpoint reference
```

## C.7 Build order & why

Schema → state machine → services → API → agents → graph → frontend. The state
machine is built before any API surface that could mutate content, so no endpoint
can ever exist without its invariant enforcement.
