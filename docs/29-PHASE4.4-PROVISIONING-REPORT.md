# 29 — Phase 4.4: founder provisioning and external activation

Phase 4.4 was meant to be operational: provision credentials, run one real creative
canary, complete one real OAuth, publish one test post with the founder watching,
measure it, validate three ad accounts.

**It stopped at step 1.** This environment cannot complete a TLS handshake with any
provider, and no credential exists for any of them. Both are external to the code.
This document records the measurement, the provisioning that is ready to be
applied, and the exact list of what the founder must supply.

Nothing below is a guess: every number was produced by
`scripts/environment_preflight.py` on 2026-11-04 and stored in
`docs/preflight.json`.

---

## 1. Environment preflight

### Method

Per provider: DNS resolution → TCP connect to 443 → **verified** TLS handshake
(`ssl.create_default_context()`; verification is never disabled, no certificate is
pinned or bypassed) → one credential-free HTTPS request. Then: proxy variables,
custom CA variables, and the *names* of the environment variables each integration
needs.

### Result

| Provider host | DNS | TCP 443 | Verified TLS | HTTPS | Reachable |
| --- | --- | --- | --- | --- | --- |
| `api.openai.com` | ok | ok | **fail** | not attempted | **no** |
| `api.replicate.com` | ok | ok | **fail** | not attempted | **no** |
| `graph.facebook.com` / `www.facebook.com` | ok | ok | **fail** | not attempted | **no** |
| `api.linkedin.com` / `www.linkedin.com` | ok | ok | **fail** | not attempted | **no** |
| `api.twitter.com` / `twitter.com` | ok | ok | **fail** | not attempted | **no** |
| `oauth2.googleapis.com` / `www.googleapis.com` | ok | ok | **fail** | not attempted | **no** |
| `oauth.reddit.com` / `www.reddit.com` | ok | ok | **fail** | not attempted | **no** |
| `googleads.googleapis.com` | ok | ok | **fail** | not attempted | **no** |

Every failure is the same one, in 20–100 ms:

```text
SSLZeroReturnError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
  → blocked_by: no TLS response (SNI/policy block or proxy refusal)
```

### What kind of restriction this is

The pattern is specific enough to name:

| Test | Result | Interpretation |
| --- | --- | --- |
| `github.com`, `pypi.org`, `registry.npmjs.org` | TLS succeeds | an allowlist exists |
| `github.com` certificate issuer | `organizationName=E2B` | the sandbox terminates TLS for allowed hosts with its own CA |
| `example.com`, `api.anthropic.com`, all providers | `SSLZeroReturnError`, no certificate returned | not on the allowlist; the connection is closed before a certificate is offered |

So: **DNS is fine, routing is fine, and an egress policy refuses these names.** It
is not a certificate problem, not a clock problem, and not a proxy variable problem
— `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` are all unset and no custom CA variables
are set. There is nothing to fix in this repository, and nothing was bypassed.

---

## 2. Credentials

### Present now

**None.** `scripts/provision_credentials.py check` reports every tier `MISSING`:

| Tier | Missing |
| --- | --- |
| core | `SECRET_KEY`, `DATABASE_URL` |
| vault | `TOKEN_VAULT_BACKEND`, `TOKEN_VAULT_KEY` |
| oauth_redirect | `OAUTH_REDIRECT_BASE_URL` |
| llm | `LLM_PROVIDER`, `OPENAI_API_KEY` |
| creative_image | `CREATIVE_IMAGE_PROVIDER`, `OPENAI_API_KEY`, `CREATIVE_PRICING_JSON`, `CREATIVE_CAMPAIGN_BUDGET_USD`, `CREATIVE_ALLOW_PAID_GENERATION` |
| creative_video | `CREATIVE_VIDEO_PROVIDER`, `REPLICATE_API_TOKEN`, `REPLICATE_VIDEO_MODEL`, pricing, budget, switch |
| publishing | `PUBLISHING_ENABLED` |
| social:facebook / instagram / linkedin / x / youtube / reddit | each platform's client id + secret |
| ads:meta_ads / google_ads / linkedin_ads | each platform's own ads credentials |

### Tooling added (no secret ever passes through a log)

```bash
# 1. generate TOKEN_VAULT_KEY into a 0600 file OUTSIDE the repo; the value is
#    never printed, and an existing file is never overwritten.
python ../scripts/provision_credentials.py keygen --out ~/crusor-vault.key

# 2. load it into the deployment's secret store, then delete the file
export TOKEN_VAULT_KEY="$(cut -d= -f2- ~/crusor-vault.key)"
rm ~/crusor-vault.key

# 3. validate the whole deployment before running any canary
python ../scripts/provision_credentials.py check --env-file /etc/crusor/env
```

`check` validates *shape*, which is where people lose hours: a 63-character vault
key, an `http://` redirect, `REPLICATE_VIDEO_MODEL=owner/model` without the pinned
version, a Google customer id that is not 10 digits, a LinkedIn account that is not
a `urn:li:sponsoredAccount:`. It reports names and verdicts only.

### What the founder must supply, in order

1. **A deployment host with provider egress.** Without it, nothing below can be
   verified. This is the blocking item.
2. `SECRET_KEY` (≥ 32 chars), `DATABASE_URL` (PostgreSQL 16 + pgvector).
3. `TOKEN_VAULT_BACKEND=db` + `TOKEN_VAULT_KEY` (from `keygen`).
4. `OAUTH_REDIRECT_BASE_URL` — an HTTPS URL, registered in each provider app.
5. One LLM provider (`LLM_PROVIDER` + its key) and verified pricing
   (`LLM_PRICING_JSON`), otherwise budget checks refuse before spending.
6. One creative provider: `OPENAI_API_KEY` (+ `CREATIVE_PRICING_JSON` with the
   contracted image price, a non-zero `CREATIVE_CAMPAIGN_BUDGET_USD`, and
   `CREATIVE_ALLOW_PAID_GENERATION=true`) or `REPLICATE_API_TOKEN` +
   `REPLICATE_VIDEO_MODEL=owner/model:version`.
7. One social platform's OAuth app — LinkedIn is the shortest path, because a
   personal profile UGC post needs `w_member_social` and no organisation approval.
8. Optionally, ad credentials per platform (see §6).

---

## 3. First real creative canary — **not run**

| Step the phase asks for | State |
| --- | --- |
| Provider configured | no — `OPENAI_API_KEY` and `REPLICATE_API_TOKEN` unset |
| Price checked before submission | implemented (`_estimate_cost`; unknown price is refused, never treated as $0) |
| Explicit authorisation for spend | implemented (`--allow-spend` requires a signed authorisation file) |
| Submit one controlled job | **blocked** — no credential, no egress |
| Poll real status | not reached |
| Retrieve and verify output | not reached |
| Persist + campaign/version association | implemented and proven with the mock provider (acceptance 37–43) |

The exact command that will do it once §2 is satisfied:

```bash
python ../scripts/integration_canary.py --provider openai_image --network --live
```

and, with the founder's signed ceiling in place:

```bash
python ../scripts/integration_canary.py --provider openai_image --network --live \
  --allow-spend --founder-authorization ~/authorization.json
```

---

## 4. First social canary — **not run**

| Step | State |
| --- | --- |
| Complete real OAuth | **blocked** — no OAuth app, redirect not HTTPS, vault `none`, no egress |
| Verify identity and permissions | not reached |
| Store credentials in the encrypted vault | implemented (AES-256-GCM, HKDF-derived key); proven by unit tests |
| Refresh the token | implemented (per-provider refresh paths, `offline.access` for X, `prompt=consent` for Google) |
| Prepare a test post | implemented (platform validators refuse unsupported shapes locally) |
| Founder approval before publishing | implemented and enforced end-to-end (`test_phase4_e2e_lifecycle.py`) |
| Read back the published object, external id + permalink | implemented (`publish()` always follows with `verify()`); never executed against a platform |

Recommended first platform: **LinkedIn personal profile**. It needs one OAuth app,
one scope (`w_member_social`), no organisation review, and produces a real permalink
that the read-back path can compare.

---

## 5. Analytics canary — **not run**

`POST /api/v1/analytics/refresh` (and the Celery `refresh_analytics` job) return
`available: false` with a reason rather than zeros. The lifecycle run recorded
exactly that:

```json
{"available": false, "ingested": 0, "skipped": 0,
 "unavailable": [{"platform": "all",
   "reason": "token vault is disabled (TOKEN_VAULT_BACKEND=none); OAuth connections are unavailable"}],
 "detail": "Analytics ingestion is disabled: no credential vault."}
```

Nothing was fabricated, and nothing was zero-filled. Once a post exists on a
connected account, the same endpoint returns the platform's numbers and lists the
metrics the platform does not expose (for X, impressions are not public; for
Facebook, insight windows are bounded).

---

## 6. Advertising — **not run**

| Platform | Account permissions validated | Draft/paused campaign created | Spend |
| --- | --- | --- | --- |
| Meta Marketing API (v21.0) | no — no credentials | no | $0.00 |
| Google Ads API (v17) | no — no credentials | no | $0.00 |
| LinkedIn Marketing API (202405) | no — no credentials | no | $0.00 |

Read-only validation is wired (`validate_account()` per transport, exposed through
`POST /api/v1/ads/campaigns/{id}/validate-account` and
`integration_canary.py --provider meta_ads --live`). Creation is PAUSED-only by
design and activation needs a founder authorisation bound to the proposal hash.

---

## 7. Matrix and the full Director workflow

`docs/25-platform-readiness.md` was regenerated with the Phase 4.4 columns:
**implemented · mock-tested · credentials · network reachable · authenticated ·
sandbox-verified · production-verified · exact blockers**. `network reachable` is
measured from `docs/preflight.json`, not assumed.

The complete Director lifecycle was run again against a live server
(`docs/lifecycle-run.json`, 7/7 governance checks pass, 25 trace entries, 26
checkpoints). It recorded its own runtime, which is the honest caveat on that
evidence:

```json
{"llm_configured": true, "real_providers_in_use": false,
 "creative_provider": "mock", "publishing": "unavailable",
 "publishing_reason": "token vault is not configured …"}
```

So: **the workflow runs end to end, through mock providers, because no real
provider can be reached.** Running it "with real integrations" is precisely what
§1 forbids in this environment. The moment a host with egress and credentials
exists, the same command produces the real thing:

```bash
LIFECYCLE_EMAIL=… LIFECYCLE_PASSWORD=… \
  python ../scripts/lifecycle_evidence.py --base-url https://… --write
```

---

## 8. Stop point

Phase 4.4 stops here, at two external blockers, neither of which is code:

1. **No provider egress.** Verified TLS fails for every provider host with
   `SSLZeroReturnError`; DNS and TCP are fine, and allowed hosts show a
   sandbox-issued certificate, so an egress allowlist is the mechanism.
2. **No credentials.** Nothing is configured, and none will be invented.

Everything that could be built without them is built and tested: the preflight
(`environment_preflight.py`), the provisioning tool (`provision_credentials.py`),
the canary harness, the lifecycle evidence runner, the six-column matrix, and 32
new tests covering their refusal paths (suite: 1308 passed on SQLite).

```text
ENVIRONMENT PREFLIGHT:   MEASURED — DNS ok, TCP ok, TLS blocked for every provider
PROVIDER EGRESS:         BLOCKED (allowlist; nothing bypassed)
CREDENTIALS:             NONE CONFIGURED
CREATIVE CANARY:         NOT RUN
SOCIAL CANARY:           NOT RUN
ANALYTICS CANARY:        NOT RUN
ADS VALIDATION:          NOT RUN
DIRECTOR LIFECYCLE:      EXECUTED — mock providers, real governance
FLOW:                    DISABLED — provenance unverified
PRODUCTION READINESS:    NOT CLAIMED
```

**Release recommendation unchanged: do not release as production-ready.** The
product is provisionable and testable; it is not verified, because it has never
spoken to a provider. Two founder actions unblock the rest, in this order:
a host with egress, then the credentials in §2.
