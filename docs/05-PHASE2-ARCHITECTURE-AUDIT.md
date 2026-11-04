# Phase 2 — Architecture Audit (pre-implementation)

Audit of the Phase 1 baseline (`43d633e`) before any Phase 2 code was written.
Everything below was read, not assumed. Nothing in Phase 1 is rebuilt; Phase 2
extends the seams that already exist.

## 1. Baseline inventory

| Area | State at `43d633e` |
| --- | --- |
| Tables | 21 (16 required + `users`, `audit_log`, `idempotency_keys`, `api_keys`, `workflow_checkpoints`, `workflow_writes`) |
| Migrations | one: `02cf853b2147_initial_schema` (head) |
| API routes | 57 under `/api/v1`, grouped in 10 routers |
| Services | 10 (`brand`, `campaign`, `content`, `approval`, `publishing`, `creative`, `social`, `analytics`, `usage`, `editor`) |
| Agents | 6: `research`, `strategy`, `content`, `creative`, `brand_guardian`, `growth_analyst` |
| Director graph | 13 nodes, supervisor dispatch loop, one `interrupt` at `human_approval`, database checkpointer |
| LLM | provider-independent gateway, 3 HTTP providers, router keyed by `agent_name`, monthly USD budget, `model_usage` ledger |
| Tests | 160 passing |
| Frontend | 10 nav routes + 3 detail routes, typed API client, dark design system |

## 2. What already exists that Phase 2 must reuse (not duplicate)

**Agent contract.** `BaseAgent` already provides exactly what Phase 2 needs:
typed `input_schema`/`output_schema`, `max_attempts`/`timeout_seconds`/`max_tokens`,
an `AgentRun` ledger row with a per-attempt `trace`, `try_llm()` with graceful
degradation to a deterministic path, and typed errors (`AgentTimeout`,
`AgentBudgetExceeded`, `AgentInputInvalid`, `AgentOutputInvalid`). **Phase 2 adds
no second agent framework.** New agents subclass `BaseAgent`.

**`AgentResult` already carries provenance.** `generation_mode`
(`llm` / `deterministic`), `data_source`, `confidence`, `warnings`,
`requires_human_review`. This is precisely the honesty contract Phase 2's
"never fabricate citations" requirement maps onto — research findings extend it
with per-finding source metadata rather than inventing a parallel scheme.

**Gateway routing is keyed by `agent_name` only.** `ModelRouter.resolve(agent_name)`
reads `LLM_ROUTING_JSON[agent_name]` then `LLM_ROUTING_JSON["default"]`. It has no
concept of a *task class* (cheap vs. reasoning), no auxiliary-model separation, no
pre-flight cost estimate, and no cancellation. Phase 2 must extend this — additively.

**Budget enforcement exists but is coarse.** `LLMGateway.structured()` checks
`usage.month_spend() >= llm_monthly_budget_usd` before each call and the agent
checks a per-run token cap. There is no per-workflow/per-task cost ceiling, no
estimate-before-execute, and no "stop when exhausted" mid-run behaviour.

**Approval is content-only.** `ApprovalRecord` has a non-nullable FK to
`content_items` and `content_versions`. Phase 2 needs approval over *strategies*.
Approach: a separate `strategy_approvals` table with the **same semantics** — exact
version binding, append-only, `invalidated_at`, founder-only, hash-matched. The
content path is untouched, so Phase 1 enforcement cannot regress.

**The Director graph is content-shaped.** `director/nodes.py::plan()` hardcodes a
content task DAG; `RESULT_FIELD` maps the 5 delegatable agents to content fields.
Phase 2 needs a *research → strategy* workflow. Rather than mutate the proven
content graph, Phase 2 adds a **second compiled graph** (`research_strategy`)
sharing `DirectorNodes` plumbing, the checkpointer and the runtime pattern.

**Brand knowledge already exists** as `BrandProfile` JSON columns + `MarketingGoal`,
`CompetitorProfile`, `ContentPillar`, with version bumping and audit. Phase 2 adds
*structured* audiences and *product facts with verification status* alongside,
without rewriting the existing table.

## 3. Gaps Phase 2 must close

| # | Gap | Consequence if not closed |
| --- | --- | --- |
| G1 | No research persistence | Findings cannot be cited, filtered or audited |
| G2 | No research provider seam | Conclusions are brand-only reasoning, not research |
| G3 | No source/verification model | "Never fabricate citations" is unenforceable |
| G4 | No strategy entity or versions | A strategy cannot be approved or diffed |
| G5 | Router has no task-class or auxiliary tier | Cheap/simple calls would burn reasoning-model budget |
| G6 | No pre-flight estimate or per-task ceiling | ₹5 target cannot be honoured or reported |
| G7 | No loop/duplicate-task detection beyond max_iterations | Research asks could repeat forever |
| G8 | `ApprovalRecord` is content-bound | Strategy approval has nowhere to live |
| G9 | No unverified-fact workflow | Unknown info would be silently omitted or invented |
| G10 | Director has no research-quality judgement | It cannot ask for more research |

## 4. Constraints discovered that shape the design

1. **No network in the test environment.** Phase 2 must therefore make research
   providers *injectable*, and the deterministic/no-provider path must be a real
   implementation rather than a stub — consistent with the Phase 1 precedent
   (agents run deterministically and say so).
2. **`ApprovalRecord.content_item_id` is `NOT NULL`.** Migrating it to a nullable
   polymorphic column would put the Phase 1 publish gate at risk. A separate table
   is strictly safer and keeps `test_publish_blocked.py` untouched.
3. **LangGraph rejects node names that collide with state keys.** Any new graph
   follows the existing `{agent}_agent` suffix convention.
4. **Checkpoint writes use a write-behind buffer drained per superstep.** A second
   graph must use the same `DatabaseCheckpointer` and the same `runtime._step`
   pattern, or it will hit the `Session is already flushing` failure recorded in
   Phase 1.
5. **SQLite is the only DB available here.** New tables must use `GUID`/`JSONB`
   decorators and the migration must be `batch_alter_table`-safe, with Postgres DDL
   verified offline.
6. **DeepSeek V4.1 Flash and Qwen 3.7 Flash are not real, resolvable model IDs in
   the providers available from this environment.** They must be *configuration*
   (priced and routed as data), never hardcoded strings in agent code, and the
   system must state plainly when a configured model is unverified.

## 5. Reuse decisions (explicit, to prevent duplication)

| Need | Reuse | Do not create |
| --- | --- | --- |
| Agent lifecycle, limits, ledger | `BaseAgent`, `AgentContext`, `AgentRun` | a second runner |
| LLM calls + accounting | `LLMGateway`, `ModelUsage`, `UsageService` | a second client |
| Provider seam | `LLMProvider` protocol + `build_providers` | a second registry |
| Persistence patterns | `UUIDPrimaryKeyMixin`, `TimestampMixin`, `GUID`, `JSONB`, `record_audit` | new base classes |
| Workflow persistence | `DatabaseCheckpointer`, `WorkflowRuntime._step` | a second checkpointer |
| Approval semantics | `ApprovalService` logic, mirrored for strategies | weakening content approval |
| RBAC | `FounderUser`, `EditorUser`, `CurrentUser` | new auth |
| Frontend shells | `Card`, `PageHeader`, `useApi`, `Tabs`, `Badge`, `EmptyState` | a second design system |

## 6. Phase 2 scope boundary (what will not be built)

Explicitly **out of scope**, to keep Phase 2 about intelligence rather than
execution:

* No publishing, email sending, ad spend, or any production mutation. The
  "safety" list in the brief is enforced by there being *no code path* for it.
* No live social-account modification.
* No embeddings upgrade (pgvector semantic memory stays as Phase 1 left it).
* No new dependencies. Research providers use the `httpx` already pinned.

## 7. Verdict

The baseline is a good foundation for Phase 2: the agent contract, gateway,
audit and approval patterns all generalise. The three areas needing genuine
extension rather than reuse are (a) model routing by *task class* with budgets,
(b) research persistence with provenance, and (c) strategy versioning with its
own approval ledger. Everything else is additive.
