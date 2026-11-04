# 25 — Platform readiness and founder checklist

Generated 2026-11-04T13:29:57+00:00 from code + the current environment (`ENVIRONMENT=development`).

**How to read the columns.** `implemented` is what the code does. `mock-tested` means an automated suite exercises it against `httpx.MockTransport`. `sandbox-verified` and `production-verified` can only be filled by a real call against a real developer account — this repository has not made one, so both are `no` for every row. A feature is not production-ready because its code exists.

## Six platforms

| Platform | Implemented | Mock-tested | Sandbox-verified | Production-verified |
| --- | --- | --- | --- | --- |
| Facebook Page | yes | yes (httpx.MockTransport suites) | no | no |
| Instagram Business | yes | yes (httpx.MockTransport suites) | no | no |
| LinkedIn Page | yes | yes (httpx.MockTransport suites) | no | no |
| X (Twitter) | yes | yes (httpx.MockTransport suites) | no | no |
| YouTube | yes | yes (httpx.MockTransport suites) | no | no |
| Reddit | yes | yes (httpx.MockTransport suites) | no | no |

## Paid media

| Platform | API | Implemented | Credentials present | Mock-tested | Sandbox-verified | Production-verified |
| --- | --- | --- | --- | --- | --- | --- |
| Meta Marketing API | Graph v21.0 | yes | no | yes (httpx.MockTransport suites) | no | no |
| Google Ads API | REST v17 | yes | no | yes (httpx.MockTransport suites) | no | no |
| LinkedIn Ads API | 202405 | yes | no | yes (httpx.MockTransport suites) | no | no |

## Creative generation

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

### B2. Instagram Business

- Scopes needed: `instagram_basic`, `instagram_content_publish`, `instagram_manage_insights`
- App approval: Meta app review for instagram_content_publish + a Business account linked to a Page
- Missing now:
  - [ ] missing credentials: META_APP_ID, META_APP_SECRET
  - [ ] token vault not configured (TOKEN_VAULT_BACKEND=db + TOKEN_VAULT_KEY)
  - [ ] PUBLISHING_ENABLED is false
  - [ ] Meta app review for instagram_content_publish + a Business account linked to a Page
- [ ] Set the credentials as deployment secrets, connect the account via `POST /api/v1/social/oauth/instagram/start`, then run `POST /api/v1/social/accounts/{id}/verify` and record the result.

### B3. LinkedIn Page

- Scopes needed: `w_organization_social`, `r_organization_social`, `rw_organization_admin`
- App approval: LinkedIn Marketing Developer Platform access for the app
- Missing now:
  - [ ] missing credentials: LINKEDIN_CLIENT_ID, LINKEDIN_CLIENT_SECRET
  - [ ] token vault not configured (TOKEN_VAULT_BACKEND=db + TOKEN_VAULT_KEY)
  - [ ] PUBLISHING_ENABLED is false
  - [ ] LinkedIn Marketing Developer Platform access for the app
- [ ] Set the credentials as deployment secrets, connect the account via `POST /api/v1/social/oauth/linkedin/start`, then run `POST /api/v1/social/accounts/{id}/verify` and record the result.

### B4. X (Twitter)

- Scopes needed: `tweet.write`, `tweet.read`, `users.read`, `offline.access`
- App approval: X developer app with write access (a paid API tier as of 2025)
- Missing now:
  - [ ] missing credentials: X_CLIENT_ID, X_CLIENT_SECRET
  - [ ] token vault not configured (TOKEN_VAULT_BACKEND=db + TOKEN_VAULT_KEY)
  - [ ] PUBLISHING_ENABLED is false
  - [ ] X developer app with write access (a paid API tier as of 2025)
- [ ] Set the credentials as deployment secrets, connect the account via `POST /api/v1/social/oauth/x/start`, then run `POST /api/v1/social/accounts/{id}/verify` and record the result.

### B5. YouTube

- Scopes needed: `https://www.googleapis.com/auth/youtube.upload`
- App approval: Google OAuth consent screen verification for the YouTube upload scope
- Missing now:
  - [ ] missing credentials: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
  - [ ] token vault not configured (TOKEN_VAULT_BACKEND=db + TOKEN_VAULT_KEY)
  - [ ] PUBLISHING_ENABLED is false
  - [ ] Google OAuth consent screen verification for the YouTube upload scope
- [ ] Set the credentials as deployment secrets, connect the account via `POST /api/v1/social/oauth/youtube/start`, then run `POST /api/v1/social/accounts/{id}/verify` and record the result.

### B6. Reddit

- Scopes needed: `submit`, `read`, `identity`
- App approval: Reddit app registration with a script/web app type and a subreddit you moderate
- Missing now:
  - [ ] missing credentials: REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET
  - [ ] token vault not configured (TOKEN_VAULT_BACKEND=db + TOKEN_VAULT_KEY)
  - [ ] PUBLISHING_ENABLED is false
  - [ ] Reddit app registration with a script/web app type and a subreddit you moderate
- [ ] Set the credentials as deployment secrets, connect the account via `POST /api/v1/social/oauth/reddit/start`, then run `POST /api/v1/social/accounts/{id}/verify` and record the result.

### C. Paid media

- [ ] Meta Marketing API (Graph v21.0): supply `META_ADS_ACCESS_TOKEN`, `META_ADS_ACCOUNT_ID`; approval: ads_management permission on the ad account + a funded payment method.
- [ ] Google Ads API (REST v17): supply `GOOGLE_ADS_ACCESS_TOKEN`, `GOOGLE_ADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_LOGIN_CUSTOMER_ID`; approval: A developer token approved for the account + a customer id with billing.
- [ ] LinkedIn Ads API (202405): supply `LINKEDIN_ADS_ACCESS_TOKEN`, `LINKEDIN_ADS_ACCOUNT_URN`; approval: Advertising API access on the LinkedIn app + a sponsored account.
- [ ] Run `POST /api/v1/ads/campaigns/{id}/validate-account` and paste the result into the release record.
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

