# Phase 3C.2.1 — Provenance, Licensing & Asset-Export Trust Corrections

**Milestone:** Phase 3C.2.1 (follows Phase 3C.2, baseline `bff49c1`)
**Scope:** three trust-boundary weaknesses found while reviewing Phase 3C.2.
**Verdict on the upstream integration:** still **NO-GO**. U1–U7 remain unresolved and
unattempted; the DeepX source package is still absent and unlicensed. Nothing in this phase
touches upstream code, enables live generation, or claims an end-to-end integration.

What *did* change is that three of Crusor's own checks no longer say "trusted" when they
have only established something weaker. Each was a false claim in a different form:

| # | Defect | The false claim it produced |
| --- | --- | --- |
| 1 | A file named `LICENSE`, containing anything at all, set the source gate to `AUTHORIZED` | "we may modify and redistribute this" from a filename |
| 2 | `verify_provenance()` returned `trusted` for self-reported version strings and a source commit | "these bytes are the release we support" from a HELLO the peer wrote about itself |
| 3 | The chunk route checked token↔URL ticket identity *after* `accept_chunk()` had already mutated the token's own ticket | "this token can only touch this ticket" while a token for B could mutate A |

Live Flow generation remains disabled. All Phase 3C.1 and 3C.2 acceptance criteria still
pass (61/61), and no existing security boundary was relaxed to make the new tests pass.

---

## 1. Audit method

Each file named in the review brief was read in full, then the *behaviour* was exercised
rather than the text searched:

- `app/flow/source_gate.py`, `app/flow/provenance.py` — drove the gates with synthetic
  packages and HELLOs to see which inputs moved them.
- `app/flow/bridge.py` — connected over a real WebSocket and read the actual close codes and
  `hello_ack` payloads.
- `app/services/flow_gateway.py`, `app/api/v1/flow.py` — posted real chunk bytes against
  mismatched ticket/URL pairs and inspected database state and storage afterwards.
- `tests/test_phase3c2_flow_compatibility.py` — checked which of its assertions would have
  failed if the defect were removed (none would: the tests encoded the defective behaviour).
- `docs/14-PHASE3C2-DEEPX-REMEDIATION.md` — checked for claims the code could not support.

The Phase 3C.2 tests that asserted the wrong things were rewritten rather than deleted, so
the regression is now pinned in the direction of the fix.

## 2. Defect 1 — licence presence was treated as permission

**Before.** `evaluate_source_gate()` set `AUTHORIZED` whenever a file from
`LICENSE_EVIDENCE` existed and was non-empty. `LICENSE` containing `All rights reserved.
Proprietary.` authorized modification. An empty `LICENSE` did not, but `MIT` (three bytes)
did. The gate was a filename check wearing the costume of a legal determination.

**After.** Five states, and only the last one authorizes:

| State | Means | `authorized_to_modify` |
| --- | --- | --- |
| `SOURCE_ABSENT` | no package at the configured path | `false` |
| `LICENSING_BLOCKED` | package present, no substantive licence/permission text (empty and whitespace-only files do not count) | `false` |
| `LICENSE_EVIDENCE_PRESENT` | substantive licence-shaped text exists; no human attestation | `false` |
| `AUTHORIZATION_REVIEW_REQUIRED` | an attestation exists but is incomplete, inconsistent, stored inside the package, or bound to different bytes | `false` |
| `AUTHORIZED` | complete attestation, named reviewer, digest equal to the package actually present | `true` |

Evidence and authorization are now **separate artefacts**. Evidence lives inside the
package and is only *described* (name, size, SHA-256). Authorization lives in a JSON
attestation **outside** the package (`FLOW_AUTHORIZATION_ATTESTATION_PATH`), and an
attestation found inside the package it describes is rejected
(`attestation_inside_package`) — the package cannot certify itself.

Attestation contents (`app/flow/authorization.py`, schema `crusor.flow.authorization.v1`):

```json
{
  "schema": "crusor.flow.authorization.v1",
  "upstream_project_url": "https://github.com/<owner>/<repo>",
  "release": {"tag": "v1.2.1", "commit": "<7-40 hex>"},
  "package": {"sha256": "<tree digest the reviewer reviewed>"},
  "licence": {"name": "MIT", "evidence_reference": "LICENSE", "evidence_sha256": "<64 hex>"},
  "rights": {"modification_permitted": true, "redistribution_permitted": true},
  "review": {"reviewer": "Name <email>", "reviewed_at": "2026-11-03T18:00:00Z",
             "evidence_reference": "legal-ticket-1234"}
}
```

Mechanical checks (each produces a named problem, never a partial pass): schema, project
URL, release identity, package digest format, licence name, evidence reference, evidence
digest, `rights` as real JSON booleans (`"true"` as a string is treated as *not asserted*),
reviewer ≥ 3 characters, ISO-8601 timestamp **with** timezone, review evidence reference,
evidence file present in the package and matching its recorded digest, and package digest
equal to the observed tree digest.

`package_tree_digest()` is `sha256-tree-v1`: sorted POSIX relative paths with length-prefixed
paths and payloads, symlinks skipped, VCS/cache directories excluded. Changing one byte of
one file changes it, so an attestation cannot survive a package edit.

**Evidence (synthetic package, no DeepX bytes):**

| Scenario | Status | Authorized |
| --- | --- | --- |
| no licence | `LICENSING_BLOCKED` | no |
| `LICENSE` containing only spaces | `LICENSING_BLOCKED` | no |
| `LICENSE` = `All rights reserved. Proprietary.` | `LICENSE_EVIDENCE_PRESENT` | no |
| `LICENSE` = MIT text, no attestation | `LICENSE_EVIDENCE_PRESENT` | no |
| reviewed attestation bound to the digest | `AUTHORIZED` | yes |
| same attestation, one byte changed in `service-worker.js` | `AUTHORIZATION_REVIEW_REQUIRED` | no |
| attestation with a different package digest | `AUTHORIZATION_REVIEW_REQUIRED` | no |
| attestation stored inside the package | `AUTHORIZATION_REVIEW_REQUIRED` | no |

The scanner follows the same ordering: exit `3` when nothing readable, exit `2` when scanned
but not authorized, exit `1` when authorized and findings exist, exit `0` when authorized and
clean. A scan finding is evidence for a reviewer; it never implies permission to edit.

## 3. Defect 2 — syntactic well-formedness was reported as trust

**Before.** `ProvenanceReport.trusted` was `claims_well_formed and components_consistent`.
Every input to that conjunction came from the HELLO — the party being checked. A HELLO could
be perfectly consistent, entirely fabricated, and still be reported `trusted: true`.
`docs/14` amplified this by describing the check as "provenance verified".

**After.** Four independent facts, reported separately in `hello_ack`, `/flow/status` and the
bridge snapshot:

| Fact | Question it answers | Who can satisfy it |
| --- | --- | --- |
| `claims_well_formed` | does the HELLO parse, with semver components, a commit and a build id? | the extension (necessary only) |
| `components_consistent` | does the package agree with itself? (extension == service worker; MCP on the same release line unless drift is explicitly accepted) | the extension |
| `artifact_integrity_verified` | do the installed bytes match a digest **Crusor** already holds? | only an independent anchor |
| `provenance_trusted` | all three | conjunction — never fewer |

Two anchors are supported, both configured by an operator and neither supplied by the peer:

1. **Pinned digest** — `FLOW_TRUSTED_ARTIFACT_SHA256` (plus optional
   `FLOW_TRUSTED_RELEASE`, `FLOW_TRUSTED_SOURCE_COMMIT`), compared against the digest in the
   HELLO.
2. **Ed25519-signed release manifest** — `FLOW_RELEASE_MANIFEST_PATH` +
   `FLOW_RELEASE_MANIFEST_PUBLIC_KEY`; the signature is verified over the canonicalised
   manifest body before its artifact digest is used. Unsigned manifests, malformed
   manifests, unknown schemas, malformed keys and invalid signatures all raise
   `ProvenanceRejected` rather than degrading to "trusted enough".

Behaviour that follows from this:

- **No anchor → `UNVERIFIED`.** The report says so, with an
  `artifact_integrity_unverified` warning. The session is still admitted (the handshake
  authentication is unchanged) but is never trusted, and `live_generation_enabled=true`
  without an anchor is refused at configuration time — an unverified build must not be able
  to spend money.
- **Anchor present but unprovable → refusal (`4422`).** Missing digest, mismatched digest,
  or a release/commit that differs from the pinned one closes the socket.
- **Wrong release identity → refusal.** A connecting extension that is not the exact release
  this bridge supports is rejected before any command.
- **Drift is never trust.** With `FLOW_ALLOW_COMPONENT_DRIFT=true` a drifted package may
  connect, but the report stays untrusted; the flag cannot bypass artifact verification, and
  it is `false` by default.
- **Protocol compatibility is a separate question**, answered by `SUPPORTED_PROTOCOLS` and
  closed `4426`, before any provenance check runs.

The `hello_ack` now emits `claims_well_formed`, `components_consistent`,
`artifact_integrity_verified`, `provenance_trusted`, `provenance_state` and `trust_anchor`
as distinct fields. An earlier draft of this fix reused the conjunction for the narrower
name `artifact_integrity_verified`; that is the same defect in miniature and was corrected
during review of this phase.

## 4. Defect 3 — export ticket binding was enforced after mutation

**Before**, in the route:

```python
await service.get_ticket(ticket_id)          # token must belong to *this* ticket
ticket, _asset = await service.accept_chunk(token=..., ...)
if ticket.id != ticket_id:                   # too late
    raise ValidationProblem(...)
```

A token for ticket **B** posted to ticket **A**'s URL mutated **B** — chunks written, staging
objects created, `chunks_received` incremented, audit rows emitted — and only then returned
an error. The URL was decoration, not authorization.

**After**, identity is resolved and compared inside the service boundary, in
`FlowAssetExportService._usable_ticket()`, before any mutation:

```python
ticket = await self._ticket_for_token(token)          # resolve token -> its real ticket
if expected_ticket_id is not None and str(ticket.id) != str(expected_ticket_id):
    raise ValidationProblem("This export token does not belong to that ticket.")
now = _now()
if ticket.status != "pending": ...                    # only now does state get touched
```

`expected_ticket_id` is threaded through both `accept_chunk()` and `upload_bytes()`, so the
single-shot path is bound the same way. The route no longer contains a post-mutation
comparison at all. The rejection is deliberately identical to a token that does not exist,
so the response cannot be used as an oracle for which ticket a token belongs to.

Guarantees asserted by tests and by acceptance criterion 60:

- no staging object is written;
- no asset row is created;
- neither ticket changes (`status`, `chunks_received`, `received_bytes`, `storage_key`);
- the session can be rolled back safely (nothing was flushed that the caller must keep);
- the correct token/ticket pairing still uploads in order and produces a verified asset.

## 5. Regression tests

New file `backend/tests/test_phase3c21_trust_boundaries.py` — **36 tests**, groups L
(licensing), P (provenance), X (export):

**Licensing** — arbitrary `LICENSE` does not authorize; empty/whitespace/proprietary/
incomplete evidence stays blocked; an authorization document inside the package is not an
attestation; a reviewed attestation is bound to the exact package digest; changing one file
invalidates it; every incomplete-attestation mutation (missing reviewer, missing rights,
string-`true` rights, missing timestamp, digest mismatch, wrong schema) never authorizes;
the recorded identity matches the package.

**Provenance** — a self-reported commit does not establish trust; a self-reported package
hash does not establish trust; a missing digest refuses `trusted` even with an anchor; a
digest mismatch is untrusted; a pinned digest and release verify; a release other than the
supported one is refused; component drift stays untrusted even when the operator allows it
and cannot bypass artifact verification; a verified release manifest produces trust; an
invalid signature is rejected; unsigned/malformed manifests are rejected; live generation
cannot be enabled without an anchor.

**Export** — a token for another ticket cannot mutate this one; mismatched identity produces
no database mutation; no staging object is written; no asset is created; matching identity
still uploads in order; a foreign companion still cannot use a ticket.

Existing suites were updated in the direction of the fix, not around it: the Phase 3C.2
`test_a_consistent_package_is_trusted` became
`test_a_consistent_package_is_well_formed_but_not_trusted`, the bridge fixture gained a
pinned digest, and the scanner test now expects exit `2` and `authorized: false` for a
licensed-but-unattested package.

## 6. What was deliberately not changed

- Handshake authentication: pairing id/secret, constant-time comparison, `4401`, Host and
  Origin validation, size and timeout limits, loopback-only bind — all untouched.
- Command surface: still exactly `status`, `generate_image`, `generate_video`,
  `check_video_status`, `export_asset`; the eight dangerous MCP tools remain unavailable to
  Crusor.
- `FLOW_LIVE_GENERATION_ENABLED=false`; `FLOW_ALLOW_COMPONENT_DRIFT=false`.
- The founder approval binding, the coordinator lifecycle, and the one-attempt dispatch rule.
- No new permission, host or domain was added anywhere.
- The status payload's `phase` label moved from `3C.1` to `3C.2.1`; every boundary it
  reports is unchanged, and the Phase 3C.1 test asserts the boundaries as well as the label.

## 7. Acceptance and verification evidence

Run `2026-11-03T20:01:34Z` against the live API on `:8000`, migration head `d5e6f7a8b9c0`.

| Check | Result |
| --- | --- |
| Full backend suite | **813 passed** (776 → 813) |
| `test_phase3c1_flow_security.py` | 95 passed |
| `test_phase3c2_flow_compatibility.py` | 65 passed |
| `test_phase3c21_trust_boundaries.py` | 36 passed |
| Acceptance harness | **61/61** (54–58 preserved, 59–60 new) |
| Source audit, default | exit `3`, `SOURCE_ABSENT` |
| Source audit, licensed synthetic package | exit `2`, `LICENSE_EVIDENCE_PRESENT`, `authorized: false`, 6 findings across 14 scans |
| Source audit, attested synthetic package | exit `1`, `AUTHORIZED`, 6 findings — permission to edit does not make the defects disappear |
| Same attestation, one byte changed in the package | `AUTHORIZATION_REVIEW_REQUIRED` |
| Migration up → down → up on a scratch DB | `c4d5e6f7a8b9 → d5e6f7a8b9c0` replayed cleanly (no schema change this phase) |
| Frontend `tsc -b && vite build` | clean |

New acceptance criteria:

- **59** — no licence and an empty licence are blocked; arbitrary proprietary text is
  evidence only; a reviewed attestation bound to the tree digest authorizes; a digest
  mismatch and a one-byte package change both invalidate it.
- **60** — a token for ticket B posted to ticket A's URL is refused `422` with no mutation
  on either ticket and no asset; the correct pairing still consumes and verifies the asset.

## 8. Unresolved risks

| Risk | Severity | Status |
| --- | --- | --- |
| U1–U7 upstream defects | **critical** | unresolved; source absent and unlicensed |
| No authorized upstream package | **critical** | gate at `SOURCE_ABSENT`; nothing vendored |
| `flow_trusted_artifact_sha256` is a manual operator value — a wrong pin silently denies or, if copied from the extension's own claim, would be circular | medium | documented; the pin must come from the release artifact, never from a HELLO |
| Trust anchors are configured per-deployment; a deployment with no anchor admits UNVERIFIED sessions | medium | such sessions cannot generate (config refuses live generation without an anchor) and are reported as UNVERIFIED everywhere |
| `FLOW_ALLOW_COMPONENT_DRIFT=true` still admits a drifted package | medium | never trusted, off by default |
| An attestation is only as good as the human who wrote it | medium | the gate records reviewer, timestamp and evidence reference; it cannot audit the reviewer |
| Extension-side export protocol still unimplemented upstream | high | Crusor's receiver contract is tested against synthetic bytes and a real HTTP client, nothing more |
| No browser or Google-account test is possible | high | none is claimed |

## 9. Requirements for Phase 3C.3 (unchanged, restated)

Phase 3C.3 still requires, before any upstream work:

1. the seven required package files at `FLOW_SOURCE_PACKAGE_PATH`;
2. licence evidence granting modification **and** redistribution, plus a human-reviewed
   attestation **outside** the package whose `package.sha256` equals the observed tree digest;
3. one release line across manifest/README/service worker/MCP, with a commit id, a build id
   and an artifact digest published out of band (or an Ed25519-signed release manifest whose
   public key Crusor already holds);
4. behavioural proof for U1–U7 as listed in `docs/14` §15 — text scans are not proof;
5. the audit scanner at exit `0` or `1` on the authorized package, and a full green suite;
6. live generation enabled only against a pinned artifact anchor, with the founder approval
   binding intact.

Until then the integration verdict remains **NO-GO**, with the source gate reporting
`SOURCE_ABSENT` on every acceptance run.
