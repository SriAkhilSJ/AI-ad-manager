# Phase 2.6 — Fail-Closed Budget Safety

**Status:** COMPLETE
**Commit:** `cbf212c` (implementation); this line was added in a follow-up docs commit.
**Scope:** `BudgetGuard` semantics, atomic cost reservations, pricing trust, cost observability.
**Out of scope and untouched:** Phase 3, social publishing, Google Flow, live provider calls, provider credential isolation, approval enforcement.

---

## 1. Executive summary

The reported defect was "`BudgetGuard(max_cost_usd=0)` means unlimited spending". The
audit found the defect is **wider than reported**: *five* distinct inputs all failed
open, every one of them silently granting unlimited spend.

| `max_cost_usd` input | stored before | `remaining_usd` before | blocks a $999 request? |
|---|---|---|---|
| `None` | `0.0` | `inf` | **no — fail-open** |
| `0` | `0.0` | `inf` | **no — fail-open** |
| `-5` | `-5.0` | `inf` | **no — fail-open** |
| `NaN` | `nan` | `nan` | **no — fail-open** |
| `inf` | `inf` | `inf` | **no — fail-open** |
| `0.06` | `0.06` | `0.06` | yes |

Root cause, one line in `BudgetGuard.remaining_usd`:

```python
if self.max_cost_usd <= 0:
    return math.inf          # ← "zero, missing, negative and NaN all mean spend anything"
```

Three structural problems were also found and fixed:

1. **Check-then-spend race.** `gateway.structured()` called `budget.check()` and only
   added spend *after* the provider returned. Two concurrent calls both read the same
   headroom and both proceeded.
2. **No shared scope.** The Director persisted its budget as a plain dict in workflow
   state (`strategy_runtime.py`, `strategy_state.py:157`). Research and strategy each
   built their own private `ExecutionGuard`, so "remaining budget" was an *estimate
   recomputed per node*, not a ledger.
3. **Placeholder pricing presented as a price.** Models absent from the price table
   silently fell back to a generic per-provider rate and were budgeted against it.

Phase 2.6 makes every one of these fail **closed**. `0` blocks, missing blocks,
invalid configurations are rejected at construction, prices must be verified, and
spend is claimed atomically before the request leaves the process.

---

## 2. Audit: every affected execution path

Documented before any code changed (per the Phase 2.6 brief).

### 2.1 The guard itself
| Location | Finding |
|---|---|
| `app/llm/routing.py` `BudgetGuard.__init__` | `float(max_cost_usd or 0.0)`; `None`→`0.0`, then `0`→unlimited |
| `BudgetGuard.remaining_usd` | `<= 0` → `math.inf` |
| `BudgetGuard.remaining_tokens` | `<= 0` → `2**31-1` (also unbounded) |
| `BudgetGuard.check` | cost gate skipped entirely unless `max_cost_usd > 0` |
| `BudgetGuard.check` (tool calls) | gate only when `max_tool_calls > 0` |

### 2.2 Paths that construct a guard
| Path | Code | Behaviour before |
|---|---|---|
| App-level guard | `app/agents/limits.py:94` (`ExecutionGuard.__post_init__`) | inherits every fail-open above |
| Research agent | `app/agents/research.py:266–276` | builds its **own** guard from settings, then overlays `payload.max_cost_usd` |
| Strategy agent | `app/agents/strategy.py:339–347` | same, own guard |
| Director nodes | `app/director/strategy_nodes.py:101–130` (`_limits`), `:169` | budget is a **dict**, no live guard |
| Director → strategy handoff | `strategy_nodes.py:603–614` | `round(remaining, 6)` — can be `0.0`, which meant **unlimited** |
| Paid canary | `app/cli/canary.py:156` | `BudgetGuard(max_cost_usd=ceiling_usd)`; `0` meant unlimited |
| Request schemas | `app/schemas/{research,strategy}.py` | `max_cost_usd: Field(ge=0)` — permits 0, which was the trap |

### 2.3 The single spend choke point
Every paid model call — primary **and** auxiliary — passes through
`LLMGateway.structured()`:

```
routing.resolve → estimate_cost → budget.check (L294) → monthly cap (L297–300)
   → provider.structured() → budget.record (L311)
```

Because both model tiers share that function and receive the same `budget=`
argument, the reservation had to be inserted **there** — one place, not per agent.

### 2.4 Other budget surfaces checked
- `usage.month_spend()` monthly cap (`gateway.py:297–300`): already fails closed
  (`spent + estimate > cap`). Left as-is, but its refusal now releases the reservation.
- `ExecutionGuard.check_budget_before_call` (`limits.py:211`): constructs an estimate
  with `provider="preflight"`, `price_verified=False`. Under the new pricing rule this
  would have raised for a correctly budgeted run. Exempted deliberately (§5.4).
- `docs/09`-era test `test_zero_cost_budget_means_uncapped_and_that_is_a_known_trap`
  **pinned the old fail-open behaviour**. It is inverted, not deleted (§7).

---

## 3. New budget semantics

Implemented in `app/llm/routing.py`.

| Configuration | Before | After |
|---|---|---|
| `0` | unlimited | **blocks all paid requests** |
| missing / `None` | unlimited | **blocks all paid requests** |
| negative | unlimited | `BudgetConfigurationError` at construction |
| `NaN` | unlimited | `BudgetConfigurationError` at construction |
| `inf` | unlimited | `BudgetConfigurationError`, unless `allow_unlimited=True` |
| positive finite | within budget | within budget (unchanged) |
| unknown model pricing | allowed | **blocks paid requests** |

```python
BudgetGuard(max_cost_usd=0)                     # blocks everything
BudgetGuard()                                   # blocks everything
BudgetGuard(max_cost_usd=-1)                    # BudgetConfigurationError
BudgetGuard(max_cost_usd=float("nan"))          # BudgetConfigurationError
BudgetGuard(max_cost_usd=float("inf"))          # BudgetConfigurationError
BudgetGuard(max_cost_usd=float("inf"),
            allow_unlimited=True)               # explicit; snapshot says max_cost_usd=None
```

**Unlimited mode can never be enabled implicitly.** It requires a literal positive
infinity *plus* `allow_unlimited=True`, and `snapshot()` reports
`unlimited: True, max_cost_usd: None` so it is always visible in telemetry.

`BudgetConfigurationError` subclasses `ValueError`, so any caller written before
Phase 2.6 that catches `ValueError` keeps working.

Two further details, both found by tests while implementing:

- **The ceiling is no longer rounded.** An earlier draft rounded to 6 decimals, which
  turned a `0.0000001` budget into `0.0` — silently converting a tiny envelope into a
  full block. A test caught it.
- **Token and tool-call ceilings keep `0 = no ceiling`,** and this is deliberate:
  they are not money. Cost is the axis that fails closed. An absent cost budget is
  never a licence to spend; an absent token cap is bounded by the cost cap.

---

## 4. Atomic cost reservations

`BudgetGuard` now owns a ledger:

```python
claim = budget.reserve(estimate)          # atomic claim, raises if it does not fit
response = await provider.structured(request)   # only after the claim succeeds
budget.reconcile(claim, actual_cost_usd=response.cost_usd, tokens=response.total_tokens)
```

The seven required steps, mapped to code:

| # | Requirement | Implementation |
|---|---|---|
| 1 | Estimate maximum request cost | `estimate_cost(...)` with `completion_tokens=request.max_tokens` (worst case) |
| 2 | Atomically reserve | `reserve()` under a lock: gate → check → increment `_reserved_usd`, all in one critical section |
| 3 | Reject if over remaining | `amount > available + 1e-12` → `BudgetExceeded` |
| 4 | Execute only after reservation | `gateway.py`: `reserve()` precedes `provider.structured()` |
| 5 | Reconcile actual cost | `reconcile(claim, actual_cost_usd=...)` |
| 6 | Release unused | `released_usd = reserved - actual`, credited back in the same critical section |
| 7 | Preserve on ambiguity | `reconcile(..., ambiguous=True)` charges the **full reservation** and marks the claim `ambiguous` |

`remaining_usd` is now `max_cost_usd − spent − reserved`, so an open claim is
visible to the next caller before any money is spent.

### 4.1 Concurrency guarantee

The claim is taken inside a critical section guarded by a re-entrant lock
(`threading.RLock`). That gives two distinct guarantees:

- **Across asyncio tasks** — `reserve()` contains no `await`, so on a single event
  loop the read-check-write sequence cannot be interleaved with another task.
- **Across threads** — the lock covers worker threads, scripts and test drivers that
  the event loop does not serialise.

Test evidence: 10 concurrent callers against a budget with room for exactly 3 →
exactly 3 reach the provider, `spent_usd ≤ ceiling`, 7 refused (`test_concurrent_requests_cannot_overspend_the_same_budget`).
The same guarantee is proven with 8 OS threads (`test_reservations_are_atomic_across_threads`).

A deadlock in `snapshot()` (it re-enters `remaining_usd`) was found and fixed by
switching to `RLock`; before that fix the full suite hung.

### 4.2 Shared budget scope

- **Within a node:** the Director now creates one `BudgetGuard` in `_ctx()` and hands
  it to the agent via `AgentContext.extra["budget_guard"]`. The agent **adopts** it
  (`ExecutionGuard(shared_budget=...)`) instead of building a private copy, so primary
  and auxiliary calls settle against one ledger.
- **Research → strategy:** `_remaining_budget()` derives the ceiling from persisted
  state and returns exactly `0.0` when research consumed the envelope. `0.0` now
  blocks. This is a deliberate behaviour change: a Director whose research exhausted
  the budget will stop at the strategy node with `budget_exhausted` instead of
  spending without limit.

---

## 5. Pricing safety

Three states are now distinguished, and the snapshot says which one applies
(`pricing_trusted`):

| State | Source | Allowed? |
|---|---|---|
| **Verified** | operator `LLM_PRICING_JSON`, or an exact built-in table match | yes |
| **Placeholder** | generic per-provider fallback for an unlisted model | only with explicit opt-in |
| **Unknown** | no price at all | never |

Placeholder pricing is **never** reported as verified. When a request would be priced
from a placeholder, `reserve()` refuses with:

> refusing to spend on untrusted pricing: model price for '<model>' from provider
> '<provider>' is an unverified placeholder. Supply verified prices via
> `LLM_PRICING_JSON`, or pass `allow_unknown_pricing=True` to accept the estimate as
> an upper bound.

The opt-in is explicit at two levels and never implicit:
- per guard: `BudgetGuard(..., allow_unknown_pricing=True)`
- per deployment: `LLM_ALLOW_UNVERIFIED_PRICING=true` (default **false**), copied into
  `ExecutionLimits.allow_unknown_pricing` by `for_research()` / `for_strategy()`, and
  into the Director's per-node guard.

### 5.4 The "preflight" exemption
`ExecutionGuard.check_budget_before_call` supplies the caller's own ceiling and quotes
no provider price. Applying pricing-trust to it would block correctly budgeted runs,
so `provider="preflight"` estimates are exempt from the pricing rule (but **not** from
the zero-budget rule). This is pinned by
`test_caller_supplied_ceiling_is_not_subject_to_pricing_trust`.

---

## 6. Cost observability

`BudgetGuard.snapshot()` now reports, per scope:

```
label, max_cost_usd, unlimited, spent_usd, reserved_usd, committed_usd,
remaining_usd, open_reservations, pricing_trusted, max_tokens, tokens_used,
tool_calls, max_tool_calls, exhausted, stopped_reason, rejections[]
```

Each rejection entry carries `kind`, `reason`, `provider`, `model`,
`estimated_cost_usd` and `request_id` (set via `set_request_id()` for correlation), so
a refusal can be traced to a specific workflow step. Rejection kinds:
`zero_budget`, `cost_budget`, `token_budget`, `tool_calls`, `untrusted_pricing`,
`unknown_claim`.

An infinite limit is reported as `None`, never as a large number, so nothing downstream
can mistake "unlimited" for a figure.

**Preserved:** `ModelUsage` token telemetry is untouched (`tier`, `task_class`,
`cost_usd` per call still recorded by `_record_usage`). Credential redaction is
unchanged — `/api/v1/system/status` still reports providers as
`credential_present`/`base_url_supplied` booleans and contains no `API_KEY`, `sk-`,
`secret_key` or `password` substring (verified after restart). Rejection reasons name
the provider and model only.

---

## 7. Test evidence

All 23 acceptance checks pass; the full pytest suite is 464 tests with 0 failures.

| Suite | Tests | Result |
|---|---|---|
| Full suite (`pytest tests/`) | **464** | 0 failures, 0 errors |
| — of which new `tests/test_budget_safety.py` | **41** | all pass |
| — pre-existing | 423 | all pass (6 updated as described below) |
| `tests/test_model_routing.py` | 33 | all pass |
| `tests/test_provider_routing_security.py` | 58 | all pass |
| Acceptance (`scripts/acceptance.py`) | 23/23 | PASS |
| Preflight (`app.cli.preflight`) | PASS 9 / FAIL 3 / SKIP 2 | BLOCKED (unchanged; see §9) |

Every provider used in the new tests is a fake. No test can reach a real endpoint or
spend money.

### 7.1 Tests changed (and why)

1. **`test_zero_cost_budget_means_uncapped_and_that_is_a_known_trap`** →
   **`test_zero_cost_budget_blocks_every_paid_request`**. The old test asserted the
   fail-open behaviour on purpose, so a Phase 2.5 reader would see the residual risk.
   It is **inverted rather than deleted** so the semantic change stays visible in
   history.
2. **Five budget tests in `test_model_routing.py`** now run against `priced_settings()`
   — the same settings with operator-supplied prices. They test *envelopes*, and
   under the new regime an unpriced model would block before the envelope logic ran.
   Prices mirror the `openrouter` fallback those tests were calibrated against, so the
   assertions keep their original meaning.
3. `tests/test_provider_routing_security.py::test_...` calls now use
   `BudgetGuard(max_cost_usd=0)` to mean "may not spend", instead of the old
   `1e-9` workaround that existed only because `0` was unsafe.

### 7.2 New coverage map (`tests/test_budget_safety.py`)

| Requirement | Test |
|---|---|
| Zero budget blocks | `test_zero_budget_blocks_check_and_reserve`, `test_zero_budget_never_reaches_the_provider` |
| Missing budget blocks | `test_missing_budget_blocks_rather_than_meaning_unlimited` |
| Positive budget permits eligible | `test_positive_budget_permits_eligible_request` |
| Invalid numerics rejected | `test_invalid_budget_values_are_rejected_at_construction` (8 params), `test_negative_budget_error_is_a_value_error`, `test_invalid_token_and_tool_call_limits_are_rejected` |
| Unlimited only on opt-in | `test_unlimited_requires_an_explicit_opt_in` |
| Unknown prices block | `test_unverified_pricing_blocks_paid_execution`, `test_verified_pricing_allows_the_same_request`, `test_operator_can_opt_into_placeholder_pricing_explicitly` |
| Concurrent requests cannot overspend | `test_concurrent_requests_cannot_overspend_the_same_budget`, `test_reservations_are_atomic_across_threads` |
| Failed requests release | `test_failed_request_releases_its_reservation`, `test_unexpected_exception_releases_its_reservation`, `test_monthly_cap_reservation_is_released_when_the_cap_refuses` |
| Ambiguous timeouts retain | `test_timeout_retains_the_reservation_as_ambiguous`, `test_ambiguous_reconcile_keeps_the_whole_reservation` |
| Primary + auxiliary share scope | `test_primary_and_auxiliary_share_one_budget_scope`, `test_execution_guard_adopts_a_shared_budget_instead_of_copying_it` |
| Director handoff cannot uncap | `test_director_remaining_budget_is_zero_when_research_spent_it_all`, `test_director_node_guard_uses_the_remaining_budget`, `test_director_remaining_budget_never_goes_negative` |
| Provider isolation intact | `test_budget_does_not_bypass_provider_configuration_checks` + the 58 Phase 2.5 security tests |
| Observability / redaction | `test_snapshot_exposes_the_full_cost_story`, `test_rejection_ledger_records_reason_provider_model_and_request_id`, `test_rejection_does_not_leak_credentials_into_the_ledger` |
| Estimate error is bounded | `test_estimate_error_is_bounded_to_one_request` |

---

## 8. Migration implications

**This release changes behaviour on purpose. Read this before deploying.**

1. **`max_cost_usd=0` now blocks.** Any configuration, script or integration that set
   `0` to mean "no limit" will stop spending. Use `allow_unlimited=True` with
   `float("inf")` to restore unlimited behaviour *explicitly*.
2. **Omitted budgets now block.** `BudgetGuard()` with no ceiling permits nothing.
   Every construction site must pass a real number.
3. **Unpriced models now block paid execution.** A deployment whose tier models are
   not in the price table will refuse to call them until `LLM_PRICING_JSON` is
   supplied (or `LLM_ALLOW_UNVERIFIED_PRICING=true` is set deliberately).
   *In this repository's current state the tier models are unpriced, so real paid runs
   are blocked — this is the intended fail-closed outcome and matches the
   `accounting.pricing` preflight FAIL that was already being reported.*
4. **Research that exhausts the budget now stops the Director** at the strategy node
   (`status: budget_exhausted`) rather than continuing without a ceiling.
5. **Invalid numbers raise at construction,** not at first call. A bad settings value
   fails at startup rather than after a workflow has begun.
6. **No database migration.** The ledger is in-process; the workflow state dict is
   unchanged in shape. No schema changes, no data migration.
7. **New setting:** `LLM_ALLOW_UNVERIFIED_PRICING` (default `false`), documented in
   `.env.example` with no value secrets.

---

## 9. Known limitations (stated, not hidden)

1. **The reservation is only as accurate as the estimate.** If a provider bills more
   than the estimate, the ceiling can be exceeded by the requests already in flight.
   The guard hard-stops immediately afterwards, and further calls are refused
   (`test_estimate_error_is_bounded_to_one_request`). The guarantee is *"never start a
   call that cannot fit an honest estimate"*, not *"never exceed the ceiling under any
   circumstances"*.
2. **The ledger is per-process and in-memory.** Two API workers do not share a
   reservation ledger; each enforces its own view. Closing that would need a shared
   store (out of scope: no database redesign in Phase 2.6). The **monthly cap** is the
   existing cross-process backstop.
3. **Ambiguous timeouts are charged at the estimate,** because we cannot know what the
   vendor billed. This is conservative by design: it can under-spend, never over-spend.
   A later reconciliation against provider invoices is not implemented.
4. **Director-level scope is re-derived per node** from persisted state, not carried as
   a live object across LangGraph checkpoints (a live guard is not serialisable). The
   handoff is therefore a *recomputed, fail-closed* ceiling rather than a single
   continuous ledger.
5. **`allow_unknown_pricing=True` weakens the guarantee.** It accepts that a placeholder
   may under-estimate; it is opt-in and appears as `pricing_trusted: false` in every
   snapshot.
6. **No live verification.** No paid canary was run: there are no provider credentials
   in this environment and Phase 2.6 forbids live inference. All evidence above is
   from mocked providers. **This is not a claim of production readiness.**
7. Preflight still reports **BLOCKED** (`credentials.present`, `credentials.tier_match`,
   `accounting.pricing`), unchanged from Phase 2.5 — those are operator configuration
   gaps, not code defects, and Phase 2.6 now *enforces* the pricing one at runtime.

---

## 10. Verdict

| Question | Answer |
|---|---|
| Is `BudgetGuard(max_cost_usd=0)` still unlimited? | **No.** It blocks every paid request. |
| Can unlimited mode be enabled implicitly? | **No.** Requires `inf` + `allow_unlimited=True`, and says so in the snapshot. |
| Are missing / negative / NaN / infinite budgets safe? | **Yes.** Missing blocks; the others are rejected at construction. |
| Can concurrent requests overspend? | **No** within one process, given honest estimates (proven with tasks and threads). |
| Is a reservation preserved when a timeout is ambiguous? | **Yes.** |
| Do primary and auxiliary models share a scope? | **Yes** within a node; across Director nodes the ceiling is re-derived and fails closed. |
| Is placeholder pricing ever reported as verified? | **No.** |
| Are Phase 1, Phase 2 and Phase 2.5 intact? | **Yes.** 464 tests pass, 23/23 acceptance, provider isolation and approval gates unchanged. |
| Is Phase 3 started? | **No.** |

**PHASE 2.6: COMPLETE — FAIL-CLOSED BUDGET SAFETY IN PLACE.**
Live paid execution remains blocked pending operator pricing and credentials, which is
the correct state. **Phase 3 remains NO-GO.**
