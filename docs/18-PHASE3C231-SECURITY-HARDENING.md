# Phase 3C.2.3.1 — Security Hardening and Trust-Boundary Corrections

**Milestone:** Phase 3C.2.3.1 (baseline `4b857880fbd256123c9305d1125e61075d2a8af6`, the Phase
3C.2.3 commit)
**Objective:** close the five defects found by the independent review of Phase 3C.2.3 without
changing the architecture, weakening any control, or starting Phase 3C.3.

## Verdict

```
RELEASE ANCHOR:          MISSING      (no authorized release artifact or signing key exists)
INSTALLED EXTENSION:     NOT FOUND    (no extension installation is configured or present)
LOCAL MEASUREMENT PATH:  HARDENED     (descriptor-relative, byte-revalidated, no path trust)
SIGNED ≠ IDENTIFIED:     FIXED        (a signed but incomplete manifest is not live-capable)
COMPONENT REQUIREMENTS:  STRICTLY ADDITIVE (no downgrade branch exists any more)
SOURCE AUTHORIZATION:    SETTINGS-ONLY (no `os.getenv` in the bridge or the gate)
BUDGET CONTROL:          REAL LEDGER OR BLOCKED (NaN/±Inf/zero/negative caps rejected)
OVERALL PROVENANCE:      UNVERIFIED
LIVE FLOW GENERATION:    DISABLED     (`FLOW_LIVE_GENERATION_ENABLED=false`)
PHASE 3C.3:              NO-GO
U1–U7:                   UNRESOLVED
```

Nothing in this phase changes the real-world verdict. There is no authorized DeepX release, no
installed extension, no Google account and no licence; every fixture below is **synthetic** —
temporary trees, self-generated Ed25519 keys, in-process settings objects. A synthetic release
passing these tests is evidence about the machinery, never about DeepX.

---

## 1. Finding 1 — the budget reservation had no lifecycle

### What was wrong

| Defect | Consequence |
| --- | --- |
| `FlowBudgetLedger.reserve()` mutated `spent_usd`/`reservations` without a lock and `release()` did not exist | concurrent submissions could overspend the cap; a dispatch that failed left a permanent charge |
| `FlowBudgetLedger(cap_usd=0.0)` and `cap_usd=float("inf")` were accepted | `SpendingControls.active` reported an active control that could never refuse anything, and "0" read as "unlimited" |
| the coordinator reserved *before* the replay/idempotency/existing-job checks | a replay, a rejected duplicate or a rejected second job each consumed budget without an attempt |
| nothing refunded a send failure | a socket that closed before the frame left was billed (the 3C.2.3.1 fix over-corrected; 3C.2.3.2 classifies delivery instead) |

### What it does now

`app/services/flow_budget.py`

* `validate_cap(cap)` rejects `NaN`, `±Inf`, `0` and negatives with `InvalidBudgetCap`; a
  non-numeric cap is rejected too. `FlowBudgetLedger` calls it in `__init__`.
* `flow_ledger_from_settings()` returns `None` — no ledger, therefore no enforceable control and
  a `budget_controls_missing` blocker — for any unusable cap. It never returns a ledger that
  cannot refuse.
* `reserve()` is atomic under the ledger's `threading.Lock`: it validates the amount, checks the
  cap with the lock held, and only then increments `spent_usd`/`reservations`. Two threads
  reserving against a cap that can only cover one cannot both succeed.
* `release()` also runs under the lock, refuses to refund an amount that was never reserved or to
  drive `spent_usd` below zero, and decrements `reservations`.

`app/flow/coordinator.py`

* `self._reservations: dict[str, float]` records the exact amount reserved for each job.
* `submit()` performs, **in this order**: live-flag check → trust check → approval check →
  replay lookup → existing-job rejection → `reserve()` → envelope build → dispatch. Nothing
  is reserved until the attempt is genuinely new.
* `_release_reservation(job_id)` refunds exactly once (the map entry is popped first, so a
  double release cannot double-refund) and `_settle_reservation(job_id)` drops the tracking and
  *keeps* the spend.
* Refund happens only when dispatch is **provably not delivered**: the provider returned a
  result carrying `error.dispatched is False`, or the transport classified its own failure as
  definite non-delivery. After a confirmed submit, after an ambiguous result
  (`UNKNOWN_SUBMISSION`) or after any terminal success, the reservation becomes spend and is
  never refunded — a replay is never refunded either, because a replay never reserved.

  > **Corrected in Phase 3C.2.3.2.** This milestone originally treated *any* exception from
  > `transport.send()` as proof the frame never left. That is unsound once
  > `websocket.send()` has been entered, where an exception can follow a partial write or a
  > lost acknowledgement. Delivery is now classified explicitly
  > (`DEFINITELY_NOT_SENT` / `MAY_HAVE_BEEN_SENT` / `SENT`); only the first releases budget.
  > See `docs/19-PHASE3C232-RETRY-DELIVERY-FILESYSTEM-CLOSURE.md` §2.
* The reservation is tracked through the ambiguity window: `_mark_unknown()` and the
  `UNKNOWN_SUBMISSION` branch settle, the `dispatched: false` branch releases *before*
  settling, and `snapshot()` now reports `budget` alongside the job list so an operator can see
  the same numbers the guard sees.

Test coverage: `tests/test_phase3c231_security_hardening.py` §1 — unusable caps, replay,
existing job, send failure, ambiguous result, confirmed dispatch, provider-reported
non-dispatch, pre-dispatch exception, concurrent reservations, and the guard/coordinator sharing
one cap value.

---

## 2. Finding 2 — traversal trusted a path once it resolved inside the root

### What was wrong

`measure()` resolved each declared path (`Path.resolve()`), checked it was inside the root, and
then opened it **by name**. Between the resolution and the open, a component of the path could
be replaced by a symlink or a different directory, and the measurement would happily hash
whatever the name pointed at afterwards. Bounding checks (count/size/total) were applied to the
resolved tree; nothing checked that the tree being enumerated was the tree being hashed.

### What it does now

`app/flow/installed_artifact.py` walks by **descriptor**, never by name:

* `DIR_FD_SUPPORTED` requires `O_DIRECTORY`, `open`/`stat` in `os.supports_dir_fd`, `stat` in
  `os.supports_follow_symlinks`, and `scandir` in `os.supports_fd`. When it is false the
  measurement **fails closed** with `unsupported_platform`; there is no path-based fallback and
  no silent weaker mode. (This platform supports all of it; the probe that established it was
  run before the code was written rather than assumed.)
* The root is opened once with `O_RDONLY|O_DIRECTORY|O_NOFOLLOW`; every child directory is
  opened with `openat` and its `fstat` identity compared against `stat(name, dir_fd, …,
  follow_symlinks=False)`. A directory that is a symlink is refused (`symlink_in_package`), and
  a directory whose name and descriptor disagree is refused
  (`directory_replaced_during_measurement`).
* Files are opened `O_NOFOLLOW` relative to the directory descriptor; `ELOOP` is
  `symlink_in_package`. The read is capped at the policy size, and one extra byte past `st_size`
  means `file_mutated_during_measurement`. Pre- and post-read `fstat` must agree, and the
  descriptor's identity must equal the identity recorded from the directory enumeration
  (`file_replaced_during_measurement`).
* After a directory has been read, every file it contributed is re-checked: identity, size,
  mtime, ctime — and then **re-read and byte-compared** through a fresh descriptor. Timestamps
  are not evidence: this filesystem hands a freed inode back with an unchanged size *and* an
  unchanged mtime, so a same-size rewrite can only be caught by comparing bytes. The second
  read is what makes the "same inode, new content" case detectable.
* The directory is then re-listed (`scandir` on the same descriptor, sorted, because `scandir`
  iteration order is arbitrary) and compared with the first listing: any change is
  `file_added_during_measurement`.
* Finally `_assert_root_still_the_same()` compares the root descriptor's identity with the
  root's `lstat`: `root_replaced_during_measurement`.
* Enumeration failures surface as `enumeration_failed`; a path that escapes the root
  (`..`, absolute names, empty components) as `unsafe_relative_path`. The service worker is
  resolved to a **normalized relative name** and compared against names actually scanned, so a
  file with the same basename in another directory no longer satisfies the policy.
* Count/size/total bounds (`too_many_files`, `file_too_large`, `package_too_large`) are
  unchanged and still enforced during the walk.

### The guarantee, stated honestly

The measurement is **not** an atomic snapshot of the filesystem, and `report.limitations`
contains `no_atomic_snapshot` to say so. It is a *stability-checked* read: each file is read
through a no-follow descriptor relative to a verified directory, and its bytes are re-read and
compared before the directory is considered finished. Adversarial concurrent mutation cannot
produce a digest of files the verifier never saw; it can still, in principle, interleave writes
between the second read and the digest computation of a *later* directory. `report.checks`
records `descriptor_relative_traversal` so the caller can see the mode that actually ran.

---

## 3. Finding 3 — "validly signed" was treated as "fully identified"

### What was wrong

`TrustAnchor.live_capable` required the v2 schema, a verified signature, per-component digests
for the manifest's required components, and a well-formed extension digest. A manifest that
declared only the extension digest — no version, no source commit, no build id, no protocol, no
digest algorithm — was therefore live-capable: `VERIFIED` could be reached on the strength of a
signature alone, and measured identity/version were compared against empty expectations.

### What it does now

`app/flow/provenance.py`

* A live-capable anchor requires **all** of: `schema == crusor.flow.release.v2`; a verified
  Ed25519 signature against the out-of-band key; `SEMVER` release version; `SOURCE_COMMIT`
  source commit; `BUILD_ID` build identity (≥6 chars); a well-formed Chrome extension id
  (`CHROME_EXTENSION_ID`, `^[a-p]{32}$`); a protocol in `proto.SUPPORTED_PROTOCOLS`; a
  **declared** digest algorithm in `RUNTIME_DIGEST_ALGORITHMS`; a well-formed extension digest;
  and a well-formed digest for every component the release marks required.
* A declaration that contradicts the release's declared algorithm is refused while parsing
  (`ProvenanceRejected`), as is an algorithm this build does not implement.
* v1 manifests and pinned single digests remain **readable** (an operator can inspect what they
  describe) and are never live-capable.
* No identity is ever backfilled from the HELLO: `InstallationSpec` identity now comes from the
  anchor only, and `accept()` compares the measured extension id/version against the HELLO **and**
  the anchor's expectations. An anchor that cannot state them is not live-capable, so there is
  nothing to compare against and no fallback path.

Test coverage: `tests/test_phase3c231_security_hardening.py` §3 — thirteen "signed but
incomplete" mutations, each a valid signature over a defect, plus the refused-outright cases
(malformed digest, missing digest, unsupported algorithm, undeclared algorithm, duplicate and
unknown components, contradictory extension digest) and the "perfect measurement evidence
cannot reach `VERIFIED` with an incomplete anchor" case.

---

## 4. Finding 4 — `FLOW_REQUIRED_COMPONENTS` was documented additively, enforced as a replacement

### What was wrong

`effective_required_components()` computed the documented union, then `accept()` compared the
configured list against the manifest's and appended a `required_component_downgrade` failure
when the configured list omitted a manifest requirement. The documented use — "add
`local_companion` to a release that requires `mcp_server`" — was rejected as a downgrade, and
the only way to satisfy the check was to repeat the manifest's names.

### What it does now

`app/flow/trust.py`

* The downgrade branch is **deleted** (the variable and the `failures.append` line together —
  removing one alone leaves a dangling name, which is how the earlier mistake survived a
  review).
* The requirement is `{chrome_extension} ∪ manifest.required ∪ configured_additions`, computed
  once in `effective_required_components()`.
* Unknown names in the configured list are rejected at config time (`BridgeConfigError`) and
  again in `accept()` (`unknown_component_addition`).
* An addition the signed release does not describe has no expected digest, so it could never
  verify; `accept()` refuses the submission with `component_addition_not_in_release` instead of
  accepting it and recording a permanently unverifiable requirement.
* A required root that is not configured fails the measurement (`component_not_configured`), so
  an addition can never be silently unmeasured.
* Replacement semantics would need a separate setting; none exists, deliberately.

Test coverage: §4 — additive union with each addition, unknown addition refused at both layers,
addition absent from the release refused, no configuration can shrink the manifest set, addition
present in the release verifies, addition without a root fails the measurement.

---

## 5. Finding 5 — the bridge read the source gate from the environment

### What was wrong

The bridge read `FLOW_SOURCE_PACKAGE_PATH` / `FLOW_AUTHORIZATION_ATTESTATION_PATH` with
`os.getenv`, while the rest of the application reads Pydantic settings. A process started with a
stale or injected environment could authorize an upstream source that the configured settings
did not name, and the reported status could disagree with what the application believed.

### What it does now

* `FlowBridge._source_gate_status()` reads both paths from the **injected** Pydantic settings
  object and passes them **explicitly** to `evaluate_source_gate(package_path=…,
  attestation_path=…)`. When the settings object is unavailable it returns
  `LICENSING_BLOCKED` — fail closed, never "try the environment".
* `app/flow/source_gate.py` has no `os.getenv` fallback: an omitted path is an empty path, which
  is `SOURCE_ABSENT`, not "look elsewhere".
* The same gate result feeds the status endpoint and the trust aggregation, so the two can no
  longer disagree; `app/flow/companion.py` constructs the bridge with the shared settings object.

Test coverage: §5 — settings paths reach the gate with the environment cleared, the environment
cannot override the configured path, missing settings fail closed, the configured attestation
path is the one evaluated, and the real Pydantic settings class carries the fields the bridge
reads.

---

## 6. Cross-component regression

`tests/test_phase3c231_security_hardening.py` §6 re-asserts the interactions that a fix in one
component could break: a blocked trust state writes nothing to the transport and reserves
nothing; an approval binding cannot bypass a missing budget; a blocked state cannot be bypassed
by an approval; a stale decision refuses a generation with zero commands dispatched; the guard
and the coordinator report the same ledger; the coordinator still reports real idempotency
enforcement; and the bridge's budget snapshot matches the guard's view.

---

## 7. Evidence

All commands were run in this workspace against the committed tree plus these changes.

| Check | Command | Result |
| --- | --- | --- |
| Backend suite | `../.venv/bin/python -m pytest tests -q -p no:warnings` | **985 passed** (913 before + 72 new), 0 failed, 0 errors |
| New module | `pytest tests/test_phase3c231_security_hardening.py` | 72 passed |
| Trust pipeline | `pytest tests/test_phase3c23_trust_pipeline.py` | 33 passed |
| Installed artifact | `pytest tests/test_phase3c22_installed_artifact.py` | 67 passed |
| Acceptance | `../.venv/bin/python ../scripts/acceptance.py --base-url http://127.0.0.1:8000` | **65/65** (`docs/acceptance-run.json`, `2026-11-04T03:11:37Z`) |
| Migration | `alembic upgrade head` / `downgrade -1` / `upgrade head` | `c4d5e6f7a8b9 → d5e6f7a8b9c0` → back → forward |
| Frontend | `npx tsc --noEmit` + `npm run build` | TypeScript clean; `dist/assets/index-B7cdMNiO.js` 402.08 kB (gzip 110.07 kB), `index-COHJWXOi.css` 33.24 kB |
| Source audit | `../.venv/bin/python ../scripts/flow_source_audit.py` | `SOURCE_ABSENT`, exit 3 |

The exact totals and timestamps are recorded in `docs/acceptance-run.json`; the commit
containing this document records the SHA and the test counts in its message.

---

## 8. What did not change

* The nine trust states, the guard blockers, the digest format
  (`len(path)[4B BE] ‖ path ‖ len(data)[8B BE] ‖ data`, sorted POSIX relative path), the
  `sha256-tree-runtime-v1` definition, and the settings defaults (all fail closed).
* `FLOW_LIVE_GENERATION_ENABLED` stays `false`; no live generation path exists; no Google
  account, Flow tab, DeepX source or paid provider call was involved at any point.
* No approval, budget, pairing, provenance, licensing or trust control was weakened. Every
  change either adds a refusal or replaces an implicit trust with an explicit check.

## 9. Next step

Phase 3C.2.3.1 is the last milestone authorized in this conversation. Phase 3C.3 remains
**NO-GO** until U1–U7 are resolved and an authorized release artifact, an installed extension and
a licensed upstream source exist. It is not started automatically.
