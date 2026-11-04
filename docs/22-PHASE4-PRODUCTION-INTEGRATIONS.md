# Phase 4 — Production integrations, platform transports and paid-media governance

**Branch:** `arena/c7e4be51-ai-ad-manager` · **Baseline:** `bc7cde5` (Phase 3C.2.3.4.1) ·
**PR:** #1 · **Date:** 2026-11-04

---

## 1. What this phase was for

Phases 1–3C built an honest system that could not publish. Every adapter was
`transport_available = False`, no OAuth existed, nothing ingested platform
metrics, and there was no paid-media domain at all. That was the correct stance
while the security gates were being closed, but it is not a product a founder can
run a marketing operation on.

Phase 4 builds the missing middle: **real transports that are only reachable
through the existing approval gate**, and a paid-media layer whose default
behaviour is refusal.

Three rules governed the work:

1. **Never weaken an existing gate to make a new feature work.** The approval
   gate, the budget ledger, the Flow provenance gates and the trust policy are
   unchanged. New code sits *behind* them.
2. **Verification is not optimism.** A publish is only recorded after the created
   object is read back from the platform. A metric is only stored if the platform
   returned it. A capability only claims what has actually been exercised.
3. **A refusal is a result, not an error to be swallowed.** Blocked publishes,
   blocked ad launches and failed ingestion each produce a recorded, inspectable
   outcome with a reason.

---

## 2. What was actually built

### 2.1 Credential vault (`app/integrations/crypto.py`, `vault.py`, `models/integration.py`)

| Property | Implementation |
| --- | --- |
| Algorithm | AES-256-GCM, random 96-bit nonce per record |
| Key derivation | HKDF-SHA256 from `TOKEN_VAULT_KEY` (hex / base64 / ≥32-char passphrase) |
| Owner binding | Associated data = `brand_profile_id \| platform \| account_key` |
| At-rest exposure | `oauth_tokens.sealed_access` holds `v1:<nonce>:<ciphertext>`. No plaintext column exists. |
| API exposure | No schema maps to `oauth_tokens`; `social_accounts` still has **no token column**, only `token_ref = "vault:<uuid>"`. |
| Missing key | Vault reports `available = False`; every store/load raises `VaultUnavailableError`. There is no development default key. |
| Cross-account replay | Copying ciphertext between rows fails authentication (AAD mismatch). |
| Rotation | `TokenCipher.rotate_key()` derives a new cipher; the version prefix makes future migrations detectable. |

A revoked credential has its ciphertext erased, not merely flagged.

### 2.2 OAuth (`app/integrations/oauth.py`, `services/oauth.py`)

Real authorisation-code flow for six platforms with pinned endpoints and
per-platform client authentication:

| Platform | Authorize | Token | PKCE | Client auth | Notes |
| --- | --- | --- | --- | --- | --- |
| Facebook | `facebook.com/v21.0/dialog/oauth` | Graph `oauth/access_token` | — | form body | Page posting |
| Instagram | shared Meta app | Graph `oauth/access_token` | — | form body | Media containers |
| LinkedIn | `linkedin.com/oauth/v2/authorization` | `oauth/v2/accessToken` | — | form body | UGC Posts |
| X | `twitter.com/i/oauth2/authorize` | `api.twitter.com/2/oauth2/token` | **S256** | HTTP Basic | `offline.access` for refresh |
| YouTube | `accounts.google.com/o/oauth2/v2/auth` | `oauth2.googleapis.com/token` | — | form body | `access_type=offline`, `prompt=consent` |
| Reddit | `reddit.com/api/v1/authorize` | `api/v1/access_token` | — | HTTP Basic | `duration=permanent` |

Security properties:

* **Signed state** — HMAC-SHA256 over a payload naming brand, platform, redirect
  and expiry; constant-time comparison; absolute expiry.
* **Single use** — one `oauth_flows` row per attempt, `state` unique,
  `consumed_at` set on success. A replayed callback is rejected before any
  exchange.
* **PKCE verifier sealed at rest** with the same cipher and bound to the state
  nonce.
* **No credential sharing** — each platform reads only its own
  `*_client_id`/`*_client_secret`. A missing pair is an explicit refusal; there is
  no fallback to another provider's app.
* **Redirect target allow-listed**; OAuth errors never echo the request body,
  which contains the client secret.
* **Post-exchange identity probe** (`/me`, `/v2/me`, `/2/users/me`,
  `channels.list`, `/api/v1/me`) gives the account a real handle and a chance to
  prove the credential works. A failure is reported as `verified: false` with a
  reason; it never invents a handle.

### 2.3 Platform transports (`app/integrations/platforms.py`)

Each platform client implements `publish`, `verify` and `fetch_metrics`.

| Platform | Publish | Verification | Measured metrics |
| --- | --- | --- | --- |
| Facebook | `POST /{page}/feed` | `GET /{post}` (`id`, `permalink_url`) | shares, comments, reactions |
| Instagram | container → `media_publish` | `GET /{media}` (`permalink`, `media_type`) | reach, likes, comments, saved |
| LinkedIn | `POST /v2/ugcPosts` | `GET /v2/ugcPosts/{urn}` | likes, comments |
| X | `POST /2/tweets` | `GET /2/tweets/{id}` | likes, replies, reposts, quotes, bookmarks |
| YouTube | resumable upload | `GET /youtube/v3/videos?id=` | views, likes, comments, favorites |
| Reddit | `POST /api/submit` | `GET /api/info?id=t3_…` | score, comments, upvote ratio |

**Local refusals before any network call:** Instagram with no media (it has no
text surface), YouTube with no video asset or a non-https URL, X beyond 280
characters, LinkedIn without an `urn:li:`.

Transport-level behaviour (`app/integrations/http.py`): retries are limited to
429/5xx with jitter and `Retry-After`; **create calls are never retried**
(`idempotent=False`) because a retried publish can duplicate a post; 401/403 map
to an auth error rather than a retry; timeouts are failures, never successes.

### 2.4 Publishing wiring (`app/publishing/adapters.py`, `services/publishing.py`)

`publish_enabled` is now true only when **all** of:

1. the platform's own application credentials exist,
2. a usable token vault exists,
3. the resolved account carries a `token_ref`.

The publish path resolves a `LiveAdapter`, refreshes the token if it is within
`TOKEN_REFRESH_SKEW_SECONDS` of expiry, calls the transport, and **checks that the
adapter returned an external id** before writing `PublishedPost`. A result without
an id raises instead of marking content published.

The order of gates is unchanged and observable: approval → channel → transport →
optimistic state claim → adapter. A missing approval still fails before anything
else is considered.

### 2.5 Analytics ingestion (`app/services/analytics_ingest.py`)

`POST /api/v1/social/analytics/ingest` walks published posts in a window, loads
each account's credential, calls the platform's metrics endpoint and writes an
`analytics_snapshots` row **only when at least one value was measured**.

Each snapshot records:

```json
{
  "measured":    {"likes": 7, "comments": 2},
  "unavailable": ["clicks", "impressions", "reach"],
  "derived":     {"engagements": 9},
  "external_post_id": "urn:li:share:123"
}
```

with `source = "platform_api:<endpoint>"`. A platform that refuses, times out or
has no credential contributes a row to `unavailable` — never a zero-filled
snapshot that a chart would render as real performance.

### 2.6 Paid advertising governance (`app/models/ads.py`, `services/ads.py`, `integrations/ads.py`)

The domain exists; the spend gate is real; the transports are not implemented and
say so.

**Proposal** — an agent or an operator may draft a campaign (platform, objective,
plan, targeting, creatives, budgets in cents). A **proposal hash** is computed
over exactly those fields.

**Authorisation** — founder-only, naming the campaign, the hash, a spend ceiling,
an operation allow-list and an expiry (≤7 days, and capped by
`ADS_MAX_AUTHORIZED_SPEND_CENTS`). The authorisation cannot cover less than the
campaign's total budget.

**Execution** — refused unless *all* hold:

* actor role is `founder`;
* the authorisation exists, belongs to this campaign, is `active`, unexpired, and
  lists this operation;
* the **current** proposal hash equals the authorised hash (any edit — budget,
  targeting, creative — invalidates it);
* the campaign budget is within the ceiling;
* an ads transport is implemented and configured.

The transport check happens **before** the authorisation is consumed, so a launch
that cannot proceed does not burn the founder's approval. Consumption is an
`UPDATE … WHERE status='active' AND consumed_at IS NULL` with a rowcount check, so
two concurrent launches cannot both spend one approval.

**Ledger** — `ad_operations` is append-only and records refusals with their
detail: `refused: no founder authorisation`, `refused: the proposal changed after
it was authorised (hash mismatch)`, `refused: ads transport unavailable …`. A
unique `(campaign, kind, fingerprint)` constraint makes a repeated request
idempotent rather than double-counted.

**Transports** — Meta Marketing API, Google Ads API and LinkedIn Marketing API are
declared with their documented endpoints and an explicit reason they cannot run
here (unapproved app / no developer token / access form not granted). `GET
/api/v1/ads/transports` and `GET /api/v1/system/status` both report this.

### 2.7 Scheduled publishing and analytics refresh (`services/scheduler.py`)

Before this, a scheduled post was a row nothing acted on, and `refresh_analytics`
was a placeholder that always returned `skipped`. Both now run:

* `SchedulerService.run_due()` claims due rows with
  `UPDATE … WHERE status='pending'` (rowcount-checked, so two workers cannot
  publish one post), then calls the **same** `PublishingService.publish()` a
  manual publish uses. A schedule whose approval was invalidated by an edit
  therefore fails instead of shipping a stale version.
* Failures increment `attempts`, store the reason in `last_error`, and park the
  row as `failed` after three attempts rather than looping forever on a refusal
  that can never succeed.
* `refresh_analytics` walks the brands and delegates to the ingestion service;
  without a vault it reports `unavailable` per brand and writes nothing.
* Both are registered as Celery jobs (`publish_due`, `refresh_analytics`) and
  exposed as `POST /api/v1/social/scheduled/run` (founder-only).

Writing the tests found a **latent defect in the publish path** that had never
been reachable: `publish()` performed its optimistic `→ PUBLISHING` claim and then
called `approvals.apply(..., "publish")`, whose legality check evaluates the
*current* status — already `PUBLISHING` — so every publish died with
`Action 'publish' is not allowed from status 'PUBLISHING'`. It was invisible while
no transport existed, because the request was refused earlier. The claim now
writes its own audit entry instead, and
`test_due_post_is_published_through_the_normal_gates` is the regression test.

### 2.8 Production engineering

| Piece | File | State |
| --- | --- | --- |
| Backend image (multi-stage, non-root, healthcheck) | `backend/Dockerfile` | written, **not built here** (no Docker in this sandbox) |
| Frontend image (build → nginx, single origin, security headers) | `frontend/Dockerfile`, `frontend/nginx.conf` | written, not built here |
| Stack: PG16 + pgvector, Redis, backend, worker, frontend | `docker-compose.yml` | written; YAML validated; **not run here** |
| CI: backend tests, migrations up/down/up, audit exit-3 assertion, frontend typecheck+build, secret scan | `.github/workflows/ci.yml` | written; **not executed here** |
| Migration `d5e6f7a8b9c0 → e6f7a8b9c0d1` | `alembic/versions/e6f7a8b9c0d1_phase4_social_oauth_and_ads.py` | **executed**: up → down → up, head `e6f7a8b9c0d1` |

---

## 3. Completion matrix

Five columns, because they answer five different questions. **Nothing is marked
verified that was not actually exercised.**

| Capability | Implemented in code | Tested with mocks | Verified on official sandbox | Verified in production | Blocked by |
| --- | --- | --- | --- | --- | --- |
| Approval gate (exact version, founder-only) | yes | yes (existing suite) | n/a | n/a | — |
| Content / creative / brand pipeline | yes | yes | n/a | n/a | — |
| LLM routing, budgets, pricing verification | yes | yes | no | no | provider keys + verified pricing |
| Flow bridge (DeepX) | yes | yes | no | no | source licence, signed release, installed artifact |
| Token vault (AES-256-GCM) | yes | yes (roundtrip, AAD, revoke, cross-row replay) | n/a | n/a | — |
| OAuth start/callback/refresh (6 platforms) | yes | yes (signed state, replay, PKCE, exchange, refresh) | **no** | **no** | approved app per platform |
| Facebook publishing | yes | yes (create + verify + metrics) | **no** | **no** | Meta app w/ `pages_manage_posts`, connected page |
| Instagram publishing | yes (image/video) | yes (capability refusal, container flow) | **no** | **no** | Meta app w/ `instagram_content_publish`, IG business account |
| LinkedIn publishing | yes (text/image) | yes | **no** | **no** | LinkedIn app + community-management approval |
| X publishing | yes (text) | yes (280-char refusal, verify) | **no** | **no** | X app with write access |
| YouTube publishing | yes (video) | no live upload exercised | **no** | **no** | Google OAuth client + quota |
| Reddit publishing | yes (self post) | yes | **no** | **no** | Reddit app + subreddit permissions |
| Scheduled publishing (`run_due`) | yes | yes (publishes, refuses edited approval, no double-publish) | n/a | n/a | — |
| Post verification before PUBLISHED | yes | yes (verify-fails case raises) | no | no | — (logic proven offline) |
| Token refresh on expiry | yes | yes | **no** | **no** | provider credentials |
| Analytics ingestion | yes | yes (measured vs unavailable) | **no** | **no** | connected accounts |
| Ads proposal → authorisation → ledger | yes | yes (hash invalidation, single-use, refusals, idempotency, role gates) | n/a | n/a | — |
| Meta / Google / LinkedIn Ads transports | **no** (declared + refused) | yes (refusal path) | no | no | approved ads app, developer token, billing |
| Docker images + compose | written | YAML validated | no | no | Docker unavailable in this environment |
| CI pipeline | written | YAML validated | no | no | not executed here |
| Migrations | yes | yes | n/a | n/a | — |

### What this means in one sentence

The **gate** is production-grade and proven; the **transports** are implemented
and mock-proven but have never spoken to a real platform, because doing so
requires app approvals and credentials that do not exist in this environment.

---

## 4. Security audit of the new surface

| Threat | Control | Evidence |
| --- | --- | --- |
| Token theft from DB dump | AES-256-GCM, key outside the DB | `test_vault_stores_only_ciphertext_and_roundtrips` |
| Token copied into another account's row | AAD binds ciphertext to owner | `test_vault_ciphertext_cannot_be_moved_between_accounts` |
| Token leak via API | no schema maps to `oauth_tokens`; `SocialAccountOut` is an allow-list | existing `test_security.py` + `test_platform_status_exposes_vault_and_capabilities` |
| Forged OAuth callback | HMAC state + expiry + brand binding | `test_signed_state_rejects_forgery_and_expiry` |
| Replayed callback | single-use flow row | `test_oauth_callback_exchanges_and_stores_an_encrypted_credential` (replay refused) |
| Cross-provider credential reuse | per-platform settings only | `test_each_platform_uses_its_own_credentials` |
| Fake publish success | read-back verification + external-id check | `test_publish_without_verification_raises_instead_of_claiming_success` |
| Unsupported post silently attempted | local capability refusal before network | `test_publish_refuses_an_unimplemented_capability_before_any_call` |
| Fabricated analytics | measured-only snapshots | `test_analytics_ingest_records_only_measured_values` |
| AI spending money | founder-only, hash-bound, single-use authorisation | `test_ads_edit_after_authorisation_invalidates_it`, `test_ads_non_founder_execution_is_refused` |
| Approval burned by a blocked launch | transport checked before consumption | `test_ads_execution_is_blocked_by_an_unavailable_transport_without_consuming` |
| Duplicate launch from a retry | unique operation fingerprint | `test_ads_operation_is_idempotent` |
| Silent capability inflation | `verification` starts `unverified` and is asserted | `test_no_capability_claims_verification_that_did_not_happen` |

Unchanged and re-asserted by the existing suite: approval-before-publish,
immutable versions, budget ledger settlement, Flow provenance gates, trust
policy, role boundaries.

---

## 5. Verification (local, 2026-11-04 UTC)

| Check | Result |
| --- | --- |
| Phase 4 integrations module | **44 passed** |
| Phase 4.1 scheduler module | **7 passed** |
| Acceptance | **65/65**, `generated_at 2026-11-04T09:24:19Z` (Phase 4 run) |
| Full backend suite | **1204 passed in 556.02s**, 0 failed (10:24:35Z → 10:33:54Z) |
| Migration | `d5e6f7a8b9c0 → e6f7a8b9c0d1` up → down → up, head `e6f7a8b9c0d1` |
| Frontend types | `npx tsc --noEmit` exit 0 |
| Frontend build | `index-Bqzo970z.js` 411.77 kB (gzip 112.31 kB) |
| Source audit | `RESULT blocked (SOURCE_ABSENT)`, exit 3 |
| Acceptance suite | 65/65 (see §7 for the run attached to this commit) |

Docker and GitHub Actions were **not executed** in this environment; both are
declared in the matrix as written-but-unrun.

---

## 6. Remaining external blockers

| # | Blocker | What unblocks it | Owner |
| --- | --- | --- | --- |
| B1 | No approved Meta app (`pages_manage_posts`, `instagram_content_publish`, `instagram_manage_insights`) | Meta developer app + business verification + App Review | Founder |
| B2 | No LinkedIn app with community-management access | LinkedIn developer app + Marketing Developer Platform approval | Founder |
| B3 | No X app with write scope | X developer account + app | Founder |
| B4 | No Google Cloud OAuth client with YouTube upload + analytics scopes | GCP project + OAuth consent screen verification | Founder |
| B5 | No Reddit app | Reddit app registration + subreddit rights | Founder |
| B6 | No ads access (Meta Marketing / Google developer token / LinkedIn Ads) | three separate approvals + billing | Founder |
| B7 | No `TOKEN_VAULT_KEY` in a real deployment | `openssl rand -hex 32` into the secret store | Founder |
| B8 | Flow/DeepX provenance | licensed source + signed release + installed artifact | Founder / vendor |
| B9 | Docker and CI never executed | run `docker compose up --build` and the workflow on a machine with Docker | Operator |
| B10 | LLM pricing unverified | supply `LLM_PRICING_JSON` from the provider's price list | Founder |

Nothing above is a code defect. Each is an external authorisation or an
environment this sandbox does not have.

---

## 7. Verdict

```text
RELEASE ANCHOR:          MISSING
INSTALLED EXTENSION:     NOT FOUND
OVERALL PROVENANCE:      UNVERIFIED
LIVE FLOW GENERATION:    DISABLED
U1–U7:                   UNRESOLVED
PHASE 3C.3:              NO-GO — NOT STARTED

SOCIAL TRANSPORTS:       IMPLEMENTED, MOCK-VERIFIED, NEVER LIVE-VERIFIED
PAID MEDIA:              GOVERNANCE COMPLETE, TRANSPORTS ABSENT (REFUSED + RECORDED)
PRODUCTION READINESS:    NOT CLAIMED — images and CI written, not executed
```

A founder can now run the full marketing cycle **except** the last mile: connect
accounts (code ready, app approvals missing), publish (gate + transports ready,
credentials missing) and spend (governance complete, transports absent). Every one
of those gaps reports itself, in the API and in the UI, with the specific missing
input.

**Phase 3C.3 has not been started and must not start** while the provenance
verdict above is unchanged.
