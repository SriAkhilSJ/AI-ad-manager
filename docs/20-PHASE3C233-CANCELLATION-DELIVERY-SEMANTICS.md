# Phase 3C.2.3.3 — Cancellation and End-to-End Delivery Semantics

**Milestone:** Phase 3C.2.3.3 (baseline `b5548bde6e178760b504f346722d63551b39a4fb`, Phase 3C.2.3.2)
**Objective:** close two defects from the independent review of Phase 3C.2.3.2 — cancellation
during a send, and the collapsing of *transport delivery* into *provider dispatch* — without
adding product features, weakening a control, or starting Phase 3C.3.

## Verdict

```
RELEASE ANCHOR:          MISSING      (no authorized release artifact or signing key exists)
INSTALLED EXTENSION:     NOT FOUND    (no extension installation is configured or present)
CANCELLATION PATH:       RECORDED, SETTLED, RE-RAISED (never a silent success or refund)
DELIVERY vs DISPATCH:    SEPARATE STATES (transport ≠ provider, both in every snapshot)
COMPANION RELAY:         STATES PRESERVED (no blanket `dispatched:false`)
OVERALL PROVENANCE:      UNVERIFIED
LIVE FLOW GENERATION:    DISABLED     (`FLOW_LIVE_GENERATION_ENABLED=false`)
U1–U7:                   UNRESOLVED
PHASE 3C.3:              NO-GO — NOT STARTED
```

There is still no authorized release, no installed extension and no licence. Every fixture in
this milestone is synthetic: in-memory transports, a fake socket, an `httpx` mock for the
companion. No Google account, Flow tab, DeepX source or paid provider call was involved.

---

## 1. Defect A — cancellation during a send left inconsistent state

### What was wrong

`GenerationCoordinator._deliver()` and `FlowBridge.send_command()` wrapped their sends in
`except Exception`. In Python 3.8+, `asyncio.CancelledError` derives from `BaseException`, so that
handler never ran for a cancellation. The consequences were all silent:

| Interruption point | What was left behind |
| --- | --- |
| inside `websocket.send()` | the pending future stayed in `session.pending`, so a later `await_result` could wait forever on a command nobody was going to answer |
| inside `_deliver()` | the job stayed `PENDING`, the reservation stayed in `_reservations`, and no state named what happened |
| while waiting for the answer | the job stayed `DISPATCHED` and the reservation was never settled — the same "leaked reservation" class of defect 3C.2.3.1 set out to remove |

Cancellation is also the one signal that must not be swallowed: a caller that cancels is telling
the process to stop, and turning that into a returned job object would be a lie about both the
control flow and the delivery state.

### What it does now

**Bridge (`FlowBridge.send_command`)** — validation, allowlist check and frame serialisation all
happen before the socket operation. The pending future is registered immediately before the send
and the cancellation branch is explicit:

```python
try:
    await session.websocket.send(frame)
except asyncio.CancelledError:
    if session.pending.get(envelope.id) is future:   # exactly once, and only if ours
        session.pending.pop(envelope.id, None)
    if not future.done():
        future.cancel()
    raise                                            # cancellation semantics preserved
except Exception as exc:
    ... raise DeliveryUncertain(...)                 # ambiguous, never "not sent"
```

The cleanup removes **this command's** future only (`is future`), so it cannot detach a future that
a later attempt registered under the same id. The `CancelledError` is re-raised untouched: it is
not converted into `DeliveryUncertain` or `DefiniteNonDelivery`, because a cancellation is not a
delivery result.

**Coordinator (`GenerationCoordinator._deliver`)** — a cancellation is classified as
`MAY_HAVE_BEEN_SENT`, because it landed inside the transport operation and cannot prove the frame
stayed home:

* `delivery = MAY_HAVE_BEEN_SENT`, `provider_dispatch = UNKNOWN`, `dispatched = True`;
* the job moves to `UNKNOWN_SUBMISSION` with `retryable=False` and an explanatory
  `ErrorDescriptor` (`reason: "cancelled during send"`);
* the reservation is **settled** (treated as spend — the frame may already be billable), never
  released;
* the job stays inspectable through `get()`, `jobs` and `snapshot()`;
* the `CancelledError` is re-raised, so `submit()`/`retry()` do not return a value.

The single exception is a transport that declares it was cancelled *before* it touched the socket
(`transport.cancellation_delivery = DEFINITELY_NOT_SENT`, an opt-in attribute no transport in this
repository sets). Absent that declaration the classification is ambiguity — the requirement is
that ambiguity is the default, not that it be inferred from the exception type.

Cancellation while waiting for the answer (after a successful send) is also recorded: the job is
marked `UNKNOWN_SUBMISSION`, settled, and the cancellation is re-raised. Identical handling
applies in `submit()` and `retry()`, because both share `_deliver()` and the same result path.

---

## 2. Defect B — transport delivery and provider dispatch are different events

### What was wrong

Phase 3C.2.3.2 mapped an extension answer carrying `error.dispatched == false` to transport
`DEFINITELY_NOT_SENT`. But by then the frame had been written to the socket and the extension had
answered it — the transport *did* deliver. Only the extension's submission to the external
provider did not happen. Recording the first as "not sent" was a false statement about history,
and it fed a retry gate that only looked at `delivery`, so the distinction could not be recovered
later.

### The two independent classifications

```python
class DeliveryOutcome(str, Enum):          # Crusor → extension
    DEFINITELY_NOT_SENT = "definitely_not_sent"
    MAY_HAVE_BEEN_SENT  = "may_have_been_sent"
    SENT                = "sent"

class ProviderDispatchOutcome(str, Enum):  # extension → external provider
    NOT_DISPATCHED = "not_dispatched"
    DISPATCHED     = "dispatched"
    UNKNOWN        = "unknown"
```

`FlowJob` stores both (`delivery`, `provider_dispatch`), and `as_dict()`/`snapshot()` expose both
by name. `FlowJob.dispatched` is kept for API compatibility and defined exactly once:

> `dispatched` is `True` whenever the command may have reached the extension, and `False` only
> when the transport proved nothing was sent (or nothing was attempted). It is **never** cleared
> because the provider reports that no generation was submitted.

### The state mapping

An absent provider-dispatch field is `UNKNOWN`, never `NOT_DISPATCHED`. A non-boolean
(`"false"`, `0`, `{}`) is `UNKNOWN`. A foreign job id, a foreign command id or a contradictory
`request_hash` forces `UNKNOWN` and settles the spend.

---

## 3. Companion relay

`LocalFlowCompanion._handle_command()` used to post `{"dispatched": False}` for **every**
exception — including timeouts and socket failures raised after `bridge.dispatch()` had already
written the frame. That is the same false claim one hop earlier, and the backend had no way to
tell the two apart.

`FlowBridge.dispatch()` now raises `DispatchFailed`, which carries both states, and the companion
maps them without inventing anything:

| Failure | transport | provider | `error.dispatched` |
| --- | --- | --- | --- |
| command not allowlisted (`CommandDenied`) | `definitely_not_sent` | `not_dispatched` | `false` (true) |
| no authenticated session (`NotPaired`) | `definitely_not_sent` | `not_dispatched` | `false` (true) |
| bridge unavailable / generation disabled in the companion | `definitely_not_sent` | `not_dispatched` | `false` (true) |
| socket send raised | `may_have_been_sent` | `unknown` | **omitted** |
| delivered, no answer / timeout | `sent` | `unknown` | **omitted** |
| delivered, correlated provider refusal | `sent` | `not_dispatched` | **omitted** |

`dispatched: false` is emitted only where it is true. The result payload carries the two
classifications at the top level (`transport`, `provider_dispatch`) and inside `error`. Both are
new optional fields on `CommandResultIn`, so an older companion that omits them still validates;
the backend records the empty string ("not stated"), which is not the same as `not_dispatched`.

The schema rejects contradictions rather than trusting them: `provider_dispatch=not_dispatched`
with `transport=may_have_been_sent`, `provider_dispatch=dispatched` with
`transport=definitely_not_sent`, or `error.dispatched == false` with an ambiguous transport all
fail validation.

A cancelled companion command posts **nothing**. Silence is honest: the backend's own state for
the command remains whatever it recorded, and it never becomes a fabricated non-delivery.

---

## 4. Retry eligibility

`retry()` requires every earlier gate (live flag, fresh verified trust, approval binding, lock) plus
a previous attempt that the two states agree was never submitted:

| Previous attempt | Retryable |
| --- | --- |
| `DEFINITELY_NOT_SENT` + `NOT_DISPATCHED` | **yes** (nothing left the machine) |
| `SENT` + `NOT_DISPATCHED` | **yes** (delivered, correlated provider refusal) |
| `MAY_HAVE_BEEN_SENT` + any | no |
| any + `UNKNOWN` | no |
| any + `DISPATCHED` | no |
| `UNKNOWN_SUBMISSION` state | never |

A retry that is refused is **demoted to `UNKNOWN_SUBMISSION`** with `provider_dispatch=UNKNOWN`, so
it cannot drift back into a retryable state, and no attempt number is consumed. An eligible retry
takes a fresh reservation inside the same lock as the `FAILED → PENDING` transition, resets both
classifications to `(None, UNKNOWN)`, and settles or releases exactly once according to the new
attempt's classification.

---

## 5. Late results and pending futures

* A result that arrives after a timeout is applied to a job that is already
  `UNKNOWN_SUBMISSION`; `_apply_submission_result` can only *further* qualify it (and a foreign id,
  a malformed error or a contradictory hash keeps it unknown). It can never re-arm a retry.
* Pending futures are removed exactly once, only when they are still the future that was
  registered for that command id, and they are cancelled before removal if unresolved.
* Cleanup is a transport record operation and makes no claim about the frame — that is why the
  coordinator, not the bridge, decides the classification.

## 6. Budget settlement

| Event | Reservation |
| --- | --- |
| proven transport non-delivery | released (once) |
| correlated provider `NOT_DISPATCHED` | released (once) — nothing could have been billed |
| ambiguous transport, cancellation inside the send, timeout, unknown provider | settled as spend (never refunded) |
| cancelled while waiting for the answer | settled as spend |
| confirmed submit | settled once the outcome is known |

## 7. Concurrency guarantees and limits

* **In-process only.** The job lock, the `FAILED → PENDING` dispatch token and the ledger's own
  lock are all in one process. A multi-process deployment would need a shared reservation store;
  none exists and none is claimed.
* **Cancellation ordering is observable, not atomic.** The classification reflects *where* the
  cancellation landed; a transport that is cancelled between the socket write and its return is
  indistinguishable from one cancelled before the write, so both are ambiguous. That asymmetry is
  deliberate: it can over-report delivery, never under-report it.
* **Transport honesty.** A transport that claims `DefiniteNonDelivery` after writing bytes can
  still cause a refund it should not have. The default path is safe; a lying transport is outside
  what this code can detect.
* **Companion trust.** The companion's two states are recorded, not re-derived: the backend cannot
  independently verify that the extension did or did not call the provider. They are evidence
  supplied by an authenticated local process, exactly as before.

## 8. Remaining threat-model limitations

* No authorized release anchor, no installed extension, no licence: the real verdict is unchanged
  and `VERIFIED` remains unreachable on this machine (`SOURCE_ABSENT`).
* The upstream extension is not verified here. Nothing in this milestone claims the extension
  reports `dispatched` honestly; it claims only that Crusor no longer *invents* a delivery state
  the data does not support.
* Cancellation inside a socket send cannot be resolved into a definite answer by any local
  mechanism; it is recorded as ambiguity by design.
* `no_atomic_snapshot` and the filesystem limitations from 3C.2.3.2 are untouched.

## 9. Tests

`backend/tests/test_phase3c233_cancellation_delivery.py` — 48 tests:

| Area | Coverage |
| --- | --- |
| cancellation | before any transport interaction (parked on the lock); inside the send; while waiting for the answer; during a retry; job survives and stays inspectable; reservation settled once; never swallowed into a returned job; ambiguous cancellations are never retryable |
| state mapping | local refusal; completed send; raised send; cancelled send; correlated provider non-dispatch; correlated provider dispatch; timeout; missing field; malformed values (`"false"`, `0`, `""`, `[]`, `{}`, `None`); foreign job id; foreign command id; contradictory request hash; matching request hash |
| retry | only eligible pairs retry; ambiguous provider; ambiguous transport; foreign answer; trust and approval still required; reservation settled exactly once; newly reserved retry |
| bridge | cancelled socket send cleans up and re-raises; no non-delivery claim on cancellation; socket exception → uncertainty; `dispatch()` reports both states for a provider refusal and for a timeout; pre-send refusal is provable |
| companion | pre-send refusal relays definite non-delivery; timeout and socket failure relay uncertainty without `dispatched:false`; correlated provider refusal preserved; cancellation posts nothing; schema contradiction rejection |
| lifecycle | late result cannot re-arm a retry; cancelling a dispatched job keeps the spend and unknown provider; one-attempt-only intact; both states exposed |

Existing suites were updated only where they encoded the old conflation — the provider-refusal
assertions that expected `dispatched is False`, the 3C.2.3.2 retry helper that expected
`DEFINITELY_NOT_SENT` for a provider refusal, and the scripted transports now echo the command id
they answer (as a real extension does), since the coordinator validates that correlation.

## 10. Verification

Recorded on this machine (UTC) at baseline `b5548bd`, with no code change between the
final acceptance run and the final suite run:

| Check | Command | Result |
| --- | --- | --- |
| Backend suite | `../.venv/bin/python -m pytest tests` | **1079 passed in 515.89s**, 0 failed (05:14:31Z → 05:23:09Z) |
| `test_phase3c1_flow_security.py` | collected/run in the suite | 95 passed |
| `test_phase3c2_flow_compatibility.py` | collected/run in the suite | 65 passed |
| `test_phase3c22_installed_artifact.py` | collected/run in the suite | 67 passed |
| `test_phase3c23_trust_pipeline.py` | collected/run in the suite | 33 passed |
| `test_phase3c231_security_hardening.py` | collected/run in the suite | 72 passed |
| `test_phase3c232_retry_delivery_filesystem.py` | collected/run in the suite | 46 passed |
| `test_phase3c233_cancellation_delivery.py` | collected/run in the suite | 48 passed (new) |
| Acceptance | `../.venv/bin/python ../scripts/acceptance.py --base-url http://127.0.0.1:8000` | **65/65**, generated_at `2026-11-04T05:14:26Z` in `docs/acceptance-run.json` |
| Migration | `DATABASE_URL=sqlite+aiosqlite:////tmp/mig_3c233.db` `alembic upgrade head` → `downgrade -1` → `upgrade head` | `c4d5e6f7a8b9 → d5e6f7a8b9c0`, head `d5e6f7a8b9c0` after the round trip |
| Frontend types | `npx tsc --noEmit` | clean |
| Frontend build | `npm run build` | `index-B7cdMNiO.js` 402.08 kB (gzip 110.07 kB), `index-COHJWXOi.css` 33.24 kB |
| Source audit | `../.venv/bin/python ../scripts/flow_source_audit.py` | `RESULT blocked (SOURCE_ABSENT)`, exit **3**, `2026-11-04T05:23:32Z` |

Two fixture-level corrections were needed and are recorded here so the counts are auditable
rather than asserted:

* `scripts/acceptance.py` criterion 57's inline transport answered with a fixed
  `id='c'` instead of echoing the command id. Under 3C.2.3.3 that answer is correctly
  rejected as uncorrelated, so the job became `UNKNOWN_SUBMISSION` (terminal) and the
  subsequent `cancel()` was refused. The transport now echoes `envelope.id`, exactly as a
  real extension does; every assertion in the criterion is unchanged.
* The 3C.2.3.2 scripted transports in the test suite gained the same `command_id` echo, and
  the two provider-refusal assertions that encoded the old conflation were inverted (they
  now assert `SENT` + `NOT_DISPATCHED` + `dispatched is True`, which is the stronger claim
  the milestone makes).

## 11. Next step

None in this conversation. Phase 3C.2.3.3 is the last authorized milestone; Phase 3C.3 remains
NO-GO and is not started automatically.
