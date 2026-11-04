# Phase 2 — Implementation Plan

Derived from `docs/05-PHASE2-ARCHITECTURE-AUDIT.md`. Each milestone is
independently testable and leaves the suite green.

## M0 — Baseline lock
Run the full suite and the acceptance harness before touching anything; record
the numbers so regressions are measurable. ✅ 160 tests, 14/14 acceptance checks.

## M1 — Brand knowledge: structured audiences + product facts
Add `audience_segments` and `product_facts` (with `verification_status`:
`verified | unverified | needs_confirmation`). Extend the seed with the five
required audiences and the known facts only. Nothing invented is stored as
`verified`.
**Tests:** seed produces only verifiable facts; every unknown is `unverified`;
updating a fact requires a source to become `verified`.

## M2 — Model routing by task class + auxiliary tier + budgets
Extend (not replace) `ModelRouter`:
* `LLM_ROUTING_JSON` gains `tasks: {classification|extraction|summarisation|formatting → cheap model}` and `reasoning: {competitor_analysis|positioning|strategy|research_synthesis → strong model}`.
* `resolve(agent_name, task_class)` — explicit task class wins, then agent, then default.
* Primary model `deepseek-v4.1-flash`, auxiliary `qwen-3.7-flash` as *configuration* with prices; unverified IDs are flagged in `/system/status`.
* Pre-flight `estimate_cost()`, per-run `max_cost_usd`, `BudgetGuard` that stops before overspending.
* Auxiliary usage tracked separately (`model_usage.tier`).
**Tests:** routing precedence, cheap vs. reasoning selection, estimate accuracy, budget exhaustion stops the call, auxiliary tracked separately, no paid call when unavailable.

## M3 — Research persistence model
`research_projects`, `research_findings`, `research_sources`, `audience_pain_points`.
Findings carry: `finding`, `source_url`, `source_title`, `retrieved_at`,
`confidence`, `evidence_summary`, `verification_status`.
**Tests:** persistence, invalid/missing source rejected, deduplication, filtering.

## M4 — Pluggable research providers
`ResearchProvider` protocol with `DeterministicProvider` (no network, real work
over brand + stored sources), `HttpSearchProvider` (generic, configurable
endpoint), and a registry. Findings from a provider are normalised and every one
requires a source or is marked `unverified` with a reason.
**Tests:** provider swap, malformed provider output handled, no fabricated
citation accepted.

## M5 — Research Agent (upgraded)
Extend the existing `research` agent to emit persisted, source-carrying findings
across the required categories (competitors, positioning, features, content
strategy, pain points, trends, communities, opportunities, questions/objections).
Keeps the deterministic baseline and `try_llm` path.
**Tests:** execution, category coverage, provenance completeness, competitor dedup.

## M6 — Strategy Agent (upgraded)
Emit the required structured outputs, each linked to one or more of the five
business objectives; developer-acquisition-first measurement.
**Tests:** schema completeness, objective linkage, no likes-only optimisation.

## M7 — Strategy persistence + versioning + approval
`marketing_strategies`, `strategy_versions`, `growth_experiments`,
`marketing_objectives`, `strategy_approvals`.
Editing an approved strategy invalidates approval (same semantics as content).
**Tests:** versioning, approval binding to exact version, invalidation on edit,
founder-only, non-founder blocked, audit rows.

## M8 — Research/Strategy Director graph
Second compiled LangGraph graph, sharing the runtime pattern:
```
intake → context → plan_research → dispatch ─┬→ research_agent ─┐
                                             ├→ evaluation ─────┤
                                             └→ (bounded loop)  │
evaluation → (needs more research? → plan_research : strategy_agent)
strategy_agent → brand_check → gap_analysis → persist_strategy
             → request_approval → human_approval(interrupt) → finalize
```
With loop detection, duplicate-task prevention, iteration/tool/cost/time caps.
**Tests:** orchestration, bounded loops, duplicate prevention, persistence, interrupt.

## M9 — API surface
`/research/*`, `/strategies/*`, `/objectives/*`, `/director/research-workflows/*`,
plus brand-knowledge extensions. Auth + RBAC on every route.
**Tests:** authz per role, validation, 404s, idempotency where mutating.

## M10 — Frontend
Research Centre, Strategy Studio, upgraded Director (conversational objective →
live activity → cost), Brand Knowledge extensions. Existing design system.
**Tests:** `tsc --noEmit`, production build.

## M11 — Evidence
`docs/06-PHASE2-ACCEPTANCE-EVIDENCE.md`, extend `scripts/acceptance.py` with the
16 Phase 2 criteria, re-run Phase 1 criteria to prove no regression.
