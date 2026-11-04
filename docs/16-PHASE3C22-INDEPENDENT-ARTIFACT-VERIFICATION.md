# Phase 3C.2.2 — Independent Installed-Artifact Verification

**Milestone:** Phase 3C.2.2 (baseline `7e9ae17`, which is an ancestor of this commit)
**Objective:** establish exactly what Crusor can prove about the code actually installed on
the user's machine, before any Flow generation can be trusted.

## Verdict

```
RELEASE ANCHOR:          MISSING      (no authorized release artifact or signing key exists)
INSTALLED EXTENSION:     NOT FOUND    (no extension installation is configured or present)
ARTIFACT DIGEST:         UNVERIFIED
SESSION BINDING:         NOT EXERCISED (no session can be established without the above)
MCP SERVER:              UNVERIFIED
LOCAL COMPANION:         UNVERIFIED
RUNTIME CORRELATION:     UNVERIFIED
OVERALL PROVENANCE:      UNVERIFIED
LIVE FLOW GENERATION:    DISABLED
PHASE 3C.3:              NO-GO
```

The verification machinery is implemented and proven against **synthetic** fixtures. A
synthetic package passing these tests is not evidence that DeepX is verified: no authorized
release artifact exists to measure, so the real-world verdict is unchanged and Phase 3C.3
remains blocked. U1–U7 are still unresolved upstream; no DeepX source was obtained,
fabricated or vendored; no Google account was connected and no paid call was made.

---

## 1. Threat model

What could make Crusor believe it is driving reviewed code when it is not?

| # | Threat | Mitigated by | Residual |
| --- | --- | --- | --- |
| T1 | The extension lies about its version/commit/digest in the HELLO | HELLO fields are *claims*; artifact integrity needs an independent anchor + measurement (§3, §7) | A lying HELLO is detected only if the measurement disagrees |
| T2 | A genuinely signed release exists, but the installed copy was patched | local measurement compared against the signed per-component digest (§4, §8) | Requires the browser to actually load the measured directory |
| T3 | The signed manifest itself is forged or re-signed by another key | Ed25519 verification against a public key from Crusor config, not from the package (§3) | Key custody on the operator's side |
| T4 | A *different* installation (another profile, another version) passes | extension identity from the path/manifest key, version from path **and** manifest, both compared to the release (§4) | — |
| T5 | Files change between measurement and use (TOCTOU) | `O_NOFOLLOW`, fd-based read, `fstat` before/after, `lstat` inode check, double directory listing (§5) | A writer can still change the files *after* the measurement completes |
| T6 | A stale measurement from an earlier session is replayed | single-use, short-lived, session/companion/extension-bound challenge; freshness window; future timestamps refused (§6) | Replay inside the same session before expiry is impossible (challenge is consumed on first use) |
| T7 | One component is verified and the others are assumed | per-component measurement and per-component trust state; required components must all be `VERIFIED` (§7) | A component with no measurement is `NOT_MEASURED`, never `VERIFIED` |
| T8 | A flag is flipped and an unverified build spends money | the live-generation guard requires the conjunction of eight facts; the coordinator refuses dispatch without a verified artifact (§9) | Human error in configuring an anchor is still possible |
| T9 | The local machine is compromised | **not mitigated.** A compromised companion reports whatever it likes | Documented, and the reason "runtime correlation" is evidence, not proof (§12) |
| T10 | In-memory code injection in the browser | **not mitigated.** Filesystem hashing cannot see it | Documented; blocks any claim of runtime integrity (§12) |

## 2. Release-anchor architecture

Two anchors remain, both configured by an operator, never supplied by the peer:

1. **Pinned digest** (`FLOW_TRUSTED_ARTIFACT_SHA256`, optional release/commit) — a single
   artifact digest for the extension.
2. **Signed release manifest** (`FLOW_RELEASE_MANIFEST_PATH`,
   `FLOW_RELEASE_MANIFEST_PUBLIC_KEY`) — an Ed25519-signed document describing the release
   and **each executable component**. This is the anchor a live integration requires,
   because a single blob digest cannot tell you whether the MCP server next to it is the
   reviewed one.

Properties enforced by `verify_release_manifest()`:

- the public key must come from Crusor configuration; a key shipped alongside the manifest is
  just another claim;
- the signature is verified over a canonicalised body (sorted keys, no whitespace, signature
  excluded) before any field is used;
- unsigned, malformed, unknown-schema, malformed-key and bad-signature manifests raise
  `ProvenanceRejected` — never a partial result;
- the manifest must state a SHA-256 for the extension, and it may not contradict itself
  between `extension.sha256` and the extension component entry;
- a legacy `crusor.flow.release.v1` manifest is still readable, but reports
  `identifies_components: false` and therefore cannot support a live integration.

## 3. Signed manifest schema (`crusor.flow.release.v2`)

```json
{
  "schema": "crusor.flow.release.v2",
  "product": "deepx-flow-connector",
  "release": {
    "version": "1.3.0",
    "commit": "0343884",
    "build_id": "build-2026-11-03",
    "tag": "v1.3.0",
    "released_at": "2026-11-01T00:00:00Z"
  },
  "extension": {
    "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "version": "1.3.0",
    "protocol": "flow.bridge.v1",
    "sha256": "<sha256-tree-v1 of the installed extension tree>"
  },
  "components": [
    {"name": "chrome_extension", "version": "1.3.0", "sha256": "<tree digest>",
     "extension_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
     "protocol": "flow.bridge.v1", "build_id": "build-2026-11-03", "required": true},
    {"name": "mcp_server", "version": "1.3.0", "sha256": "<tree digest>", "required": true},
    {"name": "local_companion", "version": "1.3.0", "sha256": "<tree digest>", "required": false}
  ],
  "signature": "<base64 Ed25519 over the canonical body>"
}
```

Validation rules: known schema; known component names only; every component carries a valid
SHA-256; extension digest agreed between `extension.sha256` and the component entry; the
required components are exactly those the deployment declares; release, commit, build id and
protocol are carried forward to the anchor and compared with the HELLO.

## 4. Installed-artifact discovery

`app/flow/installed_artifact.py`. Discovery is **explicit**: the verifier reads
`FLOW_EXTENSION_INSTALL_ROOT` and nothing else. It never searches a profile directory, never
picks "the first directory with a matching name", and rejects a root that is a symbolic link
or that resolves through one.

| Layout | Root shape | Identity source |
| --- | --- | --- |
| `versioned` (default) | `…/Extensions/<extension-id>/<version>/` | path segments, cross-checked against the manifest `key` when present |
| `directory` | any explicit directory (unpacked build) | manifest `key` → Chrome extension id |

Extension identity for an unpacked build is derived the way Chrome derives it: SHA-256 over
the DER public key in `manifest.key`, first 16 bytes, each nibble mapped to `a`–`p`. Without a
`key` the directory name would be the only identity available, and the verifier refuses that
rather than trusting a folder name.

Rejected, each with its own error code: missing installation, symlinked root, wrong extension
id, unexpected version (path ↔ manifest mismatch included), unsupported manifest version,
missing/invalid `manifest.json`, missing or symlinked service worker, traversal or absolute
paths declared by the manifest, symlinked files or directories inside the package, files that
are not regular files, more files than the policy allows, packages larger than the policy
allows, and — under `allow_extra_files=False` — files the manifest never declares.

## 5. Digest algorithm and TOCTOU protection

`sha256-tree-v1` is now implemented **once**, in `app/flow/authorization.py`
(`tree_digest_from_pairs`), and both producers call it: the licensing gate's
`package_tree_digest()` and the verifier's measurement. Same path normalisation (POSIX
relative), same sort, same exclusion set (`.git`, `__pycache__`, `node_modules`, `.venv`,
`.mypy_cache`), same length prefixes, same byte handling. Shared test vectors assert both
produce identical digests for the same tree, for a copy of that tree, and with excluded
directories present.

TOCTOU handling per file:

1. `os.open(path, O_RDONLY | O_NOFOLLOW)` — the final component may not be a symlink;
2. `fstat` before reading; reject non-regular files and files over the per-file limit;
3. read exactly `st_size` bytes from the descriptor, then confirm no extra byte exists;
4. `fstat` again; compare size, `st_mtime_ns`, inode and device;
5. `lstat` the path and confirm it still names the same inode and is not a symlink.

The directory is listed twice, and any difference fails the report. Failures are codes, not
partial digests: `file_mutated_during_measurement`, `file_replaced_during_measurement`,
`file_added_during_measurement`, `symlink_in_package`, `unsafe_relative_path`,
`path_outside_root`.

Every report carries a `measurement_id` (32 hex), start/finish timestamps and its limitations,
including the two that matter: *local filesystem only* and *no runtime correlation*.

## 6. Session challenge protocol

```
companion ── ws ──> bridge            : hello (pairing + provenance claims)
bridge  ── ws ──> companion           : hello_ack (measurement_required, components)
bridge  ── ws ──> companion           : verification_challenge {challenge_id, nonce,
                                        session_id, expires_at, required_components}
companion: measures locally, answers with verification_report {challenge_id, nonce,
                                        components:[{name, digest, extension_id, version,
                                        measurement_id, measured_at}]}
bridge: consumes the challenge, validates identity/version/digest/freshness,
        aggregates state, records it against the session
```

`ChallengeStore` rules: 32-byte URL-safe random nonce; TTL 120 s by default; single use
(consumed on first acceptance attempt, so a failed report also burns it); bound to session,
companion and extension id; nonce compared in constant time; a new connection always gets a
new challenge, and the measurement of one session can never be applied to another. The nonce
is never logged and never appears in a status payload.

`SessionAttestation.accept()` computes the six evidence fields and refuses a measurement that
predates the challenge or claims to come from the future (clock skew or a lying companion).

**What the challenge proves:** the companion measured files and reported them for *this*
session within a short window. **What it does not prove:** that the browser loaded them, or
that the companion told the truth on a compromised machine.

## 7. Component verification

| Component | Executable? | Measured how | Required by default |
| --- | --- | --- | --- |
| Chrome extension (incl. its service worker) | yes | tree measurement of the install directory | yes |
| MCP server | yes, when installed separately | its own tree measurement, own root | only when declared in `FLOW_REQUIRED_COMPONENTS` |
| Local companion | yes | its own tree measurement, own root | only when declared |

A component is `VERIFIED` only when its measured digest equals the signed digest for that
component. Otherwise it is `MISMATCH`, `NOT_MEASURED`, or `UNVERIFIED` (required but absent
from the release). Any required component that is not `VERIFIED` keeps the whole decision
below `VERIFIED` — the extension's HELLO never vouches for a sibling executable.

## 8. Trust-state aggregation

Nine states, computed in `aggregate_trust()` in this order:

`SOURCE_ABSENT` → `RELEASE_ANCHOR_MISSING` → (malformed claims / drift → `UNVERIFIED`) →
`INSTALLED_ARTIFACT_NOT_FOUND` → `ARTIFACT_DIGEST_MISMATCH` / `MEASUREMENT_STALE` /
`COMPONENT_UNVERIFIED` / `ARTIFACT_MEASUREMENT_FAILED` → `VERIFIED`.

`VERIFIED` requires all of: supported protocol, well-formed claims, consistent components, a
valid independent anchor, a measurement of the installed extension, a digest match, a fresh
challenge-bound measurement, runtime identity correlation (the extension's HELLO id and
version agree with the measured bytes), no unverified required component, and an authorized
upstream source gate. The state is a diagnosis, never a boolean: `/flow/status` reports the
state, the failing reasons and the evidence fields side by side.

## 9. Live-generation guard

`evaluate_live_generation_guard()` requires, in order: the live-generation flag, an
`AUTHORIZED` source/licence gate, a `VERIFIED` trust decision, a founder approval binding,
coordinator idempotency, and budget controls. A flag alone never passes. `FlowBridge` and
`GenerationCoordinator` both enforce it: the coordinator takes a `trust_provider` and refuses
to dispatch for anything other than `VERIFIED`, before building a command or touching a
transport. In this repository `FLOW_LIVE_GENERATION_ENABLED=false`, and the settings default
`flow_require_installed_measurement=false` — with no anchor configured, no measurement is
even requested, and the status block says `SOURCE_ABSENT`.

## 10. Status and audit

`/api/v1/flow/status` now carries a `trust` block: state, trusted, the nine supported states,
reasons, source-gate status, anchor (type, schema, product, signature verified, components
identified, release timestamp), expected identity (extension id, version, commit, build id,
protocol, artifact digest), measured identity (id, version, digest, measurement id, timestamp,
age, freshness), challenge validity, per-component states, unverified components, the six
evidence fields, recent trust events and the two limitations.

Never exposed: private keys, the manifest public key material, pairing secrets, companion
secrets, challenge nonces, Google bearer tokens, raw authorization headers, or filesystem
paths beyond the operator-configured root.

Audit events (`flow.trust.*`, in-memory ring buffer + structured log, no secrets):
`measurement_started`, `measurement_succeeded`, `digest_mismatch`,
`challenge_replayed`, `challenge_expired`, `measurement_expired`,
`extension_identity_mismatch`, `trust_revoked`.

## 11. Test results

| Suite | Result |
| --- | --- |
| Full backend suite | **881 passed** (813 → 881) |
| `test_phase3c22_installed_artifact.py` | **68 passed**, groups R/M/C/T/G/S/B |
| `test_phase3c21_trust_boundaries.py` | 36 passed (unchanged) |
| `test_phase3c2_flow_compatibility.py` | 65 passed (unchanged) |
| `test_phase3c1_flow_security.py` | 95 passed (label moved to 3C.2.2; boundaries unchanged) |
| Acceptance harness | **64/64** at `2026-11-03T20:54:50Z` (1–60 preserved, 61–63 new) |
| Source audit | exit `3`, `SOURCE_ABSENT` |
| Migration up → down → up | `c4d5e6f7a8b9 → d5e6f7a8b9c0` replayed (no schema change this phase) |
| Frontend `tsc -b && vite build` | clean |

New acceptance criteria: **61** — trust is `UNVERIFIED` with no measurement and no anchor;
**62** — a synthetic install is measured to the same digest the gate computes, a signed v2
manifest matches per component, a post-signing tamper is rejected, one patched file changes
the digest, and a symlink is refused; **63** — the flag alone never permits generation, a
`VERIFIED` fixture still cannot generate with the flag off, and the coordinator refuses
`flow_untrusted_artifact` with zero dispatches.

Test coverage by requirement: release verification (valid signature, tampered body, wrong
key, missing digest, unknown component, component without digest, self-contradiction, legacy
schema), measurement (match, modified worker, modified manifest, added executable, strict
policy, missing worker, wrong id, symlinked file/dir/root, traversal, unsupported layout,
unexpected version, unpacked key derivation, file added/mutated/replaced during measurement,
shared digest vectors), session binding (fresh, expired, replayed, wrong companion, wrong
extension, wrong session, stale, future, contradicting HELLO digest), trust states (all nine
paths), generation safety (guard blockers, approval cannot bypass, coordinator refusal), and
status/audit (no secrets, revocation events).

## 12. Platform and security limitations

| Claim | What is actually established |
| --- | --- |
| Signed release authenticity | Ed25519 signature verifies against a key Crusor holds. Says nothing about what is installed. |
| Installed files on disk | The files under the configured root, at measurement time, hash to this digest. Says nothing about which files the browser loads. |
| Browser-reported extension identity | Chrome's id derivation is reproduced from the manifest key; the *running* id is still what the HELLO claims. |
| Actual executing runtime code | **Not established.** No CDP/DevTools attachment, no browser attestation interface is used. |
| Compromised local machine | **Not mitigated.** Any process that can write these files can present any bytes. |
| In-memory code injection | **Not mitigated.** Filesystem hashing cannot see it. |
| Browser-profile compromise | **Not mitigated.** A profile that loads a different unpacked copy is not detected. |
| Companion-process compromise | **Not mitigated.** The challenge binds a report to a session; it does not make a liar honest. |

Therefore: this phase claims *installed-file integrity relative to a signed release*, and
explicitly **not** runtime integrity. Live generation remains blocked because runtime
correlation, MCP verification and an authorized source do not exist.

## 13. Remaining security risks

| Risk | Severity | Status |
| --- | --- | --- |
| U1–U7 upstream defects | critical | unresolved; no authorized source |
| No release artifact or public key exists to anchor anything | critical | `RELEASE_ANCHOR_MISSING` by design |
| Runtime correlation is HELLO-based | high | documented; cannot be closed without a browser attestation interface |
| Local-machine compromise defeats local measurement | high | documented; out of scope for a local verifier |
| Anchor pin can be configured wrongly (e.g. copied from the extension itself) | medium | documented; a pin must come from the release artifact |
| `flow_require_installed_measurement` defaults to false | medium | no session is trusted without an anchor anyway; live generation additionally requires the guard |
| Challenge TTL/freshness are time-based, not monotonic-clock-based | low | a suspended machine may need a fresh challenge; fails closed (stale → untrusted) |
| Nested `node_modules` excluded from digests | low | same policy as the gate; a package whose code lives there would be refused by the strict policy anyway |

## 14. Exact prerequisites for a later canary

Phase 3C.3 (or any canary) requires **all** of the following, none of which exists today:

1. An authorized upstream source package (see `docs/15` §9) with a human attestation bound to
   its tree digest.
2. A release artifact produced from that source, with one release line, a commit id, a build
   id and a `sha256-tree-v1` digest per component.
3. An Ed25519 public key distributed out of band and configured in Crusor — never alongside
   the package.
4. A signed `crusor.flow.release.v2` manifest naming extension, MCP server and companion
   digests, with `required` set for every component the deployment actually runs.
5. A real installed extension at a configured root whose measurement equals the manifest
   digest, plus measurements for each required sibling component.
6. A companion build whose challenge response carries those measurements, verified end to end
   over the real bridge.
7. An honest answer to the runtime-correlation question: either an accepted browser
   attestation mechanism, or an explicit written acceptance that runtime integrity is
   unverified while the flag is on.
8. All U1–U7 behavioural proofs from `docs/14` §15 and `docs/15` §9.
9. Green acceptance with trust state `VERIFIED` for a synthetic fixture **and** a documented
   manual run against the real artifact.
10. Founder sign-off recorded as an approval binding, with the kill switch tested.

Until then: **NO-GO**, `FLOW_LIVE_GENERATION_ENABLED=false`, and the trust state on every
deployment is `SOURCE_ABSENT` → `UNVERIFIED`.
