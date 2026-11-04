# 27 — Integration canary report

Generated 2026-11-04T15:40:09+00:00 by `scripts/integration_canary.py`.

A canary is the smallest real call that can prove a provider accepts us. This run was executed with `--network=yes` and `--live=yes`, so read the `blocked` column literally: it names the first thing that must change before the next column can move.

| Provider | Kind | Credentials | Live canary | Sandbox-verified | Production-verified |
| --- | --- | --- | --- | --- | --- |
| Facebook Page | social | blocked | blocked | no | no |
| Google Ads API | ads | blocked | blocked | no | no |
| Instagram Business | social | blocked | blocked | no | no |
| LinkedIn | social | blocked | blocked | no | no |
| LinkedIn Marketing API | ads | blocked | blocked | no | no |
| Meta Marketing API | ads | blocked | blocked | no | no |
| OpenAI Images | creative | blocked | blocked | no | no |
| Reddit | social | blocked | blocked | no | no |
| Replicate video | creative | blocked | blocked | no | no |
| X | social | blocked | blocked | no | no |
| YouTube | social | blocked | blocked | no | no |

## Per provider

### Facebook Page (`facebook`)

- **credentials** — blocked: missing: meta_app_id, meta_app_secret
- **oauth_config** — blocked: TOKEN_VAULT_BACKEND=none — a token cannot be stored after the exchange
- **publishing_gate** — info: PUBLISHING_ENABLED=false — publishing is refused before any network call
- **preview** — info: authorize ['graph.facebook.com'] /me read; scopes pages_manage_posts, pages_read_engagement, read_insights
- **network** — blocked: GET https://graph.facebook.com/ failed after 57 ms — ConnectError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
- **live** — blocked: a social canary needs a connected account and runs through the API: set CANARY_API_BASE and CANARY_API_TOKEN (a founder/service token), and connect the account with POST /social/oauth/{platform}/start first
- **evidence** — info: no live call has succeeded for this provider

### Google Ads API (`google_ads`)

- **credentials** — blocked: missing: GOOGLE_ADS_ACCESS_TOKEN, GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_LOGIN_CUSTOMER_ID
- **preview** — info: v17 → https://googleads.googleapis.com/v17; available=False; reason=Google Ads needs GOOGLE_ADS_ACCESS_TOKEN, GOOGLE_ADS_DEVELOPER_TOKEN and GOOGLE_ADS_LOGIN_CUSTOMER_ID (the customer id with billing). Without all three the transport reports unavailable.
- **network** — blocked: GET https://googleads.googleapis.com/ failed after 23 ms — ConnectError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
- **live** — blocked: not configured: Google Ads needs GOOGLE_ADS_ACCESS_TOKEN, GOOGLE_ADS_DEVELOPER_TOKEN and GOOGLE_ADS_LOGIN_CUSTOMER_ID (the customer id with billing). Without all three the transport reports unavailable.
- **evidence** — info: no live call has succeeded for this provider

### Instagram Business (`instagram`)

- **credentials** — blocked: missing: meta_app_id, meta_app_secret
- **oauth_config** — blocked: TOKEN_VAULT_BACKEND=none — a token cannot be stored after the exchange
- **publishing_gate** — info: PUBLISHING_ENABLED=false — publishing is refused before any network call
- **preview** — info: authorize ['graph.facebook.com'] /me read; scopes instagram_basic, instagram_content_publish, instagram_manage_insights, pages_show_list
- **network** — blocked: GET https://graph.facebook.com/ failed after 84 ms — ConnectError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
- **live** — blocked: a social canary needs a connected account and runs through the API: set CANARY_API_BASE and CANARY_API_TOKEN (a founder/service token), and connect the account with POST /social/oauth/{platform}/start first
- **evidence** — info: no live call has succeeded for this provider

### LinkedIn (`linkedin`)

- **credentials** — blocked: missing: linkedin_client_id, linkedin_client_secret
- **oauth_config** — blocked: TOKEN_VAULT_BACKEND=none — a token cannot be stored after the exchange
- **publishing_gate** — info: PUBLISHING_ENABLED=false — publishing is refused before any network call
- **preview** — info: authorize ['api.linkedin.com', 'www.linkedin.com'] /me read; scopes w_member_social, r_liteprofile, r_organization_social, rw_organization_admin
- **network** — blocked: GET https://api.linkedin.com/ failed after 21 ms — ConnectError: TLS/SSL connection has been closed (EOF) (_ssl.c:992); GET https://www.linkedin.com/ failed after 46 ms — ConnectError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
- **live** — blocked: a social canary needs a connected account and runs through the API: set CANARY_API_BASE and CANARY_API_TOKEN (a founder/service token), and connect the account with POST /social/oauth/{platform}/start first
- **evidence** — info: no live call has succeeded for this provider

### LinkedIn Marketing API (`linkedin_ads`)

- **credentials** — blocked: missing: LINKEDIN_ADS_ACCESS_TOKEN, LINKEDIN_ADS_ACCOUNT_URN
- **preview** — info: 202405 → https://api.linkedin.com/rest; available=False; reason=LinkedIn Ads needs LINKEDIN_ADS_ACCESS_TOKEN and LINKEDIN_ADS_ACCOUNT_URN, plus an app approved for the Advertising API. Without them the transport is unavailable.
- **network** — blocked: GET https://api.linkedin.com/ failed after 40 ms — ConnectError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
- **live** — blocked: not configured: LinkedIn Ads needs LINKEDIN_ADS_ACCESS_TOKEN and LINKEDIN_ADS_ACCOUNT_URN, plus an app approved for the Advertising API. Without them the transport is unavailable.
- **evidence** — info: no live call has succeeded for this provider

### Meta Marketing API (`meta_ads`)

- **credentials** — blocked: missing: META_ADS_ACCESS_TOKEN, META_ADS_ACCOUNT_ID
- **preview** — info: v21.0 → https://graph.facebook.com/v21.0; available=False; reason=Meta Marketing API needs META_ADS_ACCESS_TOKEN and META_ADS_ACCOUNT_ID; POST /ads/transports reports this as unavailable until both exist.
- **network** — blocked: GET https://graph.facebook.com/ failed after 20 ms — ConnectError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
- **live** — blocked: not configured: Meta Marketing API needs META_ADS_ACCESS_TOKEN and META_ADS_ACCOUNT_ID; POST /ads/transports reports this as unavailable until both exist.
- **evidence** — info: no live call has succeeded for this provider

### OpenAI Images (`openai_image`)

- **credentials** — blocked: missing: OPENAI_API_KEY
- **preview** — info: model gpt-image-1; sizes 1024x1024, 1536x1024, 1024x1536
- **network** — blocked: GET https://api.openai.com/ failed after 20 ms — ConnectError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
- **live** — blocked: missing: OPENAI_API_KEY
- **evidence** — info: no live call has succeeded for this provider

### Reddit (`reddit`)

- **credentials** — blocked: missing: reddit_client_id, reddit_client_secret
- **oauth_config** — blocked: TOKEN_VAULT_BACKEND=none — a token cannot be stored after the exchange
- **publishing_gate** — info: PUBLISHING_ENABLED=false — publishing is refused before any network call
- **preview** — info: authorize ['oauth.reddit.com', 'www.reddit.com'] /me read; scopes identity, submit, read, flair
- **network** — blocked: GET https://oauth.reddit.com/ failed after 29 ms — ConnectError: TLS/SSL connection has been closed (EOF) (_ssl.c:992); GET https://www.reddit.com/ failed after 30 ms — ConnectError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
- **live** — blocked: a social canary needs a connected account and runs through the API: set CANARY_API_BASE and CANARY_API_TOKEN (a founder/service token), and connect the account with POST /social/oauth/{platform}/start first
- **evidence** — info: no live call has succeeded for this provider

### Replicate video (`replicate_video`)

- **credentials** — blocked: missing: REPLICATE_API_TOKEN, REPLICATE_VIDEO_MODEL
- **preview** — info: a pinned owner/model:version is required; an unpinned model is refused
- **network** — blocked: GET https://api.replicate.com/ failed after 30 ms — ConnectError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
- **live** — blocked: missing: REPLICATE_API_TOKEN, REPLICATE_VIDEO_MODEL
- **evidence** — info: no live call has succeeded for this provider

### X (`x`)

- **credentials** — blocked: missing: x_client_id, x_client_secret
- **oauth_config** — blocked: TOKEN_VAULT_BACKEND=none — a token cannot be stored after the exchange
- **publishing_gate** — info: PUBLISHING_ENABLED=false — publishing is refused before any network call
- **preview** — info: authorize ['api.twitter.com'] /me read; scopes tweet.read, tweet.write, users.read, offline.access
- **network** — blocked: GET https://api.twitter.com/ failed after 50 ms — ConnectError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
- **live** — blocked: a social canary needs a connected account and runs through the API: set CANARY_API_BASE and CANARY_API_TOKEN (a founder/service token), and connect the account with POST /social/oauth/{platform}/start first
- **evidence** — info: no live call has succeeded for this provider

### YouTube (`youtube`)

- **credentials** — blocked: missing: google_client_id, google_client_secret
- **oauth_config** — blocked: TOKEN_VAULT_BACKEND=none — a token cannot be stored after the exchange
- **publishing_gate** — info: PUBLISHING_ENABLED=false — publishing is refused before any network call
- **preview** — info: authorize ['oauth2.googleapis.com', 'www.googleapis.com'] /me read; scopes https://www.googleapis.com/auth/youtube.upload, https://www.googleapis.com/auth/youtube.readonly, https://www.googleapis.com/auth/yt-analytics.readonly
- **network** — blocked: GET https://oauth2.googleapis.com/ failed after 23 ms — ConnectError: TLS/SSL connection has been closed (EOF) (_ssl.c:992); GET https://www.googleapis.com/ failed after 22 ms — ConnectError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
- **live** — blocked: a social canary needs a connected account and runs through the API: set CANARY_API_BASE and CANARY_API_TOKEN (a founder/service token), and connect the account with POST /social/oauth/{platform}/start first
- **evidence** — info: no live call has succeeded for this provider

## What a green row still would not prove

* A read-only identity call proves the credential and the account, not the publish path, the media upload, the insights window or the rate limit under load.
* Ads `validate_account` proves access to the account; it does not prove that a create is accepted, that billing is funded, or that the review status allows the objective we intend.
* A creative capability probe proves the key is accepted by the service listing; it does not prove a generation succeeds, costs what we think, or returns an asset that passes integrity checks. Only a submitted job proves that.

