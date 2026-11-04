# 25 — Platform readiness and founder checklist

Generated 2026-11-04T18:33:35+00:00 from code + the current environment (`ENVIRONMENT=development`).

**How to read the columns.** `implemented` is what the code does. `mock-tested` means an automated suite exercises it against `httpx.MockTransport`. `network reachable` is **measured**, not assumed: it comes from `docs/preflight.json` (DNS → TCP → verified TLS → HTTPS from the deployment host). `credentials` is only about presence. `authenticated` means a provider accepted those credentials in a real call. `credential-configured` is only about presence: the environment variables exist *and* the OAuth prerequisites below are met; it is not evidence that a provider accepted them. `sandbox-verified` and `production-verified` can only be filled by a real call against a real developer account, so both are `no` for every row until one is recorded in `docs/04-ACCEPTANCE-EVIDENCE.md`. A feature is not production-ready because its code exists.

## Network

Measured by `scripts/environment_preflight.py` at 2026-11-04T18:33:34+00:00.

- **Facebook Page**: graph.facebook.com: SSLZeroReturnError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
- **Instagram Business**: graph.facebook.com: SSLZeroReturnError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
- **LinkedIn Page**: api.linkedin.com: SSLZeroReturnError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
- **X (Twitter)**: api.twitter.com: SSLZeroReturnError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
- **YouTube**: oauth2.googleapis.com: SSLZeroReturnError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
- **Reddit**: oauth.reddit.com: SSLZeroReturnError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
- **Meta Marketing API**: graph.facebook.com: SSLZeroReturnError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
- **Google Ads API**: googleads.googleapis.com: SSLZeroReturnError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
- **LinkedIn Ads API**: api.linkedin.com: SSLZeroReturnError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
- **OpenAI Images**: api.openai.com: SSLZeroReturnError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
- **Replicate video**: api.replicate.com: SSLZeroReturnError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)

## OAuth prerequisites

- `OAUTH_REDIRECT_BASE_URL`: http://localhost:8000
- Token vault: backend `none`, key MISSING
- `PUBLISHING_ENABLED`: False

Every provider also requires its own OAuth app review, and the redirect above
must be registered in that app. Sharing one app's credentials across providers
is not supported anywhere in this codebase.

## Six platforms

| Platform | Implemented | Mock-tested | Credentials | Network reachable | Authenticated | Sandbox-verified | Production-verified |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Facebook Page | yes | yes (httpx.MockTransport suites) | no | no | no | no | no |
| Instagram Business | yes | yes (httpx.MockTransport suites) | no | no | no | no | no |
| LinkedIn Page | yes | yes (httpx.MockTransport suites) | no | no | no | no | no |
| X (Twitter) | yes | yes (httpx.MockTransport suites) | no | no | no | no | no |
| YouTube | yes | yes (httpx.MockTransport suites) | no | no | no | no | no |
| Reddit | yes | yes (httpx.MockTransport suites) | no | no | no | no | no |

## Paid media

| Platform | API | Implemented | Credentials | Network reachable | Authenticated | Sandbox-verified | Production-verified |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Meta Marketing API | Graph v21.0 | yes | no | no | no | no | no |
| Google Ads API | REST v17 | yes | no | no | no | no | no |
| LinkedIn Ads API | 202405 | yes | no | no | no | no | no |

## Creative generation

| Provider | Implemented | Mock-tested | Credentials | Network reachable | Authenticated | Sandbox-verified | Production-verified | Blocked by |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| OpenAI Images | yes | yes (httpx.MockTransport suites) | no | no | no | no | no | missing OPENAI_API_KEY, price not pinned in CREATIVE_PRICING_JSON, CREATIVE_CAMPAIGN_BUDGET_USD is 0, CREATIVE_ALLOW_PAID_GENERATION is not true |
| Replicate video | yes | yes (httpx.MockTransport suites) | no | no | no | no | no | missing REPLICATE_API_TOKEN, REPLICATE_VIDEO_MODEL, price not pinned in CREATIVE_PRICING_JSON, CREATIVE_CAMPAIGN_BUDGET_USD is 0, CREATIVE_ALLOW_PAID_GENERATION is not true |

- Provider set: `mock`
- State: `available` — Deterministic mock provider. No model or external service is called, and video requests produce a poster frame rather than an encoded video.
- Flow: disabled — source, signed release and installed artifact remain unverifiable

## Founder checklist

Everything below is a *human* action. Nothing in this repository can do it, and nothing in this repository pretends it happened.

### A. Core platform

- [ ] Provision PostgreSQL 16 with the `vector` extension and run `alembic upgrade head`.
- [ ] Provision Redis and run a Celery worker (`celery -A app.tasks.celery_app:celery_app worker`).

### B1. Facebook Page

- Scopes needed: `pages_manage_posts`, `pages_read_engagement`, `read_insights`
- App approval: Meta app review for pages_manage_posts + a Page the app is allowed to post to
- Missing now:
  - [ ] missing credentials: META_APP_ID, META_APP_SECRET
  - [ ] token vault not configured (TOKEN_VAULT_BACKEND=db + TOKEN_VAULT_KEY)
  - [ ] PUBLISHING_ENABLED is false
  - [ ] Meta app review for pages_manage_posts + a Page the app is allowed to post to
- [ ] Set the credentials as deployment secrets, connect the account via `POST /api/v1/social/oauth/facebook/start`, then run `POST /api/v1/social/accounts/{id}/verify` and record the result.
- [ ] Then: `python ../scripts/integration_canary.py --provider facebook --network --live` and paste the JSON into `docs/04-ACCEPTANCE-EVIDENCE.md` before this row may say `sandbox-verified`.

### B2. Instagram Business

- Scopes needed: `instagram_basic`, `instagram_content_publish`, `instagram_manage_insights`
- App approval: Meta app review for instagram_content_publish + a Business account linked to a Page
- Missing now:
  - [ ] missing credentials: META_APP_ID, META_APP_SECRET
  - [ ] token vault not configured (TOKEN_VAULT_BACKEND=db + TOKEN_VAULT_KEY)
  - [ ] PUBLISHING_ENABLED is false
  - [ ] Meta app review for instagram_content_publish + a Business account linked to a Page
- [ ] Set the credentials as deployment secrets, connect the account via `POST /api/v1/social/oauth/instagram/start`, then run `POST /api/v1/social/accounts/{id}/verify` and record the result.
- [ ] Then: `python ../scripts/integration_canary.py --provider instagram --network --live` and paste the JSON into `docs/04-ACCEPTANCE-EVIDENCE.md` before this row may say `sandbox-verified`.

### B3. LinkedIn Page

- Scopes needed: `w_organization_social`, `r_organization_social`, `rw_organization_admin`
- App approval: LinkedIn Marketing Developer Platform access for the app
- Missing now:
  - [ ] missing credentials: LINKEDIN_CLIENT_ID, LINKEDIN_CLIENT_SECRET
  - [ ] token vault not configured (TOKEN_VAULT_BACKEND=db + TOKEN_VAULT_KEY)
  - [ ] PUBLISHING_ENABLED is false
  - [ ] LinkedIn Marketing Developer Platform access for the app
- [ ] Set the credentials as deployment secrets, connect the account via `POST /api/v1/social/oauth/linkedin/start`, then run `POST /api/v1/social/accounts/{id}/verify` and record the result.
- [ ] Then: `python ../scripts/integration_canary.py --provider linkedin --network --live` and paste the JSON into `docs/04-ACCEPTANCE-EVIDENCE.md` before this row may say `sandbox-verified`.

### B4. X (Twitter)

- Scopes needed: `tweet.write`, `tweet.read`, `users.read`, `offline.access`
- App approval: X developer app with write access (a paid API tier as of 2025)
- Missing now:
  - [ ] missing credentials: X_CLIENT_ID, X_CLIENT_SECRET
  - [ ] token vault not configured (TOKEN_VAULT_BACKEND=db + TOKEN_VAULT_KEY)
  - [ ] PUBLISHING_ENABLED is false
  - [ ] X developer app with write access (a paid API tier as of 2025)
- [ ] Set the credentials as deployment secrets, connect the account via `POST /api/v1/social/oauth/x/start`, then run `POST /api/v1/social/accounts/{id}/verify` and record the result.
- [ ] Then: `python ../scripts/integration_canary.py --provider x --network --live` and paste the JSON into `docs/04-ACCEPTANCE-EVIDENCE.md` before this row may say `sandbox-verified`.

### B5. YouTube

- Scopes needed: `https://www.googleapis.com/auth/youtube.upload`
- App approval: Google OAuth consent screen verification for the YouTube upload scope
- Missing now:
  - [ ] missing credentials: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
  - [ ] token vault not configured (TOKEN_VAULT_BACKEND=db + TOKEN_VAULT_KEY)
  - [ ] PUBLISHING_ENABLED is false
  - [ ] Google OAuth consent screen verification for the YouTube upload scope
- [ ] Set the credentials as deployment secrets, connect the account via `POST /api/v1/social/oauth/youtube/start`, then run `POST /api/v1/social/accounts/{id}/verify` and record the result.
- [ ] Then: `python ../scripts/integration_canary.py --provider youtube --network --live` and paste the JSON into `docs/04-ACCEPTANCE-EVIDENCE.md` before this row may say `sandbox-verified`.

### B6. Reddit

- Scopes needed: `submit`, `read`, `identity`
- App approval: Reddit app registration with a script/web app type and a subreddit you moderate
- Missing now:
  - [ ] missing credentials: REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET
  - [ ] token vault not configured (TOKEN_VAULT_BACKEND=db + TOKEN_VAULT_KEY)
  - [ ] PUBLISHING_ENABLED is false
  - [ ] Reddit app registration with a script/web app type and a subreddit you moderate
- [ ] Set the credentials as deployment secrets, connect the account via `POST /api/v1/social/oauth/reddit/start`, then run `POST /api/v1/social/accounts/{id}/verify` and record the result.
- [ ] Then: `python ../scripts/integration_canary.py --provider reddit --network --live` and paste the JSON into `docs/04-ACCEPTANCE-EVIDENCE.md` before this row may say `sandbox-verified`.

### C. Paid media

- [ ] Meta Marketing API (Graph v21.0): supply `META_ADS_ACCESS_TOKEN`, `META_ADS_ACCOUNT_ID`; approval: ads_management permission on the ad account + a funded payment method.
- [ ] Google Ads API (REST v17): supply `GOOGLE_ADS_ACCESS_TOKEN`, `GOOGLE_ADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_LOGIN_CUSTOMER_ID`; approval: A developer token approved for the account + a customer id with billing.
- [ ] LinkedIn Ads API (202405): supply `LINKEDIN_ADS_ACCESS_TOKEN`, `LINKEDIN_ADS_ACCOUNT_URN`; approval: Advertising API access on the LinkedIn app + a sponsored account.
- [ ] Run `POST /api/v1/ads/campaigns/{id}/validate-account` (or `python ../scripts/integration_canary.py --provider <platform> --network --live`) and paste the result into the release record.
- [ ] Set `ADS_MAX_AUTHORIZED_SPEND_CENTS` to the real ceiling per authorisation.

### D. Creative

- [ ] Choose an image provider (`CREATIVE_IMAGE_PROVIDER=openai`) and/or a video provider (`CREATIVE_VIDEO_PROVIDER=replicate`).
- [ ] Supply the provider key(s) and pin `REPLICATE_VIDEO_MODEL` to an `owner/model:version` you have approved.
- [ ] Enter the contracted price per generation in `CREATIVE_PRICING_JSON` (`{"image": 0.0, "video": 0.0}`). Unknown pricing is refused, never treated as zero.
- [ ] Set `CREATIVE_CAMPAIGN_BUDGET_USD` to a non-zero budget and only then `CREATIVE_ALLOW_PAID_GENERATION=true`.
- [ ] Confirm Google Flow stays disabled: its source, signed release and installed artifact are still unverifiable.

### E. Publishing and spending

- [ ] Set `PUBLISHING_ENABLED=true` only after a platform's sandbox post round-trip succeeds.
- [ ] Keep `FLOW_LIVE_GENERATION_ENABLED=false`.
- [ ] Decide the founder authorisation policy for ad spend (`ADS_MAX_AUTHORIZED_SPEND_CENTS`, per-campaign ceilings).

