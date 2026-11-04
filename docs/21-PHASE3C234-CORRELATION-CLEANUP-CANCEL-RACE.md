# Phase 3C.2.3.4 — Strict Correlation, Pending-Future Cleanup and Cancellation Race Closure

**Milestone:** Phase 3C.2.3.4 (baseline `0fe77e4be3a5f6c184c5192d138e732bf6de5430`, Phase 3C.2.3.3)
**Objective:** close exactly three findings from the independent review of Phase 3C.2.3.3 —
correlation that accepted *absent* evidence, pending records that outlived their commands, and a
`cancel()` that could race a submission. No product features, no control weakened, Phase 3C.3 not
started.

## Verdict

```
RELEASE ANCHOR:          MISSING      (no authorized release artifact or signing key exists)
INSTALLED EXTENSION:     NOT FOUND    (no extension installation is configured or present)
CORRELATION:             PRESENCE AND EQUALITY REQUIRED (absent values are a rejection)
PENDING RECORDS:         RETIRED ON EVERY PATH (result, timeout, cancel, disconnect, decode)
CANCEL RACE:             LOCK-ARBITRATED (no send after a cancel wins, no false non-delivery)
OVERALL PROVENANCE:      UNVERIFIED
LIVE FLOW GENERATION:    DISABLED     (`FLOW_LIVE_GENERATION_ENABLED=false`)
U1–U7:                   UNRESOLVED
PHASE 3C.3:              NO-GO — NOT STARTED
```

Every fixture in this milestone is synthetic: in-memory transports, a fake socket, a fake frame
stream, an `httpx` mock. No Google account, Flow tab, extension installation, DeepX source or paid
provider call was involved, and nothing here claims any runtime property of a third-party extension.

---

## 1. Finding 1 — missing correlation could authorize `NOT_DISPATCHED`

### What was wrong

Phase 3C.2.3.3 compared correlation only when a value was present:

```python
if command_id and result.id and result.id != command_id:      # absent id was fine
if result.job_id and result.job_id != job.job_id:             # absent job id was fine
```

and the request hash did not exist on the wire at all. An answer that simply *omitted* the job id
was therefore treated as correlated. If it also carried `dispatched: false`, the coordinator
recorded `ProviderDispatchOutcome.NOT_DISPATCHED`, released the budget reservation and left the job
`FAILED` — which is retryable. A missing field could therefore unlock a second paid generation, and
an `ok: true` answer with no correlation could claim a media id for the wrong job.

### The rule now

`correlate_result()` in `app/flow/coordinator.py` returns a verdict, and the verdict is applied
before *anything* else in `_apply_result_locked()`:

| Evidence | Requirement |
| --- | --- |
| frame kind | `type == "result"` (a forged envelope is rejected before parsing) |
| command id | present **and** exactly equal to the id of the frame that was sent |
| job id | present **and** exactly equal to the job awaiting an answer |
| request hash | present **and** exactly equal to `CommandEnvelope.request_hash` (when the command carried one) |
| provider flag | an explicit Python boolean in `error.dispatched` |
| contradictions | `error.transport` must be absent or `sent`; `error.provider_dispatch` must be absent or agree; an embedded `error.request_hash` must match |

Anything else is uncorrelated. There is no `if result.job_id` shortcut left, and an absent field is
not a wildcard — it is a rejection.

### What a rejection means

| Outcome | transport | provider | `dispatched` | Budget | Job |
| --- | --- | --- | --- | --- | --- |
| fully correlated refusal (`dispatched: false`) | `sent` | `not_dispatched` | `true` | **released** | `FAILED`, retryable |
| fully correlated success | `sent` | `dispatched` | `true` | settled | `SUBMITTED` |
| missing/wrong command id, job id or hash | `sent` | `unknown` | `true` | **settled** | `UNKNOWN_SUBMISSION` |
| malformed error, non-boolean flag, contradiction | `sent` | `unknown` | `true` | **settled** | `UNKNOWN_SUBMISSION` |
| `ok: true` without correlation | `sent` | `unknown` | `true` | **settled** | `UNKNOWN_SUBMISSION`, no media claimed |

Only the first row can release a reservation after a delivered frame, so only the first row can make
a retry eligible. `retry()` re-checks both states under the lock and demotes anything else to
`UNKNOWN_SUBMISSION` before raising `UnknownSubmissionRemains`.

### Binding the hash to the frame

`CommandEnvelope.request_hash` is computed from the request that is actually being sent
(`GenerationCommand.request_hash()`), and `_deliver()` refuses to send a frame whose `job_id` or
`request_hash` does not match the job it is being sent for. A caller cannot supply a hash that is not
tied to the pending command, and a result cannot be correlated against a value chosen after the fact.

### Success is not exempt

`ok: true` goes through the same verdict. An uncorrelated success is **not** evidence that a
generation was submitted: the job becomes `UNKNOWN_SUBMISSION`, the spend is settled, no media id is
claimed (`_media_owner` untouched) and no retry is authorized.

### Protocol and compatibility

* `ResultEnvelope` gained `request_hash: str = ""`. It is optional on the wire so an older peer still
  *validates*, but it is mandatory **evidence**: an answer that omits it, or omits the job id, is
  uncorrelated and fails closed. Backward compatibility here means "older companions cannot
  authorize a retry", not "older companions are trusted".
* `CommandResultIn` (the companion's HTTP report) gained `request_hash`, and now rejects
  `provider_dispatch == "not_dispatched"` unless `command_id`, `job_id` **and** `request_hash` are all
  present and the error carries an explicit boolean `dispatched: false`.

  > **Corrected in Phase 3C.2.3.4.1.** This milestone wrote that check as
  > `if self.error is not None and isinstance(self.error, dict): if error.get("dispatched") is not
  > False: raise ...`, so a payload with valid-looking correlation fields and `error=None` skipped
  > the explicit-refusal branch entirely and passed validation while claiming `not_dispatched`. The
  > check is now unconditional: `provider_dispatch == "not_dispatched"` requires an error **object**
  > whose `dispatched` field is explicitly the boolean `False`. `error=None`, a missing
  > `dispatched`, `dispatched=True`, the string `"false"`, `0`, `None` and a non-object `error` are
  > all rejected; a fully correlated refusal with `dispatched: false` is accepted. `extra="forbid"`
  > and the transport/provider contradiction checks are unchanged, and nothing on the coordinator
  > side (correlation, refusal classification, budget, retry, cancellation) was touched.
* `QueuedCommand` carries `request_hash` to the companion (in memory, alongside the payload), and the
  companion echoes it. When a command arrives without one, the companion reports `not_dispatched` as
  `unknown` instead, because it cannot attribute the refusal to one attempt.
* `FlowBridge.dispatch()` applies the same verdict, so an uncorrelated answer can never become
  `DispatchFailed(provider=NOT_DISPATCHED)`; it becomes `flow_result_uncorrelated` with provider
  `UNKNOWN`. A refusal for a command that carried no job id is treated the same way.
* Status (`check_video_status`) and export (`export_asset`) answers are correlated on command id and
  job id (those commands carry no request hash); a status answer that fails correlation raises
  `ForeignResult`, and an export answer raises `ExportUnverified` — neither can move a job or hand
  over bytes.

## 2. Finding 2 — pending records outlived their commands

`FlowBridge.await_result()` awaited `asyncio.wait_for(future, timeout)`. `wait_for` cancels the
future on timeout but leaves the **dictionary entry** in `session.pending`, and the same was true of
cancellation and of any exception. The record survived until the socket closed, and nothing tied it
to the attempt that had already been classified as ambiguous.

### The lifecycle now

`await_result()` removes the entry in a `finally` block, exactly once, and only when the entry is
*still ours*:

```python
try:
    return await asyncio.wait_for(future, timeout=timeout)
finally:
    if session.pending.get(command_id) is future:      # identity, not key
        session.pending.pop(command_id, None)
    if not future.done():
        future.cancel()
```

| Event | Pending record | Waiter |
| --- | --- | --- |
| result frame arrives | removed by `_ws_loop` before `set_result` | receives the envelope |
| timeout | removed by `await_result` | `TimeoutError` → `UNKNOWN_SUBMISSION` |
| task cancellation | removed by `await_result` | `CancelledError` re-raised → `UNKNOWN_SUBMISSION` |
| socket disconnect | `retire_pending()` fails and clears every record | `ConnectionError` |
| malformed frame | removed, future completed with `FlowProtocolError` | protocol error → unknown |
| send raised or cancelled | removed by `send_command`'s own cleanup | classified by the coordinator |
| delivered, then cancelled | removed by `discard()` via the coordinator's optional hook | nobody waits |

`_ws_loop` resolves frames by identity, drops a frame whose entry is gone (late) or whose future is
already done (duplicate), and retires the record before validating — so a malformed frame cannot
leave a dangling future either. `send_command` refuses a command id that is already pending and
refuses an empty one, so a late answer can never satisfy a newer command; ids come from
`proto.new_id()` and are never reused.

`retire_pending()` was extracted from the disconnect path so the guarantee is directly testable
rather than implied.

## 3. Finding 3 — `cancel()` could race a submission

### What was wrong

`cancel()` took no lock and read the job state directly, while `submit()` released the coordinator
lock after reserving budget and before `_deliver()` wrote to the transport. Between those two points
a cancel could set `CANCELLED` and refund, and the submission would then dispatch anyway — a send
after a cancellation had won — or the send would overwrite the cancel's state. A cancel of a
`PENDING` job also left its reservation tracked forever.

### The state machine

`FlowJob.send_started` is set under the lock at the moment the attempt is about to enter the
transport. `cancel()` takes the same lock, so exactly one of the two orderings happens.

| State at `cancel()` | Result | Reservation | Job |
| --- | --- | --- | --- |
| `PENDING`, send not started | returns the job | **released** (provable non-delivery) | `CANCELLED`, `dispatched=False` |
| `PENDING`, send started | `CancellationAmbiguous` | settled (spend) | `UNKNOWN_SUBMISSION`, provider `UNKNOWN` |
| `DISPATCHED` | `CancellationAmbiguous` | settled (spend) | `UNKNOWN_SUBMISSION`, provider `UNKNOWN` |
| `SUBMITTED` / `RUNNING` | returns the job | already settled | `CANCELLED` + `provider_may_still_complete` |
| any terminal state | `CoordinatorError` | untouched | untouched |
| unknown job | `CoordinatorError` | untouched | untouched |

`_deliver()` re-checks the state under the lock before entering the transport:

* a cancel that already won ⇒ the job is returned and **nothing is written to the socket**;
* a cancel that lands after the transition ⇒ the send may complete; `_record_send_success()` then
  records the delivery truthfully (`SENT`, `dispatched=True`) but keeps the cancel's terminal state
  and the settled reservation, and retires the pending record nobody will await.

Both `submit()` and `retry()` use the same `_deliver()`, so a retry inherits the identical race
policy, and `retry()` resets `send_started` when it takes the `FAILED → PENDING` token so a new
attempt is not mistaken for an already-started one.

### Reservation accounting

| Event | Effect |
| --- | --- |
| provable non-delivery (transport, or cancel before the send) | released once |
| correlated provider `NOT_DISPATCHED` | released once |
| ambiguous send, cancellation, timeout, unknown provider | settled once (never refunded) |
| cancel **after** the transport entry | settled; a later transport-confirmed non-delivery does **not** refund it |

`_release_reservation()` pops the tracked estimate before calling the ledger, so a second release is a
no-op; `_settle_reservation()` only forgets the tracking entry, so a second settle changes nothing.
Both are reached from a single code path per attempt.

## 4. Known limitations

* **In-process only.** The lock, the `send_started` token and the ledger are one process's state. A
  multi-process deployment would need a shared store; none exists and none is claimed.
* **Cancellation ordering is observable, not atomic.** A send interrupted after the write and one
  interrupted before it are indistinguishable, so both are ambiguous. The asymmetry is deliberate:
  it can over-report delivery, never under-report it.
* **A lying transport or extension** that echoes correlation values it did not receive is outside what
  this code can detect; the protocol is correct, the peer is not proven.
* **No runtime proof of third-party behaviour.** `request_hash` echo is a documented requirement of
  this repository's protocol. Nothing here demonstrates that an upstream extension sends it; the
  claim is only that Crusor fails closed when it does not.
* No authorized release anchor, no installed extension, no licence: the source audit still reports
  `SOURCE_ABSENT` and `OVERALL PROVENANCE` stays `UNVERIFIED`.

## 5. Corrections to earlier documentation

* `docs/20` §2 described correlation as a *mismatch* check ("a foreign job id, a foreign command id or
  a contradictory `request_hash` forces `UNKNOWN`"). It now also states that **absent** values are a
  rejection.
* `docs/19` is annotated where it introduced the retry-gate pair, so a reader is pointed at this
  milestone for the evidence rule.
* Test fixtures across 3C.1, 3C.2.2, 3C.2.3.1, 3C.2.3.2 and 3C.2.3.3 now echo command id, job id and
  request hash through a shared helper (`tests/flow_correlation.py`), which fills **only empty**
  values: a deliberately foreign id, job id or hash is preserved and still fails correlation, so the
  refusal tests keep testing refusal.

## 6. Tests

`backend/tests/test_phase3c234_correlation_cleanup_cancel_race.py` — 74 tests (58 from Phase
3C.2.3.4, 16 added in Phase 3C.2.3.4.1 for the explicit-refusal invariant):

| Area | Coverage |
| --- | --- |
| verdict | fully correlated answer accepted; each missing field names itself; wrong command id, job id, hash; a frame that is not a result envelope |
| correlation | correlated refusal releases; every incomplete case (missing/wrong × command id, job id, hash) is `UNKNOWN` + settled + non-retryable; malformed error; contradictory transport/provider fields; uncorrelated success; foreign success; duplicate/late answer cannot move a settled job; envelope bound to another request refused; boolean-only evidence classifier |
| schema | `not_dispatched` rejected without command id, job id or hash; rejected without the explicit boolean; accepted when fully correlated; **Phase 3C.2.3.4.1** — `error=None`, missing `dispatched`, `dispatched=True`, `"false"`, `0`, `null` and a non-object error are each rejected, the honest non-refusal combinations still validate, and the contradiction rules still reject |
| bridge dispatch | uncorrelated refusal → `flow_result_uncorrelated`/`UNKNOWN`; refusal for a job-less command refused; correlated refusal preserved; foreign hash rejected |
| pending futures | success, timeout, cancellation, disconnect, malformed frame all retire the record; late result discarded; duplicate cannot satisfy another command; replacement future survives stale cleanup; duplicate and empty command ids refused; delivered-then-cancelled retires the abandoned record; `discard()` semantics |
| cancel races | cancel wins before the send (no send, refunded); after the transport entry (ambiguous, settled, state kept, one attempt); waiting for the lock; while awaiting the result; during a retry; cancel-before-retry-sends refunds only the retry; repeated cancel of a terminal job; unknown job; a cancelled job can never be retried; bridge send cancelled mid-socket stays ambiguous with no pending record |

Every wait is an `asyncio.Event` barrier or a monkeypatched gate, not a sleep, so the races are
deterministic rather than timing-dependent.

## 7. Verification

Recorded on this machine (UTC), with no code change between the final acceptance run and the final
suite run. Every line below is a **local** run; GitHub CI is a separate signal and is not claimed
here.

### Phase 3C.2.3.4 (baseline `0fe77e4`, commit `07d2680`)

| Check | Command | Result |
| --- | --- | --- |
| Backend suite | `../.venv/bin/python -m pytest tests` | **1137 passed in 521.41s**, 0 failed (05:58:03Z → 06:06:48Z) |
| new module | `test_phase3c234_correlation_cleanup_cancel_race.py` | 58 passed |
| 3C.2.3.3 regression | `test_phase3c233_cancellation_delivery.py` | 48 passed |
| 3C.2.3.2 regression | `test_phase3c232_retry_delivery_filesystem.py` | 46 passed |
| 3C.2.3.1 regression | `test_phase3c231_security_hardening.py` | 72 passed |
| 3C.2.3 regression | `test_phase3c23_trust_pipeline.py` | 33 passed |
| 3C.2.2 regression | `test_phase3c22_installed_artifact.py` | 67 passed |
| 3C.2 compatibility | `test_phase3c2_flow_compatibility.py` | 65 passed |
| 3C.1 security | `test_phase3c1_flow_security.py` | 95 passed |
| Acceptance | `../.venv/bin/python ../scripts/acceptance.py --base-url http://127.0.0.1:8000` | **65/65**, `generated_at 2026-11-04T06:16:21Z` |
| Migration | `DATABASE_URL=sqlite+aiosqlite:////tmp/mig_3c234.db` up → down → up | `c4d5e6f7a8b9 → d5e6f7a8b9c0`, head `d5e6f7a8b9c0` |
| Frontend types | `npx tsc --noEmit` | exit 0 |
| Frontend build | `npm run build` | `index-B7cdMNiO.js` 402.08 kB (gzip 110.07 kB), `index-COHJWXOi.css` 33.24 kB |
| Source audit | `../.venv/bin/python ../scripts/flow_source_audit.py` | `RESULT blocked (SOURCE_ABSENT)`, exit **3**, `2026-11-04T06:17:24Z` |

### Phase 3C.2.3.4.1 (baseline `07d2680`, commit `Phase 3C.2.3.4.1: require explicit refusal evidence`)

Re-run after the schema invariant was made unconditional. The table above describes the 3C.2.3.4
run and is left as recorded; these are the numbers the correction produced.

| Check | Command | Result |
| --- | --- | --- |
| focused module | `../.venv/bin/python -m pytest tests/test_phase3c234_correlation_cleanup_cancel_race.py` | **74 passed** (58 + 16 new refusal-evidence tests) |
| Backend suite | `../.venv/bin/python -m pytest tests` | **1153 passed in 512.98s**, 0 failed (06:32:58Z → 06:41:33Z) |
| Acceptance | `../.venv/bin/python ../scripts/acceptance.py --base-url http://127.0.0.1:8000` | **65/65**, `generated_at 2026-11-04T06:50:53Z` |
| Migration | `DATABASE_URL=sqlite+aiosqlite:////tmp/mig_3c2341.db` up → down → up | `c4d5e6f7a8b9 → d5e6f7a8b9c0`, head `d5e6f7a8b9c0` |
| Frontend types | `npx tsc --noEmit` | exit 0 |
| Frontend build | `npm run build` | `index-B7cdMNiO.js` 402.08 kB (gzip 110.07 kB), `index-COHJWXOi.css` 33.24 kB |
| Source audit | `../.venv/bin/python ../scripts/flow_source_audit.py` | `RESULT blocked (SOURCE_ABSENT)`, exit **3**, `2026-11-04T06:52:06Z` |

Fixture maintenance is recorded rather than hidden: the scripted transports in 3C.1, 3C.2
compatibility, 3C.2.2, 3C.2.3.1, 3C.2.3.2 and 3C.2.3.3 now echo command id, job id and request hash
(through `tests/flow_correlation.py`), the 3C.2.3.3 placeholders `id="cmd"`/`id="c1"` became empty so
the echo can fill them, the fake bridges accept the `request_hash` kwarg, and acceptance criterion
57's inline transport answers with `job_id=envelope.job_id, request_hash=envelope.request_hash`.
No assertion was weakened to obtain these counts: the fixtures describe a compliant extension, and
the tests that hand the coordinator a foreign or missing value stay exactly as they were.

## 8. Next step

None in this conversation. Phase 3C.2.3.4 is the last authorized milestone; Phase 3C.3 remains
NO-GO and is not started automatically.
