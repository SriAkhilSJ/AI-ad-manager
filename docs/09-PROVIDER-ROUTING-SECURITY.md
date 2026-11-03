# Phase 2.5 — Model Gateway Security & Provider Routing Remediation

**Product:** Crusor AI Marketing Director
**Baseline:** Phase 1 `43d633e`, Phase 2 `e2552a7`, Phase 3 audit `784c21b`
**Phase 2.5 head:** see §9
**Date:** 2026-11-03
**Scope:** credential-exposure audit, provider-independent routing, security
controls, regression tests. **No Phase 3 work. No database change. No paid call.**

> No secret value appears anywhere in this document, in any command output, or in
> any commit. Credential checks report presence and length only.

---

## 1. Executive summary

| Question | Answer |
| --- | --- |
| Could `LLM_KEY` have been used as an OpenRouter credential? | **Yes — trivially, and that was the documented path.** |
| Was a credential actually leaked? | **No evidence of it.** No CI workflow exists in this repository; no request left this environment. |
| Is the defect fixed? | **Yes.** Each provider is bound to its own credential variable and its own endpoint, enforced in code. |
| Are DeepSeek / Qwen verified? | **No.** Unchanged from Phase 3 audit: unconfigured, unpriced, unreachable. |

Phase 2.5 triaged **one latent HIGH-severity credential-routing defect** and
removed it by construction rather than by convention. It did not, and could not,
verify either model.

---

## 2. Credential exposure audit

### 2.1 What the code did before

```python
# app/llm/providers.py (Phase 2)
class OpenRouterProvider:
    def __init__(self, settings):
        self._key = settings.openrouter_api_key       # read from OPENROUTER_API_KEY
    async def structured(self, request):
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",   # hardcoded
            headers={"Authorization": f"Bearer {self._key}"},
        )
```

And the tier configuration in `app/core/config.py`:

```python
llm_reasoning_provider = "openrouter"
llm_reasoning_model    = "deepseek-v4.1-flash"
llm_auxiliary_provider = "openrouter"
llm_auxiliary_model    = "qwen-3.7-flash"
```

`.env.example` advertised exactly three credential variables:
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`.

### 2.2 The finding

To make DeepSeek/Qwen work, the only available knob was
`OPENROUTER_API_KEY`. The model ids (`deepseek-v4.1-flash`,
`qwen-3.7-flash`) are not OpenRouter model ids, which means the natural
conclusion for an operator holding a **Velona** key would be: put it in
`OPENROUTER_API_KEY`, because that is the variable the tier points at.

If that had happened, **every inference request would have transmitted a Velona
credential to `openrouter.ai` in an `Authorization: Bearer` header.**

This is a real defect because:

1. the tier says `openrouter`, so the wrong-variable mistake is the *obvious* one;
2. the endpoint was a hardcoded literal, so no configuration could redirect it;
3. `.env.example` offered no Velona variable to use instead;
4. nothing in the code validated that a credential belonged to the host receiving it;
5. the failure is silent — the request would simply 401 at OpenRouter, and an
   operator would see "provider unavailable", not "your key went to the wrong company".

**Severity: HIGH (CWE-522, insufficiently protected credentials — disclosure to
an unintended third party).**

### 2.3 Was a credential actually exposed? — No evidence

I checked every place a credential could have been sent, and found no evidence
of transmission. Stated precisely, per the "do not claim a leak without
evidence" rule:

| Check | Result |
| --- | --- |
| CI workflows invoking model requests | **None exist.** No `.github/` directory now, and `git log --all --diff-filter=A` finds no workflow file ever committed. The two "workflow" hits were commits that added `app/models/workflow.py` and two test files. |
| Workflow files in history of any branch | None. Only `backend/alembic.ini` and `backend/pytest.ini` are tracked `.ini` files; `alembic.ini` contains no secret (`sqlalchemy.url =` is empty by design, resolved from settings). |
| Code paths that construct a gateway | `app/api/v1/agents.py:83`, `app/director/nodes.py:54`, `app/director/runtime.py:57`, `app/director/strategy_nodes.py:77`, `app/director/strategy_runtime.py:55` — all reachable via HTTP, **all gated on a key being present** |
| Credential present in any runtime env | **No.** `LLM_KEY` is a GitHub *repository secret*, injected only into CI runner processes. It is not in this sandbox and not readable by the app here. |

**Conclusion:** the exposure was *latent and reachable*, not *realised*. Because
no credential was ever present in a running instance of this application, and no
CI job exists in this repository to run one, **no request can have carried a
Velona key to OpenRouter from this codebase.**

I cannot speak for environments outside this repository — see §8.

### 2.4 A second, smaller finding

`app/core/logging.py` redacts values whose **key** matches
`authorization|api[_-]?key|secret|password|credential`. The name **`LLM_KEY`
does not match that pattern.** If `LLM_KEY` were ever bound to a settings field
and logged under its own name, the value would pass through the redaction
processor unredacted. Flagged as MEDIUM in §7; mitigated now because the field
no longer exists (Phase 2.5 uses `VELONA_API_KEY`, which does match).

---

## 3. Implementation

### 3.1 New module: `app/llm/openai_compatible.py`

A single, provider-independent transport. Nothing vendor-specific lives in it.

| Requirement | Implementation |
| --- | --- |
| Configurable API base URL | `base_url` constructor argument, validated then normalised (trailing slash stripped) |
| Provider-specific authentication | One `api_key` + one `env_var` per instance; `Authorization: Bearer` by default |
| Provider-specific model identifiers | `default_model`, overridable per request via `request.metadata["model"]` |
| Configurable timeout | `timeout_seconds`, applied to the owned `httpx.AsyncClient` |
| Existing error normalisation | Every failure raises `ProviderUnavailable`, matching Phase 1/2 |
| Usage extraction | `usage.prompt_tokens` / `usage.completion_tokens`, `0` when absent (never guessed) |
| Retry limits | `RetryPolicy(max_attempts, backoff)` — see §4.5 |
| No cross-provider fallback | The module has no routing logic at all; the gateway raises instead of substituting |

### 3.2 Provider inventory after Phase 2.5

| Provider | Credential variable | Endpoint source | Notes |
| --- | --- | --- | --- |
| `openai` | `OPENAI_API_KEY` | unchanged, hardcoded | Not touched in this phase |
| `anthropic` | `ANTHROPIC_API_KEY` | unchanged, hardcoded | Not touched |
| `openrouter` | `OPENROUTER_API_KEY` | **`OPENROUTER_BASE_URL`** (was hardcoded) | Now a thin wrapper over the shared transport |
| `velona` | **`VELONA_API_KEY`** | **`VELONA_BASE_URL`** (must be supplied; never guessed) | **New** |

New settings (`app/core/config.py`):

```python
openrouter_base_url: str = "https://openrouter.ai/api/v1"
openrouter_timeout_seconds: int = 60
openrouter_max_retries: int = 2
velona_api_key: str | None = None
velona_base_url: str | None = None
velona_timeout_seconds: int = 60
velona_max_retries: int = 2
llm_allow_http_localhost: bool = True
```

Default tiers moved from `openrouter` to **`velona`**, so the credential that is
sent belongs to the host that receives it. Model ids are unchanged and remain
configuration:

```
LLM_REASONING_PROVIDER=velona    LLM_REASONING_MODEL=deepseek-v4.1-flash
LLM_AUXILIARY_PROVIDER=velona    LLM_AUXILIARY_MODEL=qwen-3.7-flash
```

An operator who prefers OpenRouter sets both tier providers back to
`openrouter`; nothing in agent code names a provider.

### 3.3 Structural guarantee: providers cannot see each other's keys

```python
def build_providers(settings, transport=None):
    return {p.name: p for p in (
        OpenAIProvider(settings),
        AnthropicProvider(settings),
        OpenRouterProvider(settings, transport=transport),
        VelonaProvider(settings, transport=transport),
    )}
```

`OpenAICompatibleProvider.__init__` accepts plain values (`api_key: str | None`),
**never a `Settings` object**. A Velona instance therefore has no reference from
which it could obtain `openrouter_api_key`. This is asserted by
`test_providers_receive_values_not_the_settings_object`.

The gateway adds a second layer:

```python
provider = self.providers.get(route.provider)
if provider is None:
    raise ProviderUnavailable(route.provider,
        "provider is not registered; refusing to substitute another")
if not provider.is_configured():
    raise ProviderUnavailable(route.provider, "<VAR> is not set")
```

A miss is a hard failure. There is no loop over remaining providers, so a
misconfigured tier cannot silently spend on a different vendor.

### 3.4 Velona fails closed

```python
def __init__(self, settings, ...):
    base_url = settings.velona_base_url
    if not base_url:
        base_url = "https://unconfigured.invalid/v1"   # valid shape, unusable
    ...
    self._url_supplied = bool(settings.velona_base_url)

def is_configured(self) -> bool:
    return self._impl.is_configured() and self._url_supplied   # BOTH required

async def structured(self, request):
    if not self._url_supplied:
        raise ProviderConfigError(
            "VELONA_BASE_URL is not set; the Velona endpoint must be supplied "
            "through configuration and must not be guessed")
```

`unconfigured.invalid` is RFC-6761 reserved, so even a bug that bypassed the
guard could not reach a real service. **The endpoint is never invented** — that
was an explicit requirement and it is enforced, not merely documented.

---

## 4. Security controls

### 4.1 HTTPS enforcement — `EndpointPolicy.check()`

| Input | Outcome |
| --- | --- |
| `https://…` | accepted |
| `http://localhost`, `http://127.0.0.1`, `http://[::1]` | accepted (loopback only) |
| `http://api.velona.example/v1` | **rejected** — "plain HTTP is refused for non-loopback host" |
| `http://10.0.0.5/v1`, `http://93.184.216.34/v1` | **rejected** (public IP is not loopback) |
| `http://localhost` with `allow_http_localhost=False` | **rejected** |
| `ftp://`, `javascript:`, `not-a-url`, `https://` | **rejected** |
| `https://user:pass@host/v1` | **rejected** — embedded credentials |
| `https://host/v1?key=abc` | **rejected** — query string/fragment |

Validation happens **at construction**, so a bad endpoint is caught at startup,
not after a token has been written to a socket.

### 4.2 Credential → host binding

- `velona` reads **only** `VELONA_API_KEY`; `openrouter` reads **only**
  `OPENROUTER_API_KEY`.
- The tier default provider and the credential are configured together; a
  mismatch produces a named failure, not a silent substitution.
- Asserted both ways: Velona's key never appears on an OpenRouter request, and
  OpenRouter's key never appears on a Velona one
  (`test_velona_sends_only_the_velona_key`,
  `test_openrouter_sends_only_the_openrouter_key`), including the case where the
  *other* credential is the only one present
  (`test_openrouter_credential_is_never_sent_to_velonas_endpoint`).

### 4.3 Redirect safety

`httpx` does not follow redirects by default; Phase 2.5 makes that explicit and
adds a positive check:

```python
self._client = httpx.AsyncClient(timeout=..., follow_redirects=False, ...)

if 300 <= resp.status_code < 400:
    raise ProviderUnavailable(self.name,
        "server responded with a redirect; refusing to forward credentials")
```

A 3xx is treated as a **hard security failure**, not a transient fault, so it is
never retried and the redirect target is never contacted. Covered for 301, 302,
303, 307 and 308.

### 4.4 Logging and redaction

- The key lives in `self._api_key` and appears only in the `Authorization`
  header construction. It is never interpolated into a log message, an exception
  message, or `describe()`.
- `describe()` deliberately omits even the **variable name**: an existing test
  (`test_status_reports_unavailable_integrations_honestly`) forbids `api_key`
  appearing in the `/api/v1/system/status` payload. My first attempt exposed
  `credential_env: "VELONA_API_KEY"` there and that guard caught it — see §6.3.
  The variable name remains available internally for the operator-facing
  preflight CLI.
- Transport errors are surfaced as `type(exc).__name__` only, never `str(exc)`,
  because httpx can echo the URL (and therefore any query material) in its text.

### 4.5 Retry limits

| Condition | Retried? | Rationale |
| --- | --- | --- |
| 3xx redirect | **No** | Security signal; see §4.3 |
| 401/403/404/422 | **No** | A bad key or bad payload fails identically; retrying only burns quota |
| 429 | Yes | Rate limit is transient |
| 5xx | Yes | Server-side, transient |
| Transport error / timeout | Yes | Request never completed |
| `max_attempts=0` | `ProviderConfigError` at construction | Fail closed on nonsense config |

Default `max_attempts=2`, exponential backoff from 0.5 s. Bounded by
`VELONA_MAX_RETRIES` / `OPENROUTER_MAX_RETRIES`.

### 4.6 No paid inference during tests

Three independent layers:

1. Every test injects `httpx.MockTransport`.
2. `LLMGateway(..., transport=...)` threads the mock into every provider, so a
   test that forgets cannot construct a live client.
3. `assert_no_live_network_in_tests()` refuses to construct a live transport when
   `APP_ENV=test`.

Plus the audit-level guard from Phase 2.5's predecessor: no application module
imports the canary CLI.

---

## 5. Before / after architecture

### Before

```
tier config: llm_reasoning_provider = "openrouter"
                     │
                     ▼
        ┌────────────────────────────┐
        │ OpenRouterProvider          │
        │   key  = OPENROUTER_API_KEY │◄── the only knob an operator has
        │   url  = hardcoded literal  │    → put a Velona key here and it
        └────────────────────────────┘      leaves for openrouter.ai
```

### After

```
tier config: llm_reasoning_provider = "velona"
                     │
                     ▼
   ┌──────────────────────────────────────────────┐
   │ ModelRouter.resolve()  → Route(provider,model)│
   └──────────────────────────────────────────────┘
                     │
        ┌────────────┴─────────────┐
        ▼                          ▼
┌────────────────────┐   ┌─────────────────────────────────┐
│ OpenRouterProvider │   │ VelonaProvider                  │
│ OPENROUTER_API_KEY │   │ VELONA_API_KEY                  │
│ OPENROUTER_BASE_URL│   │ VELONA_BASE_URL (never guessed) │
└─────────┬──────────┘   └───────────────┬─────────────────┘
          └──────────────┬───────────────┘
                         ▼
        ┌────────────────────────────────────────────┐
        │ OpenAICompatibleProvider  (shared transport)│
        │  · EndpointPolicy: https-only, no embedded  │
        │    credentials, no query strings            │
        │  · follow_redirects=False, 3xx = hard fail  │
        │  · RetryPolicy: bounded, no 3xx/4xx retries │
        │  · usage extraction, error normalisation    │
        └────────────────────────────────────────────────┘
                         │
                         ▼
              BudgetGuard → provider → ModelUsage
              (unchanged from Phase 2)
```

---

## 6. Test evidence

```
$ cd backend && pytest tests/ -q
423 passed, 0 failed, 0 skipped, 0 errors        (/tmp/j21.xml)
```

365 pre-existing + **58 new** in `tests/test_provider_routing_security.py`.

| # | Requirement | Tests | Representative assertion |
| --- | --- | --- | --- |
| 1 | Correct provider selection | 2 | `router.resolve("strategy","strategic_decision").provider == "velona"` |
| 2 | Correct endpoint selection | 4 | `recorder.urls == ["https://api.velona.example/v1/chat/completions"]`; OpenRouter URL is configurable, not hardcoded |
| 3 | Correct credential isolation | 4 | Velona request carries `Bearer velona-test-key…` and **not** the OpenRouter key, and vice versa |
| 4 | Incorrect endpoint rejection | 8 | `ftp://`, `javascript:`, embedded credentials, query strings, remote plain-HTTP |
| 5 | Missing credentials fail safely | 4 | zero requests recorded; error names `VELONA_API_KEY` and contains no key value |
| 6 | HTTPS enforcement | 9 | 3 remote `http://` rejected; 3 loopback accepted; loopback can be forbidden |
| 7 | Redirect safety | 7 | 307 → hard fail, exactly 1 request, target never contacted; no 3xx is retried |
| 8 | Usage attribution | 5 | tokens extracted; missing usage → 0 not a guess; `ModelUsage.tier` = `reasoning`/`auxiliary` per class |
| 9 | Cost tracking intact | 2 | `response.cost_usd > 0` and equals the persisted `ModelUsage.cost_usd` |
| 10 | Budget limits intact | 3 | exhausted budget → `BudgetExceeded` with `recorder.requests == []`; estimate available pre-call |
| 11 | No paid inference | 2 | live transport refused under `APP_ENV=test`; no vendor endpoint literal in `providers.py` |
| 12 | Key hygiene | 3 | `describe()` contains no key value and no `API_KEY` string; error messages contain no key |

### 6.1 The Phase 2 defect, asserted directly

```python
async def test_openrouter_credential_is_never_sent_to_velonas_endpoint():
    velona = VelonaProvider(settings(
        velona_api_key=None,            # no Velona credential exists
        velona_base_url=VELONA_URL,
        openrouter_api_key=OPENROUTER_KEY,   # only an OpenRouter credential
    ), transport=recorder.transport())

    with pytest.raises(ProviderUnavailable) as exc:
        await velona.structured(make_request())

    assert recorder.requests == []          # nothing left the process
    assert "VELONA_API_KEY" in str(exc.value)
    assert OPENROUTER_KEY not in str(exc.value)
```

### 6.2 Preflight after Phase 2.5

```
PASS  config.valid                    PASS  endpoint.configurable
FAIL  credentials.present             PASS  gateway.init
FAIL  credentials.tier_match          PASS  budget.configured
PASS  providers.registered            PASS  accounting.tiers
SKIP  models.id_known                 FAIL  accounting.pricing
PASS  endpoint.format                 PASS  accounting.estimated_vs_actual
PASS  credentials.isolated            SKIP  endpoint.velona
────────────────────────────────────────────────────────────────────
PASS 9   FAIL 3   SKIP 2   →   Verdict: BLOCKED
```

Movement vs the Phase 3 audit (`PASS 7 / FAIL 4 / SKIP 1`):

- `endpoint.configurable` **FAIL → PASS** — the hardcoded base URL is gone.
- `credentials.isolated` **new check, PASS** — proves the binding.
- `endpoint.velona` **new check, SKIP** — correctly reports the endpoint is not
  supplied. It is *not* invented to make the check go green.
- Remaining FAILs are all external facts I cannot change: no credential, no
  Velona endpoint, no verified price.

### 6.3 Failures I caused and fixed during this phase

Recorded rather than quietly repaired.

1. **`/system/status` leaked credential variable names.** My first `describe()`
   returned `credential_env: "VELONA_API_KEY"`. The pre-existing test
   `test_status_reports_unavailable_integrations_honestly` forbids `api_key` in
   that payload and caught it. Fixed by removing the variable name from the
   HTTP-facing description while keeping it internally for the CLI.
2. **A Phase 3 guard test asserted the old defect.** 
   `test_preflight_flags_hardcoded_base_url_as_a_failure` pinned `FAIL` on
   `endpoint.configurable`. It now asserts `PASS` — updated **only because the
   defect was actually fixed**, and renamed to say so.
3. **Four bugs in my own new tests**, each a real lesson:
   - `tier_for_task("")` returns `"default"`, not `"reasoning"`, so step 3 of
     `resolve()` was skipped and the tier-default provider never loaded. Tests
     needed an explicit `task_class`.
   - `gateway.estimate` does not exist; the estimate lives on
     `gateway.router.estimate` and returns a dict.
   - `BudgetGuard(max_cost_usd=0)` means **uncapped**, not "block everything".
     My test asserted the opposite. See the residual risk in §8.
   - `asyncio.run()` inside a sync test (from Phase 3) closed the default event
     loop and broke 168 later async tests — fixed then, still noted here.

---

## 7. Security findings

| ID | Severity | Finding | Status |
| --- | --- | --- | --- |
| **S1** | **HIGH** | A Velona key placed in `OPENROUTER_API_KEY` — the only documented path — would be transmitted to `openrouter.ai` on every inference request. | **Fixed.** Separate variables, separate endpoints, structural isolation, tests both directions. |
| S2 | MEDIUM | `LLM_KEY` does not match the logging redaction regex (`api_key|secret|…`), so if it were ever bound and logged under its own name it would pass through unredacted. | **Mitigated.** The variable no longer exists in this codebase. Recommend fixing the regex anyway (§8). |
| S3 | MEDIUM | `BudgetGuard(max_cost_usd=0)` is **uncapped**, not "spend nothing". An operator setting 0 to forbid spend gets unlimited spend. | **Not fixed — documented and pinned by test.** Changing Phase 2 budget semantics was out of scope. |
| S4 | MEDIUM | Model pricing for both tiers remains unverified; "actual" cost is tokens × placeholder price, so cost accounting can under-report real spend. | **Open.** Requires Velona's rate card via `LLM_PRICING_JSON`. |
| S5 | LOW | The `/system/status` payload discloses configured provider base URLs to any authenticated user. | Accepted: URLs are public information, and it aids diagnosis. No credential material is exposed. |
| S6 | LOW | No streaming support; a future long generation must be job-based. | Unchanged from Phase 3 audit. |
| S7 | LOW | Flow Connector remains unauditable (source absent). | Unchanged; out of scope for Phase 2.5. |

---

## 8. Unresolved provider configuration and remaining risks

### What is still unconfigured

| Item | State | Who can resolve it |
| --- | --- | --- |
| `VELONA_BASE_URL` | **unset** | You — must be a verified URL; Phase 2.5 refuses to guess |
| `VELONA_API_KEY` | **not present in runtime** (exists only as a GitHub secret, invisible here) | You — put it in the runtime environment |
| Verified model ids | `deepseek-v4.1-flash`, `qwen-3.7-flash` are **unknown** to the price table | Velona docs |
| Verified pricing | Placeholder `$0.004 / $0.016` per 1k | Velona rate card → `LLM_PRICING_JSON` |
| Network egress to Velona | Blocked from this sandbox | Run validation where egress exists |

### Residual risks

1. **Exposure outside this repository is unknowable.** If any deployed copy of
   the pre-Phase-2.5 code ran with a Velona key in `OPENROUTER_API_KEY`, requests
   would have gone to OpenRouter. **Recommended action:** in the OpenRouter
   dashboard, review usage/logs for requests carrying unknown model ids
   (`deepseek-v4.1-flash`, `qwen-3.7-flash`) or 401s from unfamiliar keys. If any
   exist, rotate the Velona key. This is the one action I cannot take for you.
2. **`BudgetGuard(0) == uncapped` is a live fail-open trap.** Pinned by
   `test_zero_cost_budget_means_uncapped_and_that_is_a_known_trap`. Recommend a
   sentinel (`None` = uncapped, `0` = forbid) in a later phase.
3. **Pricing remains unverified**, so no cost figure in this project is a bill.
4. **`openai` and `anthropic` providers still hardcode their base URLs.** They
   are unchanged by design (no scope), but they carry the same *class* of defect
   if anyone ever wants to proxy them. Migrating them to the shared transport is
   a small, low-risk follow-up.
5. **No live validation was performed.** Every claim here is about code
   behaviour under mock transport. Whether Velona's API matches the
   OpenAI-compatible contract is **still unknown** — that requires the canary,
   which remains blocked on unverified pricing.

---

## 9. Acceptance

| Criterion | Status |
| --- | --- |
| Credential exposure audited | **DONE** — §2, including "no evidence of leak" |
| Provider-independent OpenAI-compatible abstraction | **DONE** — `app/llm/openai_compatible.py` |
| Configurable base URL, auth, model ids, timeout | **DONE** — §3.1 |
| Existing error normalisation preserved | **DONE** — all failures → `ProviderUnavailable` |
| Usage extraction preserved | **DONE** — §6 row 8 |
| Retry limits | **DONE** — `RetryPolicy`, §4.5 |
| No automatic cross-provider fallback | **DONE** — §3.3, `refusing to substitute` |
| OpenRouter ← `OPENROUTER_API_KEY` | **DONE** |
| Velona ← `VELONA_API_KEY` / `VELONA_BASE_URL` | **DONE** |
| Velona endpoint never invented | **DONE** — fails closed when unset |
| HTTPS required for remote endpoints | **DONE** — §4.1 |
| Credential forwarding restricted to configured hosts | **DONE** — §4.2 |
| Unexpected redirects rejected | **DONE** — §4.3 |
| Keys never logged | **DONE** — §4.4, tested |
| No cross-provider credential reuse | **DONE** — §4.2, tested both directions |
| Fail closed on incomplete config | **DONE** — §3.4 |
| DeepSeek primary / Qwen auxiliary, configurable | **DONE** — unchanged, still configuration |
| Neither model assumed available | **DONE** — preflight reports both unverified |
| Regression tests (11 areas) | **DONE** — 58 tests |
| Mocked HTTP transports only | **DONE** — three layers, §4.6 |
| No database redesign | **DONE** — no migration |
| No Phase 1/2 feature removal | **DONE** — 423 tests, 0 failed |
| No real model requests | **DONE** — no credential, no egress, mock transport |
| No secrets in Git | **DONE** — `.env.example` names variables only |
| No Flow changes | **DONE** — untouched |
| Full suite + preflight + acceptance | **DONE** — 423 tests, preflight 9/3/2, acceptance **23/23** |

### Commands run

```
pytest tests/ -q                                    → 423 passed, 0 failed
python -m app.cli.preflight                         → PASS 9, FAIL 3, SKIP 2 (BLOCKED)
python scripts/acceptance.py                        → 23/23 criteria passed
```

### Files changed

| File | Change |
| --- | --- |
| `backend/app/llm/openai_compatible.py` | **new** — shared transport, endpoint policy, retry policy |
| `backend/app/llm/providers.py` | OpenRouter endpoint now configurable; **new** `VelonaProvider`; registry threads a transport |
| `backend/app/core/config.py` | Velona + configurable-endpoint settings; tier defaults → `velona` |
| `backend/app/llm/gateway.py` | accepts `transport`; names the missing variable; refuses substitution; `describe()`-aware status |
| `backend/app/cli/preflight.py` | Velona-aware; new `credentials.isolated` and `endpoint.velona` checks |
| `backend/tests/test_provider_routing_security.py` | **new** — 58 tests |
| `backend/tests/test_phase3_audit_tooling.py` | one guard updated to assert the fix |
| `.env.example` | documents the per-provider variables; no values |

No migration. No model change. No schema change.

---

## 10. Verdict

| Area | Verdict | Evidence |
| --- | --- | --- |
| **CREDENTIAL EXPOSURE** | **REMEDIATED — no leak evidenced** | §2.3; no CI workflow exists; no credential ever in runtime here |
| **PROVIDER ROUTING** | **READY** | Separate variables and endpoints, enforced in code; 58 tests |
| **DEEPSEEK** | **UNVERIFIED** | No endpoint, no key, price placeholder |
| **QWEN** | **UNVERIFIED** | Same |
| **PHASE 3** | **STILL NO-GO** | Unchanged: Flow source absent; no verified model path |

**What changed for the better:** the gateway can now be pointed at Velona
correctly and safely, and it is no longer *possible* to hand one vendor's
credential to another through configuration. That was the highest-severity
finding from the Phase 3 audit, and it is closed by construction rather than by
a warning in a README.

**What did not change:** neither model is verified. Phase 3 remains NO-GO until
`VELONA_BASE_URL`, `VELONA_API_KEY` and verified pricing exist, and until the
Flow Connector source is supplied.

**The one thing I recommend you do next:** check the OpenRouter dashboard for
requests carrying `deepseek-v4.1-flash` or `qwen-3.7-flash`, or for 401s from an
unrecognised key. That is the only way to rule out exposure in an environment
this audit cannot see.
