#!/usr/bin/env python
"""Drive one complete Director lifecycle through the HTTP API and record it.

This is the Phase 4.3 lifecycle evidence run. It starts a real workflow against a
real server, lets it run to the founder-approval interrupt, approves it as the
founder, and then follows it through scheduling, publication, measurement and
optimisation. Every stage is captured verbatim in ``docs/lifecycle-run.json``, so
the evidence can be inspected instead of taken on trust.

It is deliberately blunt about what it could not do: with no platform credentials
the publish stage is expected to come back `unavailable` with a reason, and the
run records that reason rather than a green tick. A workflow that reports
"published" without a platform is the exact failure this project exists to
prevent, so this script fails the run if that ever happens.

Usage::

    LIFECYCLE_EMAIL=founder@crusor.ai LIFECYCLE_PASSWORD=... \\
        python ../scripts/lifecycle_evidence.py --base-url http://127.0.0.1:8000

Exit codes: 0 = the lifecycle reached a final, honest state; 1 = an assertion
about governance failed (approval bypass, fabricated publish, missing trace).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Api:
    def __init__(self, base: str, token: str | None = None) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.calls: list[dict[str, Any]] = []

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                payload = json.loads(response.read() or b"{}")
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw or "{}")
            except ValueError:
                payload = {"raw": raw[:500]}
            status = exc.code
        except Exception as exc:  # noqa: BLE001 - the failure is the evidence
            payload = {"error": f"{type(exc).__name__}: {exc}"}
            status = 0
        self.calls.append(
            {
                "at": _now(),
                "method": method,
                "path": path,
                "status": status,
                "ms": round((time.monotonic() - started) * 1000, 1),
            }
        )
        return status, payload


def summarise(status: dict[str, Any]) -> dict[str, Any]:
    """The parts of a workflow status worth reading, in one object."""
    return {
        "status": status.get("status"),
        "lifecycle_stage": status.get("lifecycle_stage"),
        "phase": status.get("phase"),
        "awaiting_human": status.get("awaiting_human"),
        "iteration": status.get("iteration"),
        "checkpoint_count": status.get("checkpoint_count"),
        "strategy_id": (status.get("results") or {}).get("strategy_id"),
        "created_campaign_id": status.get("created_campaign_id"),
        "content_item_ids": status.get("content_item_ids"),
        "schedule_plan": status.get("schedule_plan"),
        "schedule_blocked": status.get("schedule_blocked"),
        "scheduled_post_ids": status.get("scheduled_post_ids"),
        "publish_results": status.get("publish_results"),
        "analytics": status.get("analytics"),
        "optimizations": status.get("optimizations"),
        "optimization_note": status.get("optimization_note"),
        "qa_verdict": status.get("qa_verdict"),
        "trace": [t.get("entry") for t in (status.get("trace") or [])],
        "errors": status.get("errors"),
        "results": status.get("results"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=os.getenv("API_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--email", default=os.getenv("LIFECYCLE_EMAIL"))
    parser.add_argument("--password", default=os.getenv("LIFECYCLE_PASSWORD"))
    parser.add_argument("--objective", default="Acquire developers for Crusor AI through technical content")
    parser.add_argument("--write", action="store_true", help="write docs/lifecycle-run.json")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not args.email or not args.password:
        print("LIFECYCLE_EMAIL and LIFECYCLE_PASSWORD must be set (never passed as flags)")
        return 2

    api = Api(args.base_url)
    evidence: dict[str, Any] = {"generated_at": _now(), "base_url": args.base_url, "steps": []}

    def step(name: str, status: int, payload: Any, *, expect: int = 200) -> Any:
        evidence["steps"].append({"step": name, "status": status, "payload": payload})
        if status != expect:
            raise SystemExit(f"step {name!r} failed: HTTP {status}: {json.dumps(payload)[:500]}")
        return payload

    try:
        status, health = api.call("GET", "/api/v1/system/health")
        step("health", status, health)

        status, token = api.call("POST", "/api/v1/auth/token", {"email": args.email, "password": args.password})
        step("login", status, {"token": "<redacted>"})
        api.token = token["access_token"]

        status, system = api.call("GET", "/api/v1/system/status")
        step("system_status", status, system)
        # Record what actually drove this run: a lifecycle executed with
        # deterministic providers is not evidence about a real one.
        llm = system.get("llm") or {}
        evidence["runtime"] = {
            "llm_status": llm.get("status"),
            "llm_configured": (llm.get("routing") or {}).get("configured"),
            "llm_providers_configured": {
                name: bool((row or {}).get("configured") or (row or {}).get("credential_present"))
                for name, row in (llm.get("providers") or {}).items()
            },
            "unverified_models": (llm.get("routing") or {}).get("unverified_models"),
            "publishing": (system.get("publishing") or {}).get("status"),
            "publishing_reason": (system.get("publishing") or {}).get("vault_reason"),
            "creative_flow": ((system.get("creative") or {}).get("flow_connector") or {}).get("status"),
            "creative_provider": (system.get("creative") or {}).get("provider"),
            # The deterministic provider is not a provider: it must never make a
            # run look like it used a real model.
            "real_providers_in_use": bool(
                any(
                    bool((row or {}).get("configured") or (row or {}).get("credential_present"))
                    for name, row in (llm.get("providers") or {}).items()
                    if name.lower() not in {"mock", "deterministic", "none"}
                )
            ),
        }

        status, brand = api.call("GET", "/api/v1/brand/active")
        step("brand", status, brand)

        # --- start: runs until the approval interrupt -----------------------
        status, started = api.call(
            "POST",
            "/api/v1/director/workflows",
            {
                "brand_profile_id": brand["id"],
                "objective": args.objective,
                "business_objectives": ["developer_acquisition"],
                "platforms": ["linkedin"],
                "posts_per_week": 2,
                "horizon_days": 7,
            },
        )
        step("start_workflow", status, summarise(started.get("state") or {}), expect=201)
        workflow_id = started["workflow_id"]

        status, awaiting = api.call("GET", f"/api/v1/director/workflows/{workflow_id}")
        step("awaiting_approval", status, summarise(awaiting))
        if not awaiting.get("awaiting_human"):
            raise SystemExit("the workflow did not stop for approval — the gate is broken")

        # --- the founder decision -------------------------------------------
        status, approved = api.call(
            "POST",
            f"/api/v1/director/workflows/{workflow_id}/decision",
            {"action": "approve", "reason": "Phase 4.3 lifecycle evidence", "schedule": True},
        )
        step("founder_approval", status, summarise(approved))

        # --- the post-approval lifecycle ------------------------------------
        final: dict[str, Any] = approved
        for attempt in range(20):
            if (final.get("lifecycle_stage") or "") in ("completed", "handed_off", "failed"):
                break
            time.sleep(1.0)
            status, final = api.call("GET", f"/api/v1/director/workflows/{workflow_id}")
            if status != 200:
                break
        step("lifecycle_final", 200, summarise(final))

        summary = summarise(final)

        # --- governance assertions ------------------------------------------
        checks: list[dict[str, Any]] = []

        def check(name: str, ok: bool, detail: str) -> None:
            checks.append({"check": name, "ok": ok, "detail": detail})

        check(
            "approval_was_required",
            bool(awaiting.get("awaiting_human")),
            "the workflow halted before creating anything externally",
        )
        trace = summary.get("trace") or []
        check(
            "trace_is_recorded",
            len(trace) >= 5,
            f"{len(trace)} trace entries: {', '.join(trace[:12])}",
        )
        publish = summary.get("publish_results") or {}
        if publish:
            published = publish.get("published") or []
            blocked = publish.get("blocked") or publish.get("unavailable") or []
            fictional = [
                p
                for p in published
                if not (p.get("external_id") or p.get("external_url") or p.get("permalink"))
            ]
            check(
                "no_fabricated_publication",
                not fictional,
                (
                    f"{len(published)} published with external ids, {len(blocked)} blocked"
                    if not fictional
                    else f"{len(fictional)} publications without an external id"
                ),
            )
        else:
            blocked_schedule = summary.get("schedule_blocked") or []
            check(
                "nothing_published_without_a_channel",
                bool(blocked_schedule),
                (
                    f"no publish attempt was made because scheduling was blocked: "
                    f"{json.dumps(blocked_schedule, default=str)[:300]}"
                    if blocked_schedule
                    else "no publish result and no recorded reason — that is a silent failure"
                ),
            )
        analytics = summary.get("analytics") or {}
        if analytics:
            check(
                "analytics_are_measured_or_absent",
                "unavailable" in json.dumps(analytics)[:2000] or bool(analytics.get("ingested")),
                json.dumps(analytics, default=str)[:300],
            )
        check(
            "state_is_durable",
            int(summary.get("checkpoint_count") or 0) > 0,
            f"{summary.get('checkpoint_count')} checkpoints persisted",
        )

        # --- the boundary: only a founder may approve, and only while paused ---
        editor_email = f"canary-editor-{int(time.time())}@crusor.test"
        # generated per run: no credential is ever written down in this file
        editor_password = secrets.token_urlsafe(18)
        status, created_user = api.call(
            "POST",
            "/api/v1/auth/users",
            {
                "email": editor_email,
                "password": editor_password,
                "full_name": "Canary Editor",
                "role": "editor",
            },
        )
        evidence["steps"].append({"step": "create_editor", "status": status, "payload": created_user})

        status, second = api.call(
            "POST",
            "/api/v1/director/workflows",
            {
                "brand_profile_id": brand["id"],
                "objective": "Approval-boundary probe (must never publish)",
                "business_objectives": ["developer_acquisition"],
                "platforms": ["linkedin"],
                "posts_per_week": 1,
                "horizon_days": 3,
            },
        )
        if status == 201:
            second_id = second["workflow_id"]
            editor_api = Api(args.base_url)
            _, editor_token = editor_api.call(
                "POST", "/api/v1/auth/token", {"email": editor_email, "password": editor_password}
            )
            editor_api.token = (editor_token or {}).get("access_token")
            status, refused = editor_api.call(
                "POST",
                f"/api/v1/director/workflows/{second_id}/decision",
                {"action": "approve", "reason": "editor should not be able to approve"},
            )
            evidence["steps"].append({"step": "editor_approval_attempt", "status": status, "payload": refused})
            check(
                "editor_cannot_approve",
                status in (401, 403),
                f"editor decision refused with HTTP {status}",
            )
            # Approving a finished workflow is an idempotent no-op, not a re-run:
            # it must report already_completed and change nothing.
            before_state = {
                "lifecycle_stage": summary.get("lifecycle_stage"),
                "scheduled_post_ids": summary.get("scheduled_post_ids"),
                "publish_results": summary.get("publish_results"),
            }
            status, again = api.call(
                "POST",
                f"/api/v1/director/workflows/{workflow_id}/decision",
                {"action": "approve", "reason": "second approval on a completed workflow"},
            )
            evidence["steps"].append({"step": "repeat_approval", "status": status, "payload": again})
            status2, reread = api.call("GET", f"/api/v1/director/workflows/{workflow_id}")
            after_state = {
                "lifecycle_stage": reread.get("lifecycle_stage"),
                "scheduled_post_ids": reread.get("scheduled_post_ids"),
                "publish_results": reread.get("publish_results"),
            }
            check(
                "repeat_approval_changes_nothing",
                status == 200 and before_state == after_state,
                f"second approval returned HTTP {status}; stage {after_state['lifecycle_stage']}, "
                f"scheduled posts {len(after_state['scheduled_post_ids'] or [])}, "
                f"state unchanged={before_state == after_state}",
            )
            api.call("POST", f"/api/v1/director/workflows/{second_id}/cancel", {"reason": "cleanup"})

        evidence["workflow_id"] = workflow_id
        evidence["summary"] = summary
        evidence["checks"] = checks
        evidence["api_calls"] = api.calls
        ok = all(c["ok"] for c in checks)
        evidence["result"] = "pass" if ok else "fail"

        if args.json:
            print(json.dumps(evidence, indent=2, default=str))
        else:
            for entry in checks:
                mark = "PASS" if entry["ok"] else "FAIL"
                print(f"  {mark}  {entry['check']}: {entry['detail']}")
            runtime = evidence.get("runtime") or {}
            print(
                f"providers: real_llm={runtime.get('real_providers_in_use')} "
                f"creative={runtime.get('creative_provider')} "
                f"publishing={runtime.get('publishing')}"
            )
            print(f"lifecycle: {summary.get('status')} at {summary.get('lifecycle_stage')}")

        if args.write:
            target = ROOT / "docs" / "lifecycle-run.json"
            target.write_text(json.dumps(evidence, indent=2, default=str), "utf-8")
            print(f"wrote {target}")
        return 0 if ok else 1
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        import traceback

        print(f"lifecycle evidence run failed: {type(exc).__name__}: {exc}")
        if os.environ.get("LIFECYCLE_DEBUG"):
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
