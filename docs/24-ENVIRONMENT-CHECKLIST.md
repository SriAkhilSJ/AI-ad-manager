# Environment and credential checklist

Everything the application reads, what happens when it is missing, and who has to
supply it. **An empty value is a supported state**: the feature reports
`unavailable` with a reason instead of failing in a confusing way.

Legend: **Req** = required in production · **Opt** = optional; feature degrades
honestly · **Never** = must never be committed or logged.

---

## 1. Core

| Variable | Req | Default | If missing |
| --- | --- | --- | --- |
| `ENVIRONMENT` | — | `development` | `production` enables strict validation (no SQLite, no placeholder key) |
| `SECRET_KEY` | **Req** | dev placeholder | production refuses to start with a placeholder |
| `DATABASE_URL` | **Req** | SQLite | production refuses SQLite |
| `REDIS_URL` | Opt | — | inline task dispatcher, in-memory rate limiter |
| `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | Opt | — | tasks run inline |
| `TASK_DISPATCHER` | — | `auto` | `auto` picks celery when a broker exists |
| `STORAGE_BACKEND` | Opt | `auto` | filesystem storage under `.data/storage` |
| `S3_*` | Opt | — | `storage_status` reports `unavailable` |
| `BOOTSTRAP_FOUNDER_EMAIL` / `_PASSWORD` | **Req (first boot)** | dev values | the first boot creates the founder from these; change them anywhere real |
| `CORS_ORIGINS` | Req (prod) | localhost | `production` refuses a wildcard |
| `RATE_LIMIT_PER_MINUTE` | — | 120 | per-IP sliding window |

## 2. LLM providers

Each provider has **its own** key. There is deliberately no shared `LLM_KEY`:
one undifferentiated key is how a cross-provider mix-up becomes possible.

| Variable | Req | If missing |
| --- | --- | --- |
| `OPENAI_API_KEY` | Opt | OpenAI routing unavailable |
| `ANTHROPIC_API_KEY` | Opt | Anthropic routing unavailable |
| `OPENROUTER_API_KEY` + `OPENROUTER_BASE_URL` | Opt | OpenRouter unavailable |
| `VELONA_API_KEY` + `VELONA_BASE_URL` | Opt | Velona unavailable; the endpoint is never guessed |
| `LLM_DEFAULT_PROVIDER` / `LLM_DEFAULT_MODEL` | Opt | deterministic agent paths, zero recorded spend |
| `LLM_TASK_ROUTING_JSON` | Opt | default provider for every task |
| `LLM_PRICING_JSON` | Opt | models are priced with a conservative placeholder and reported **unverified** |
| `LLM_ALLOW_UNVERIFIED_PRICING` | — | `false`: paid calls are blocked while a price is unverified |
| `LLM_MONTHLY_BUDGET_USD` | Opt | a default ceiling applies; reservations still gate every call |

## 3. OAuth connections and publishing (Phase 4)

| Variable | Req | If missing |
| --- | --- | --- |
| `TOKEN_VAULT_BACKEND` | Opt | `none` → OAuth and live publishing unavailable |
| `TOKEN_VAULT_KEY` | **Req for publishing** | the vault reports unavailable; nothing can be connected or published |
| `OAUTH_REDIRECT_BASE_URL` | Req for OAuth | the redirect URI is built from it; register `<base>/api/v1/social/oauth/callback` with each provider |
| `OAUTH_STATE_TTL_SECONDS` | — | 600 |
| `META_APP_ID` / `META_APP_SECRET` | Opt | Facebook and Instagram unavailable |
| `LINKEDIN_CLIENT_ID` / `_SECRET` | Opt | LinkedIn unavailable |
| `X_CLIENT_ID` / `_SECRET` | Opt | X unavailable (PKCE is mandatory) |
| `GOOGLE_CLIENT_ID` / `_SECRET` | Opt | YouTube unavailable |
| `REDDIT_CLIENT_ID` / `_SECRET` | Opt | Reddit unavailable |
| `INTEGRATION_TIMEOUT_SECONDS` | — | 30 |
| `INTEGRATION_MAX_RETRIES` | — | 3 (create calls are never retried, regardless) |
| `TOKEN_REFRESH_SKEW_SECONDS` | — | 120 seconds before expiry |

`TOKEN_VAULT_KEY` must be 32 bytes of key material: 64 hex characters
(`openssl rand -hex 32`), base64 that decodes to 32 bytes, or a passphrase of at
least 32 characters. Anything shorter is refused rather than stretched.

**Never** commit `TOKEN_VAULT_KEY`, provider client secrets, or a dump of
`oauth_tokens`. Losing the key means every account must be reconnected; leaking it
means every connected account can be operated by whoever has it.

## 4. Paid advertising

| Variable | Req | If missing |
| --- | --- | --- |
| `ADS_MAX_AUTHORIZED_SPEND_CENTS` | — | 500000 (5,000.00). Hard ceiling per founder authorisation |

There are no ads transport credentials yet, because no transport is implemented.
When one is, its credentials go beside the others in this table — never inside an
existing platform's variables.

## 5. Creative and Flow

| Variable | Req | If missing |
| --- | --- | --- |
| `CREATIVE_PROVIDER` | Opt | mock provider (synthetic frames, clearly labelled) |
| `CREATIVE_ALLOW_PAID_GENERATION` | — | `false` |
| `CREATIVE_CAMPAIGN_BUDGET_USD` | — | campaign envelope for generation |
| `CREATIVE_PRICING_JSON` | Opt | unverified pricing → paid generation blocked |
| `FLOW_LIVE_GENERATION_ENABLED` | — | `false`; must stay false while provenance is unverified |
| `FLOW_SOURCE_*` / `FLOW_RELEASE_*` / `FLOW_EXTENSION_*` | Opt | the bridge reports `SOURCE_ABSENT` / `NOT_FOUND` / `UNVERIFIED` |

## 6. What the founder must provision

| # | Item | Where it goes | Unblocks |
| --- | --- | --- | --- |
| 1 | 32-byte vault key | secret manager → `TOKEN_VAULT_KEY` | any OAuth connection |
| 2 | Meta app (business verified, App Review for `pages_manage_posts`, `instagram_content_publish`, `instagram_manage_insights`) | `META_APP_ID` / `META_APP_SECRET` | Facebook + Instagram publishing |
| 3 | LinkedIn app with Marketing Developer Platform access | `LINKEDIN_CLIENT_*` | LinkedIn publishing |
| 4 | X app with write scope, OAuth 2.0 | `X_CLIENT_*` | X publishing |
| 5 | Google Cloud OAuth client (YouTube upload + analytics scopes; consent screen verified) | `GOOGLE_CLIENT_*` | YouTube publishing |
| 6 | Reddit app | `REDDIT_CLIENT_*` | Reddit publishing |
| 7 | Meta Marketing API access + ad account with payment | *(transport not implemented)* | paid campaigns on Meta |
| 8 | Google Ads developer token + customer id with billing | *(transport not implemented)* | paid campaigns on Google |
| 9 | LinkedIn Advertising API approval | *(transport not implemented)* | paid campaigns on LinkedIn |
| 10 | LLM provider keys + operator-verified pricing | `*_API_KEY`, `LLM_PRICING_JSON` | real generation at a known cost |
| 11 | Licensed DeepX source, signed release, installed artifact | `FLOW_*` | Flow bridge + Phase 3C.3 eligibility |
| 12 | S3 bucket + credentials (optional) | `S3_*` | durable creative storage |

Items 1–6 plus a registered redirect URI are the complete set needed to publish
real posts. Items 7–9 are needed before paid advertising can do anything at all,
and they unlock nothing until a transport is written.

## 7. Verification checklist after configuring

```bash
curl -s localhost:8000/api/v1/system/health            # {"status":"ok"}
curl -s localhost:8000/api/v1/system/status | jq '.publishing'
curl -s localhost:8000/api/v1/social/platforms | jq '.[] | {platform, publish_enabled, reason}'
curl -s localhost:8000/api/v1/ads/transports | jq '.[] | {platform, available}'
```

Expected until credentials exist: `publishing.status = "unavailable"`,
`publish_enabled: false` everywhere, every ads transport `available: false` with a
reason naming the missing approval. Anything else means something is configured
that you did not intend to configure.
