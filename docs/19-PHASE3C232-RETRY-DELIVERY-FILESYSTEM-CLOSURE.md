# Phase 3C.2.3.2 — Retry, Delivery and Filesystem Security Closure

**Milestone:** Phase 3C.2.3.2 (baseline `33c3e6495f2d569d626d04cbbe098f687cb7b0c5`, Phase 3C.2.3.1)
**Objective:** close three defects found in the independent review of Phase 3C.2.3.1 without adding
product features, weakening any control, or starting Phase 3C.3.

## Verdict

```
RELEASE ANCHOR:          MISSING      (no authorized release artifact or signing key exists)
INSTALLED EXTENSION:     NOT FOUND    (no extension installation is configured or present)
RETRY PATH:              BUDGETED & TRUST-CHECKED (was neither)
DELIVERY CLASSIFICATION: EXPLICIT     (definite / ambiguous / sent; generic errors are ambiguous)
FILESYSTEM TRAVERSAL:    DESCRIPTOR-WALKED ROOT + NESTED IDENTITY REVALIDATION
OVERALL PROVENANCE:      UNVERIFIED
LIVE FLOW GENERATION:    DISABLED     (`FLOW_LIVE_GENERATION_ENABLED=false`)
U1–U7:                   UNRESOLVED
PHASE 3C.3:              NO-GO — NOT STARTED
```

Nothing here changes the real-world verdict: there is no authorized release, no installed
extension and no licence. Every fixture is synthetic — temporary trees, self-generated Ed25519
keys, in-memory transports.

---

## 1. Defect A — retry bypassed the budget and the trust check

### What was wrong

`GenerationCoordinator.retry()` was a second, weaker submit path:

| Defect | Consequence |
| --- | --- |
| no reservation | a `FAILED` job could be retried until the provider stopped answering, entirely outside the cap |
| trust checked only at submit | an expired or withdrawn decision still permitted a retry |
| the live flag was read *after* the job had been inspected and mutated | a retry could mutate state while live generation was off |
| state check was `FAILED`, not "was not dispatched" | any `FAILED` job was retryable regardless of what the transport could prove |
| no concurrency guard | two concurrent retries could both dispatch (two billable generations) |

### What it does now

Order of checks in `retry()` — each one refuses before anything is reserved or sent:

1. `live_generation_enabled` (`LiveGenerationDisabled`);
2. the current trust decision must be trusted **and fresh**, through the same
   `_require_trust()` the submit path uses (`UntrustedArtifact`);
3. an approval binding is required (`ApprovalRequired`);
4. under the coordinator lock: the job must exist and be `FAILED`; `UNKNOWN_SUBMISSION` is
   refused (`UnknownSubmissionRemains`), as is any non-failed state;
5. the previous attempt must be recorded as `DeliveryOutcome.DEFINITELY_NOT_SENT`. A `FAILED`
   job whose send was ambiguous is demoted to `UNKNOWN_SUBMISSION` and refused — `FAILED` alone
   is never permission;
6. a **fresh** reservation is taken from the same `FlowBudgetLedger` the submit path uses,
   and the state transition to `PENDING` happens in the same locked block:
   the transition *is* the dispatch token, so a second concurrent retry sees a non-`FAILED`
   job and cannot reserve or send;
7. the frame is built with `one_attempt_only: true`, `count: 1` and `retry_attempt: n`, then
   dispatched through the same `_deliver()` path as a first submission.

Lifecycle: `attempts` increments exactly once per dispatch that is attempted, the reservation
is tracked under the job id like any other, and settlement follows the delivery classification
(§2). A retry never reuses the previous attempt's reservation — that reservation was either
released (proven non-delivery) or settled (billed), and a new one is taken.

Concurrency guarantee: two `retry()` calls racing on the same job cannot both reserve or both
dispatch, because the `FAILED → PENDING` transition happens under the coordinator lock together
with the reservation. Limit: the coordinator is in-process. Two *processes* sharing one ledger
would need a shared reservation store; that does not exist and is not claimed.

---

## 2. Defect B — a send exception was treated as proof of non-delivery

### What was wrong

Phase 3C.2.3.1 released the reservation whenever `transport.send()` raised, and the production
transport is `websocket.send()`. Entering that call means bytes may already have moved: a closed
socket, a partial write, or a lost acknowledgement can all raise *after* the frame left. Refunding
such an attempt can pay for the same generation twice.

### What it does now

`app/flow/delivery.py` defines a three-valued classification:

| Outcome | Meaning | Budget | Job state | Retryable |
| --- | --- | --- | --- | --- |
| `DEFINITELY_NOT_SENT` | the transport can prove the frame was never handed to the socket | released | `FAILED` | yes |
| `MAY_HAVE_BEEN_SENT` | the send was entered; delivery is unknown | **retained** (settled as spend) | `UNKNOWN_SUBMISSION` | no |
| `SENT` | the send completed | retained until the result | `DISPATCHED` → … | n/a |

> **Corrected in Phase 3C.2.3.3.** The table above classifies **transport** delivery
> only. This milestone's first cut also let a *provider* answer (`error.dispatched:
> false`) set `DEFINITELY_NOT_SENT`; that was wrong, because the frame had in fact been
> delivered to the extension. Provider dispatch is now its own field
> (`FlowJob.provider_dispatch`), a delivered frame stays `SENT` forever, and a provider
> refusal is recorded as `SENT` + `NOT_DISPATCHED` — still retryable, but without
> rewriting the transport history. See
> `docs/20-PHASE3C233-CANCELLATION-DELIVERY-SEMANTICS.md` §2.

* `classify_send_exception()` reads a typed `DeliveryFailure`
  (`DefiniteNonDelivery` / `DeliveryUncertain`) first, then a declared `delivery` attribute, and
  otherwise returns **`MAY_HAVE_BEEN_SENT`**. A bare `ConnectionError`, `OSError` or
  `RuntimeError` therefore never refunds.
* Pre-send refusals declare themselves: `NotPaired` (no authenticated session) and
  `CommandDenied` (allowlist) both carry `DEFINITELY_NOT_SENT`, because neither can move a byte.
* `FlowBridge.send_command()` serialises the envelope, registers the pending future, and only
  then awaits `websocket.send()`. Any exception from that call is re-raised as
  `DeliveryUncertain`; the pending future is removed and cancelled as *cleanup*, not as a claim
  that nothing was sent.
* `FlowJob.delivery` records the classification and is exposed in `job.as_dict()`.
  `FlowJob.dispatched` means "may have reached the extension": it is `False` **only** for
  proven non-delivery, never merely because a send raised and (Phase 3C.2.3.3) never because the
  provider reported that no generation was submitted. Nothing can read `dispatched == False` and
  conclude a frame stayed home when that is unknown.
* Initial submissions and retries share `_deliver()` and therefore identical semantics.

Pending-future cleanup: on a raised send the future for that command id is removed from
`session.pending` and cancelled, so `await_result()` cannot later resolve a command that has no
sender. This is deterministic cleanup of a transport record; it does not assert anything about
the frame.

---

## 3. Defect C — root acquisition and nested-directory identity

### What was wrong

* `measure()` opened the root with a single `os.open("/a/b/c", O_NOFOLLOW)`. `O_NOFOLLOW`
  protects only the **last** component; every ancestor was resolved by the kernel from a string
  after the path-based ancestor checks had already run.
* After a nested directory had been scanned through its descriptor, nothing re-checked that the
  parent entry still named that directory. A same-name replacement with a different inode leaves
  the parent's entry list identical, so listing comparison cannot see it.
* `DIR_FD_SUPPORTED` used `getattr(os, "O_NOFOLLOW", 0)` in its flags, so a platform without
  `O_NOFOLLOW` would have opened symlinks while the report still claimed no-follow traversal.

### What it does now

* **Root acquisition.** `_open_root_through_anchor()` opens `/` and walks one component per
  `openat` with `O_NOFOLLOW|O_DIRECTORY`, verifying at each step that the descriptor it received
  is the entry the parent currently names (`_open_child_dir`). `..`, `.` and empty components are
  refused (`unsafe_relative_path`). The final descriptor must match `lstat(root)`
  (`root_replaced_during_measurement`); a symlinked component fails with
  `symlink_in_package`; a missing component reports `installed_artifact_not_found`.
* **Nested-directory identity.** For every recursive scan, the directory descriptor's
  `st_dev`/`st_ino` is recorded before the scan, and after the recursive scan finishes the
  parent entry is re-stat'ed with `follow_symlinks=False`. A mismatch is
  `directory_replaced_during_measurement`; a vanished entry is the same code. The parent's
  listing and per-file byte re-reads from 3C.2.3.1 still apply, and the root is revalidated
  against the descriptor after the full scan.
* **Platform gate.** `_descriptor_traversal_available()` names every primitive explicitly:
  `O_NOFOLLOW`, `O_DIRECTORY`, `open`/`stat` in `os.supports_dir_fd`, `stat` in
  `os.supports_follow_symlinks`, `scandir` in `os.supports_fd`, plus `fstat`. If any is missing
  the measurement refuses with `unsupported_platform` — there is no weaker fallback.
* **Snapshot honesty.** `no_atomic_snapshot` remains in `report.limitations`. These checks reject
  *detectable* mutation; they do not make the filesystem atomic, and a process that can modify
  and restore every inode between checks is not detectable. `host_trust_assumed` and
  `no_process_correlation` remain as well.

---

## 4. Concurrency guarantees and their limits

| Guarantee | Mechanism | Limit |
| --- | --- | --- |
| one reservation per new attempt | `reserve()` under the ledger lock, called inside the coordinator lock after the replay/job checks | in-process ledger; a multi-process deployment needs a shared reservation store |
| one retry dispatch per job | `FAILED → PENDING` transition under the coordinator lock | in-process coordinator |
| no refund after a possibly-delivered send | classification defaults to ambiguous | a transport that *lies* (`DefiniteNonDelivery` when bytes moved) cannot be second-guessed |
| measurement not redirected by a swapped path | descriptor-relative walk with per-step identity checks | not an atomic snapshot; a writer that restores everything between checks is undetectable |

---

## 5. New adversarial tests

`backend/tests/test_phase3c232_retry_delivery_filesystem.py` (46 tests):

| Area | Tests |
| --- | --- |
| retry reservation | reserves a fresh estimate; successful retry stays charged; ambiguous retry stays charged; proven non-delivery retry releases only its own reservation |
| retry gates | expired decision; untrusted decision; missing approval; live flag off; insufficient budget blocks before dispatch; `UNKNOWN_SUBMISSION` non-retryable; `FAILED` without proven non-delivery non-retryable; Phase 3C.2.3.4 — the evidence must be *present* and correlated (command id, job id, request hash), not merely non-contradictory (`docs/21` §1) |
| retry concurrency | two concurrent retries cannot both dispatch; repeated retries cannot bypass the cap |
| delivery classification | returned send = `SENT`; untyped exception = ambiguous; typed failures classify themselves; pre-send refusals are definite |
| delivery effects | generic exception retains budget and blocks retry; proven non-delivery releases exactly once; confirmed send retains; `not sent`/`uncertain` answers without raising; bridge classifies a missing session and a socket exception; pending futures cleaned up |
| filesystem | symlinked ancestor; symlinked component; root acquired by one-component-per-`openat` walk; root replaced mid-measurement; nested directory replaced after scanning; nested directory replaced during scanning; entry added; entry removed; missing `O_NOFOLLOW`; missing `scandir` on a descriptor; snapshot limitations stated; stable installation digests identically |
| cross-component | one shared ledger; identical submit/retry semantics; ambiguous never releases; approval cannot bypass trust or budget; unusable cap never becomes a control; filesystem instability never reaches `VERIFIED`; a peer digest is not a measurement |

Existing suites were updated only where the corrected semantics demand it:

* `test_phase3c231_security_hardening.py::test_a_proven_pre_dispatch_failure_releases_the_reservation`
  — it used to raise a bare `ConnectionError` and expect a refund, which is precisely the defect
  this phase fixes. It now uses `DefiniteNonDelivery`.
* `test_a_directory_replaced_during_traversal_fails_closed` — the monkeypatched helper now
  forwards the root-walk keyword argument and only swaps inside the measured tree.

---

## 6. Verification

| Check | Command | Result |
| --- | --- | --- |
| Backend suite | `../.venv/bin/python -m pytest tests -q -p no:warnings` | **1031 passed** (985 + 46 new), 0 failed |
| Phase 3C.2.2 | `pytest tests/test_phase3c22_installed_artifact.py` | 67 passed |
| Phase 3C.2.3 | `pytest tests/test_phase3c23_trust_pipeline.py` | 33 passed |
| Phase 3C.2.3.1 | `pytest tests/test_phase3c231_security_hardening.py` | 72 passed |
| Phase 3C.2.3.2 | `pytest tests/test_phase3c232_retry_delivery_filesystem.py` | 46 passed |
| Acceptance | `../.venv/bin/python ../scripts/acceptance.py --base-url http://127.0.0.1:8000` | **65/65** (`docs/acceptance-run.json`, `2026-11-04T03:55:30Z`) |
| Migration | `alembic upgrade head / downgrade -1 / upgrade head` | `c4d5e6f7a8b9 → d5e6f7a8b9c0` → back → forward |
| Frontend | `npx tsc --noEmit` + `npm run build` | TypeScript clean; `dist/assets/index-B7cdMNiO.js` 402.08 kB (gzip 110.07 kB), `index-COHJWXOi.css` 33.24 kB |
| Source audit | `../.venv/bin/python ../scripts/flow_source_audit.py` | `SOURCE_ABSENT`, exit 3 (`2026-11-04T03:55:51Z`) |

Exact timestamps are recorded in `docs/acceptance-run.json` and in the commit message of the
commit containing this document.

---

## 7. Remaining threat-model limitations

* **No authorized release exists.** `live_capable` anchors, component digests and signature
  verification are exercised only against self-generated keys in tests. The verdict above is
  unchanged by anything in this document.
* **Single-process accounting.** Budget and retry exclusivity are enforced inside one process.
  Multiple companion processes sharing one ledger would need a shared reservation store; that is
  not implemented and not claimed.
* **Transport honesty.** The classification is only as good as the transport's own statement.
  `DeliveryUncertain` is the safe default, but a transport that wrongly raises
  `DefiniteNonDelivery` after bytes moved can still cause a duplicate paid attempt.
* **Not an atomic snapshot.** The measurement rejects detectable changes; it cannot prevent a
  hostile process with write access to the tree from modifying and restoring files between
  checks. `no_atomic_snapshot`, `host_trust_assumed` and `no_process_correlation` say so in every
  report.
* **No live evidence.** No Google account, Flow tab, extension, DeepX source or paid provider
  call was used. U1–U7 remain unresolved and Phase 3C.3 stays NO-GO.

## 8. Next step

None in this conversation. Phase 3C.2.3.2 is the last authorized milestone; Phase 3C.3 is not
started automatically.
