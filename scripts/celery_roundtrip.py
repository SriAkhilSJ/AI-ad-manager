#!/usr/bin/env python
"""Round-trip a real task through the real broker and assert it succeeded.

This is the check that a queued task is *delivered and executed*, not merely
enqueued. It fails loudly when:

* no broker is reachable,
* the worker does not know the task (the ``crusor.run_job`` registration bug),
* the task raises, or
* the result never comes back.

Usage (a worker must already be running) — see ``.github/workflows/ci.yml``::

    celery -A app.tasks.celery_app:celery_app worker --pool=solo --loglevel=info &
    python ../scripts/celery_roundtrip.py

Exit code 0 means every dispatched job returned ``SUCCESS`` with an expected
payload key; anything else is a failure.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

TIMEOUT = float(os.environ.get("CELERY_ROUNDTRIP_TIMEOUT", "90"))

#: task name -> a set of plausible result-dict keys, and an acceptable "empty"
#: outcome. The point is a decoded result object, not a specific number.
EXPECTED = {
    "publish_due": {"due", "published", "failed"},
    "refresh_analytics": {"brands", "ingested"},
    "health_check": None,
}


def main() -> int:
    from app.core.config import get_settings
    from app.tasks.celery_app import celery_app
    from app.tasks.dispatcher import get_dispatcher

    settings = get_settings()
    dispatcher = get_dispatcher(settings)
    print(f"resolved dispatcher: {dispatcher.driver} (available={dispatcher.available})")
    if dispatcher.driver != "celery":
        print("FAIL: the dispatcher did not resolve to celery; check REDIS_URL/TASK_DISPATCHER")
        return 4

    broker = celery_app.conf.broker_url
    print(f"broker: {broker}")
    print(f"backend: {celery_app.conf.result_backend}")

    inspections = celery_app.control.inspect(timeout=10.0)
    try:
        ping = inspections.ping() or {}
    except Exception as exc:  # noqa: BLE001 - broker not reachable
        print(f"FAIL: could not reach a worker: {type(exc).__name__}: {exc}")
        return 2
    if not ping:
        print("FAIL: no worker answered ping (start one with --pool=solo)")
        return 2
    print(f"workers: {', '.join(ping)}")

    registered = set()
    for names in (inspections.registered() or {}).values():
        registered |= set(names or [])
    print(f"registered tasks: {sorted(registered)}")
    missing = {name for name in ("crusor.run_job", "crusor.health_check")} - registered
    if missing:
        print(f"FAIL: worker does not know {sorted(missing)}")
        return 3

    jobs = {
        "publish_due": celery_app.send_task("crusor.run_job", kwargs={"job": "publish_due"}),
        "refresh_analytics": celery_app.send_task(
            "crusor.run_job", kwargs={"job": "refresh_analytics"}
        ),
        "health_check": celery_app.send_task("crusor.health_check"),
    }

    failures = 0
    started = time.monotonic()
    for name, async_result in jobs.items():
        try:
            value = async_result.get(timeout=TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - surface the task error
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            failures += 1
            continue
        state = async_result.state
        print(f"{name}: state={state} value={json.dumps(value, default=str)[:300]}")
        if state != "SUCCESS":
            print(f"FAIL {name}: state was {state}")
            failures += 1
            continue
        keys = EXPECTED.get(name)
        if keys and not (keys & set(value or {})):
            print(f"FAIL {name}: result has none of {sorted(keys)}")
            failures += 1
        if name == "health_check" and value != "ok":
            print(f"FAIL {name}: unexpected value {value!r}")
            failures += 1

    elapsed = time.monotonic() - started
    print(f"round trip finished in {elapsed:.2f}s")
    if failures:
        print(f"FAILED: {failures} job(s) did not complete")
        return 1
    print("OK: every dispatched task executed on the worker")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
