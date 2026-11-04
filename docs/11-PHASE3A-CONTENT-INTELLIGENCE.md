# Phase 3A — Content Intelligence & Internal Content Calendar

**Status:** COMPLETE (deterministic mock provider only — no live model call was made)
**Scope:** platform specification layer, Content Agent, Brand Guardian extension, campaign
content planner, weekly generation, Content Studio UI, internal content calendar, versioning
and approval integration, budget controls, documentation, tests, acceptance.
**Explicitly out of scope and untouched:** Phase 3B, Phase 4, Google Flow, social publishing,
external scheduling, paid model calls, new model/provider configurations.

---

## 1. Executive summary

Phase 3A turns an approved strategy into **platform-specific drafts**, checks them with an
extended **Brand Guardian**, plans them into a **balanced weekly calendar**, and lets a founder
or marketer edit, validate, compare and version them inside a **Content Studio** — while making
publishing and external scheduling structurally impossible.

| Capability | State | Evidence |
|---|---|---|
| Platform spec layer (6 platforms, 11 content types) | working | `GET /content-studio/platforms`, `tests/test_phase3a_content.py` |
| Content Agent (structured, platform-aware) | working via mock | `POST /content-studio/generate` |
| Multiple distinct variants per brief | working | variant comparison + selection tests |
| Brand Guardian (13 rule families, 4 verdicts) | working | `POST /content-studio/items/{id}/validate` |
| Campaign content planner (balanced, not identical) | working | `POST /content-studio/plans` |
| Weekly generation ("prepare next week") | working | acceptance criterion 23 |
| Content Studio frontend (overview, editor, variants) | working | `tsc` clean, `vite build` clean |
| Internal content calendar (month/week/list, moves) | working | `GET/PATCH /content-studio/calendar` |
| Versioning + approval reuse (no second system) | working | criteria 27–28 |
| Publishing / external scheduling | **unavailable by construction** | criterion 30 |
| Real AI generation | **not implemented in this phase** | mock provider only |

**The one thing this report will not claim:** that real model-generated content works. Every
draft in this phase came from the deterministic mock provider. The gateway path for a real
provider exists and is unchanged from Phase 1/2, but it was never exercised for content, and
`content_provider` defaults to `mock` precisely so it cannot be exercised by accident.

---

## 2. Architecture

```
brand knowledge ─┐
research refs ───┼─► Content Agent ──► variant set ──► Brand Guardian ──► verdict
campaign brief ──┘        │                                   │
                          ▼                                   ▼
                 ContentPlanningService  ◄────  validation gate (approval submit)
                          │
              ┌───────────┴────────────┐
              ▼                        ▼
   ContentPlan + slots        ContentItem + ContentVersion (Phase 1)
              │                        │
              ▼                        ▼
      weekly/weekly bounds     ContentCalendarEntry (internal only)
```

Reuse, not duplication:

* **content items, versions, approvals** are the Phase 1 models (`app/models/content.py`,
  `app/services/content.py`, `app/services/approval.py`). Phase 3A adds no second state machine.
* **agent runtime, gateway, budget guard, usage recording** are Phase 1/2 components, unchanged
  except for the non-billable mock exemption described in §9.
* New tables exist only for things Phase 1 never modelled: plans, plan slots, internal calendar
  entries, validations (`app/models/content_plan.py`).

### 2.1 Files added or changed

| File | Purpose |
|---|---|
| `backend/app/content/platforms.py` | `ContentType`, frozen `PlatformSpec`, 6 platform specs, overrides, `validate_against_spec` |
| `backend/app/models/content_plan.py` | `ContentPlan`, `ContentPlanSlot`, `ContentCalendarEntry`, `ContentValidation` |
| `backend/alembic/versions/7c1f9a2d4e83_phase3a_content_intelligence.py` | additive migration (upgrade + downgrade proven) |
| `backend/app/llm/mock_content.py` | deterministic mock provider + variant frames |
| `backend/app/agents/content.py` | Content Agent (Phase 1 agent extended, not replaced) |
| `backend/app/agents/brand_guardian.py` | guardian rules incl. Phase 3A claim/evidence checks |
| `backend/app/services/content_planning.py` | planner, weekly generation, validation recorder |
| `backend/app/services/content_calendar.py` | internal calendar service |
| `backend/app/api/v1/content_studio.py` | 14 routes under `/api/v1/content-studio` |
| `backend/app/schemas/content_studio.py` | request/response schemas (`extra="forbid"` on inputs) |
| `frontend/src/pages/ContentStudio.tsx` | overview, weekly plan, single draft, drafts table |
| `frontend/src/pages/ContentDraftEditor.tsx` | editor, preview, validation, version history, variants |
| `frontend/src/pages/ContentCalendar.tsx` | internal planner (month/week/list) added above the Phase 1 list |

---

## 3. Workflow

1. **Brief.** A campaign (name, objective, audience, message, pillars, platforms, duration, CTA,
   success metrics) plus the brand knowledge base.
2. **Plan.** `POST /content-studio/plans` normalises the founder-configurable cadence
   (posts/week, active platforms, content mix, publishing windows, variants, duration) and
   proposes one slot per selected platform, then fills the remainder round-robin. Days spread
   across the week (`index % 7`), times come from the publishing windows.
3. **Draft.** Each slot runs the Content Agent → N variants; the highest-scoring variant becomes
   the version text and **the whole variant set is stored on that version**.
4. **Validate.** The Brand Guardian returns a verdict plus structured issues; the verdict is
   persisted against the exact version.
5. **Review.** Drafts appear in Content Studio with filters, and in the internal calendar by
   planned date.
6. **Edit / compare / select.** Every edit (manual or variant selection or regeneration) creates a
   new `ContentVersion`; approval is invalidated and the history is retained.
7. **Approve.** Through the Phase 1 state machine, gated by the guardian: a `BLOCKED` or
   `NEEDS_REVISION` verdict returns `409` and lists the blocking issues.
8. **Plan internally.** Dates and times are internal plans. Publishing remains unavailable.

---

## 4. Agent responsibilities

| Agent | Phase 3A responsibility | Cannot |
|---|---|---|
| **Content Agent** | Produce platform-shaped drafts: hook, body, CTA, hashtags, outline (carousels = text outline only), plus generation metadata | Publish, schedule, invent facts, call a paid provider while `content_provider=mock` |
| **Brand Guardian** | Validate product accuracy, unsupported claims, unverified statistics, fabricated testimonials, voice, audience relevance, platform formatting, repetition, CTA relevance, source evidence | Rewrite approved content, approve anything, publish |
| **Director / planner** | Propose a balanced slot distribution | Choose identical content for every platform |

Every Content Agent output carries: campaign ID, objective, platform, content type, audience,
pillar, primary message, hook, body, CTA, hashtags, research refs, brand facts used,
verification status, suggested date/time, version number and generation metadata (provider,
model, `is_mock`, `generation_mode`, variant index, budget snapshot).

---

## 5. Platform specifications

`app/content/platforms.py` is the single source of truth; it is data, not code paths, so a new
platform or limit is a data change plus tests. `GET /content-studio/platforms` exposes it.

| Platform | Body limit | Hashtags | Notable rules |
|---|---|---|---|
| LinkedIn | 2600 | 0–3 | professional tone, no engagement bait |
| X | 280 | 0–2 | per-post limit for threads, short hooks |
| Instagram | 2200 | 3–8 | caption-first, carousel needs slides |
| YouTube | 5000 | 0–3 | title limit, short-form script sections |
| Facebook | 5000 | 0–2 | conversational, link-free body preferred |
| Reddit | 10000 | 0 | community value first, no promotional CTA, no claimed personal experience |

Validation rule IDs are a public contract and are never renamed: `platform_length`,
`thread_post_within_limit`, `no_hashtags`, `carousel_needs_slides`,
`content_type_unsupported`.

---

## 6. Brand Guardian rules

Statuses: `PASS`, `PASS_WITH_WARNINGS`, `NEEDS_REVISION`, `BLOCKED`.

* **Blocking (never approvable):** unsupported product claims, unverified statistics,
  fabricated testimonials, claims the brand marked `do_not_claim`.
* **Warning:** tone, audience fit, CTA relevance, repetition, missing source evidence,
  formatting drift.
* **Founder-confirmation flag:** facts that are unknown rather than false are marked
  `requires_founder_confirmation` instead of being silently softened.

The guardian never edits text. It returns `issues[]` with severity, category, explanation,
evidence, affected text, suggestion and the founder-confirmation flag. A `BLOCKED` or
`NEEDS_REVISION` verdict blocks `submit` at the approval service with `409`, and no alternate
path to `APPROVED` exists.

---

## 7. API endpoints

All routes are under `/api/v1/content-studio` and require authentication; mutations require
`marketer` or `founder`, approval itself remains founder-only on the Phase 1 route.

| Method | Path | Purpose |
|---|---|---|
| GET | `/platforms` | platform specs |
| GET | `/capabilities` | what is and is not available, plus provider honesty flags |
| GET | `/overview` | Studio metrics (drafts, pending, approved, planned, blocked, distributions) |
| POST | `/generate` | one draft + N variants (idempotent) |
| GET | `/plans` | list plans |
| POST | `/plans` | create a campaign plan, optionally generate |
| GET | `/plans/{id}` | plan detail with slots |
| POST | `/plans/{id}/generate` | fill remaining slots (idempotent per slot) |
| GET | `/items/{id}/variants` | compare the variant set stored on the current version |
| POST | `/items/{id}/variants/regenerate` | write a fresh variant set to a **new** version |
| POST | `/items/{id}/variants/select` | adopt a stored variant as a **new** version |
| POST | `/items/{id}/validate` | run the Brand Guardian now |
| GET | `/items/{id}/validations` | validation history |
| GET | `/calendar` | internal calendar (`view=month|week|list`) |
| PATCH | `/calendar/{item_id}` | move/rename an internal plan |

Editing, version history and approval stay on the Phase 1 routes (`/content/{id}/versions`,
`/content/{id}/transition`, `/content/{id}/approvals`). **There is no publish or external
schedule route in this module**, and `POST /content-studio/publish`,
`/content-studio/schedule`, `/content-studio/items/{id}/publish` all return `404`/`405`.

Idempotency: `POST /plans` and `POST /generate` honour `Idempotency-Key`. Replaying the same key
and body returns the original plan/item; the same key with a different body returns
`409 idempotency_key_reuse`. Regeneration is idempotent *per slot*: it never duplicates items.

---

## 8. Database changes

One additive migration, `7c1f9a2d4e83` (`down_revision = 60ed69ffc987`), proven up and down on a
fresh database.

| Table | Key relationships |
|---|---|
| `content_plans` | `brand_profile_id` → brands, `campaign_id` → campaigns, unique `(brand, campaign, week_start)` |
| `content_plan_slots` | `plan_id` → content_plans, `content_item_id` → content_items |
| `content_calendar_entries` | `content_item_id` → content_items (unique), `plan_id` → content_plans |
| `content_validations` | `content_item_id` → content_items, `content_version_id` → content_versions |

`content_validations` deliberately keeps `content_version_id`, so a verdict can never be moved
onto a different version. No Phase 1/2 table was altered in a breaking way; no Phase 1 table was
duplicated.

---

## 9. Approval, versioning and budget enforcement

**Versioning.** Approval binds to a `content_version_id`, as in Phase 1. Any edit — manual,
variant selection or regeneration — creates a new version through `ContentEditor.edit`, which
invalidates the live approval, keeps every approval record and never mutates an approved
version's text. The Studio exposes the history and a side-by-side comparison.

**Budget.** Phase 3A adds no provider configuration and no new pricing. What it adds is one
exemption and one enforcement point:

* the mock provider declares `is_billable=False`, so its `CostEstimate` is `billable=False`,
  `estimated_cost_usd=0.0`, it reserves nothing and is exempt from the zero-budget,
  pricing-trust and monthly-cap gates. Token and tool-call limits still apply;
* every other provider keeps Phase 2.6 semantics: reservations before the call, reconciliation
  after, zero budget blocks, unverified placeholder prices block, no zero-means-unlimited.

Per-campaign and per-execution budgets come from `ExecutionLimits.for_content(settings)` and the
shared `budget_guard` in the agent context. Usage rows record provider, model, tokens, cost,
execution/agent-run id and status, so campaign spend is attributable.

---

## 10. Mock provider: what it does and does not prove

`app/llm/mock_content.py` composes drafts from templates with a variant frame
("The problem" / "The trade-off we made" / "What we changed"), so variants differ in
**substance**, not synonyms. It cannot emit `%` or `$` figures, which is why the mock cannot
accidentally produce a fabricated statistic. Every response is labelled:

```json
{"provider": "mock", "model": "mock-content-v1", "is_mock": true,
 "billable": false, "cost_usd": 0.0, "generation_mode": "mock"}
```

`GET /capabilities` reports `is_mock_provider: true` and `live_model_calls_enabled: false`, and the
Studio renders a banner whenever the mock is active.

**Limitations, stated plainly:**

* the text is template-composed; it demonstrates plumbing, platform shape and guardrails, **not
  copywriting quality**;
* it proves the guardian blocks unsupported claims because the test injects real claim text into
  a draft — it does **not** prove a real model would avoid such claims unprompted;
* latency, token accounting and real cost behaviour are unmeasured because nothing was billed;
* therefore no claim in this document should be read as "AI content generation works".

Switching to a real provider is a configuration change (`CONTENT_PROVIDER`), and it is
deliberately not enabled: doing so would spend money and is a Phase 3B decision.

---

## 11. Tests and acceptance

* Full backend suite: **549 passed, 0 failed, 0 errors** (`pytest tests/ -q`).
* Phase 3A service/agent tests: 61 (`tests/test_phase3a_content.py`).
* Phase 3A API tests: 24 (`tests/test_phase3a_api.py`).
* Frontend: `tsc --noEmit` clean, `vite build` clean.
* Acceptance harness: **36/36** criteria (23 Phase 1/2 checks + 13 Phase 3A proofs), including
  publishing unavailable, zero budget blocking paid providers, approved-version immutability and
  idempotent retries. Evidence: `docs/acceptance-run.json`.

The 13 Phase 3A proofs, verbatim from the harness output:

1. weekly plan created for a campaign → 6 slots, 6 platforms, 4 content types, `generated_by=mock`;
2. platform-specific drafts persisted and linked to the campaign (`linkedin` 213ch, `x` 46ch,
   `instagram` 163ch, `youtube` 324ch, `facebook` 185ch, `reddit` 406ch);
3. guardian validates → `PASS` after 18 rules, bound to a version;
4. unsupported claims blocked → `BLOCKED` with 4 blocking issues (`source_evidence`,
   `unsupported_testimonial`, `unverified_statistic`), `submit` → `409`;
5. edit → new version, original text unchanged;
6. approval binds to the exact version and a later edit invalidates it while keeping history;
7. internal calendar move persists, `external_scheduling=false`;
8. publishing unavailable on 4 paths;
9. zero budget blocks paid providers while the mock is served at cost `0.0`;
10. mock attribution is visible everywhere;
11. variants compared and preserved (2 distinct variants, selection → v2 with that exact body);
12. generation idempotent (plan retry → same plan, generate retry → same item);
13. Phase 1/2 acceptance intact in the same run.

### 11.1 Defects found and fixed during Phase 3A

| Defect | Impact | Fix |
|---|---|---|
| Content→mock routing was a `ModelRouter` rule | Broke any gateway constructed with an injected provider set (4 Phase 1/2 tests failed with `ProviderUnavailable`) | Pin moved into `LLMGateway.structured`, applied before normal routing, only when the gateway holds `mock` |
| `mock_content._render` read `variant_index` after a branch that used it | `UnboundLocalError` whenever an earlier hook had to be avoided, silently degrading to deterministic copy | Selector read before all branches (+ debug traceback logging for degraded calls) |
| Plan-generated items stored `variant_count` but not the variants | Variant comparison was empty for every planned draft | Persist the variant payloads on the version; regression test added |
| Studio "regenerate" button only re-read the variant list | A control that appeared to act but did not | New `POST /items/{id}/variants/regenerate` writes a fresh set to a new version; deterministic-provider offset prevents returning identical text |
| Weekly plan regeneration reset `item_count` to 0 | Overview totals lied | `item_count` recomputed from slots that hold an item |

---

## 12. Requirements for Phase 3B (not implemented here)

1. **Real generation** behind an explicit, founder-authorised switch, with verified pricing and a
   per-campaign budget; the mock path must remain the default in tests.
2. **Prompt/eval quality work:** golden-set evaluation of real output against the guardian, since
   the mock proves plumbing only.
3. **Carousel and short-form assets:** image/video generation is out of scope here; carousels are
   text outlines only and `video_rendering` is reported as false.
4. **Approval UX at scale:** bulk review, reviewer assignment and comment threads on versions.
5. **Calendar intelligence:** cadence recommendations from performance data instead of static mix.
6. **Publishing** (a Phase 4 concern): still requires a separate, explicitly authorised design —
   Phase 3A adds no adapter, credential or dispatch path, and `publishing.status` must stay
   honest until then.

---

## 13. How to run

```bash
# backend
cd backend && ../.venv/bin/alembic upgrade head
../.venv/bin/python -m pytest tests/ -q

# UI
cd frontend && npx tsc --noEmit && npx vite build

# acceptance (needs a running API and the acceptance env)
.venv/bin/python scripts/acceptance.py --base-url http://127.0.0.1:8000
```

`CONTENT_PROVIDER=mock` is the default. It is the only content provider exercised in this phase.
