# Phase 3B — Creative Studio & Asset Pipeline

**Status:** COMPLETE for the mock provider. No paid generation call was made, no Google Flow
integration exists, and nothing in this phase publishes or schedules anything externally.
**Scope:** Creative Director briefs, provider abstraction, mock generation, job lifecycle, asset
storage/versioning, Creative QA, approval binding, Creative Studio UI, cost controls, docs, tests,
acceptance.
**Explicitly out of scope and untouched:** Phase 3C, Phase 4, real image/video model calls,
Google Flow implementation (blocked on missing source), social publishing, external scheduling.

---

## 1. Executive summary

Phase 3B turns an **approved content version** into a **structured creative brief**, executes that
brief through a **provider-independent generation service**, stores the result as a **versioned
asset**, validates it with **automated Creative QA**, and binds the whole combination — content
version + brief version + asset version — into the **existing** approval system.

| Capability | State | Evidence |
|---|---|---|
| Creative Director brief (26 structured fields) | working | `app/agents/creative.py`, `POST /creative-studio/briefs` |
| Brief never modifies content | enforced + tested | `test_brief_creation_binds_the_version_without_touching_content` |
| Provider abstraction (6 methods, structured responses) | working | `app/creative/contracts.py` |
| Deterministic mock provider (image + video poster) | working | `app/creative/providers.py` |
| Job lifecycle (7 states, retry, cancel, timeout) | working | `app/services/creative_generation.py` |
| Idempotency and duplicate prevention | working | content-hash dedupe + `Idempotency-Key` |
| Asset storage with checksum + read-back verification | working | `tests/test_phase3b_creative.py` |
| Asset versioning (no overwrite, no inherited selection) | working | `POST /creative-studio/assets/{id}/versions` |
| Creative QA (17 checks, 4 verdicts) | working | `app/services/creative_qa.py` |
| Approval binding + invalidation | working | `app/services/approval.py` |
| Creative Studio UI (overview, briefs, generation, library, detail) | working | `tsc` clean, `vite build` clean |
| Google Flow | **unavailable, fail-closed** | `/creative-studio/flow/status` |
| Paid generation | **disabled by default** | `creative_allow_paid_generation = False` |
| Publishing / external scheduling | **does not exist** | no route; `publishing_available: false` |

**What this report will not claim:** that real model-generated creative works. Every asset in this
phase was produced by the deterministic mock provider: an abstract charcoal frame painted by
`render_brand_frame()`. It is not model art, not a screenshot of Crusor software, and not a video.
Video jobs produce a **poster frame only** and say so in `capabilities.video_playback`.

---

## 2. Architecture

```
approved content version ─┐
campaign objective ───────┤
brand knowledge ──────────┼─► Creative Director ──► CreativeBrief (versioned, hashed)
creative direction ───────┘                              │
                                                        ▼
                                        CreativeGenerationService
                                          │  capability gate
                                          │  paid/budget gate
                                          │  idempotency + duplicate check
                                          ▼
                                     CreativeProvider (Protocol)
                                     ├── MockCreativeProvider   (default, free)
                                     ├── FlowCreativeProvider   (fail-closed)
                                     └── DisabledCreativeProvider
                                          │
                                          ▼
                                CreativeGenerationJob (7 states, audit trail)
                                          │  validate bytes → store → read back → verify SHA-256
                                          ▼
                                   CreativeAsset (versioned, checksummed)
                                          │
                                          ├─► CreativeQA  ──► verdict + report
                                          └─► CreativeService.select()
                                                    │
                                                    ▼
                                    ApprovalRecord (Phase 1 table)
                                    content_version + brief_version + asset_version
```

**Reuse, not duplication.** There is no second approval system, no second asset table and no
second state machine:

* content items/versions/approvals — `app/models/content.py`, `app/services/approval.py`
* asset rows — the existing `creative_assets` table, extended with studio columns
* storage — the existing `app/storage/base.py` backend (`local` in dev)
* audit — the existing `record_audit()` trail

New tables: `creative_briefs`, `creative_generation_jobs` (migration `9b4c7d1e5a02`).

---

## 3. Creative Director agent

`app/agents/creative.py` — `build_director_brief()` runs alongside the Phase 1 creative agent, so
existing callers receive exactly what they did before (`test_legacy_creative_agent_behaviour_is_unchanged`).

The brief carries every field the phase spec requires:

| Field | Source |
|---|---|
| campaign / content / content version IDs | approved content item + its exact current version |
| platform, asset type | content item + request |
| creative objective | campaign objective, else content headline |
| visual concept, scenes, subjects, background, lighting, composition | brand visual direction + platform spec |
| colour palette | brand knowledge (`#0A0A0B`, `#C0C0C8`, `#8B5CF6` by default) |
| typography, brand asset requirements | brand knowledge + logo policy (use the official file; never redraw) |
| image/media prompt, negative prompt | assembled, plus `BASE_NEGATIVE_PROMPT` |
| aspect ratio, resolution, duration | `PLATFORM_ASPECT`, `PLATFORM_VIDEO_SECONDS` |
| reference assets | the content version itself, marked read-only |
| validation constraints | palette, text coverage, contrast, human-review requirement |

**Never silently modifies content.** Brief generation reads the version, records its `content_hash`,
and writes only to `creative_briefs`. The API test diffs the content item before/after.

Dedupe: a brief is keyed by `(content_version_id, asset_type, direction, aspect, duration)`; asking
twice returns the same brief with `200`, `force_new_version=True` creates `version_number + 1`.

---

## 4. Provider contract

`app/creative/contracts.py` defines the Protocol every provider implements:

```python
async def get_capabilities() -> CreativeCapabilities
async def submit_image_generation(request: GenerationRequest) -> GenerationSubmission
async def submit_video_generation(request: GenerationRequest) -> GenerationSubmission
async def get_generation_status(job_id: str) -> GenerationState
async def retrieve_asset(job_id: str) -> GeneratedMedia
async def cancel_generation(job_id: str) -> bool
```

Structured responses always carry: provider, job id, status, submitted prompt, parameters,
timestamps, asset metadata (MIME, size, dimensions, duration, checksum), a normalised error
(`code`, `message`, `retryable`, `provider_raw`) and cost **when the provider actually reports it** —
`None` otherwise, never `0` by default.

`GenerationRequest.canonical()` produces the deterministic payload the mock hashes into its job id,
so identical requests are reproducible across processes.

### Mock provider

* job id `mock:{epoch}:{sha256[:16]}:{b64url(canonical)}` — stateless, reproducible, self-describing
* image: `render_brand_frame()` — charcoal base, metallic rule, purple rim light, PNG encoder written
  for this repo (no Pillow available in the environment)
* video: a **poster frame**, `video_playback: "poster_only"`, `encoded_video: null`
* cost `0.0`, `billable: False`
* failure simulation through an explicit `parameters.mock_simulate` (`fail`, `timeout`,
  `ambiguous_timeout`, `running`) — deterministic, no randomness

### Disabled provider

Refuses every call with `integration_unavailable`. Selected by `CREATIVE_PROVIDER=disabled`.

---

## 5. Google Flow boundary

Verified three times: **there is no Flow Connector source in this repository.** No manifest, no
messaging API, no bridge, no MCP tool. Per the phase constraint, none was invented.

`FlowCreativeProvider` therefore fails closed:

* `get_capabilities()` → `supports_image: false`, `supports_video: false`, `requires_authorization: true`
* every submit/retrieve/cancel raises `IntegrationUnavailableError`
* `required_information()` lists exactly what is needed to implement it

`GET /creative-studio/flow/status` reports:

```json
{
  "status": "not_available",
  "available": false,
  "enabled": false,
  "source_found": false,
  "adapters_implemented": ["creative_provider_boundary"],
  "required_information": [ ... 6 items ... ]
}
```

The six items: the connector's manifest/capability descriptor; the messaging API shape (transport,
message types, correlation ids); authentication and account-binding model; generation parameters and
their limits; status/result retrieval and asset delivery (URLs vs bytes); pricing and rate limits, or
written confirmation that it is free.

**What would change:** drop the real module in, implement the six Protocol methods against the
verified protocol, set `CREATIVE_PROVIDER=flow`, then re-run the provider contract tests. Until then
the provider stays disabled and the UI says so.

---

## 6. Job lifecycle

```
QUEUED ──► SUBMITTED ──► RUNNING ──► SUCCEEDED
   │            │           │  │
   │            │           │  ├──► FAILED      (normalised error, retryable flag)
   │            │           │  ├──► CANCELLED   (only when the provider confirms)
   │            │           │  └──► TIMED_OUT   (ambiguous ⇒ no automatic resubmission)
```

* **Duplicate prevention**: same brief + same canonical request returns the existing job (`created=false`);
  same `Idempotency-Key` with a different request is a `409`.
* **Timeouts**: past `creative_job_timeout_seconds`, the service asks the provider once more. If the
  provider cannot confirm, the job becomes `TIMED_OUT` with `ambiguous=true` and
  `error_retryable=false`. `retry()` refuses unless the caller explicitly passes
  `authorize_ambiguous=true` — this is the rule that prevents a duplicate paid generation.
* **Retries**: bounded by `creative_job_max_attempts`; beyond that → `409`.
* **Cancellation**: allowed from queued/submitted/running; a terminal job → `409`.
* **Audit**: every transition appends to `job.audit_trail` and to the platform audit log.

---

## 7. Asset storage and versioning

Each asset retains: asset id, campaign, content item, content version, brief id + version, job id,
provider, media type, MIME (declared **and** detected), size, width/height/duration, aspect ratio,
SHA-256 checksum, storage key, created date, validation status, approval status, `is_mock`,
`asset_version`, `supersedes_asset_id`, `root_asset_id`, `selected`.

* Bytes are written through the existing storage backend and **read back**; the recovered SHA-256 must
  equal the recorded checksum or the job fails rather than registering a corrupt asset.
* MIME is decided by file signature (`inspect_media`), never by client metadata; a ZIP renamed
  `hero.png` is rejected (`422`).
* Size is capped by `creative_max_upload_bytes` (25 MiB default).
* The original bytes are never overwritten. A replacement upload writes to a **new** storage key,
  creates `asset_version + 1`, marks the previous row `superseded`, and **does not inherit**
  `selected` or any approval.

---

## 8. Creative QA

`app/services/creative_qa.py`. Verdicts: `PASS`, `PASS_WITH_WARNINGS`, `NEEDS_REVISION`, `BLOCKED`.

Automated and actually performed (13–14 checks depending on format):

| Family | Checks |
|---|---|
| integrity | bytes readable, checksum matches stored bytes |
| format | MIME, dimensions known, platform minimum (320/400/640 px short edge), platform aspect ratio, requested resolution met |
| brand | real pixel analysis: mean luma, dark ratio ≥ 0.45, purple ratio ≤ 0.35, metallic presence |
| claims | unsupported claim patterns (`%`, multipliers, “trusted by”, “best”, fabricated customers) |
| brand | logo recreation requests, fabricated product UI/screenshot instructions |
| relevance | overlap between brief/content keywords and the prompt |

Real pixels, not metadata: `decode_png_pixels()` decodes 8-bit RGB/RGBA non-interlaced PNG
(filters 0–4) and samples ~6k pixels (≈1 ms). Non-PNG formats report `performed: false`.

**Never performed, always declared as such:** `text_readability` (no OCR), `watermark_detection`
(a mock adds none; third-party watermarks are not detectable), `human_visual_review`. Every report
carries `visual_verification: "not_performed"` and `human_visual_review_required: true`, and the UI
renders that banner on the asset page.

---

## 9. Approval integration

The Phase 1 ledger does the work; Phase 3B only tells it what to bind.

* `CreativeAsset.selected` marks the chosen asset for a content version. Selecting deselects peers
  and invalidates live approvals.
* On approve, `ApprovalService` stamps `creative_brief_id`, `creative_brief_version`,
  `creative_asset_id`, `creative_asset_version` and `creative_binding_hash` — a SHA-256 over
  `{content_version_id, brief_id, brief_version, asset_id, asset_version, asset_checksum}`.
* `valid_approval()` returns `None` when the content version, selection or binding hash no longer
  matches, so editing content, generating a new asset, or selecting another one voids the approval
  without deleting the record (history is preserved, `invalidated_at` + reason are set).
* A generated asset **never** auto-approves or auto-selects anything: a job returns an asset with
  `selected: false` and the content item stays in `DRAFT`.

---

## 10. Cost and safety controls

| Control | Behaviour |
|---|---|
| Default provider | `mock`; `billable: false`; cost `0.0` recorded |
| Paid generation | `creative_allow_paid_generation = False`; a billable provider is refused before a request is built |
| Provider identity | a billable provider must match the configured `CREATIVE_PROVIDER` name, or the request is refused |
| Unknown pricing | `creative_pricing_json` empty ⇒ price unknown ⇒ refused. Unknown is **not** zero. |
| Budget | `creative_campaign_budget_usd`; `0` blocks every billable request (zero is not unlimited) |
| Estimate | estimated cost must fit the budget before submission; actual cost is recorded after |
| Tests | no live call is possible: every test uses the injected mock or a fake billable provider that asserts it is never invoked |
| Publishing | no route, `publishing_available: false`, `external_scheduling: false` |

---

## 11. API surface

| Method | Path | Notes |
|---|---|---|
| GET | `/creative-studio/capabilities` | provider, mock/billable flags, Flow status, limits |
| GET | `/creative-studio/flow/status` | fail-closed readiness report |
| GET | `/creative-studio/overview` | counts by campaign/platform/validation, provider + storage |
| POST/GET | `/creative-studio/briefs` | create (200 on dedupe, 201 on new), list, get |
| POST/GET | `/creative-studio/jobs` | submit (200 on replay, 201 on new), list, get |
| POST | `/creative-studio/jobs/{id}/poll` | advance once |
| POST | `/creative-studio/jobs/{id}/retry` | `{authorize_ambiguous}` |
| POST | `/creative-studio/jobs/{id}/cancel` | provider-confirmed cancellation |
| GET | `/creative-studio/assets` | filters: campaign, content item, platform, kind, validation, search |
| GET | `/creative-studio/assets/{id}` | detail + approval + brief + job + versions |
| GET | `/creative-studio/assets/{id}/bytes` | raw bytes, `X-Asset-Checksum`, `X-Asset-Mock` |
| POST | `/creative-studio/assets/{id}/validate` | re-run Creative QA |
| POST | `/creative-studio/assets/{id}/select` | select for approval (invalidates) |
| POST | `/creative-studio/assets/{id}/versions` | upload a new version (never overwrites) |
| POST | `/creative-studio/assets/upload` | strict external upload |

All read routes require authentication; all writes require editor or founder.

---

## 12. Frontend

Extends the existing `/creative` route: **Overview**, **Brief editor**, **Generation centre**,
**Asset library** (grid/list, search, filters), **Asset detail** (`/creative/assets/:id`).

* every mock asset is labelled in the UI (`mock_label`, `MOCK` badge)
* no control exists without a working endpoint behind it
* retrying an ambiguous timeout is a explicit, confirmed action, not a button that looks like a
  normal retry
* the asset page shows the QA report *including* the checks that were not performed

Verification: `tsc --noEmit` clean, `vite build` clean (402.08 kB / 110.07 kB gz).

---

## 13. Testing

`backend/tests/test_phase3b_creative.py` (43) and `backend/tests/test_phase3b_api.py` (25) cover:

brief fields and never-mutating behaviour, dedupe and versioning, provider contract conformance,
capabilities honesty, mock determinism, poster-not-video, failure/timeout/ambiguous simulation,
Flow fail-closed + requirements, provider selection, disabled provider, job lifecycle, idempotency
and duplicate prevention, retry limits, ambiguous-timeout refusal, cancellation, asset persistence
and checksum read-back, MIME spoof rejection, size/extension limits, version immutability, QA
verdicts (pass/blocked/checksum/unsupported claim/logo/UI/off-palette/not-performed), approval
binding, approval invalidation, 401/403/404/422 paths, and the absence of any publishing route.

Full suite: **617 passed** (549 prior + 68 new). Acceptance harness: **48/48** criteria.
No test makes a live call.

---

## 14. Live acceptance evidence

Run against the real API on `:8000` with the acceptance database:

| Step | Result |
|---|---|
| `GET /capabilities` | `provider=mock`, `is_mock=true`, `billable=false`, `live_generation_enabled=false`, `video_playback=poster_only` |
| `GET /flow/status` | `not_available`, `available=false`, `enabled=false`, 6 required items |
| `POST /briefs` | brief `linkedin / 1.91:1 / 1080x565`, hash recorded |
| content before/after | **unchanged** |
| `POST /jobs` | `succeeded`, `provider=mock`, `is_mock=true`, `cost_actual_usd=0.0`, `mock:…` job id |
| replay of same job | same job id returned, no second asset |
| `GET /assets/{id}` | `960x503`, `image/png`, `pass_with_warnings`, 17 checks, `selected=false`, `unapproved` |
| `GET /assets/{id}/bytes` | 54 842 bytes, `image/png`, SHA-256 matches the asset checksum |
| `GET /overview` | `total_assets=1`, `mock_assets=1`, `succeeded_jobs=1`, `approved_assets=0` |
| `POST /jobs` (video brief) | submitted `running`, polled to `succeeded`; asset is a PNG poster with `video_playback: poster_only` |
| `GET /system/status` | creative section: `mock`, paid `false`, flow `not_available`, no publishing |
| POST to `/publishing`, `/creative-studio/publish`, `/assets/{id}/publish` | `404` (no such endpoint) |
| `GET /overview` without a token | `401` |
| Alembic `9b4c7d1e5a02` | up → down → up on a fresh DB; `creative_assets.selected` and the `created_at` server defaults present after re-upgrade |
| `scripts/acceptance.py` | **48/48** criteria pass (35 prior + 12 Phase 3B + the cross-phase regression check) |

---

## 15. Security findings

1. **Client metadata is never trusted.** MIME comes from magic bytes; `detected_mime` is stored next
   to `mime_type` so a mismatch is visible. The legacy `/creative/upload` route stays permissive
   (`strict=False`) to preserve its documented behaviour; the studio routes are strict.
2. **Path traversal is not reachable.** Storage keys are generated server-side
   (`creatives/{uuid}/{slug}`); client filenames are only ever stored as metadata.
3. **No SSRF surface.** The mock provider makes no network call; no asset is fetched from a
   provider URL — bytes are delivered in-band and verified.
4. **Checksum pinning.** Assets are verified on write and on QA; a mismatch blocks.
5. **Approval cannot be spoofed by regeneration.** The binding hash includes the asset checksum, the
   brief version and the content version.
6. **Authorisation** is enforced per route (viewer read-only; writes need editor; approval needs
   founder) and re-checked inside the services.
7. **Ambiguous timeouts** deliberately require an explicit, separate authorisation — the automation
   cannot spend money twice by itself.

---

## 16. Limitations

* **No real generation.** Only the deterministic mock runs. Image and video provider integration is
  a later phase.
* **Video is a poster frame.** No encoded video is produced or stored; the UI labels this.
* **QA is partial by design.** No OCR, no aesthetic scoring, no watermark detection for third-party
  media. Text readability and brand judgement require a human.
* **Pixel analysis is PNG-only.** JPEG/WebP dimension checks still run, but palette analysis is
  reported as not performed.
* **Flow cannot be implemented** until the six documented pieces of information exist.
* **No publishing or scheduling.** Not implemented, not stubbed, not reachable.
* **Cost accounting for real providers is untested** — no paid provider has ever been configured.
* The mock caps generated output at `creative_mock_max_dimension` (960), so briefs that ask for
  1080 px are downscaled and QA raises `resolution_reduced` as a warning rather than hiding it.
