# 28 — Phase 4.3: integration canaries and release assessment

Phase 4.3 was meant to leave the mock world: real OpenAI images, real Replicate
video, real OAuth for six platforms, one real publish, three real ad accounts.
This document records what could be executed **in this environment**, what could
not, and the exact reason for each gap.

Read it as an evidence ledger, not a status report. Every "no" below has a reason
that a founder can act on; none of them is "ran out of time".

---

## 0. The CI dependency defect (fixed, verified in Actions)

The `backend-postgres` job had been failing at its last step — the Celery broker
round-trip — and Actions logs live on a storage host this environment cannot
reach, so the first job was to make the job diagnose itself. That shipped as a
`if: failure()` step that posts its own tail as a commit comment (redacted by both
the app and `scripts/celery_roundtrip.py`).

The comment it posted on `8c7b00e`:

```text
celery/kombu/redis: 5.4.0 5.7.0 5.0.8
[CRITICAL/MainProcess] Unrecoverable error: VersionMismatch(
  'Redis transport requires redis-py versions 6.1.0 or later. You have 5.0.8')
```

Root cause: `celery==5.4.0` accepts any `kombu<6`, so an unpinned install pulls
**kombu 5.7**, whose Redis transport refuses redis-py below **6.1.0**. The
original `redis==5.2.1` was incompatible with that transport, and pinning redis
alone could not fix it — kombu had to be pinned too.

| Change | Why |
| --- | --- |
| `kombu==5.6.2` + `redis==5.0.8` | a mutually supported pair (kombu 5.6.2 declares `redis>=4.5.2,<6.5`), verified here by a real worker round-trip |
| `pip check` + resolved-versions step | `pip check` passes on the broken combination; only printing the trio makes it visible |
| Redis readiness probe through redis-py | the service container's health check is not proof the client library can talk to it |
| Worker step fails when it never reports `ready` | it previously `cat`-ed the log and succeeded either way |
| unique worker node name `crusor-ci@%h` | two workers sharing `celery@host` make `inspect()` answer from whichever registered last |
| `scripts/celery_roundtrip.py` | probes the broker first, prints the trio, and redacts broker URLs |
| diagnostics commit comment on failure | Actions logs are unreachable from here; the job now publishes its own evidence |

### The result, from GitHub Actions

Run **41872857374** on commit `13a9d80` (and its pull-request twin 41872868695),
all four jobs green:

| Job | Result |
| --- | --- |
| `backend` — tests, migrations, audit (SQLite) | success |
| `backend-postgres` — PostgreSQL 16 + pgvector + Redis | **success**, 6m25s |
| `frontend` — typecheck, build | success |
| `guard` — no credentials in source | success |

Step by step in `backend-postgres`: Setup → `pip check` + resolved versions →
Wait for PostgreSQL → Alembic upgrade/downgrade/upgrade → **full suite on
PostgreSQL** → Wait for Redis → **start a Celery worker** → **broker round trip
(dispatch → worker → result)** → stop the worker. The round trip is three real
tasks (`crusor.run_job` ×2, `crusor.health_check`) dispatched over Redis and
returned `SUCCESS` — the evidence Phase 4.2 could not produce.

Two defects were found by making that job honest, and both are real:

1. **kombu/redis-py incompatibility** (above) — the worker could not even start.
2. **Celery tasks closed the event loop between tasks.** `app/tasks/jobs.py`
   called `asyncio.run()` per task while the SQLAlchemy engine is a
   process-wide singleton with a real async pool. The second task then checked
   out a connection created on a closed loop:

   ```text
   FAIL refresh_analytics: RuntimeError: Task ... got Future ...
     attached to a different loop
   ERROR ... asyncpg ... raise RuntimeError('Event loop is closed')
   ```

   The first task always passed, so a one-task smoke test would never have caught
   it, and SQLite tolerates the cross-loop reuse. `_run()` now keeps one loop per
   worker process; the round trip passes on PostgreSQL.

---

## 1. External integration readiness audit

Derived from the code, not from memory: `app/integrations/oauth.py` (providers),
`app/integrations/platforms.py` (`CAPABILITIES`), `app/integrations/ads.py`
(transports), `app/creative/remote.py` (creative providers),
`scripts/integration_canary.py` (the audit runner).

### 1.1 Credentials — none are present, and none may be committed

| Provider | Settings the deployment must supply | Present now |
| --- | --- | --- |
| Facebook / Instagram | `META_APP_ID`, `META_APP_SECRET` | no |
| LinkedIn (organic) | `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET` | no |
| X | `X_CLIENT_ID`, `X_CLIENT_SECRET` | no |
| YouTube | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | no |
| Reddit | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | no |
| Meta Marketing API | `META_ADS_ACCESS_TOKEN`, `META_ADS_ACCOUNT_ID` | no |
| Google Ads API | `GOOGLE_ADS_ACCESS_TOKEN`, `GOOGLE_ADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | no |
| LinkedIn Marketing API | `LINKEDIN_ADS_ACCESS_TOKEN`, `LINKEDIN_ADS_ACCOUNT_URN` | no |
| OpenAI Images | `OPENAI_API_KEY` | no |
| Replicate | `REPLICATE_API_TOKEN`, `REPLICATE_VIDEO_MODEL` | no |

Every provider reads only its own keys. There is no shared credential path in
`app/integrations/ads.py` — `resolve_credentials()` maps each platform to its own
settings, and a transport refuses to run with another platform's token.

### 1.2 OAuth redirect and vault configuration

| Setting | Current value here | Consequence |
| --- | --- | --- |
| `OAUTH_REDIRECT_BASE_URL` | `http://localhost:8000` (default) | must be an HTTPS URL registered in each provider's app; providers reject plain HTTP |
| `TOKEN_VAULT_BACKEND` | `none` | **no consent flow can complete**: there is nowhere to store the token |
| `TOKEN_VAULT_KEY` | unset | required when the backend is `db` (AES-256-GCM) |
| `PUBLISHING_ENABLED` | `false` | publishing is refused before any network call |

State handling itself is implemented and unit-tested: `build_state()` signs the
state with an expiry and a nonce, `verify_state()` rejects tampering and replay,
and X additionally uses PKCE (`uses_pkce=True`).

### 1.3 Provider-specific restrictions and unsupported operations

From `CAPABILITIES`, where `implemented=false` means this build refuses the
operation locally rather than attempting it:

| Platform | Not implemented | Provider restriction it reflects |
| --- | --- | --- |
| Facebook | video post, native scheduling, paid ads | video upload needs the resumable chunked flow; `native_scheduled_publish_time` is a separate parameter; ads belong to the ads module |
| Instagram | text-only post, scheduling, paid ads | the API requires at least one media container |
| LinkedIn | video post, scheduling, paid ads | video needs the separate Videos API |
| X | image post, video post, scheduling, paid ads | media upload is a v1.1 endpoint; the v2 API has no native scheduling |
| YouTube | text post, image post, paid ads | YouTube has no text surface; thumbnails are not posts |
| Reddit | image post, video post, scheduling | media posts are a different endpoint; no native scheduling |
| All ads transports | campaign activation | `create` is PAUSED/draft by design; activation requires a separate founder authorisation |

### 1.4 The environment cannot make these calls at all

This is not a code finding, and it is the single most important line in this
document:

```text
GET https://graph.facebook.com/      failed after 30 ms — ConnectError: TLS/SSL connection has been closed (EOF)
GET https://api.openai.com/          failed after 17 ms — ConnectError: TLS/SSL connection has been closed (EOF)
GET https://api.replicate.com/       failed after 37 ms — ConnectError: TLS/SSL connection has been closed (EOF)
GET https://googleads.googleapis.com/ failed after 22 ms — ConnectError: TLS/SSL connection has been closed (EOF)
GET https://api.linkedin.com/        failed after 29 ms — ConnectError: TLS/SSL connection has been closed (EOF)
GET https://oauth2.googleapis.com/   failed after 23 ms — ConnectError: TLS/SSL connection has been closed (EOF)
```

TCP connects succeed (an egress proxy accepts the socket) and every TLS handshake
is then closed. So: **no canary in this environment can reach any provider**, even
with perfect credentials. Sections 2–5 record the canaries as *blocked*, with the
credential gap as the first blocker and this as the second.

---

## 2. Creative provider verification

`scripts/integration_canary.py --provider openai_image --provider replicate_video`
is implemented and tested for everything up to the network boundary. The gates it
respects are the ones the service already enforces (`CreativeGenerationService.submit`):

| Requirement | Implemented | Proven here |
| --- | --- | --- |
| Credential validation | yes — `describe_requirement()` names the missing variable | blocked: `OPENAI_API_KEY` / `REPLICATE_API_TOKEN` absent |
| Cost estimate before submission | yes — `_estimate_cost()`; an unpriced billable provider is refused, never treated as $0 | unit-tested (`test_phase4_creative_providers.py`, 23 tests) |
| Budget + founder authorisation | yes — `CREATIVE_ALLOW_PAID_GENERATION`, non-zero `CREATIVE_CAMPAIGN_BUDGET_USD`, estimate ≤ budget | unit-tested; the canary refuses `--allow-spend` without a signed authorisation file |
| Job submission | yes — `POST /v1/images/generations` (OpenAI), `POST /v1/predictions` (Replicate) | **never executed against a provider** |
| Status polling | yes — persisted job states, `POST /creative-studio/jobs/{id}/poll` | mock-tested |
| Asset retrieval + integrity | yes — sha256 + mime + dimension checks on retrieval | mock-tested |
| Storage persistence | yes — asset bytes stored and served `byte-for-byte` (acceptance 40) | proven locally |
| Campaign / content-version binding | yes — job bound to brief → content version → campaign | proven locally (acceptance 42–43) |

**Status: blocked.** First blocker is the missing API key; second is that this
environment cannot reach `api.openai.com` or `api.replicate.com`. Nothing about a
real generation can be claimed. The canary will run the moment both are fixed.

---

## 3. Social OAuth canaries

Per platform the harness checks: app credentials → redirect + vault → publishing
gate → the request it would make → reachability → (with `--live`) a read-only
identity verification through the running API.

| Platform | Credentials | Redirect | Vault | Live canary | Status |
| --- | --- | --- | --- | --- | --- |
| Facebook Page | missing | not HTTPS | `none` | blocked (no connected account) | **blocked** |
| Instagram Business | missing | not HTTPS | `none` | blocked (no connected account) | **blocked** |
| LinkedIn | missing | not HTTPS | `none` | blocked (no connected account) | **blocked** |
| X | missing | not HTTPS | `none` | blocked (no connected account) | **blocked** |
| YouTube | missing | not HTTPS | `none` | blocked (no connected account) | **blocked** |
| Reddit | missing | not HTTPS | `none` | blocked (no connected account) | **blocked** |

What *is* proven, by tests rather than by a provider: state signing/expiry/replay
rejection, PKCE for X, token exchange parsing per provider, encrypted vault
storage, refresh, revocation, and account isolation (a token is bound to one
account and one platform). What is **not** proven is that any provider accepts the
exchange — that requires the founder's browser at the consent screen.

---

## 4. Publishing canaries

Nothing was published. `PUBLISHING_ENABLED=false` and no account is connected, so
the path is refused before any network call — which is the designed behaviour, and
the acceptance suite proves the refusal (criterion 10: unapproved publish → 409,
approved publish without a transport → 503 and the item stays `APPROVED`).

The read-back requirement (`external_id` + permalink, duplicate prevention, failure
recovery, metrics measured-or-absent) is implemented in
`app/integrations/platforms.py` and covered by `test_phase4_social_integrations.py`
(44 tests) — against `httpx.MockTransport`, not against a platform.

**A publicly visible test post has not been made and must not be made without the
founder present.** The harness deliberately has no flag for it.

---

## 5. Paid advertising canaries

| Platform | Implemented | Account access validated | Preview | Draft/paused create | Read-back | Status |
| --- | --- | --- | --- | --- | --- | --- |
| Meta Marketing API (v21.0) | yes | no — no credentials | yes (local) | not attempted | not attempted | **blocked** |
| Google Ads API (v17) | yes | no — no credentials | yes (local) | not attempted | not attempted | **blocked** |
| LinkedIn Marketing API (202405) | yes | no — no credentials | yes (local) | not attempted | not attempted | **blocked** |

The transports refuse to act without their own credentials
(`AdsNotConfigured`), create everything paused, never retry a create, and read the
object back before recording it (30 transport tests). No campaign was created, no
billing was touched, and **no money was spent**.

---

## 6. Full Director lifecycle, executed

Unlike sections 2–5, this one ran for real against a live server:
`scripts/lifecycle_evidence.py`, evidence in `docs/lifecycle-run.json`.

| Check | Result |
| --- | --- |
| Workflow halts for founder approval before any external effect | **PASS** |
| Trace recorded (25 entries: intake → plan → dispatch → agents → strategy → campaign → content → guardian → approval request) | **PASS** |
| No fabricated publication (0 published, 0 blocked, nothing claimed) | **PASS** |
| Analytics measured-or-absent (`available: false`, reason: token vault disabled) | **PASS** |
| Durable state (26 checkpoints persisted) | **PASS** |
| An editor cannot approve (HTTP 401 on the decision endpoint) | **PASS** |
| Approving a finished workflow is a no-op, not a re-run (state unchanged) | **PASS** |

Final state: `completed` / `handed_off`. Scheduling produced no posts because no
account is connected, and the run recorded that rather than inventing a queue.

The chain that ran: research → strategy → campaign → content → creative →
Brand Guardian → founder approval → scheduling (blocked, recorded) → publishing
(not attempted, no transport) → analytics (unavailable, recorded) → optimisation →
hand-off.

---

## 7. Per-provider status

Six states, and they are not cumulative in the direction people assume:
`mock-tested` never implies `credential-configured`, and neither implies
`sandbox-verified`.

| Provider | Implemented | Mock-tested | Credential-configured | Sandbox-verified | Production-verified | Blocked by |
| --- | --- | --- | --- | --- | --- | --- |
| Facebook Page | yes | yes | no | no | no | app review + `META_APP_*` + HTTPS redirect + vault |
| Instagram Business | yes | yes | no | no | no | same Meta app + `instagram_content_publish` review |
| LinkedIn | yes | yes | no | no | no | Marketing Developer Platform access |
| X | yes | yes | no | no | no | paid API tier + OAuth 2.0 client |
| YouTube | yes | yes | no | no | no | OAuth consent verification for upload scope |
| Reddit | yes | yes | no | no | no | app registration + moderated subreddit |
| Meta Marketing API | yes | yes | no | no | no | `ads_management` + funded ad account |
| Google Ads API | yes | yes | no | no | no | approved developer token + billing customer id |
| LinkedIn Marketing API | yes | yes | no | no | no | Advertising API access + sponsored account |
| OpenAI Images | yes | yes | no | no | no | `OPENAI_API_KEY` (or `OPENAI_BASE_URL`), pinned price, budget, authorisation |
| Replicate video | yes | yes | no | no | no | `REPLICATE_API_TOKEN` + pinned video model |
| Google Flow | refused | n/a | n/a | no | no | source, signed release and installed artifact unverifiable — **stays disabled** |

---

## 8. Founder checklist (delta for Phase 4.3)

The full checklist is `docs/25-platform-readiness.md` §A–E. This is what Phase 4.3
added or sharpened:

- [ ] **Make egress possible.** Run the canaries from a host that can reach the
      provider APIs; this one terminates every TLS handshake. Nothing else on this
      list can be verified until that is true.
- [ ] Set `OAUTH_REDIRECT_BASE_URL` to an HTTPS URL and register it in each app.
- [ ] Set `TOKEN_VAULT_BACKEND=db` and `TOKEN_VAULT_KEY` (32 bytes hex). Without a
      vault, OAuth can complete a consent flow and still store nothing.
- [ ] For creative: `OPENAI_API_KEY` and/or `REPLICATE_API_TOKEN` +
      `REPLICATE_VIDEO_MODEL`, `CREATIVE_PRICING_JSON`, a non-zero
      `CREATIVE_CAMPAIGN_BUDGET_USD`, then `CREATIVE_ALLOW_PAID_GENERATION=true`.
- [ ] For a spend canary, copy `docs/founder-authorization.example.json` to a
      path outside the repository, fill it in, and pass it explicitly with
      `--founder-authorization`. The file must name the founder, the provider, a
      `max_spend_usd` ceiling and a reference. `--allow-spend` without it exits 4;
      there is no default authorisation and no way to encode one in the code.
- [ ] For ads: per-platform credentials, then
      `POST /api/v1/ads/campaigns/{id}/validate-account`. Creating a campaign is
      still refused until a founder authorisation binds to its proposal hash.
- [ ] For a publish canary: connect the account, verify it, then run the publish
      with a human watching; `--allow-public-post` does not exist on purpose.

---

## 9. Release recommendation

```text
CI (PostgreSQL 16 + pgvector + Redis + Celery round-trip):  GREEN — run 41872857374
DIRECTOR LIFECYCLE:      EXECUTED END-TO-END, GOVERNANCE CHECKS PASS
CREATIVE PROVIDERS:      BLOCKED — no credentials, no egress
SOCIAL OAUTH:            BLOCKED — no credentials, no vault, no egress
PUBLISHING:              BLOCKED — not attempted by design
PAID MEDIA:              BLOCKED — no credentials, nothing created, nothing spent
FLOW:                    DISABLED — provenance unverified
PRODUCTION READINESS:    NOT CLAIMED
```

**Recommendation: do not release as "production-ready".** The product is ready to
be *verified*, not to be claimed. Everything that can be proven without a provider
has been: the suite on the production dialect, the broker round-trip, the lifecycle
with its approval boundary, and the refusal paths. Everything that requires a
provider is blocked at exactly two things — credentials, and network egress from
the authoring environment — and both are founder actions, not code.

The moment those two exist, `scripts/integration_canary.py --live` and
`scripts/lifecycle_evidence.py` produce the evidence for the next column. Until
then every `sandbox-verified` and `production-verified` cell stays `no`, and that
is the correct state of this repository.
