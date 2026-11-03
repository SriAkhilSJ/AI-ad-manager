# Phase 3C.2 — DeepX Extension Remediation & Secure Flow Integration

> **Superseded in three places by [`docs/15`](15-PHASE3C21-TRUST-BOUNDARY-REVIEW.md)
> (Phase 3C.2.1).** The licensing gate now distinguishes five states and never authorizes on
> file presence; provenance separates well-formedness, self-consistency, artifact integrity
> and trust, and requires an independently configured anchor; and export-ticket identity is
> enforced inside the service before any mutation. The scanner's exit contract is now
> `0/1/2/3/4`, where `2` means "scanned, still not authorized". Everything else below still
> describes the current system; the verdict remains **NO-GO**.

**Milestone:** Phase 3C.2 (follows Phase 3C.1 `0343884`)
**Baseline:** `0343884` — 712 backend tests, acceptance 54/54, live Flow generation disabled
**Verdict:** **NO-GO.** The DeepX Flow Connector source package was not supplied to this
environment and carries no licence granting a right to modify and redistribute it.
U1–U7 therefore **cannot be resolved at the source level here**, and no upstream file has
been copied, forked or vendored. Everything that *can* be done without that source —
the receiving half of the contract — is implemented, tested and enforced: provenance
verification, protocol negotiation, the canonical eight-state generation lifecycle with
idempotency and reconciliation, chunked verified asset export, and a fail-closed
source/licensing gate with a repeatable audit scanner.

> The objective was a genuinely secure, verified Flow integration — not a simulated
> successful integration. A milestone that reported U1–U7 "fixed" against source it never
> read would be exactly that simulation. It is not reported fixed.

Live Flow generation remains **disabled** (`FLOW_LIVE_GENERATION_ENABLED=false`), no Google
account was connected, no image or video generation request was made, and no paid API call
was issued during this phase.

---

## 1. Provenance

| Component | Declared version | Where it comes from | Consistent? |
| --- | --- | --- | --- |
| Extension manifest | `1.2.1` | `manifest.json` (as reported in the audit) | — |
| README | `1.2.0` | `README.md` | ✗ vs manifest |
| Service-worker HELLO | `1.2.0` | `service-worker.js` handshake | ✗ vs manifest |
| MCP server / package | `1.0.0` | `mcp-server/package.json`, `mcp-server/src/server.js` | ✗ vs extension |
| Local bridge | unversioned | `mcp-server/src/ws-bridge.js`, port `8792` | ✗ |
| Source commit / release id | **absent** | not supplied | ✗ |
| Build id | **absent** | not supplied | ✗ |
| Package checksum | **absent** | no artifact supplied | ✗ |

Three different release lines ship inside one distributor package, and nothing ties any of
them to a reproducible artifact or commit. Under the Phase 3C.1 baseline that is not a
cosmetic mismatch: an "authorized" Flow client whose HELLO claims `1.2.0` while running
`1.2.1` code is a client whose self-reported capabilities cannot be trusted for a security
decision.

Crusor's side therefore treats version strings as **claims to be verified against
structure**, never as facts:

- `HelloMessage` must carry `extension_id`, a semver `extension_version`,
  `service_worker_version`, `mcp_version`, a 7–40 hex `source_commit`, a non-empty
  `build_id`, and (optionally) a SHA-256 `package_sha256`.
- `app/flow/provenance.py::verify_provenance()` re-derives the report from the HELLO plus
  the build Crusor is running against. A claim that cannot be corroborated is a refusal,
  not a warning.
- `extension_version` must equal `service_worker_version`. A service worker that reports a
  different version from the manifest is the exact drift this package exhibits.
- The MCP component may sit on a different `major.minor` line only when
  `FLOW_ALLOW_COMPONENT_DRIFT=true` — off by default, because the audited package *is*
  drifted.
- `provenance_trusted` is a single boolean surfaced in `hello_ack`, `/flow/status`, and the
  bridge snapshot. There is no code path that sets it without a successful verification.
- The actual connected extension version is what the bridge stores; it is never a
  configured constant, so `/flow/status` cannot report a version the peer did not send.

## 2. Licensing determination

The determination is a **gate**, not a paragraph. `app/flow/source_gate.py` returns one of
three states, and only the third permits source-level work:

| State | Meaning | Effect |
| --- | --- | --- |
| `SOURCE_ABSENT` (**current**) | no package path configured or no package present | U1–U7 blocked; nothing may be vendored |
| `LICENSING_BLOCKED` | a package exists but no `LICENSE`/`COPYING`/`AUTHORIZATION`/`PERMISSION` evidence | U1–U7 blocked; scanning permitted, edits forbidden |
| `AUTHORIZED` | package present **and** licence evidence grants modification + redistribution | source-level remediation permitted |

Current state, verbatim from `GET /api/v1/flow/status`:

```
status: SOURCE_ABSENT
source_present: false
license_files: []
authorization_files: []
authorized_to_modify: false
can_vendor_source: false
upstream_remediation_allowed: false
```

What was searched: `LICENSE`, `LICENSE.md`, `LICENSE.txt`, `COPYING`, `AUTHORIZATION.md`,
`PERMISSION.md` — in the repository, in the upload directory, and anywhere a DeepX package
could plausibly have been staged. The repository contains **no** `content-script-main.js`,
`service-worker.js` or `ws-bridge.js`; acceptance criterion 54 asserts that every run.

Required files, in the exact form the gate expects (they are also the files U1–U7 name):

```
manifest.json
service-worker.js
content-script-main.js
content-script-isolated.js
mcp-server/src/server.js
mcp-server/src/ws-bridge.js
mcp-server/package.json
```

Decision recorded: with no licence and no authorization, the correct action is to **stop**
at the source boundary, document the missing prerequisite, and implement only what Crusor
can enforce unilaterally. Inventing a licence, copying the package "just to fix it", or
publishing a patched fork would each convert a licensing gap into a redistribution
liability.

## 3. Original U1–U7 findings (as reported, not as remediated)

These are the findings from `DeepX_Flow_Integration_Audit.md`, restated because the
milestone must not lose them. **None has been fixed**: the code that contains them is not
present in this repository.

| ID | Finding | Blast radius |
| --- | --- | --- |
| **U1** | Flow auth tokens are broadcast over a wildcard `postMessage` and exposed to page JavaScript through `get_token` / `get_flow_context` | Any script in the Flow tab — including third-party or injected script — can read the bearer token |
| **U2** | `api_post(url, body, token)` attaches authorization to a caller-supplied URL | A page-context caller can aim Crusor's token at an arbitrary host |
| **U3** | `ws-bridge.js` listens on `127.0.0.1:8792` with no authentication and no HELLO gate | Any local process, and any web page that can reach loopback, can drive the extension |
| **U4** | MCP registration includes `eval_script` (Chrome debugger `Runtime.evaluate` / `window.eval`), `native_click`, `native_key`, `batch_generate`, `rapid_batch_dispatch`, `autonomous_storyboard_pipeline`, `rename_project`, `open_new_project` | Arbitrary code execution in an authenticated Flow session; unattended bulk generation; project destruction |
| **U5** | `host_permissions` include `http://*/*` and `https://*/*`, plus `debugger`, `tabs`, `scripting` | The extension can attach to any site and debug any tab |
| **U6** | No request identity before dispatch; retries after a lost response | Duplicate paid generations; timeouts read as failures |
| **U7** | Downloads are moved into place without byte verification | A truncated or substituted file can become an approved asset |

## 4. Implemented Crusor-side fixes (what this phase actually changed)

Each item below is enforced by running code and by a test that fails when the enforcement is
removed. Nothing in this section claims a fix to upstream code.

### 4.1 Provenance and protocol negotiation (§10)
`app/flow/provenance.py` (new): `ExtensionProvenance`, `ProvenanceReport`, `verify_provenance()`,
`provenance_from_hello()`, `ProvenanceRejected`, `UnsupportedProtocol`.
`app/flow/protocol.py`: `SUPPORTED_PROTOCOLS = {"flow.bridge.v1"}`, close codes
`4426` (unsupported protocol) and `4422` (provenance rejected) alongside the Phase 3C.1
`4401` (bad pairing) and `4000` (superseded).
`app/flow/bridge.py`: `_authenticate_hello()` now checks the presented protocol **before**
model validation, so an old build receives an unambiguous `4426` instead of a generic
pairing failure; `hello_ack` carries `provenance` and `provenance_trusted`; refusals
increment `stats.ws_rejected_protocol` / `ws_rejected_provenance`.

### 4.2 Generation lifecycle, idempotency, reconciliation (§8)
`app/flow/coordinator.py`: the canonical eight states
`NOT_SUBMITTED / SUBMITTING / SUBMITTED / RUNNING / SUCCEEDED / FAILED /
UNKNOWN_SUBMISSION / CANCELLED` with an explicit `LEGAL_TRANSITIONS` table and
`IllegalTransition`. `UNKNOWN_SUBMISSION` has **no** edge back to `NOT_SUBMITTED`; only
verified evidence moves it to `SUCCEEDED`, and it never becomes `FAILED` by assumption.
`submit(kind, request, approval_binding=..., idempotency_key=...)` maps one key to one
request hash for the coordinator's lifetime: identical replay returns the existing job
without touching the socket, a different payload raises `IdempotencyConflict`.
`cancel()` distinguishes "nothing was sent" from "the submit is in flight"
(`CancellationAmbiguous`) from "Google has it" (`provider_may_still_complete`).
`reconcile()` refuses a success claim without a correlated, unclaimed media id
(`ReconciliationRejected`).

### 4.3 Chunked, verified asset export (§7)
Migration `d5e6f7a8b9c0` adds `upload_mode`, `chunk_bytes`, `chunk_count`,
`chunks_received`, `chunk_manifest` (JSONB) to `flow_asset_exports`.
`app/services/flow_gateway.py` gained `accept_chunk()` and was refactored into
`_usable_ticket / _staging_key / _assemble / _cleanup_staging / _finalise / _fail`, so the
single-shot and chunked paths share one verification funnel. `POST
/api/v1/flow/exports/{id}/chunks` requires `X-Companion-Id`, `X-Companion-Secret`,
`X-Export-Token`, `X-Chunk-Index`, `X-Chunk-Count`, `X-Chunk-Sha256`; index order is
strict; duplicate, reordered, corrupt and oversized chunks are refused; each chunk is
verified against its own SHA-256, written to a staging key, and read back. Assembly
re-verifies the whole object — media sniffing, declared content type, ticket SHA-256,
storage read-back — **before** any `CreativeAsset` row exists. Staging objects are deleted
and stripped from `chunk_manifest` on success. Defaults: 8 MiB per chunk, 4096 chunks,
25 MiB per asset, all configurable.

### 4.4 Source/licensing gate (§4)
`app/flow/source_gate.py` + `scripts/flow_source_audit.py` (read-only, 14 scans S1–S14,
exit `0` clean / `1` findings / `3` blocked / `4` error). `/flow/status` exposes
`source_gate` and `protocol`.

## 5. Before / after architecture

**Before (upstream, as audited)**

```
Flow tab page JS ──postMessage(*)──> content script ──> bearer token exposed
                                     │
                        api_post(url, body, token) ──> any host
                                     │
        local process ──ws://127.0.0.1:8792 (no auth)──> service worker ──> debugger eval
                                     │
                              MCP server (9 tools)
```

**After (Crusor side, this phase)**

```
companion (Crusor-controlled, 127.0.0.1 only)
   │  ws://127.0.0.1:<ephemeral>/ws
   │  HELLO: pairing_id + pairing_secret + extension_id + 4 versions + commit + build_id
   ▼
FlowBridge  ── protocol in SUPPORTED_PROTOCOLS?  no ──> close 4426
            ── credentials constant-time match?   no ──> close 4401
            ── provenance verified?               no ──> close 4422
            ── Host/Origin validated, size/timeouts ─────> paired
   │
   ▼
GenerationCoordinator ── 8-state lifecycle, idempotency key + request hash,
   │                     one dispatch, no auto-retry, UNKNOWN_SUBMISSION on ambiguity
   ▼
FlowAssetExportService ── one-time ticket → chunks → per-chunk SHA-256 → read-back
   │                      → assembled re-verification → CreativeAsset (immutable)
   ▼
existing CreativeProvider / asset storage / audit log  (no second store, no second job table)
```

The upstream node in that diagram is still the audited one. The diagram is honest about
that: Crusor controls the right-hand side only.

## 6. Permission changes

**Upstream (unchanged by us, still required work):**

| Permission | Verdict |
| --- | --- |
| `http://*/*`, `https://*/*` | must be replaced by the verified Flow origins only |
| `debugger` | only defensible if `eval_script` is removed; this phase's recommendation is removal |
| `tabs` | scope to Flow tabs by code, never by broad query |
| `scripting` | scope to the isolated world, Flow origin only |

**Crusor (this phase):** no new browser permission, no new host, no invented domain. The
one operational addition is a configured companion identity
(`FLOW_COMPANION_ID` / `FLOW_COMPANION_SECRET`) and chunking limits — configuration, not
authority. The bridge still binds loopback with an ephemeral port; `0.0.0.0` remains
impossible by construction (`assert_loopback_bind`).

## 7. Authentication protocol

1. Companion dials `ws://127.0.0.1:<port>/ws` (port chosen by the bridge, never published).
2. Host and Origin are validated before the socket is accepted.
3. The first frame must be a `hello` within `handshake_timeout`.
4. Protocol check → `4426`. Credential check (constant-time, hashed at rest) → `4401`.
5. Provenance check against the build under test → `4422`.
6. `hello_ack` echoes the verified provenance and `provenance_trusted: true`; the session
   records the peer's *actual* extension id and version.
7. Commands are accepted only from a paired, proven session, only from the five-name
   allowlist, and only inside the message-size and timeout budgets.
8. HTTP routes require `X-Companion-Id` + `X-Companion-Secret`; unauthenticated callers get
   `401` and never learn whether a ticket exists.

A second bridge on `8792` is explicitly not part of this design: the companion speaks
`flow.bridge.v1` to Crusor's existing bridge rather than forwarding to an unauthenticated
one.

## 8. Token boundary

Crusor never asks for, receives, stores, logs, or returns a Google bearer token. The
`hello_ack`, `/flow/status`, error envelopes, audit events, and telemetry contain no token
field at all — the boundary is structural (there is nothing to leak) rather than a filter.
`FLOW_LIVE_GENERATION_ENABLED=false` means the coordinator refuses before a command is even
built. When the upstream extension is delivered, U1's requirement stands unchanged: tokens
stay inside the isolated world, `get_token`/`get_flow_context` and the wildcard broadcast
are deleted, and the injected-page-script test must show no token returned.

## 9. MCP command surface

Crusor exposes exactly five commands, and the coordinator plus the bridge both enforce the
list independently:

```
status, generate_image, generate_video, check_video_status, export_asset
```

Not reachable from Crusor, by construction and by test: `eval_script`, `native_click`,
`native_key`, `batch_generate`, `rapid_batch_dispatch`, `autonomous_storyboard_pipeline`,
`rename_project`, `open_new_project`. Removing them from the *upstream* MCP server remains
blocked work (U4) — this phase guarantees only that Crusor's side can never call them.

## 10. Generation lifecycle

| State | Meaning | Legal exits |
| --- | --- | --- |
| `NOT_SUBMITTED` | no dispatch attempted | `SUBMITTING`, `CANCELLED` |
| `SUBMITTING` | frame on the wire, outcome unknown | `SUBMITTED`, `RUNNING`, `UNKNOWN_SUBMISSION` |
| `SUBMITTED` | Google accepted it | `RUNNING`, `SUCCEEDED`, `FAILED` |
| `RUNNING` | provider reports progress | `SUCCEEDED`, `FAILED` |
| `SUCCEEDED` | media id claimed and bound | terminal |
| `FAILED` | failure proven **before** acceptance | `NOT_SUBMITTED` (explicit re-arm only) |
| `UNKNOWN_SUBMISSION` | ambiguity | `SUCCEEDED`, `FAILED` — never `NOT_SUBMITTED` |
| `CANCELLED` | Crusor stopped tracking | terminal |

A timeout is not proof of failure; a lost response is `UNKNOWN_SUBMISSION`; a duplicate
idempotency key with a different payload is refused rather than merged; the human resolves
anything the coordinator cannot prove.

## 11. Asset export

```
Chrome Flow → extension → companion → one-time ticket → Crusor upload (chunked)
            → per-chunk SHA-256 + storage read-back → assembly
            → media sniff (MIME from bytes) → declared-type + ticket SHA-256 check
            → storage read-back of the assembled object → immutable CreativeAsset
```

No asset row exists before the final verification. Filenames are sanitised, sizes are
bounded by configuration, tickets are one-time and expire (`FLOW_EXPORT_TTL_SECONDS=900`),
and a consumed ticket cannot be replayed (acceptance 56 asserts `422`). Approved assets are
never overwritten.

## 12. Version compatibility

| Crusor build | Speaks | Accepts |
| --- | --- | --- |
| Phase 3C.1 | `flow.bridge.v1` | authenticated HELLO, no provenance requirement |
| Phase 3C.2 (this) | `flow.bridge.v1` | authenticated **and** proven HELLO; consistent extension/service-worker versions; matching MCP major.minor unless drift is explicitly allowed |

A Phase 3C.1 extension will connect and then be refused `4422`, which is the intended
outcome: the audited package is drifted and cannot prove itself. Upgrade path: ship a
package with one release line, a commit id and a build id.

## 13. Tests and evidence

Run at `2026-11-03T18:18:05Z` against the live API on `:8000` with migration head
`d5e6f7a8b9c0`.

| Suite | Result |
| --- | --- |
| `tests/test_phase3c1_flow_security.py` | **95 passed** (baseline preserved; HELLO fixture updated to a proven build) |
| `tests/test_phase3c2_flow_compatibility.py` | **64 passed**, groups A–G |
| full `pytest tests` | **776 passed** |
| `scripts/acceptance.py` | **59/59** (48–53 preserved, 54–58 new) |
| `scripts/flow_source_audit.py` | exit `3` `SOURCE_ABSENT`; exit `3` `LICENSING_BLOCKED` on an unlicensed tree; exit `1` with 17 findings across 13/14 scans on a synthetic *licensed* package |
| frontend `npm run build` | clean (`tsc -b && vite build`) |

New acceptance criteria:

- **54** — the source/licensing gate fails closed, 7 requirements are published, and no
  upstream file exists in the repository.
- **55** — a consistent build is accepted with provenance; a `1.2.1/1.2.0/1.0.0` split and
  a missing commit are closed `4422`; `flow.bridge.v0` is closed `4426`; the bridge stays
  unpaired after every refusal.
- **56** — three chunks assemble into a byte-verified asset; out-of-order and corrupt
  chunks are refused `422`; the broken ticket accumulates no bytes and creates no asset; a
  consumed ticket cannot be replayed.
- **57** — duplicate key replays with one dispatch; conflicting key →
  `flow_idempotency_conflict`; `UNKNOWN_SUBMISSION` cannot be re-armed; cancel after
  submit reports `provider_may_still_complete`; a success claim without a media id is
  refused.
- **58** — all Phase 3C.1 criteria still pass and the allowlist, loopback bind, token policy
  and live-generation-off flag are unchanged.

Tests are behavioural: they drive the real bridge over a socket, the real service over HTTP
with raw bytes, and the real coordinator through its public API. No Google call is made and
no real credential exists in the fixtures.

## 14. Unresolved risks

| Risk | Severity | Status |
| --- | --- | --- |
| U1 token exposure | **critical** | unresolved upstream — source absent |
| U2 arbitrary authorized API | **critical** | unresolved upstream |
| U3 unauthenticated local bridge on 8792 | **critical** | unresolved upstream; Crusor does not use it |
| U4 `eval_script` and destructive tools | **critical** | unresolved upstream; not exposed to Crusor |
| U5 wildcard host permissions | high | unresolved upstream |
| U6 retry/dispatch ambiguity | high | **contained on Crusor's side**; upstream still lacks request identity |
| U7 unverified bytes | high | **contained on Crusor's side** (chunked + verified); upstream still lacks an export protocol |
| Version drift with no commit/build identity | medium | **detected and refused**; not corrected |
| Operator could set `FLOW_ALLOW_COMPONENT_DRIFT=true` | medium | off by default, documented, and it never bypasses an unsupported protocol |
| No upstream integration test possible | high | nothing beyond mocks can be claimed; no claim is made |

## 15. Exact requirements for Phase 3C.3

Phase 3C.3 may not begin until the following are supplied and verified. None is a
judgement call.

1. **Source package** at `FLOW_SOURCE_PACKAGE_PATH` containing, at minimum:
   `manifest.json`, `service-worker.js`, `content-script-main.js`,
   `content-script-isolated.js`, `mcp-server/src/server.js`, `mcp-server/src/ws-bridge.js`,
   `mcp-server/package.json`.
2. **Licence evidence** — `LICENSE`, `COPYING`, `AUTHORIZATION.md` or `PERMISSION.md` from
   the copyright holder explicitly granting the right to modify **and** redistribute. The
   gate flips to `AUTHORIZED` only when both are present.
3. **One release line** — manifest, README, service-worker HELLO and MCP package on the
   same version; a 7–40 hex commit id; a build id; a SHA-256 of the packaged artifact.
4. **U1** — token never leaves the isolated world; `get_token`, `get_flow_context` and the
   wildcard broadcast removed; an injected page script receives nothing (behavioural test).
5. **U2** — fixed allowlist of Flow origins and paths; method, schema and destination
   validated before authorization is attached; encoded, redirected and alternate-origin
   requests rejected; negative tests for external hosts, downgrade, redirects and path
   traversal.
6. **U3** — no second bridge: the companion speaks `flow.bridge.v1` to Crusor's bridge;
   `8792` removed; HELLO authenticated before any command.
7. **U4** — the eight dangerous tools deleted from the upstream registration, not merely
   hidden; `eval_script` unreachable even with a debugger permission.
8. **U5** — `host_permissions` reduced to verified Flow origins; every remaining permission
   documented with the code that needs it.
9. **U6** — request id generated before dispatch and persisted; one attempt; lost response
   → `UNKNOWN_SUBMISSION`; reconciliation by correlated id; human sign-off required.
10. **U7** — the upstream export path emits an export id, chunk index, chunk count and
    checksums compatible with the contract in §4.3 and §11 of this document.
11. **Evidence** — a passing run of `scripts/flow_source_audit.py` at exit `0`, the full
    backend suite, and acceptance with the source gate reporting `AUTHORIZED`.
12. **Rollback** — a documented way to disable Flow without touching the rest of the
    product.

Until then: **NO-GO**, live generation off, acceptance at 59/59 with criterion 54 asserting
the gate still fails closed.
