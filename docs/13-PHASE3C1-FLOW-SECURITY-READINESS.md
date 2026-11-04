# Phase 3C.1 — DeepX Flow Bridge Security Readiness

**Status:** COMPLETE for the security boundary. **Live Flow generation is still disabled.** No Google
account was connected, no Flow generation was executed, no image or video was produced, and no paid
model was called. Nothing from the third-party extension was vendored, copied, or redistributed.

**Scope:** security audit of the DeepX Flow Connector, a loopback-only authenticated bridge, a
capability allowlist, the token-handling boundary, generation-safety semantics, a verified
asset-export contract, a local companion that dials out, the human-approval gate for real
generations, tests, and acceptance evidence.

**Explicitly out of scope and untouched:** Phase 3C.2 (live canary), Phase 4, real Flow submission,
real Google OAuth, publishing, external scheduling.

---

## 1. Executive summary

Phase 3C.1 is a **hardening phase**, not a feature phase. It answers one question: *if we ever connect
DeepX Flow to Crusor, what has to be true first?* The answer has two halves.

**What Crusor now guarantees**, all of it in this repository and all of it tested:

| Guarantee | State | Evidence |
|---|---|---|
| Bridge binds loopback only; a public bind is refused before a socket exists | enforced | `test_non_loopback_bind_is_refused_before_any_socket_exists` |
| The **actual** listening address is loopback, read from the live socket | verified | `test_actual_listening_address_is_loopback`, acceptance criterion 50 |
| HTTP requests authenticate with a 256-bit pairing credential | enforced | `test_http_requires_a_pairing_credential` |
| Extension WebSocket handshake authenticates in a `hello` frame | enforced | `test_unauth_websocket_is_closed_with_4401` |
| Host and Origin are validated before any work happens | enforced | `test_bridge_rejects_foreign_host_and_origin` |
| Request size, connection count and handshake time are capped | enforced | `test_request_limits_are_enforced` |
| Commands are an allowlist of five operations; everything else is refused by name | enforced | `denied_keys()`, `test_every_dangerous_command_is_denied` |
| No route can return a Google token, post to an arbitrary URL, or evaluate script | enforced | `test_no_route_exposes_a_dangerous_capability` |
| Secrets are redacted from logs and never echoed in responses | enforced | `test_secrets_are_redacted_from_logs_and_text`, `test_flow_responses_never_contain_credentials` |
| One request = one generation attempt; one live generation at a time | enforced | `test_one_request_is_one_attempt`, `test_only_one_live_generation_at_a_time` |
| An ambiguous submit becomes `UNKNOWN_SUBMISSION` and is never auto-retried | enforced | `test_ambiguous_submission_never_resubmits` |
| A real generation needs a founder approval bound to eight fields | enforced | `test_flow_generation_is_refused_without_an_approval` |
| An asset exists only after bytes arrive, match the checksum and survive a read-back | enforced | `test_export_ticket_accepts_only_verified_bytes` |
| The remote backend talks to the local companion over an **outbound** connection | enforced | `test_companion_only_ever_dials_out` |

**What Crusor cannot guarantee yet**, because the fix lives inside someone else's extension: the seven
blockers **U1–U7** in §4. The honest verdict is unchanged from the audit:

> **PARTIALLY READY. NOT SAFE TO CONNECT YET.**

Crusor's side of the wire is now defensible. The upstream side is not, and Phase 3C.1 does not pretend
otherwise.

---

## 2. Source and licensing status

| Question | Answer |
|---|---|
| Upstream project | DeepX Flow Connector (Chrome MV3 extension + local MCP server) |
| Version reported by the audit | extension `1.2.1`; `README` says `1.2.0`; service-worker HELLO says `1.2.0`; MCP server `1.0.0` |
| Source archive on this machine | **not present** (searched six times: repo, `/tmp`, uploads, home) |
| Licence file in the package | **none found** |
| What was vendored | **nothing** |
| Consequence | The extension is treated as an opaque third party. Every required upstream change is recorded as a blocker with the file that must change, not implemented here. |

The version drift matters on its own: four components disagree about what build is running, so a
canary could not even prove *which* code it exercised. That is blocker **U1**'s sibling problem and is
listed with the others.

---

## 3. Bridge architecture

Two listeners, both pinned to `127.0.0.1`, never `0.0.0.0`, never a LAN address, never a hostname that
merely resolves to loopback:

```
                    user's machine                        Crusor backend (cloud)
┌──────────────────────────────────────────────┐      ┌──────────────────────────────┐
│  Chrome  ──  DeepX extension (third party)   │      │  FastAPI                     │
│        │            ▲                        │      │   /api/v1/flow/*             │
│        │ WS :8794   │ hello + pairing        │      │   FlowCompanionGateway       │
│        ▼            │                        │      │   FlowAssetExportService     │
│  ┌─────────────────┴──────────────┐          │      │   FlowApprovalGate           │
│  │  Local Flow Companion          │          │      │                              │
│  │  ├─ FlowBridge                 │          │      │                              │
│  │  │   ├─ HTTP 127.0.0.1:8793    │          │      │                              │
│  │  │   └─ WS   127.0.0.1:8794    │          │      │                              │
│  │  └─ outbound HTTPS client ─────┼──────────┼─────►│  (companion authenticates    │
│  │      hello / heartbeat /       │  dials   │      │   with a 256-bit secret;     │
│  │      drain commands / results  │  OUT     │      │   no inbound path to the     │
│  └────────────────────────────────┘          │      │   user's loopback exists)    │
└──────────────────────────────────────────────┘      └──────────────────────────────┘
```

The direction of the arrow is the design. A cloud service cannot reach `127.0.0.1` on a customer's
laptop, and a design that tries (tunnel, port-forward, "just open the firewall") is a design that
turns a browser extension into an internet-exposed command runner. Instead the companion polls:
`GET /api/v1/flow/companion/commands` long-polls for allowlisted work and `POST
/api/v1/flow/companion/results` returns the outcome.

### 3.1 HTTP surface (`127.0.0.1:8793`)

| Property | Implementation |
|---|---|
| Routes | `/api/status` (GET), `/api/command` (POST). Nothing else exists. |
| Authentication | `X-Flow-Pairing: <id>.<secret>`; both halves compared with `hmac.compare_digest`, no early exit on the id |
| Content type | `application/json` only; anything else is `415` |
| Request caps | 8 KiB header block, `Content-Length` required (`411` on chunked), 64 KiB body (`413`) |
| Timeouts | 15 s per request (`408`) |
| Connections | hard ceiling (`429`), loopback peer address re-checked (`403`) |
| Headers | `nosniff`, `no-store`, `Connection: close` — and **no CORS headers, ever**, so a page cannot read a reply |
| Host / Origin | allowlist rebuilt from the *actual* bound port; wrong Host `421`, wrong Origin `403` |

### 3.2 WebSocket surface (`127.0.0.1:8794`)

| Property | Implementation |
|---|---|
| Route | `/ws` only |
| Pre-upgrade checks | Host and Origin validated in `process_request` (before the socket becomes a WebSocket) |
| Handshake | `hello` frame within 5 s carrying protocol, pairing id, secret, extension id and version; otherwise close `4401` |
| Extension identity | must appear in `FLOW_ALLOWED_EXTENSION_IDS`; an **empty allowlist trusts nobody** |
| Post-handshake frames | `heartbeat` and `result` only; anything else closes `4400` |
| Concurrency | one authenticated extension socket; a newer pairing retires the old one with `4000` |
| Message cap | 32 MiB |

### 3.3 Credentials

`python -m app.flow.pairing --print` mints a 256-bit pairing secret (43 characters, `urlsafe`) plus a
pairing id, and prints a paste-ready `.env` block. `--fingerprint` prints the first 16 hex characters of
SHA-256, which is the only form safe to log or store. The bridge refuses to start with a missing or
short secret, and the pairing secret is in the redaction list of every bridge and companion logger.

---

## 4. Upstream blockers (reported, not fixed)

Each row is a change that must land in the third-party package. Crusor's code path is already
fail-closed for all seven, so an unmodified extension simply cannot be used.

| ID | Area | Finding | Required upstream change | Why it is not fixed here |
|---|---|---|---|---|
| **U1** | page-visible token channel | `content-script-main.js` broadcasts `DEEPX_AUTH_TOKEN_CAPTURED` with `window.postMessage(..., '*')` and answers `get_token` / `get_flow_context` for any page script | delete the broadcast and both handlers; keep tokens in the isolated world | source not supplied, licence unconfirmed |
| **U2** | arbitrary authenticated POST | `api_post(url, body, token)` attaches the Google bearer token to a caller-supplied URL | replace with fixed allowlisted Flow endpoints, return non-secret results | requires editing `content-script-main.js` |
| **U3** | pairing authentication | upstream WS bridge listens on `8792` with no hostname and accepts any client; `POST /api/command` unauthenticated | require pairing id + secret in HELLO and refuse to run unpaired | unmodified extensions cannot authenticate, so they are refused |
| **U4** | generation commands | `eval_script`, `batch_generate`, `rapid_batch_dispatch`, `autonomous_storyboard_pipeline`, `rename_project`, `open_new_project`, `native_click`, `native_key` are registered MCP tools | ship a restricted build, or gate the powerful tools behind a developer flag Crusor never sets | tool registration lives in the third-party MCP server |
| **U5** | host permissions | `manifest.json` requests `http://*/*` and `https://*/*` | narrow to Flow/Google hosts | `manifest.json` is part of the third-party package |
| **U6** | ambiguous submit retry | the service worker retries a submit whose outcome it does not know | remove the retry; surface `UNKNOWN_SUBMISSION` instead | the retry lives in `service-worker.js` |
| **U7** | asset delivery | the download command only starts a Chrome download and returns `{downloadId,status}`; there is no way to retrieve the bytes | add an export command that returns the media bytes with a checksum | requires a new command in the extension service worker |

Two more findings have no code fix at all and are recorded as facts, not tasks: **no licence** means
the package cannot be redistributed, and the four components disagree about their own version
(`1.2.1` / `1.2.0` / `1.2.0` / `1.0.0`), so a canary could not prove what it ran.

---

## 5. Token handling

The audit's most serious finding is that Google bearer tokens are currently visible to **page
JavaScript**: a `window.postMessage` broadcast with a `'*'` target and `get_token` /
`get_flow_context` handlers that answer any script on the page. Combined with `api_post(url, ...)`,
any XSS in the Flow tab, any malicious extension sharing the page, or any injected script can obtain a
token and mail it anywhere.

**What Phase 3C.1 does**

* Crusor never requests a token. `get_token`, `get_flow_context` and `api_post` are in `DENIED_COMMANDS`,
  refused by name before a frame is written (`test_every_dangerous_command_is_denied`).
* There is no Crusor route that returns credentials, and `test_status_does_not_leak_google_endpoints_or_bearer_tokens`
  asserts the status payload contains no `googleapis.com`, `Bearer `, `access_token` or `ya29.` string.
* Every log line passes through `RedactingFilter`, which scrubs `ya29.*`, `Bearer …`, cookie jars and
  pairing values even when no explicit secret list was supplied.
* The bridge's HTTP surface emits no CORS headers, so a page cannot read a bridge reply even if it can
  reach the port.
* Upstream error text never reaches Crusor verbatim: `normalise_error` maps it to a fixed vocabulary and
  keeps at most a short code in `details`, so a dumped request cannot leak through an error field.

**What Phase 3C.1 cannot do**

The token still leaves the isolated world into page JavaScript, because the code that does it is in
`content-script-main.js`, which is not in this repository and has no licence. **This is blocker U1 and
it is not fixed.** Any statement that "token exposure is resolved" would be false.

---

## 6. Command allowlist

Crusor may ask the extension for exactly five things:

| Command | Purpose |
|---|---|
| `status` | readiness and non-secret metadata |
| `generate_image` | one image, `count == 1` |
| `generate_video` | one video, `count == 1` |
| `check_video_status` | poll a **correlated** video job |
| `export_asset` | return media bytes for verification |

Refused by name, before any socket write: `eval_script`, `native_click`, `native_key`,
`batch_generate`, `rapid_batch_dispatch`, `autonomous_storyboard_pipeline`, `rename_project`,
`open_new_project`, `download_media`, `upload_asset`, `cancel_job`, `get_token`, `get_flow_context`,
`api_post`. Matching is exact — `STATUS`, `" status"`, `status;rm -rf /`, `__proto__` and
`native_mouse_move` are all denied (`test_unknown_and_dangerous_commands_are_denied`).

The unrestricted MCP tool collection is never exposed to Crusor. Crusor's own OpenAPI document is
checked for forbidden route fragments (`eval`, `api_post`, `get_token`, `native_`, `storyboard`,
`script`) by `test_no_route_exposes_a_dangerous_capability` and acceptance criterion 49.

---

## 7. Generation safety

The semantics are conservative on purpose, because a duplicated video generation costs money and a
silently dropped one costs trust.

| Rule | Implementation |
|---|---|
| One request = one attempt | `GenerationCoordinator.submit()` sends exactly one envelope; `attempts` increments once |
| One live generation at a time | `max_live_jobs = 1`; a second submit raises `JobInProgress` |
| Idempotent requests | identical request hash returns the existing job and writes nothing to the socket |
| Dispatch is the point of no return | `job.dispatched = True` immediately after `send()` succeeds |
| Uncertainty stays uncertain | timeout, dropped socket, foreign job id, or an error without explicit `dispatched: false` → `UNKNOWN_SUBMISSION` |
| Unknown never auto-resubmits | `retry()` raises `UnknownSubmissionRemains`; polling never re-submits |
| Failed ≠ uncertain | only an explicit `dispatched: false` yields `FAILED`, which is retryable, and it clears the dispatch flag |
| Jobs are correlated | result job ids must match; a media id belongs to exactly one job (`_media_owner`), so a stale tile cannot satisfy another job |
| Images are not polled into false completion | image jobs stay `SUBMITTED` until an export proves completion |

**Nothing in this phase executes a generation.** `POST /api/v1/flow/generations` runs the approval gate
and then refuses: with `dry_run: true` it returns *authorised, not dispatched*; with `dry_run: false` it
returns `503 integration_unavailable`. The companion additionally refuses `generate_image`,
`generate_video` and `export_asset` unless `allow_generation_commands` is set, which nothing sets in
Phase 3C.1 (`test_companion_refuses_generation_commands_even_if_queued`).

---

## 8. Asset export contract

The current upstream download command only starts a Chrome download (`{downloadId, status}`), and a
media URL on a Flow CDN is not reachable from a cloud backend — it may need the user's session and it
expires. So "mark the asset stored when the download starts" is not an option.

The contract implemented:

```
create ticket (backend)                 companion                    backend
──────────────────────                 ─────────                    ───────
content/brief/job ids
expected sha256 (optional)
declared content-type
size cap, TTL 900 s
32-byte token, only its
SHA-256 is stored  ──── one-time token ──►  retrieve bytes from the
                                            completed Flow surface
                                            (U7: not implemented upstream)
                                             │
                                             ▼
                          PUT /flow/exports/{id}/bytes   ──►  size cap
                          X-Companion-*  + X-Export-Token       sniffed type
                          raw body                             declared type
                                                                expected sha256
                                                                storage write
                                                                read-back hash
                                                                asset row created
                                                                ticket consumed
```

Rules that are tested, not just documented:

* an asset row is created **after** verification, never before (`test_export_checksum_mismatch_never_creates_an_asset`);
* a checksum mismatch, a size over the ticket limit, an unrecognised container, or a declared/sniffed
  type mismatch all fail closed and leave the ticket `failed`;
* a ticket is single-use, expires, and belongs to exactly one companion
  (`test_export_ticket_expiry_and_foreign_companion`);
* the raw token is never stored, only its SHA-256, and it is returned exactly once at creation.

The gap that remains is upstream: there is still no command that returns bytes, so this contract is
ready but cannot be exercised against the real extension (**U7**).

---

## 9. Local companion

```
STOPPED → STARTING → UNPAIRED ──extension hello──► PAIRED
                          │                          │
                          └──────────► CONNECTED ◄───┘
                                    │        │
                              failures   credential rejected
                                    ▼        ▼
                                DEGRADED   DISCONNECTED (no retry)
```

* **Lifecycle:** `python -m app.flow.companion` validates a 256-bit companion credential, starts the
  loopback bridge, registers with `POST /companion/hello`, then heartbeats and long-polls for commands.
* **Pairing:** the extension pairs with the *bridge* on loopback; Crusor only learns the extension id
  and version through the companion's reports.
* **Local authentication:** `X-Companion-Id` + `X-Companion-Secret`, compared in constant time, with the
  same "not configured ⇒ refuse" behaviour as the bridge.
* **Disconnect handling:** a rejected credential stops the loop after one attempt (no retry storm); a
  transport error backs off exponentially (1 s → 20 s cap) and marks the session `DEGRADED`.
* **Status protocol:** `GET /flow/status` returns sessions, queue depth, stale detection (90 s) and the
  bridge's own limits. No secret, no fingerprint, no token.
* **Error normalisation:** upstream codes are mapped to a fixed vocabulary; a command that fails is
  reported with `dispatched` so the coordinator can distinguish safe-to-retry from ambiguous.
* **Executable entry point:** `python -m app.flow.companion` is the documented way to run it. It
  validates its credentials *before* the bridge is created, so an unpaired or misconfigured companion
  logs `flow_companion_misconfigured:` and exits `2` without ever opening a listener
  (`test_companion_main_returns_configuration_error_without_starting`, acceptance criterion 53).

---

## 10. Human approval gate

Every real generation must be covered by a founder `APPROVE` record on the **live** content version,
carrying a binding hash over:

```
campaign_id · content_version_id · brief_id · brief_version ·
prompt_hash · provider · model · aspect_ratio · duration_seconds · output_count
```

`FlowApprovalGate.authorize()` reads the single existing approval ledger — no second approval system —
and rejects with `flow_approval_required` when:

* there is no live approval for the version;
* the live approval is a **Phase 3B approval** with no `flow_*` columns (absent evidence is not
  approval — `test_a_3b_approval_does_not_authorise_a_flow_generation`);
* the binding hash differs (prompt, model, ratio, duration or count changed);
* the output count differs;
* the approval was invalidated.

Because `POST /flow/generations` refuses after the gate passes, an agent cannot convert autonomy into a
dispatch: the best an agent can obtain in Phase 3C.1 is an authorisation decision that explicitly says
`dispatched: false`.

---

## 11. What changed in the repository

| Area | File |
|---|---|
| Protocol, allowlist, deny list, blockers, schemas | `backend/app/flow/protocol.py` |
| Credential generation, constant-time compare, redaction, Host/Origin policy, limiter | `backend/app/flow/security.py` |
| Job lifecycle, one-attempt semantics, export verification | `backend/app/flow/coordinator.py` |
| Loopback HTTP + WS bridge, TLS-free but authenticated, single session | `backend/app/flow/bridge.py` |
| Outbound-dialling companion | `backend/app/flow/companion.py` |
| Pairing CLI | `backend/app/flow/pairing.py` |
| Companion sessions + export tickets | `backend/app/models/flow.py`, 7 `flow_*` columns on `approval_records` |
| Migration (additive only) | `backend/alembic/versions/c4d5e6f7a8b9_phase3c1_flow_bridge.py` |
| Approval binding + gate | `backend/app/services/flow_approval.py`, `backend/app/services/approval.py` |
| Companion registry, export service, status payload | `backend/app/services/flow_gateway.py` |
| HTTP surface | `backend/app/api/v1/flow.py`, `backend/app/schemas/flow.py`, `system.py`, `router.py` |
| Tests (95) | `backend/tests/test_phase3c1_flow_security.py` |
| Acceptance checks 48–53 | `scripts/acceptance.py` |

Settings added (all default to *off*): `FLOW_BRIDGE_ENABLED=false`, `FLOW_BRIDGE_HOST=127.0.0.1`,
`FLOW_BRIDGE_PORT=8793`, `FLOW_BRIDGE_WS_PORT=8794`, `FLOW_PAIRING_ID`, `FLOW_PAIRING_SECRET`,
`FLOW_ALLOWED_EXTENSION_IDS`, `FLOW_MAX_LIVE_JOBS=1`, `FLOW_LIVE_GENERATION_ENABLED=false`,
`FLOW_EXPORT_TTL_SECONDS=900`, `FLOW_EXPORT_MAX_BYTES`, `FLOW_COMPANION_ID`, `FLOW_COMPANION_SECRET`,
`FLOW_BACKEND_URL`.

Migration `c4d5e6f7a8b9` was verified up → down → up on a scratch database; it only adds tables,
indexes and nullable columns, so a Phase 3B deployment can roll forward without a rewrite.

---

## 12. Test results

```
backend/tests/test_phase3c1_flow_security.py   95 passed
backend/tests (full suite)                     712 passed, 0 failed
scripts/acceptance.py                          54/54 criteria passed
```

The 95 tests cover, in eight groups: transport hardening (binding, auth, Host/Origin, limits,
redaction, WS handshake, session supersede), the allowlist (every denied command, unknown names,
case and injection attempts), generation safety (one attempt, idempotency, single live job, ambiguous
submit, foreign job id, stale tile, export checksums), the approval gate (missing, 3B-era, invalidated,
mismatch, no bypass), the export contract (verified, checksum mismatch, wrong type, oversize, expiry,
reuse, foreign companion), the HTTP surface (status, capabilities, companion lifecycle, generation
refusal, tickets), the local companion (dial-out only, allowlisted execution, generation refusal,
credential rejection without retry, backoff), and leak checks (route surface, response bodies, log
redaction, error normalisation). One further test pins the module entry point: a configuration failure
returns exit code `2` without constructing a bridge.

Acceptance criteria 48–53 add: the status payload is honest and generation is off; dangerous commands
are denied by name and absent from the OpenAPI surface; a **real os-level bridge** binds `127.0.0.1`
(read from `getsockname()`), refuses `0.0.0.0`, and answers `401 / 403 / 421 / 413 / 503` correctly;
unauthenticated companion callers are rejected on every companion route; a real generation request is
refused by the approval gate; and `python -m app.flow.companion` exits `2` with a
`flow_companion_misconfigured:` log line when its pairing credentials are missing, proving the CLI
fails closed before it can listen.

No paid model was called. No image or video was generated. No Google account was connected. Every
"media" byte in the tests is a PNG built in-process by `encode_png()`.

---

## 13. Remaining blockers and what unblocks Phase 3C.2

| # | Blocker | Owner | Unblocks |
|---|---|---|---|
| 1 | U1 token broadcast / `get_token` / `get_flow_context` | upstream extension | any token-handling claim |
| 2 | U2 arbitrary `api_post` destination | upstream extension | safe Flow API calls |
| 3 | U3 unauthenticated HELLO, port 8792 | upstream extension | pairing at all |
| 4 | U4 powerful MCP tools registered by default | upstream extension | capability restriction end-to-end |
| 5 | U5 `http://*/*` host permissions | upstream extension | least-privilege manifest |
| 6 | U6 ambiguous-submit retry | upstream extension | one-attempt guarantee end-to-end |
| 7 | U7 no byte export path | upstream extension | asset export contract |
| 8 | No licence for the package | upstream / legal | vendoring, pinning, reproducible builds |
| 9 | Version drift 1.2.1 / 1.2.0 / 1.2.0 / 1.0.0 | upstream | provable canary build |
| 10 | Flow tab UI selectors unverified (no headless browser here) | engineering | real generation canary |

Phase 3C.2 should not begin until items 1–9 are resolved. When they are, the canary is: one founder,
one approved binding, one image, `max_live_jobs = 1`, live generation enabled by explicit configuration,
with the export contract as the only path by which an asset can exist.

---

## 14. Deliberate non-claims

* Not claiming Flow works. Nothing was dispatched, and the extension cannot currently authenticate.
* Not claiming token exposure is fixed. The broadcast lives in `content-script-main.js`; **U1 stands**.
* Not claiming the extension is safe. It is treated as untrusted and refused unless it changes.
* Not claiming the export path is exercised against the real product. The contract is tested; the
  upstream byte export does not exist.
* Not claiming an asset is a real creative. Every test asset is a synthetic PNG.
* Not claiming redistribution rights. No upstream source was copied into this repository.
