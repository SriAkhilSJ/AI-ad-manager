# Phase 3 Integration Readiness — Pre-Implementation Audit

**Product:** Crusor AI Marketing Director
**Baseline:** Phase 1 `43d633e`, Phase 2 `e2552a7`
**Audit branch:** `arena/c7e4be51-ai-ad-manager`
**Date:** 2026-11-03
**Scope:** discovery and validation only. **No Phase 3 feature code was written.**

> Every claim below carries its evidence. Where something could not be checked,
> the section says so and the status is `SKIP`/`BLOCKED`/`UNVERIFIED`, never
> `PASS`. No secret value appears in this document, in any command output, or in
> any commit.

---

## 0. Executive verdict

| Area | Verdict | One-line reason |
| --- | --- | --- |
| **MODEL GATEWAY** | **BLOCKED** | No credential present, no network path to any provider, and no OpenAI-compatible endpoint is configurable. |
| **DEEPSEEK** | **UNVERIFIED** | Model id configured, price unverified, no provider can serve it. |
| **QWEN** | **UNVERIFIED** | Same. |
| **FLOW CONNECTOR** | **BLOCKED** | Source is not in this repository and does not exist anywhere on this filesystem. |
| **CREATIVE PIPELINE** | **NOT READY** | Brief generation works; asset generation has no provider. |
| **SECURITY** | **FINDINGS** | 1 high, 3 medium, 3 low — see §11. |
| **PHASE 3** | **NO-GO** | Both external dependencies are unverifiable from here. |

**The single most important finding:** the repository cannot talk to Velona at
all. Not "it is untested" — there is no code path that would send a request to
`velona.in` even with the key present. §3.1 shows exactly why.

---

## 1. Repository audit

| Fact | Value |
| --- | --- |
| Tracked files | 194 |
| Top-level | `backend/`, `frontend/`, `docs/`, `scripts/`, `.env.example`, `README.md` |
| Git status at audit start | clean, `e2552a7` |
| Backend | FastAPI, SQLAlchemy async, Alembic, LangGraph, httpx, pydantic |
| Frontend | React + Vite + Tailwind, 4 Phase 2 surfaces |
| Tests | **365 passing** (`/tmp/j12.xml`): 353 from Phases 1–2 + 12 new audit-guard tests |
| Flow Connector source | **absent** (see §7) |

Evidence:

```
$ git log --oneline -1
e2552a7 Phase 2 M11: final report

$ git ls-files | wc -l
194

$ git ls-files | grep -iE "flow|extension|manifest|chrome|mcp"
backend/app/agents/creative.py      ← "creative", not "Flow"
backend/app/api/v1/creative.py
backend/app/models/creative.py
backend/app/models/workflow.py
backend/app/schemas/creative.py
backend/app/services/creative.py
backend/tests/test_director_workflow.py
backend/tests/test_strategy_workflow.py
frontend/src/pages/CreativeStudio.tsx
```

`grep -ri "flow connector|chrome extension|google flow"` across all tracked
`.md/.py/.ts/.tsx/.json` → **zero matches**. Google Flow is not mentioned
anywhere in this codebase, in any document, at any phase.

---

## 2. Model gateway architecture

```
Agent / service
     │  LLMGateway.structured(agent_name, request, task_class, budget, dedupe, cancel)
     ▼
┌─────────────────────────────────────────────────────────────────────┐
│ app/llm/gateway.py  LLMGateway                                      │
│  1. cancel.check()                                                  │
│  2. ModelRouter.resolve(agent, task_class)  → Route(provider, model) │
│  3. provider.is_configured()?  → ProviderUnavailable if not         │
│  4. dedupe.get(key)            → cached response costs nothing      │
│  5. estimate_cost(...)         → CostEstimate                       │
│  6. BudgetGuard.check(estimate) + monthly cap check                 │
│  7. provider.structured(request)                                    │
│  8. budget.record(actual) / dedupe.put / UsageService.record         │
└─────────────────────────────────────────────────────────────────────┘
     ▼
┌──────────────────┬──────────────────┬───────────────────────┐
│ OpenAIProvider   │ AnthropicProvider│ OpenRouterProvider    │
└──────────────────┴──────────────────┴───────────────────────┘
     │
     ▼
app/services/usage.py → model_usage rows (provider, model, tokens,
                        cost, latency, tier, task_class, attempt)
```

### Findings against the audit checklist

| Item | Finding | Evidence |
| --- | --- | --- |
| Provider registration | Static tuple in `build_providers()`; no entry-point/plugin mechanism | `providers.py:208-216` |
| Model routing | 4-tier precedence: explicit task override → agent override → tier default → global default | `gateway.py:76-140` |
| API endpoint config | **Hardcoded per provider class.** No `base_url` setting | `providers.py:52,125,183` |
| Authentication | `Authorization: Bearer <key>` (OpenAI/OpenRouter); `x-api-key` (Anthropic) | `providers.py:76,184` |
| Request schema | `LLMRequest`: system/user prompt, `schema_name`, `json_schema`, `max_tokens`, `temperature`, `purpose` | `base.py:33-45` |
| Response schema | `LLMResponse`: provider, model, parsed `data`, prompt/completion tokens, `cost_usd`, `latency_ms`, `raw_text` | `base.py:47-60` |
| Streaming | **Not supported.** No `stream` anywhere in `app/llm/` | `grep -rn stream app/llm/` → empty |
| Retry | **Not in the provider layer.** `agent_max_attempts=2` lives in the agent layer; providers fail once and raise | `grep retry providers.py` → docstring only |
| Timeout | `settings.llm_request_timeout_seconds = 60`, passed to `httpx.AsyncClient(timeout=)` | `providers.py:51` |
| Token usage extraction | From the provider's `usage` block; defaults to 0 if absent (never guessed) | `providers.py:92-95` |
| Cost calculation | `ModelPrice.cost()` from `DEFAULT_PRICES` or `LLM_PRICING_JSON` overrides | `base.py:73-103` |
| Error normalisation | All failures → `ProviderUnavailable`; the vendor traceback never reaches a client | `providers.py:78-84` |
| Budget enforcement | Pre-call `BudgetGuard.check()` **and** monthly cap | `gateway.py:275-287` |
| Auxiliary model invocation | Distinct tier; `ModelUsage.tier` persists it separately | `gateway.py:64-74` |
| Logging and redaction | structlog processor regex-masks `authorization/api_key/secret/password/credential` recursively and scrubs bearer tokens | `app/core/logging.py:20-54` |

Streaming being absent matters for Phase 3: a 30-second video generation cannot
be streamed. It must be job-based (submit → poll), which is exactly what §9
proposes.

---

## 3. Provider configuration findings

### 3.1 The blocking defect

`deepseek-v4.1-flash` is configured to route through the **openrouter** provider.
But `OpenRouterProvider` posts to `https://openrouter.ai/api/v1/chat/completions`
— a hardcoded literal, with no way to override it:

```python
# app/llm/providers.py:183
resp = await client.post(
    "https://openrouter.ai/api/v1/chat/completions",
    headers={"Authorization": f"Bearer {self._key}"},
    json=payload,
)
```

`grep -rn "velona"` across the entire repository → **zero matches**.

**Consequence:** pointing `LLM_KEY` at Velona, even with an
OpenAI-compatible endpoint, cannot work today. The gateway would send a Velona
key to OpenRouter. There is no configuration that fixes this — it needs a code
change (a configurable `base_url`, or a generic OpenAI-compatible provider).

This is a Phase 3 implementation task, not an audit finding to be silently
patched. Per the execution rules, **no provider configuration was changed.**

### 3.2 Tier configuration as shipped

| Tier | Provider | Model | Price verified |
| --- | --- | --- | --- |
| reasoning | `openrouter` | `deepseek-v4.1-flash` | **No** |
| auxiliary | `openrouter` | `qwen-3.7-flash` | **No** |

Both are configuration (`LLM_REASONING_PROVIDER/_MODEL`,
`LLM_AUXILIARY_PROVIDER/_MODEL`), not hardcoded in any agent — that requirement
is met.

### 3.3 Credential presence — values never read

| Variable | State |
| --- | --- |
| `OPENAI_API_KEY` | absent |
| `ANTHROPIC_API_KEY` | absent |
| `OPENROUTER_API_KEY` | absent |
| any `LLM_KEY` variable | absent from every env file and from this process |

Names defined in `/tmp/env_acc.sh` and `/tmp/env.sh`:
`DATABASE_URL, PYTHONPATH, APP_ENV, JWT_SECRET, BOOTSTRAP_FOUNDER_EMAIL,
BOOTSTRAP_FOUNDER_PASSWORD, LLM_PROVIDER, LOG_LEVEL, DB_ECHO`. No key variable
among them.

**Important nuance:** the screenshot shows `LLM_KEY` as a *GitHub repository
secret*, updated seconds before the screenshot. GitHub secrets are injected only
into CI runner processes. They are not present in this sandbox, not readable by
this agent, and **not visible to the application at runtime here**. Their
presence proves a secret is stored in GitHub — not that any provider works.

### 3.4 Velona endpoint reachability

```
$ getent hosts velona.in
2606:4700:3035::6815:5bfa velona.in        ← DNS resolves (Cloudflare)
$ curl -sS -o /dev/null -w "%{http_code}" https://velona.in/v1/models
curl: (35) OpenSSL SSL_connect: SSL_ERROR_SYSCALL
HTTP 000
$ curl -sS -o /dev/null -w "%{http_code}" https://api.github.com
HTTP 200                                    ← control: egress works
```

TLS to `velona.in:443` is refused while a control host succeeds, so this is a
sandbox egress restriction specific to that host, not a general outage. Combined
with the hardcoded-base-URL defect, **no live Velona check is possible from this
environment by any route.**

---

## 4. Pricing verification status

| Model | Verified | Input /1k | Output /1k | Source of number |
| --- | --- | --- | --- | --- |
| `deepseek-v4.1-flash` | **No** | $0.004 | $0.016 | Conservative placeholder for any unknown model |
| `qwen-3.7-flash` | **No** | $0.004 | $0.016 | Same |

Neither id appears in `DEFAULT_PRICES` (`grep deepseek|qwen` on the table →
NONE). `price_for_model()` falls back to `UNKNOWN_MODEL_INPUT_PER_1K /
UNKNOWN_MODEL_OUTPUT_PER_1K` and returns `verified=False`, deliberately high so
an unknown model trips a budget check rather than quietly spending.

**Therefore every cost figure in Phases 2 and 3 documentation is an estimate
under placeholder pricing, not a bill.** To verify, supply `LLM_PRICING_JSON`
with Velona's published rate card once it is known; entries there are treated as
verified.

---

## 5. Safe preflight results

New command, no third-party dependencies, **no inference path**:

```
python -m app.cli.preflight            # offline, zero network
python -m app.cli.preflight --network  # + free metadata probes only
python -m app.cli.preflight --json
```

There is no `--generate` flag and no call to `.structured()` in the module
(`grep -c "\.structured(" app/cli/preflight.py` → `0`). Neither CLI is reachable
from any API route or agent (`grep -rn "app.cli" app/` excluding `app/cli/` →
empty), so it cannot be triggered by an HTTP request.

### Actual output on this checkout

```
MODEL GATEWAY PREFLIGHT — no inference requests are sent
==============================================================================
  [PASS] config.valid        settings loaded, both tiers name a registered provider
  [FAIL] credentials.present no LLM credential is set in this environment
  [FAIL] credentials.tier_match  reasoning needs 'openrouter': absent; auxiliary needs 'openrouter': absent
  [PASS] providers.registered    3 provider(s): ['anthropic', 'openai', 'openrouter']
  [SKIP] models.id_known     'deepseek-v4.1-flash' known=False; 'qwen-3.7-flash' known=False
  [PASS] endpoint.format     openrouter → https://openrouter.ai/api/v1/chat/completions
  [FAIL] endpoint.configurable  base URLs hardcoded; a new OpenAI-compatible endpoint needs a code change
  [PASS] gateway.init        gateway built; available=False
  [PASS] budget.configured   strategy $0.06 (≈₹4.98), research $0.12, monthly $100.00
  [PASS] accounting.tiers    ModelUsage.tier + task_class persisted per call
  [FAIL] accounting.pricing  reasoning verified=False; auxiliary verified=False
  [PASS] accounting.estimated_vs_actual  CostEstimate before; ModelUsage.cost_usd after
==============================================================================
  PASS 7   FAIL 4   SKIP 1
  Verdict: BLOCKED
```

With `--network` (still no inference):

```
  [SKIP] network.openai      no credential configured, no authenticated probe attempted
  [SKIP] network.anthropic   no credential configured, no authenticated probe attempted
  [SKIP] network.openrouter  no credential configured, no authenticated probe attempted
  [FAIL] network.reach.https://api.openai.com    ConnectError: cannot connect
  [FAIL] network.reach.https://openrouter.ai     ConnectError: cannot connect
```

Exit code is `1` when any check fails, so this is usable as a CI gate.

### Cost attribution audit

- **Estimated vs actual are distinct objects**: `CostEstimate.estimated_cost_usd`
  before the call; `ModelUsage.cost_usd` after, derived from the provider's own
  usage block.
- **Per-tier separation is real**: `ModelUsage.tier` and `.task_class` are
  indexed columns, so a `GROUP BY tier` gives reasoning vs auxiliary spend.
- **Gap:** the ledger records cost computed from *our* price table, not a figure
  confirmed by the provider. With unverified prices, "actual" cost is really
  "actual tokens × estimated price". Stated plainly rather than papered over.

---

## 6. Optional canary instructions

**Not executed. Requires explicit founder authorization in writing.**

```
python -m app.cli.canary --show-plan                            # free, prints plan
python -m app.cli.canary --i-authorize-paid-calls --max-total-usd 0.05
```

Without `--i-authorize-paid-calls` it prints the plan and exits `0` having made
zero requests. Current plan output:

```
  reasoning  openrouter / deepseek-v4.1-flash
             in $0.004/1k  out $0.016/1k  max_out=100  est=$0.00208  price_verified=False
  auxiliary  openrouter / qwen-3.7-flash
             in $0.004/1k  out $0.016/1k  max_out=100  est=$0.00208  price_verified=False
  requests      : 2 (one per tier, no retries)
  tools/loops   : none
  estimated     : $0.00416 (≈₹0.35)
  ceiling       : $0.05  within=True
  BLOCKED       : 2 model(s) have no verified price; the $0.05 ceiling cannot be guaranteed
```

Design constraints honoured: 2 requests maximum, 100 output tokens each, no
tools, no agent loop, no research retrieval, no retries (`break` after the first
failure), single structured-output task, `$0.05` ceiling enforced by a
`BudgetGuard`.

**The canary self-blocks.** Because both prices are unverified, `_execute()`
refuses before the first request and exits `2`. This is the brief's own rule
("if the provider price is unverified, stop and request authorization") encoded
as a hard gate rather than a comment.

### Prerequisites before the canary can even be attempted

1. A generic OpenAI-compatible provider with a configurable `base_url` (§3.1) —
   otherwise the request goes to OpenRouter. **Not implemented**; Phase 3 work.
2. `LLM_PRICING_JSON` populated from Velona's rate card, or the canary stays
   blocked by design.
3. A credential in the runtime environment (not only in GitHub Secrets).
4. Network egress to `velona.in` from wherever it runs.

---

## 7. Flow Connector architecture

**The Flow Connector source is not available. This section therefore contains no
findings about its internals — only proof that it is absent.**

Evidence:

```
$ git ls-files | grep -icE "manifest|flow-connector|chrome"
0
$ grep -ril "flow connector|chrome extension|google flow" <all tracked source/docs>
(no output)

$ find / -name "manifest.json" -not -path "*/node_modules/*" -not -path "/proc/*"
(no output — no Chrome extension exists anywhere on this filesystem)
$ find / -iname "*.crx" -o -iname "*flow-connector*" -o -iname "*chrome-extension*"
(no output)
```

Consequently **none** of the following could be inspected, because none of the
files exist here: `manifest.json`, background service worker, content scripts,
popup/UI components, runtime message listeners, commands, Google Flow
interaction logic, storage/config, `permissions`, `host_permissions`,
authentication assumptions, asset download behaviour, video generation
behaviour, failure/timeout handling.

### Candidate integration interfaces — status

| Interface | Status | Reason |
| --- | --- | --- |
| 1. Chrome extension messaging (`runtime.sendMessage`, external messaging) | **NOT TESTED** | Requires the extension's source and its `externally_connectable` / `onMessageExternal` declarations. Neither exists here. |
| 2. Local HTTP bridge | **NOT TESTED** | No bridge process, port, or client exists in this repo. Would be new work. |
| 3. MCP server | **NOT TESTED** | No MCP server or MCP client anywhere in the codebase. |
| 4. Browser automation | **NOT TESTED** | No Playwright/Puppeteer dependency (`frontend/package.json`, `backend/requirements.txt`). Also the highest-fragility option and the one most likely to violate Google Flow's terms. |
| 5. Documented public API | **BLOCKED** | No evidence one exists. Nothing in this repo references one. |

**Not proposed as findings:** any behaviour of the extension. A button existing
in its UI would prove nothing, and I have not seen the UI.

### To unblock this section, provide one of:

- the extension repository (or unpacked folder) so `manifest.json` and the
  service worker can be read; or
- an exported `.crx` with instruction to unpack it; or
- confirmation that a documented Flow API exists, with its reference.

---

## 8. Capability matrix

Statuses are per the brief's vocabulary. **Every capability is `BLOCKED`** for
the same single reason: no source exists to inspect and no interface exists to
call. Marking any of them `NOT TESTED` would imply a plausible interface that
merely lacks a test — that would be too generous.

| # | Capability | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Detect extension availability | **BLOCKED** | No `manifest.json`; no known extension ID; no messaging host |
| 2 | Connect to extension | **BLOCKED** | No `onMessageExternal` handler available to inspect; no ID to target |
| 3 | Submit image prompt | **BLOCKED** | No request schema, endpoint, or message contract exists |
| 4 | Submit video prompt | **BLOCKED** | Same |
| 5 | Select aspect ratio | **BLOCKED** | No parameter surface identified |
| 6 | Observe generation status | **BLOCKED** | No polling/subscription interface identified |
| 7 | Detect generation errors | **BLOCKED** | No error taxonomy available |
| 8 | Retrieve generated asset | **BLOCKED** | No retrieval path identified |
| 9 | Download generated asset | **BLOCKED** | No download path identified |
| 10 | Return asset metadata | **BLOCKED** | No metadata schema identified |
| 11 | Handle timeout | **BLOCKED** | No timeout behaviour observable |
| 12 | Cancel generation | **BLOCKED** | No cancel path identified |

Zero capabilities are `VERIFIED`. Zero are `PARTIALLY VERIFIED`. That is the
honest result of the source being absent, and it is the reason Phase 3 is NO-GO.

### What *is* ready on the Crusor side

The receiving half of the pipeline already exists and is not blocked:

| Component | State |
| --- | --- |
| Creative brief generation | Working — `app/agents/creative.py` produces composition, palette, typography, motion notes, negative prompt, aspect ratio from brand visual direction |
| `image_generation_available` flag | `False`, correctly reported |
| Asset registration + SHA-256 checksum | Working — `app/services/creative.py` |
| Approval invalidation on checksum change | Working — a changed checksum invalidates approvals referencing the old asset |
| Storage abstraction | Local + S3 + disabled |
| `CreativeAsset` model | `kind, status, filename, mime, size, storage_key, checksum, width/height, prompt, provider, metadata_json` |

So the perimeter of the integration is defined and tested; only the provider
itself is missing.

---

## 9. Creative provider interface proposal

A contract, not an assumption. It is shaped by what the gateway audit showed:
**no streaming**, so generation must be job-based.

```python
class CreativeProvider(Protocol):
    name: str

    def is_configured(self) -> bool: ...
    def get_capabilities(self) -> CreativeCapabilities: ...

    async def submit_image(self, req: ImageRequest) -> JobRef: ...
    async def submit_video(self, req: VideoRequest) -> JobRef: ...
    async def get_status(self, job: JobRef) -> JobStatus: ...
    async def retrieve_asset(self, job: JobRef) -> AssetPayload: ...
    async def cancel(self, job: JobRef) -> None: ...
```

### Request schema

```jsonc
// ImageRequest
{
  "prompt": "string, required, 1..4000",
  "negative_prompt": ["string"],          // from the Brief's negative constraints
  "aspect_ratio": "1:1|4:5|16:9|9:16",
  "brand_visual_direction": {"background": "#0A0A0B", "accent": "#8B5CF6"},
  "idempotency_key": "sha256(logical_request)",  // required
  "max_cost_usd": 0.05                    // per-submission ceiling, enforced pre-submit
}
```

### Response schema

```jsonc
// JobRef — the job identifier
{
  "provider": "flow-connector",
  "job_id": "provider-native id, string",
  "submitted_at": "ISO-8601",
  "idempotency_key": "echoed"
}

// JobStatus
{
  "state": "queued|running|succeeded|failed|cancelled|timed_out",
  "progress": 0.0,
  "provider_status_raw": "verbatim, for debugging only",
  "error": {"code": "CreativeErrorCode", "message": "string"},
  "asset": AssetPayload | null,
  "cost_usd": 0.0,
  "elapsed_seconds": 0.0
}

// AssetPayload — every field the brief requires be retained
{
  "bytes_ref": "storage key; bytes never in the DB",
  "mime_type": "image/png",
  "size_bytes": 0,
  "width": 0, "height": 0,
  "checksum_sha256": "64 hex chars",
  "original_prompt": "verbatim, never rewritten in place",
  "revised_prompt": "what the provider actually used, if it changed it",
  "provider": "flow-connector",
  "model": "provider/model id",
  "parameters": {...},
  "job_id": "...",
  "created_at": "ISO-8601"
}
```

### Error codes (normalised, vendor text never surfaced to a client)

`PROVIDER_UNAVAILABLE` · `AUTH_FAILED` · `INVALID_PROMPT` · `UNSUPPORTED_ASPECT_RATIO` ·
`QUOTA_EXCEEDED` · `BUDGET_EXCEEDED` · `JOB_NOT_FOUND` · `GENERATION_FAILED` ·
`CONTENT_POLICY_BLOCKED` · `TIMEOUT` · `CANCELLED`

### Cross-cutting rules

| Concern | Rule |
| --- | --- |
| **Idempotency** | Client generates `idempotency_key` from the full logical request. Re-submitting the same key returns the existing `JobRef`; it never pays twice. Mirrors the LLM gateway's `RequestDeduplicator`. |
| **Timeout** | Submit is short (≤30s). Generation is polled with backoff to `creative_max_wait_seconds` (proposed 300s), then transitions to `timed_out` — never an infinite loop. |
| **Retry** | Only idempotent reads (`get_status`, `retrieve_asset`) retry, max 2, exponential backoff. **Submission never auto-retries** — a duplicate video is a duplicate bill. |
| **Authentication boundary** | The provider holds its own credentials. The Director never sees a Flow token. `CreativeProvider` cannot reach `LLMGateway`'s keys and vice versa. |
| **Replaceability** | The Director depends on the protocol only. Flow-Connector-specific details (extension ID, message shape, DOM selectors if any) stay inside the adapter. A second provider — or a future official API — implements the same protocol with no orchestration change. |
| **Never lose an asset** | New bytes create a **new** `CreativeAsset` row. Overwriting is not a code path. `SUPERSEDED` marks the old one; nothing is deleted. |
| **Never silently invalidate** | A changed checksum invalidates approvals referencing the old asset — already enforced in Phase 1 — and the UI must say so. |

### Why this shape

`submit`/`poll` rather than streaming, because the gateway has no streaming and
video generation is inherently long-running. Job-based design also gives
cancellation and timeout for free, which a blocking call cannot.

---

## 10. Proposed creative workflow (design only, not built)

```
Marketing Director
   │ objective + approved strategy version
   ▼
Content Agent ──► Creative Brief (composition, palette, typography,
   │              motion, negative prompt, aspect ratio)
   ▼
Brand Guardian ──► reject on unverified claims / prohibited terms
   │ approved brief
   ▼
Creative Provider (flow-connector adapter, behind CreativeProvider protocol)
   │ JobRef
   ▼
Poll → Generated Asset ──► Asset QA (checksum, dimensions, mime, size,
   │                        brand palette presence)
   ▼
Storage (local|S3) ──► CreativeAsset row: prompt, revised_prompt, provider,
   │                    model, parameters, job_id, created_at, checksum,
   │                    storage_location, status, cost
   ▼
Human Approval ──► binds to the exact asset checksum + strategy version
```

Correspondence to the brief's required retention list — all ten fields map onto
existing or proposed columns:

| Required | Where it lives |
| --- | --- |
| Original prompt | `creative_assets.prompt` |
| Revised prompt | `metadata_json.revised_prompt` |
| Model/provider | `creative_assets.provider`, `metadata_json.model` |
| Generation parameters | `metadata_json.parameters` |
| Job identifier | `metadata_json.job_id` |
| Creation timestamp | `created_at` |
| Asset checksum | `creative_assets.checksum` |
| Storage location | `creative_assets.storage_key` |
| Generation status | `creative_assets.status` |
| Cost when available | `metadata_json.cost_usd` (nullable — "when available" is honoured) |

**No new table is strictly required.** One migration would add the missing
`metadata_json` keys as typed columns if querying demands it. No paid generation
was implemented or executed.

---

## 11. Security findings

Reported separately, per the rules. **No security code was changed during this
audit.**

### HIGH

**S1 — Credential would be sent to the wrong host.**
`OpenRouterProvider` hardcodes `https://openrouter.ai/api/v1/chat/completions`.
If `LLM_KEY` (a Velona key) is supplied as `OPENROUTER_API_KEY` — the obvious
mistake, since the tier provider is `openrouter` — a third-party credential is
transmitted to a different company's servers on every call.
*Impact:* credential disclosure to an unintended third party.
*Recommendation:* add a configurable `base_url` and an explicit
`openai_compatible` provider before any key is used. Until then, do not set
`OPENROUTER_API_KEY` to a Velona key.

### MEDIUM

**S2 — No `base_url` configurability.** Blocks the intended integration (see
§3.1). Operationally an availability risk; security-adjacent because the
workaround is to stuff a new key into an unrelated provider.

**S3 — Unverified pricing can under-report spend.** A real bill could exceed the
`$0.05` canary ceiling while the gateway's accounting reports compliance, because
"actual" cost is tokens × placeholder price. Not exploitable, but it makes a
budget guarantee unsound. *Recommendation:* treat the ceiling as advisory until
`LLM_PRICING_JSON` is populated.

**S4 — `LLM_KEY` as a single undifferentiated GitHub secret.** The name conveys
neither provider nor environment. A CI job that logs env would not be caught by
the redaction regex, which keys off names matching
`api_key|secret|password|credential|authorization` — **`LLM_KEY` does not
match**. *Recommendation:* rename to a matching pattern (e.g.
`LLM_API_KEY`) or extend the redaction regex. Verified: `logging.py:22`.

### LOW

**S5 — No streaming.** Not a vulnerability; a design constraint that forces
polling (and therefore needs explicit timeout handling, or a poller could hang).

**S6 — Extension audit is impossible in this environment.** Any future Flow
Connector code cannot be security-reviewed here. Whatever its permissions and
message passing turn out to be — MV3 `host_permissions` for `google.com`, or
`externally_connectable` without stricter origin checks — **none of it has been
assessed**. This is an open question, not a clean bill of health.

**S7 — No headless browser.** Browser automation was not attempted and should not
be the first choice: it requires broad host permissions, is fragile, and its
terms-of-service position is unclear.

### Clean

| Check | Result |
| --- | --- |
| Hardcoded credentials in source | **None** — all secrets env-driven, no usable defaults |
| Keys in log output | **None** — structlog redaction processor active; `Bearer {key}` only in the request header, never interpolated into a loggable string |
| Keys in this report or commits | **None** — presence only, length only, never a value |
| API key returned in any HTTP response | **None** — `/system/status` reports `configured: true/false` only |

---

## 12. Missing dependencies

| # | Missing | Needed for | Blocks |
| --- | --- | --- | --- |
| 1 | Flow Connector source (or unpacked extension) | Any Flow audit | Capability matrix, §7, §8 |
| 2 | Generic OpenAI-compatible provider with configurable `base_url` | Velona routing | Canary, any live LLM call |
| 3 | Velona price card → `LLM_PRICING_JSON` | Verified cost accounting | Canary (self-blocks today) |
| 4 | Runtime credential (not only GitHub Secrets) | Any authenticated call | Canary |
| 5 | Network egress to `velona.in` | Reachability | Canary from this sandbox |
| 6 | Confirmed Velona model ids for DeepSeek/Qwen | Model mapping | `models.id_known` shows both unknown |
| 7 | Playwright/Puppeteer (only if automation is chosen) | Browser route | Nothing yet — not recommended |
| 8 | Headless browser for UI verification | Visual QA | Any visual claim about the frontend |

Items 2, 3 and 6 are implementation work that belongs to Phase 3. Items 1, 4, 5
are inputs only you can supply.

---

## 13. Implementation risks

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| Flow Connector exposes no stable interface | **High** | Phase 3 creative work has no provider | Decide the fallback now: another provider behind the same protocol, or brief-only creative output |
| Chrome extension automation is fragile (DOM/selector drift) | High | Silent breakage in production | Prefer messaging over automation; contract tests; explicit `GENERATION_FAILED` surfacing |
| Terms-of-service exposure from automating a consumer web product | Medium | Legal/account risk | Get explicit confirmation before automating anything |
| Unverified pricing makes budget guarantees cosmetic | **Medium now, certain if ignored** | Overspend | Populate `LLM_PRICING_JSON` before the first real call |
| Asset duplication on retry | Medium | Duplicate cost | `idempotency_key`; never auto-retry submission |
| Large binaries in Git | Medium | Repo bloat | Storage abstraction already keeps bytes out of the DB; `.gitignore` covers `storage/` |
| Approval drift when an asset is regenerated | Low | Publishing an unapproved image | Checksum-based invalidation (already built, already tested) |
| Sandbox cannot reach Velona, so nothing can be validated here | **Certain** | No live verification in this environment | Run validation where network egress and the credential exist |

---

## 14. Recommended Phase 3 architecture

Ordered so that Phase 3 does not start with the hardest dependency.

**Step 1 — Make the gateway honest before adding anything (no new provider yet).**
Introduce a generic `OpenAICompatibleProvider` whose `base_url`, `api_key_env`
and `model` come from configuration, registered as `llm_compatible`. Leave
`openai`/`anthropic`/`openrouter` untouched. This is additive and does not change
Phase 1/2 behaviour.

**Step 2 — Verify connectivity for real.** With Steps' config in place and the
credential present, run `python -m app.cli.preflight --network` (free metadata
probe) against Velona's `/models`. Only then ask for canary authorization.

**Step 3 — Populate pricing, then canary.** Fill `LLM_PRICING_JSON`; the canary
self-unblocks when prices verify. Expected cost ≈ $0.004 (₹0.35) against a $0.05
ceiling.

**Step 4 — Content Intelligence on the verified gateway.** This is plaintext
generation, so it needs nothing from Flow. It can proceed as soon as Steps 1–3
pass, and it is the larger half of Phase 3's stated scope.

**Step 5 — Creative, behind the protocol, with a local `StubCreativeProvider`
first.** Implement `CreativeProvider`, then a deterministic stub that produces a
real PNG (e.g. a palette-composited placeholder) so the *entire* pipeline — QA,
storage, checksum, approval invalidation — is exercised end-to-end with no paid
generation and no extension. The Flow adapter lands later behind the same
protocol and changes no orchestration code.

**Step 6 — Flow adapter, once the source exists.** Only after §7 is unblocked.

This ordering means Phase 3 delivers value even if Flow never becomes
integrable, and it never blocks Content Intelligence on the creative dependency.

---

## 15. Unverified assumptions

Explicit list. None of these has been confirmed, and none is relied on by any
code shipped today.

1. That `velona.in` offers an OpenAI-compatible `/chat/completions` endpoint.
2. That `deepseek-v4.1-flash` and `qwen-3.7-flash` are the exact model ids Velona
   expects (both are `unknown` to the router's price table).
3. That Velona's pricing resembles the placeholder rate card. Unknown.
4. That `LLM_KEY` contains a Velona credential rather than something else.
5. That the Flow Connector exists as a Chrome extension with an inspectable source.
6. That the Flow Connector exposes **any** programmatic interface.
7. That either model supports reliable `response_format: json_object`.
8. That 100 output tokens is enough for a meaningful structured canary response.
9. That Google Flow permits programmatic generation under its terms.
10. That the sandbox's `velona.in` block is policy rather than a temporary fault.

Assumption 1 and 2 are the ones to confirm first — the connectivity probe in
Step 2 tests both in one free call.

---

## 16. Acceptance criteria — status

| Criterion | Status | Evidence |
| --- | --- | --- |
| Existing model gateway documented | **DONE** | §2 |
| DeepSeek configuration inspected | **DONE** | §3.2, §3.3 |
| Qwen configuration inspected | **DONE** | §3.2, §3.3 |
| Credential presence checked safely | **DONE** | §3.3 — presence/length only |
| No-spend provider preflight available | **DONE** | §5 — `python -m app.cli.preflight` |
| Model usage and cost attribution audited | **DONE** | §5, §4 |
| Flow Connector source inspected | **BLOCKED** | Source does not exist here (§7) |
| Actual extension communication interfaces identified | **BLOCKED** | §7 — zero interfaces found |
| Creative capability matrix complete | **DONE** | §8 — all 12 `BLOCKED` with cause |
| Provider-independent creative contract proposed | **DONE** | §9 |
| Security findings documented | **DONE** | §11 — 1 high, 3 medium, 3 low |
| Phase 1 and Phase 2 unchanged | **DONE** | All Phase 1/2 source untouched; 353 pre-existing tests still pass |
| Existing test suites still pass | **DONE** | **365 passed, 0 failed** (`/tmp/j12.xml`) |
| No paid canary executed | **DONE** | Canary is prepared and self-blocking |

Additions this audit introduced (CLI-only, none wired into the app):
`backend/app/cli/preflight.py`, `backend/app/cli/canary.py`,
`backend/app/cli/__init__.py`,
`backend/tests/test_phase3_audit_tooling.py` (12 guard tests).

### A self-inflicted failure worth recording

The first full-suite run after adding the guard tests reported
**365 tests, 168 failures** — while those same tests passed in isolation. Cause:
one guard test called `asyncio.run(...)` from a *sync* test. That closes the
default event loop, and with this project's `asyncio_mode = auto` every
subsequent async test then died with
`RuntimeError: There is no current event loop in thread 'MainThread'`.

Fixed by declaring the test `async def` so pytest-asyncio gives it its own loop.
Recorded here rather than quietly repaired, because it is exactly the class of
environmental coupling that produces false confidence: the suite was green in
isolation and red in aggregate, and only the full run exposed it.

Guard tests now assert: offline preflight constructs no HTTP client; missing
credentials and unverified prices are reported as `FAIL`; no `--generate` flag
exists; the canary never executes without authorization; the canary returns `2`
(refuses) with unverified prices even when authorized; and no application module
imports the canary.

---

## 17. Go / No-Go

**NO-GO for Phase 3 implementation as specified.**

Two independent blockers, either of which alone would justify waiting:

1. **No working model path.** No credential in the runtime, no egress to
   `velona.in`, no configurable base URL, and unverified prices for both models.
   The gateway is architecturally sound — the routing, budget guard, dedup,
   redaction and per-tier accounting all work and are tested — but nothing can
   reach Velona today.
2. **No Flow Connector source.** Twelve of twelve creative capabilities are
   `BLOCKED` because there is no code to inspect. Any Phase 3 creative work now
   would be built against imagined interfaces, and would violate the rule
   against claiming an integration works on the strength of mocks.

**Recommended next actions, in order:**

1. Add a configurable `OpenAICompatibleProvider` (additive; ~1 hour).
2. Put a Velona credential in the runtime environment, not only GitHub Secrets.
3. Run `python -m app.cli.preflight --network` and share the output.
4. Supply Velona's price card → `LLM_PRICING_JSON` → canary unblocks.
5. Supply the Flow Connector source → §7/§8 can be completed.
6. Meanwhile, Phase 3 **Content Intelligence** can proceed after step 1–3, since
   it needs no Flow and no images.

**What this audit did not do, deliberately:** rebuild Phase 1/2, change provider
configuration to make checks pass, publish or schedule anything, generate any
paid image or video, spend any credit, or print any secret.
