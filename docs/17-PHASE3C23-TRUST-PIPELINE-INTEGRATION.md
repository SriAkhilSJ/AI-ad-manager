# Phase 3C.2.3 — Trust Pipeline Integration and Hardening

**Milestone:** Phase 3C.2.3 (baseline `0316a99d11c18e0f72e66140472e6c211b24ec5e`)
**Objective:** make `VERIFIED` mean what it says — the configured installed artifacts were
**measured by Crusor's own local process**, matched against a **valid signed component
release**, and were **still fresh at the moment a protected operation was requested**.

## Verdict

```
RELEASE ANCHOR:          MISSING      (no authorized release artifact or signing key exists)
INSTALLED EXTENSION:     NOT FOUND    (no extension installation is configured or present)
LOCAL MEASUREMENT PATH:  IMPLEMENTED  (the companion process reads the roots itself)
PEER-REPORTED TRUST:     REMOVED      (the extension socket has no measurement verb)
COMPONENT REQUIREMENTS:  MANIFEST-FLOOR ENFORCED (config may add, never remove)
LEGACY ANCHORS (v1/pin): READABLE, NEVER LIVE-CAPABLE
FRESHNESS AT USE TIME:   ENFORCED     (re-checked by the guard and the coordinator)
RUNTIME DIGEST COVERAGE: DEPENDENCIES INCLUDED (`sha256-tree-runtime-v1`)
BUDGET CONTROL:          REAL LEDGER OR BLOCKED
OVERALL PROVENANCE:      UNVERIFIED
LIVE FLOW GENERATION:    DISABLED
PHASE 3C.3:              NO-GO
```

The trust pipeline is now wired end to end and proven against **synthetic** fixtures. A
synthetic release passing these tests is evidence about the machinery, not about DeepX: no
authorized release artifact exists to measure, so the real-world verdict is unchanged and
Phase 3C.3 remains blocked. U1–U7 are still unresolved upstream; no DeepX source was
obtained, fabricated or vendored; no Google account was connected, no Flow tab was driven,
and no paid call was made.

---

## 1. The six defects this phase had to close

Phase 3C.2.2 built a verifier and a strict aggregator, but the *pipeline* around them still
had holes. Each is named here with the change that closes it.

| # | 3C.2.2 defect | 3C.2.3 resolution |
| --- | --- | --- |
| D1 | The **extension** could submit a "verification report" over its own socket, and the bridge would accept it | the message types `verification_challenge` / `verification_report` are **deleted** from `protocol.py`; the bridge performs the measurement itself (§3) |
| D2 | Trust had no validity deadline; a decision stayed trusted while the socket stayed open | `TrustEvidence.measurement_expires_at` + `TrustDecision.valid_until`/`is_fresh()`/`expired_copy()`; both `current_trust()` and the live guard re-check at use time (§9) |
| D3 | A v1 manifest, or a pinned extension digest alone, could reach `VERIFIED` | `TrustAnchor.live_capable` — v2 schema, verified signature, component identity, SHA-256 for every required component — is a hard precondition in `aggregate_trust()` (§8) |
| D4 | Configuration could silently measure fewer components than the release declares | `effective_required_components()` = manifest ∪ config; an explicit narrower config is recorded as a `required_component_downgrade` failure (§7) |
| D5 | The live guard was passed `budget_configured=True` by its caller | `SpendingControls` derived from a real `FlowBudgetLedger`; the coordinator reserves against the same ledger (§13) |
| D6 | The licensing digest excluded `node_modules`/`.venv`/`__pycache__`, so a patched dependency kept the release digest | a second, versioned profile `sha256-tree-runtime-v1` measures dependencies; the algorithm is declared in the signed manifest and must agree (§11) |

## 2. What `VERIFIED` means now

A decision is `VERIFIED` only when **all** of the following hold, checked in this order by
`aggregate_trust()`:

1. `source_gate_status == "AUTHORIZED"` — authorization to work with the upstream source is a
   separate question from release authenticity (§12);
2. an anchor is configured and present;
3. the anchor is **live-capable** — `crusor.flow.release.v2`, Ed25519 signature verified
   against a key configured outside the package, a digest for every required component;
4. the session's HELLO is internally consistent (no component-version drift);
5. an installed artifact was **measured** — by this process, not reported by the peer;
6. the measurement carries no failure (no missing/duplicate/unknown component, no mismatch);
7. the measurement is fresh, and its expiry has not passed *at aggregation time*;
8. runtime identity is correlated — the measured extension id/version equal the HELLO's and
   the anchor's;
9. every required component (manifest ∪ config) is `VERIFIED`;
10. the measured digest matches the anchor, and the anchor itself is valid.

Anything else is one of the eight non-verified states, chosen so the **state names the
earliest thing a human would fix** — never collapsed into a boolean.

## 3. Where the measurement happens

`LocalFlowCompanion` (`app/flow/companion.py`) runs `FlowBridge` in the companion process.
That process is the thing that can reach a configured install root, so it is the thing that
measures:

```
extension WS peer                local companion process                 backend
──────────────────                ───────────────────────                 ───────
HELLO (claims, digest)  ────────► FlowBridge._authenticate_hello
                                  ├ verify_provenance()          (claims)
                                  ├ hello_ack  (trusted=false)   ← nothing measured yet
                                  └ measure_session(session)     ← asyncio task
                                      ├ SessionAttestation.issue(companion_id, …)
                                      ├ asyncio.to_thread(_measure_locally)
                                      │    └ measure_components({name: InstallationSpec})
                                      │         └ InstalledArtifactVerifier.measure()
                                      │            (read the filesystem, hash every file)
                                      ├ SessionAttestation.accept()   (strict, §10)
                                      └ aggregate_trust() → TrustRegistry → status
```

Properties that follow from the design:

* **The peer never supplies a measurement.** `SessionAttestation.issue()` is called with
  `companion_id = config.companion_id` — the companion's own identity, never
  `session.extension_id`. Session identity, companion identity and extension identity are
  three separate values, and `ChallengeStore.consume()` compares each one.
* **The challenge is a binding, not proof.** A nonce proves the *responder* saw the challenge;
  it never proves a filesystem was read. The filesystem read is the evidence; the challenge
  only ties it to this session.
* **No report verb exists.** A `verification_report` frame from the peer is an unknown frame
  type and closes the socket with `4400` (test: `test_a_peer_supplied_report_is_not_a_measurement`).
* **The ack tells the truth early.** `hello_ack` reports `provenance_trusted: false` and
  `trust_state: "UNVERIFIED"` unconditionally, because nothing has been measured at that
  instant. The claim-level verdict is reported as `hello_digest_claim_verified` under its own
  narrower name. (Phase 3C.2 and 3C.2.1 tests that asserted `provenance_trusted is True` from a
  HELLO were updated to the narrower field — a self-reported digest is not integrity.)
* **The measurement is bound to the session.** On disconnect the measurement task is cancelled
  and awaited, so a decision cannot be recorded for a session that has gone.

## 4. How settings reach the verifier

Nothing is discovered and nothing is guessed. Six settings, read once at bridge construction,
become one `ComponentInstallSpec` per component:

| Setting | Meaning | Default |
| --- | --- | --- |
| `FLOW_EXTENSION_INSTALL_ROOT` | the extension's install directory | *(empty — nothing measured)* |
| `FLOW_EXTENSION_INSTALL_LAYOUT` | `versioned` (`Extensions/<id>/<version>`) or `directory` (unpacked; identity from the manifest `key`) | `versioned` |
| `FLOW_MCP_INSTALL_ROOT` | the MCP server directory | *(empty)* |
| `FLOW_MCP_INSTALL_LAYOUT` | `tree` (a plain directory, measured whole) or `directory` | `tree` |
| `FLOW_COMPANION_INSTALL_ROOT` / `_LAYOUT` | the companion's own installation | *(empty)*, `tree` |
| `FLOW_REQUIRED_COMPONENTS` | **additional** required components, comma-separated | *(empty — the manifest decides)* |
| `FLOW_ARTIFACT_DIGEST_ALGORITHM` | which digest profile the manifest and measurement use | `sha256-tree-runtime-v1` |
| `FLOW_MEASUREMENT_FRESHNESS_SECONDS` | validity window | `300` (fails closed) |

Failure behaviour, all explicit:

* a required component with **no root** → `MeasurementError("component_not_configured")` →
  trust state `INSTALLED_ARTIFACT_NOT_FOUND` (never a silent skip);
* a root that does not exist → `installed_artifact_not_found`;
* a root reached through a symlink, or containing one → `symlink_in_package`;
* an unsupported layout, an empty root, a duplicate component, an unknown component name, or a
  `tree` layout for the Chrome extension → `BridgeConfigError` at construction time;
* a Chrome profile is **never** searched for. There is no code path that enumerates
  `~/.config/google-chrome` or any other browser directory.

## 5. Trust boundary

| Party | Can it influence `VERIFIED`? | How |
| --- | --- | --- |
| Browser extension (WS peer) | **No** | it sends a HELLO whose fields are checked as *claims*; it has no measurement verb |
| Local companion process | **Yes, it is the trust root** | it reads the disk, issues and consumes the challenge, and records the decision |
| Crusor backend | **No direct effect on the measurement** | it reads the resulting status and applies approval/budget/source gates of its own |
| Signed release manifest | **Yes** | it decides *what* must exist and *what digest* it must have |
| Operator configuration | **Yes, additively only** | it says where to look and may add requirements; it cannot remove a manifest requirement |

The backend's `/flow/status` reports the measurement configuration, so an operator can see
that a required component has no root *before* a session is refused (`measurement` block in
`flow_status_payload`).

## 6. Which component supplies each evidence item

| Evidence item | Produced by | Verified against |
| --- | --- | --- |
| `measurement_id` | `InstalledArtifactVerifier` (uuid4 per measurement) | format + presence (§10) |
| `measured_at` | the verifier's `finished_at` (local clock) | challenge window, never `challenge.issued_at` |
| `file_count` | the number of files actually hashed | must be > 0 |
| `artifact_digest` | the tree digest of the measured component | the signed manifest's per-component digest |
| `digest_algorithm` | the measurement policy | the manifest's declared `digest_algorithm` |
| `extension_id` / `version` | the install path + `manifest.json` (or the manifest key) | HELLO identity **and** anchor identity |
| `runtime_identity_correlated` | `accept()` comparing id/version across the three sources | — |
| `measurement_expires_at` | `measured_at + freshness_seconds` | re-checked at use time |
| `claims_well_formed` / `components_consistent` | `verify_provenance()` over the HELLO | — |
| `release_anchor_valid` | `verify_release_manifest()` | Ed25519 against the configured key |
| `source_gate_status` | `evaluate_source_gate()` | `AUTHORIZED` only |

## 7. Manifest authority

The signed manifest's `required` components are a **floor**:

* `effective_required_components()` returns `{chrome_extension} ∪ manifest.required ∪
  config.required`;
* a non-empty `FLOW_REQUIRED_COMPONENTS` that omits a manifest requirement records a
  `required_component_downgrade` failure — it is never silently honoured, and an empty setting
  is not a declaration, so the default cannot look like one;
* a required component that is not in the release at all fails with
  `component_not_in_release:<name>`;
* a required component that was not measured is `NOT_MEASURED` and fails
  `component_not_measured:<name>`;
* duplicate component names, unknown names, and missing digests are rejected while parsing
  (`provenance.py`), and duplicates are rejected again in `accept()`.

Test: `test_a_manifest_required_mcp_server_cannot_be_omitted` configures only the extension
for a release that requires the extension **and** the MCP server; the session ends in
`INSTALLED_ARTIFACT_NOT_FOUND` with `component_not_configured`.

## 8. Legacy anchors

Both legacy forms remain **readable** — an operator can still inspect what they describe — and
neither can produce a live-capable session:

| Anchor | `present` | `identifies_components` | `live_capable` |
| --- | --- | --- | --- |
| `crusor.flow.release.v1` + `artifact.sha256` | yes | no | **no** |
| pinned `FLOW_TRUSTED_ARTIFACT_SHA256` | yes | no | **no** |
| `crusor.flow.release.v2` (signed, all components) | yes | yes | yes |

`aggregate_trust()` returns `UNVERIFIED` with `anchor_not_live_capable` for the first two, even
when every other evidence field is perfect. A single extension digest cannot describe a
three-component integration, and v1 has no per-component identity to check.

## 9. Freshness at use time

Three independent places re-check the window, so none of them can be bypassed by keeping a
socket open:

1. `SessionAttestation.accept()` refuses a `measured_at` outside the challenge window, more than
   5 s in the future, or older than `freshness_seconds` — and it has **no fallback** to
   `challenge.issued_at`;
2. `aggregate_trust()` returns `MEASUREMENT_STALE` when `now >= measurement_expires_at`;
3. `FlowBridge.current_trust()` demotes an expired decision to `MEASUREMENT_STALE` and records
   the revocation; `evaluate_live_generation_guard()` and `GenerationCoordinator.submit()` both
   reject it before a request is built.

The default window is 300 s and is configurable; an unparseable or non-positive window fails
closed (nothing is fresh).

## 10. Report validation

`accept()` refuses, with a named code:

| Input | Code |
| --- | --- |
| missing / zero `measured_at` | `measurement_timestamp_missing` |
| `measured_at` before the challenge, in the future, or older than the window | `measurement_outside_challenge_window` |
| missing or non-hex `measurement_id` (must be 32 hex chars) | `measurement_id_missing` |
| `file_count <= 0` | `empty_measurement` |
| digest not a 64-char lowercase SHA-256 | `measurement_malformed` |
| algorithm unknown | `unsupported_digest_algorithm` |
| algorithm differs from the manifest's declaration | `digest_algorithm_mismatch` |
| the same component twice | `duplicate_component` |
| a component name outside the known set | `unknown_component` |
| no `chrome_extension` | `component_missing` |
| replayed challenge | `challenge_replayed` |
| challenge from another session / companion / extension | `challenge_wrong_session` / `challenge_wrong_companion` / `challenge_wrong_extension` |
| nonce mismatch | `challenge_nonce_mismatch` |

## 11. Runtime digest coverage

Two digest profiles now exist, and the difference is deliberate:

| Profile | Includes | Excludes | Used for |
| --- | --- | --- | --- |
| `sha256-tree-v1` | source tree | `.git`, `__pycache__`, `node_modules`, `.venv`, `.mypy_cache` | licensing/authorization gate (unchanged semantics) |
| `sha256-tree-runtime-v1` | **everything the process can load**, including `node_modules`, virtualenvs, `site-packages`, `__pycache__` | `.git` only | installed-runtime measurement |

Both use the same encoder (`tree_digest_from_pairs`), so the difference is *what is included*,
not how bytes are hashed. The algorithm is declared in the signed manifest
(`digest_algorithm`), echoed in each measurement, and a mismatch is rejected.

Two further hardening points:

* **Service-worker validation uses the exact normalized relative path.** A declared
  `background.service_worker` of `sw.js` is satisfied only by `sw.js`; `sub/sw.js` and any
  absolute or `..`-containing declaration are refused. A basename comparison was a bypass.
* **Traversal is race-resistant and fails closed.** Enumeration runs with an `onerror` collector
  (an unreadable directory is an error, not a shorter file list), symlinks and special files are
  rejected, a symlinked ancestor of the root is rejected, the listing is taken twice and
  compared, files are read through descriptors with the size re-checked, and file-count/size
  limits cap the work.

Adversarial test: `test_tampering_with_a_runtime_dependency_changes_the_digest` patches one line
of `node_modules/flow-sdk/index.js` and asserts the digest changes while the file count does
not — and that the file is present in the measured listing. At bridge level the same patch
prevents `VERIFIED`.

## 12. Source authorization vs release authenticity

A valid signature over a release says *who built it*. It does not say *we may distribute or
modify the source it was built from*. `aggregate_trust()` therefore requires
`source_gate_status == "AUTHORIZED"` before anything else, and the live guard requires it
again. `SOURCE_ABSENT`, `LICENSING_BLOCKED`, `LICENSE_EVIDENCE_PRESENT` and
`AUTHORIZATION_REVIEW_REQUIRED` all block `VERIFIED` — a licence file appearing in a directory
is never permission.

On this machine the gate reports `SOURCE_ABSENT`, so the real system cannot reach `VERIFIED`
even if every other condition were somehow satisfied.

## 13. Budget and idempotency

`SpendingControls.active` is true only when a `FlowBudgetLedger` is wired **and** carries a
positive cap **and** the enforcement mode is `flow_budget_ledger`. Defaults
(`FLOW_BUDGET_ENFORCEMENT=none`, `FLOW_GENERATION_BUDGET_USD=0`) report
`budget_controls_missing`; zero is never "unlimited".

The bridge builds one ledger from settings and hands the *same* object to the guard and to the
coordinator, so "a control exists" and "the control is enforced" cannot describe two different
things. The coordinator reserves immediately before building an envelope, releases if a
dispatch is rejected before it leaves, raises `BudgetUnavailable` when the cap cannot cover the
estimate, and reports `idempotency_enforced` from the key→request map it actually uses.

## 14. Required adversarial tests

`backend/tests/test_phase3c23_trust_pipeline.py` (33 tests) covers the fifteen required cases:

| # | Test | Proves |
| --- | --- | --- |
| 1 | `test_the_companion_measures_the_configured_roots_itself` | the production companion path calls `measure_components()` with the configured roots |
| 2 | `test_a_hello_digest_cannot_stand_in_for_the_disk` | a HELLO digest matching the release cannot hide modified files |
| 3 | `test_a_peer_supplied_report_is_not_a_measurement` | a `verification_report` frame is refused `4400`; the session is not trusted |
| 4 | `test_a_measurement_cannot_be_reused_by_another_session` / `…_extension_id_is_never_used_as_the_companion_identity` | challenge binding to session, companion and extension as three separate values |
| 5 | `test_a_manifest_required_mcp_server_cannot_be_omitted` / `test_configuration_cannot_downgrade_the_signed_requirement` | manifest floor; config can only add |
| 6 | `test_a_v1_manifest_cannot_establish_live_trust` | v1 is readable, never live |
| 7 | `test_an_extension_only_pin_cannot_establish_trust` | a pinned digest cannot describe a multi-component integration |
| 8 | `test_a_stale_previously_verified_measurement_fails_the_guard` | expiry is re-checked at use time; zero commands dispatched |
| 9 | `test_incomplete_metadata_is_refused` (6 cases) | no permissive fallbacks |
| 10 | `test_a_duplicate_component_name_is_refused` | duplicates rejected |
| 11 | `test_tampering_with_a_runtime_dependency_changes_the_digest` / `…_fails_the_bridge` | dependency coverage in the digest |
| 12 | `test_an_unauthorized_source_gate_blocks_verified` (4 states) | authenticity ≠ authorization |
| 13 | `test_a_budget_that_is_not_enforced_is_not_a_control` / `test_missing_budget_enforcement_blocks_a_verified_generation` | only a real ledger is a control |
| 14 | `test_founder_approval_cannot_substitute_for_trust` (5 defects × 3 approvals) | approval never bypasses a trust failure |
| 15 | `test_every_blocked_path_writes_nothing_to_the_transport` | every blocked path dispatches zero commands and reserves nothing |

Plus the settings-wiring tests: `test_settings_become_explicit_install_specs` and
`test_an_invalid_layout_is_refused_at_config_time`.

## 15. Status and audit surface

* `flow_status_payload()` phase → `3C.2.3`; new `measurement` block (roots configured, layouts,
  algorithm, freshness, required set, `peer_reported_measurement_accepted: false`, budget
  enforcement); `trust.required_components` is now the manifest-aware effective set.
* `trust_semantics` no longer claims that `provenance_trusted` is "all three of the claim
  checks"; it is session trust — measured, matched, still fresh.
* The trust registry still records only the most recent decision per session, plus a bounded
  audit trail (`measurement_started`, `measurement_failed`, `measurement_succeeded`,
  `measurement_expired`, `trust_revoked`). Nothing trusted is persisted across restarts.

## 16. Evidence

| Check | Result |
| --- | --- |
| Backend suite | **913 passed** in 501 s |
| 3C.2.3 adversarial suite | 33 passed |
| 3C.2.2 suite (realigned to the local path) | 67 passed |
| Acceptance harness | **65/65** (`docs/acceptance-run.json`, `2026-11-03T22:28:24Z`) |
| Migration replay | `c4d5e6f7a8b9` → `d5e6f7a8b9c0` → down → up (`/tmp/mig_c23.db`) |
| Frontend build | clean (`dist/assets/index-B7cdMNiO.js`, 402.08 kB) |
| Source audit | `SOURCE_ABSENT`, exit 3 (unchanged — no source exists) |

## 17. Remaining limits

These are properties of what was built, not open bugs to be hand-waved away:

* **Runtime identity is not proven.** Filesystem measurement proves what is on disk; it does not
  prove the browser loaded that directory, or that the running extension is that code. No
  browser interface is used, and none is claimed.
* **In-memory injection is invisible.** Code injected after load changes no file and no digest.
* **A compromised local machine defeats the trust root.** The companion is the trust root; an
  attacker who can write the install root or replace the companion can present any measurement.
* **The measurement is a snapshot.** Files can change after it completes; the freshness window
  bounds how long a snapshot is accepted, not what happens next.
* **Digest coverage is files, not behaviour.** A dependency that behaves differently on the same
  bytes (config, environment, remote code) is outside any tree digest.
* **Only configured roots are measured.** An MCP server launched from somewhere else is not
  covered; the operator has to say where it is.
* **No DeepX artifact exists.** Every positive result above comes from synthetic fixtures.

## 18. Prerequisites for a later canary (unchanged)

1. An authorized DeepX release artifact plus a signing key held outside this repository.
2. A v2 signed manifest naming the extension, MCP server and companion, each with a
   `sha256-tree-runtime-v1` digest (or an approved dependency manifest covering them).
3. Explicit install roots for every required component, with the companion running as the
   measuring process.
4. An `AUTHORIZED` source gate with U1–U7 actually resolved and evidence recorded.
5. A real Flow-generation budget configured with `flow_budget_ledger` enforcement.
6. Founder approval bound to the specific generation.

Until 1–5 exist, `FLOW_LIVE_GENERATION_ENABLED` stays `false`, `VERIFIED` is unreachable, and
Phase 3C.3 does not start.
