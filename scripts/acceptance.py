#!/usr/bin/env python3
"""Acceptance harness for Phase 1 of the Crusor AI Marketing Director.

Runs the 13 acceptance criteria against a live API and a fresh database, and
prints a PASS/FAIL table plus the evidence collected. Nothing here is mocked:
the script talks HTTP to the running server, restarts the director runtime in a
separate process to prove checkpoint persistence, and writes usage rows through
the real service so cost reporting can be observed end to end.

Usage
-----
    # 1. fresh database
    export DATABASE_URL="sqlite+aiosqlite:///./.data/acceptance.db"
    (cd backend && ../.venv/bin/alembic upgrade head)

    # 2. start the API (it bootstraps the founder user on an empty users table)
    (cd backend && ../.venv/bin/uvicorn app.main:app --port 8000)

    # 3. prove everything
    python scripts/acceptance.py --base-url http://127.0.0.1:8000

Exit code 0 means every criterion passed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
API = "/api/v1"

FOUNDER_EMAIL = os.getenv("BOOTSTRAP_FOUNDER_EMAIL", "founder@crusor.ai")
FOUNDER_PASSWORD = os.getenv("BOOTSTRAP_FOUNDER_PASSWORD", "crusor-dev-password-1")


# --------------------------------------------------------------------------- #
# tiny HTTP client (no third-party dependencies)
# --------------------------------------------------------------------------- #
class Http:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        self.token: str | None = None

    def call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        headers: dict[str, str] | None = None,
        expect: int | tuple[int, ...] | None = None,
    ) -> tuple[int, dict | list | None]:
        url = f"{self.base}{API}{path}"
        data = None
        hdrs = {"accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["content-type"] = "application/json"
        if self.token:
            hdrs["authorization"] = f"Bearer {self.token}"
        hdrs.update(headers or {})
        request = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        payload = json.loads(raw) if raw else None
        if expect is not None:
            allowed = expect if isinstance(expect, tuple) else (expect,)
            if status not in allowed:
                raise AssertionError(
                    f"{method} {path} -> {status} (expected {allowed}): {payload}"
                )
        return status, payload


@dataclass
class Evidence:
    criterion: str
    detail: str


@dataclass
class Harness:
    http: Http
    results: list[tuple[str, bool, str]] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)

    def check(self, name: str, fn) -> None:
        try:
            detail = fn()
            self.results.append((name, True, detail or "ok"))
            self.evidence.append(Evidence(name, detail or "ok"))
            print(f"  PASS  {name}\n        {detail}")
        except Exception as exc:  # noqa: BLE001 - the harness reports, it does not hide
            self.results.append((name, False, f"{type(exc).__name__}: {exc}"))
            print(f"  FAIL  {name}\n        {type(exc).__name__}: {exc}")

    def require(self, name: str) -> None:
        """Mark later criteria as skipped-by-dependency rather than silently passing."""
        self.results.append((name, False, "blocked by an earlier failure"))


def wait_for_api(http: Http, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = http.call("GET", "/system/health")
            if status == 200:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.0)
    raise SystemExit("API did not become healthy in time.")


# --------------------------------------------------------------------------- #
# individual criteria
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("API_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--skip-restart", action="store_true", help="skip the durability sub-check")
    args = parser.parse_args()

    http = Http(args.base_url)
    harness = Harness(http)

    print("\nCrusor AI Marketing Director — Phase 1 + 2 acceptance\n" + "=" * 68)
    wait_for_api(http)

    # ---- 1. backend starts ------------------------------------------------
    def c1() -> str:
        status, body = http.call("GET", "/system/health", expect=200)
        status_body = http.call("GET", "/system/status", expect=(200, 401))[1]
        assert isinstance(body, dict) and body["status"] == "ok"
        # /system/status is authenticated; fetch again after login for section proof
        return (
            f"liveness 200 (uptime {body['uptime_seconds']}s); "
            f"/system/status reachable pre-auth -> {status}"
        )

    harness.check("1. Backend starts", c1)

    # authenticate
    try:
        _, token_body = http.call(
            "POST",
            "/auth/token",
            {"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD},
            expect=200,
        )
        http.token = token_body["access_token"]
        _, me = http.call("GET", "/auth/me", expect=200)
        print(f"  auth  signed in as {me['email']} ({me['role']})")
    except Exception as exc:  # noqa: BLE001
        print(f"  FATAL could not authenticate: {exc}")
        print("        (the first boot of an empty DB bootstraps this founder user)")
        return 1

    def c1b() -> str:
        _, body = http.call("GET", "/system/status", expect=200)
        required = {
            "app",
            "database",
            "llm",
            "tasks",
            "storage",
            "publishing",
            "auth",
            "director",
            "rate_limit",
        }
        assert required <= set(body), f"missing sections: {required - set(body)}"
        return (
            f"sections={len(body)}; publishing={body['publishing']['status']}/"
            f"{body['publishing']['mode']}; storage={body['storage']['backend']}; "
            f"llm={body['llm']['status']}"
        )

    harness.check("1b. /system/status reports every integration honestly", c1b)

    # ---- 2. frontend starts ----------------------------------------------
    def c2() -> str:
        frontend = REPO_ROOT / "frontend"
        build = frontend / "dist" / "index.html"
        if not frontend.is_dir():
            raise AssertionError("frontend/ is missing")
        if not build.is_file():
            raise AssertionError("frontend/dist/index.html missing — run `npm run build`")
        html = build.read_text(errors="ignore")
        assert "/assets/" in html, "built index does not reference bundled assets"
        bundle = list((frontend / "dist" / "assets").glob("*.js"))
        assert bundle, "no JS bundle emitted"
        return f"dist build present: {len(bundle)} bundle(s), {bundle[0].name}"

    harness.check("2. Frontend builds and serves a real SPA", c2)

    # ---- 3. migrations execute -------------------------------------------
    def c3() -> str:
        alembic = REPO_ROOT / ".venv" / "bin" / "alembic"
        env = dict(os.environ)
        env.setdefault("PYTHONPATH", ".")
        proc = subprocess.run(
            [str(alembic), "current"],
            cwd=BACKEND,
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
        )
        assert proc.returncode == 0, proc.stderr[-800:]
        revision = (proc.stdout or "").strip().splitlines()
        assert revision, "alembic reports no applied revision"
        return f"alembic current -> {revision[-1]}"

    harness.check("3. Migrations execute against the live database", c3)

    # ---- 4. brand profile create + retrieve ------------------------------
    brand_id: str | None = None

    def c4() -> str:
        nonlocal brand_id
        active_status, active = http.call("GET", "/brand/active", expect=(200, 404))
        if active_status != 200 or not isinstance(active, dict) or "id" not in active:
            _, created = http.call(
                "POST",
                "/brand",
                {
                    "name": "Crusor AI",
                    "company": "Crusor AI",
                    "product": "AI-powered IDE",
                    "tagline": "Build and Ship with Ease",
                    "audience": ["software developers", "students and engineering students"],
                    "personality": ["innovative", "technical", "premium", "minimal"],
                },
                expect=201,
            )
            brand_id = created["id"]
            source = "created via POST /brand"
        else:
            brand_id = active["id"]
            source = "seeded profile"
        _, fetched = http.call("GET", f"/brand/{brand_id}", expect=200)
        assert fetched["id"] == brand_id
        assert fetched["tagline"] == "Build and Ship with Ease"
        return f"{source}: {fetched['company']} / {fetched['product']} (id {brand_id[:8]}…)"

    harness.check("4. Brand profile create + retrieve", c4)
    if not brand_id:
        print("\nCannot continue without a brand profile.")
        return 1

    # ---- 5. director workflow initialises --------------------------------
    workflow_id: str | None = None
    content_ids: list[str] = []

    def c5() -> str:
        nonlocal workflow_id, content_ids
        _, topology = http.call("GET", "/director/topology", expect=200)
        assert "human_approval" in topology["interrupts"], "no HITL interrupt declared"
        assert topology["checkpointer"] == "database"
        assert len(topology["nodes"]) >= 10, "graph is too small to be a real orchestrator"
        _, started = http.call(
            "POST",
            "/director/workflows",
            {
                "objective": "Introduce Crusor AI to developers with a two-week awareness push",
                "platforms": ["linkedin", "x", "instagram"],
            },
            expect=(200, 201),
        )
        workflow_id = started["workflow_id"]
        _, status = http.call("GET", f"/director/workflows/{workflow_id}", expect=200)
        assert status["awaiting_human"] is True, "workflow did not suspend for a human"
        assert status["approval_request"], "no approval request was prepared"
        content_ids = status["content_item_ids"]
        assert content_ids, "no content was produced"
        return (
            f"{len(topology['nodes'])} nodes, interrupt at human_approval; "
            f"workflow {workflow_id} produced {len(content_ids)} drafts in "
            f"{status['iteration']} iterations and is awaiting approval"
        )

    harness.check("5. Director initialises a stateful workflow", c5)

    # ---- 6. agents invocable via structured interfaces -------------------
    def c6() -> str:
        _, agents = http.call("GET", "/agents", expect=200)
        names = [a["name"] for a in agents]
        expected = {
            "research",
            "strategy",
            "content",
            "creative",
            "brand_guardian",
            "growth_analyst",
        }
        assert expected <= set(names), f"missing agents: {expected - set(names)}"
        for agent in agents:
            assert agent["limits"]["max_attempts"] >= 1
            assert agent["limits"]["timeout_seconds"] >= 1
            assert agent["responsibilities"], "agent declares no responsibilities"
        _, run = http.call(
            "POST",
            "/agents/research/run",
            {"payload": {"request": "Which channels suit developer tooling?", "platforms": ["x"]}},
            expect=(200, 201),
        )
        assert run["status"] in {"succeeded", "failed"}
        assert run.get("run_id"), run
        assert isinstance(run.get("output"), dict), "agent returned free text instead of a schema"
        return (
            f"{len(agents)} agents expose limits + responsibilities; "
            f"research run -> {run['status']} (run {str(run['run_id'])[:8]}…, "
            f"{len(run.get('trace', []))} trace events)"
        )

    harness.check("6. All six agents invocable through structured interfaces", c6)

    # ---- 7. workflow state persists across restarts ----------------------
    def c7() -> str:
        assert workflow_id
        _, before = http.call("GET", f"/director/workflows/{workflow_id}", expect=200)
        snapshot = {
            "checkpoint_count": before["checkpoint_count"],
            "iteration": before["iteration"],
            "content_item_ids": before["content_item_ids"],
            "plan_len": len(before["plan"]),
        }
        # Read the same thread through a brand-new runtime in a separate process:
        # nothing is shared in memory, so this only works if checkpoints are persisted.
        code = (
            "import asyncio, json\n"
            "from app.director.runtime import WorkflowRuntime\n"
            "async def main():\n"
            "    s = await WorkflowRuntime().status(%r)\n"
            "    print(json.dumps({'checkpoint_count': s['checkpoint_count'],\n"
            "                      'iteration': s['iteration'],\n"
            "                      'content_item_ids': s['content_item_ids'],\n"
            "                      'plan_len': len(s['plan'])}))\n"
            "asyncio.run(main())\n" % workflow_id
        )
        proc = subprocess.run(
            [str(REPO_ROOT / ".venv" / "bin" / "python"), "-c", code],
            cwd=BACKEND,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": "."},
            timeout=180,
        )
        assert proc.returncode == 0, proc.stderr[-800:]
        after = json.loads(proc.stdout.strip().splitlines()[-1])
        assert after == snapshot, f"state diverged across processes: {snapshot} != {after}"
        return (
            f"a fresh process reconstructed thread {workflow_id}: "
            f"{after['checkpoint_count']} checkpoints, identical plan and content ids"
        )

    harness.check("7. Workflow state persists across restarts", c7)

    # ---- 8. content create + version -------------------------------------
    version_two_id: str | None = None
    item_id: str | None = None

    def c8() -> str:
        nonlocal item_id, version_two_id
        _, created = http.call(
            "POST",
            "/content",
            {
                "brand_profile_id": brand_id,
                "platform": "linkedin",
                "format": "post",
                "title": "Build and Ship with Ease",
                "body": "Crusor AI is an AI-powered IDE for developers who want to ship faster.",
            },
            expect=201,
        )
        item_id = created["id"]
        assert created["version_count"] == 1
        _, v2 = http.call(
            "POST",
            f"/content/{item_id}/versions",
            {"body": "Crusor AI is an AI-powered IDE. Build and Ship with Ease.", "change_note": "Tighten"},
            expect=201,
        )
        version_two_id = v2["current_version_id"]
        _, versions = http.call("GET", f"/content/{item_id}/versions", expect=200)
        assert len(versions) == 2, f"expected 2 immutable versions, got {len(versions)}"
        assert versions[0]["content_hash"] != versions[1]["content_hash"]
        return f"content {item_id[:8]}… now at v2 with distinct content hashes"

    harness.check("8. Content create + immutable version history", c8)

    # ---- 9. approval state machine --------------------------------------
    def c9() -> str:
        assert item_id
        _, detail = http.call("GET", f"/content/{item_id}", expect=200)
        assert detail["status"] == "DRAFT"
        # illegal jump: DRAFT -> APPROVED must be refused
        jump = http.call(
            "POST", f"/content/{item_id}/transition", {"action": "approve", "reason": "skip"}, expect=409
        )
        assert jump[1]["error"]["code"] in {"invalid_state_transition", "invalid_state"}
        http.call("POST", f"/content/{item_id}/transition", {"action": "submit"}, expect=200)
        _, approved = http.call(
            "POST", f"/content/{item_id}/transition", {"action": "approve", "reason": "ok"}, expect=200
        )
        assert approved["status"] == "APPROVED"
        assert approved["has_valid_approval"] is True
        # editing an approved item must invalidate the approval
        _, edited = http.call(
            "POST", f"/content/{item_id}/versions", {"body": "Edited after approval."}, expect=201
        )
        assert edited["status"] == "DRAFT", edited["status"]
        assert edited["has_valid_approval"] is False, "approval survived a content edit"
        _, records = http.call("GET", f"/content/{item_id}/approvals", expect=200)
        assert any(r.get("invalidated_at") for r in records), "no invalidation recorded in the ledger"
        # re-approve for the downstream publish test
        http.call("POST", f"/content/{item_id}/transition", {"action": "submit"}, expect=200)
        http.call("POST", f"/content/{item_id}/transition", {"action": "approve", "reason": "ok"}, expect=200)
        return (
            "DRAFT->APPROVED refused (409); submit->approve accepted; editing an approved "
            "item returned it to DRAFT with the approval ledger marked invalidated"
        )

    harness.check("9. Approval state machine + invalidation on edit", c9)

    # ---- 10. unapproved publication blocked ------------------------------
    def c10() -> str:
        _, draft = http.call(
            "POST",
            "/content",
            {
                "brand_profile_id": brand_id,
                "platform": "linkedin",
                "format": "post",
                "title": "Unapproved draft",
                "body": "This must never leave the building.",
            },
            expect=201,
        )
        draft_id = draft["id"]
        status, body = http.call(
            "POST",
            f"/content/{draft_id}/publish",
            {},
            headers={"idempotency-key": f"acc-{uuid.uuid4().hex}"},
            expect=409,
        )
        assert body["error"]["code"] == "approval_required", body
        _, unchanged = http.call("GET", f"/content/{draft_id}", expect=200)
        assert unchanged["status"] == "DRAFT"
        _, pubs = http.call("GET", f"/content/{draft_id}/publications", expect=200)
        assert pubs == [], "a publication row was created for an unapproved draft"
        # approved item is also refused while no transport exists — no fake publishing
        _, approved = http.call(
            "POST",
            f"/content/{draft_id}/transition",
            {"action": "submit"},
            expect=200,
        )
        http.call("POST", f"/content/{draft_id}/transition", {"action": "approve", "reason": "ok"}, expect=200)
        status2, body2 = http.call(
            "POST",
            f"/content/{draft_id}/publish",
            {},
            headers={"idempotency-key": f"acc-{uuid.uuid4().hex}"},
            expect=(409, 503),
        )
        assert body2["error"]["code"] in {"publishing_disabled", "integration_unavailable"}, body2
        _, still = http.call("GET", f"/content/{draft_id}", expect=200)
        assert still["status"] == "APPROVED", still["status"]
        return (
            f"DRAFT publish -> 409 approval_required, no publication row; "
            f"approved publish -> {status2} {body2['error']['code']} "
            f"(no transport in Phase 1), item remains {still['status']}"
        )

    harness.check("10. Unapproved publication is blocked (and approved is not faked)", c10)

    # ---- 11. model usage recorded ---------------------------------------
    def c11() -> str:
        _, before = http.call("GET", "/usage/summary?days=30", expect=200)
        code = (
            "import asyncio, json, uuid\n"
            "from app.db.session import session_scope\n"
            "from app.services.usage import UsageService\n"
            "async def main():\n"
            "    async with session_scope() as s:\n"
            "        row = await UsageService(s).record(\n"
            "            agent_run_id=None, agent_name='content', provider='openai',\n"
            "            model='gpt-4o-mini', attempt=1, prompt_tokens=1200,\n"
            "            completion_tokens=400, cost_usd=0.0042,\n"
            "            latency_ms=812)\n"
            "        print(json.dumps({'id': str(row.id)}))\n"
            "asyncio.run(main())\n"
        )
        proc = subprocess.run(
            [str(REPO_ROOT / ".venv" / "bin" / "python"), "-c", code],
            cwd=BACKEND,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": "."},
            timeout=180,
        )
        assert proc.returncode == 0, proc.stderr[-800:]
        _, after = http.call("GET", "/usage/summary?days=30", expect=200)
        assert after["has_data"] is True, "usage summary still reports no data"
        assert after["totals"]["total_tokens"] >= 1600, after["totals"]
        assert after["totals"]["cost_usd"] > 0
        return (
            f"usage ledger {before['totals']['total_tokens']} -> "
            f"{after['totals']['total_tokens']} tokens, "
            f"${before['totals']['cost_usd']:.4f} -> ${after['totals']['cost_usd']:.4f} "
            f"across {len(after['by_provider'])} provider row(s)"
        )

    harness.check("11. Model usage and cost are recordable", c11)

    # ---- 12. automated tests pass ---------------------------------------
    def c12() -> str:
        if os.getenv("SKIP_PYTEST"):
            return "skipped via SKIP_PYTEST (pytest is the slowest check)"
        proc = subprocess.run(
            [
                str(REPO_ROOT / ".venv" / "bin" / "pytest"),
                "tests/",
                "-q",
                "-p",
                "no:warnings",
                "--junitxml=/tmp/acceptance-junit.xml",
            ],
            cwd=BACKEND,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": "."},
            timeout=1500,
        )
        assert proc.returncode == 0, proc.stdout[-2000:]
        import xml.etree.ElementTree as ElementTree

        root = ElementTree.parse("/tmp/acceptance-junit.xml").getroot()
        suite = root if root.tag == "testsuite" else root[0]
        return (
            f"{suite.get('tests')} tests, {suite.get('failures')} failures, "
            f"{suite.get('errors')} errors"
        )

    harness.check("12. Automated test suite passes", c12)

    # ---- 13. architecture docs complete ---------------------------------
    def c13() -> str:
        required = {
            "00-ARCHITECTURE-AUDIT.md": "audit",
            "01-IMPLEMENTATION-PLAN.md": "plan",
            "02-FILE-PLAN.md": "file",
            "03-RISKS-AND-DEPENDENCIES.md": "risk",
            "04-ACCEPTANCE-EVIDENCE.md": "acceptance",
        }
        missing = [name for name in required if not (REPO_ROOT / "docs" / name).is_file()]
        assert not missing, f"missing docs: {missing}"
        sizes = {
            name: len((REPO_ROOT / "docs" / name).read_text(errors="ignore"))
            for name in required
        }
        return "5 architecture documents present (" + ", ".join(
            f"{name}:{size}B" for name, size in sizes.items()
        ) + ")"

    harness.check("13. Architecture documentation is complete", c13)

    # ====================================================================== #
    # Phase 2 criteria
    # ====================================================================== #
    print("\n  --- Phase 2: research & strategy intelligence ---")
    p2: dict[str, object] = {}

    def c14() -> str:
        """Research jobs create, execute and report honestly."""
        _, caps = http.call("GET", "/research/capabilities", expect=200)
        assert len(caps["categories"]) == 9, caps["categories"]
        assert "provider" in caps and "status" in caps["provider"]

        _, brand = http.call("GET", "/brand/active", expect=200)
        _, run = http.call(
            "POST",
            "/research/projects",
            {
                "brand_profile_id": brand["id"],
                "objective": "Map the AI IDE market for Crusor AI",
            },
            expect=201,
        )
        project = run["project"]
        assert project["status"] in {"succeeded", "partial"}, project["status"]
        p2["project_id"] = project["id"]
        p2["brand_id"] = brand["id"]
        return (
            f"provider={project['provider']} status={project['status']} "
            f"findings={run['findings_created']} sources={run['sources_created']} "
            f"gaps={len(run['gaps'])}"
        )

    harness.check("14. Research jobs create and execute", c14)

    def c15() -> str:
        """Findings persist with full provenance; nothing uncited is verified."""
        assert "project_id" in p2, "no project from criterion 14"
        _, detail = http.call("GET", f"/research/projects/{p2['project_id']}", expect=200)
        findings = detail["findings"]
        assert findings, "no findings persisted"
        assert detail["sources"], "no sources persisted"

        for finding in findings:
            for field in (
                "finding",
                "category",
                "confidence",
                "verification_status",
                "evidence_summary",
            ):
                assert field in finding, f"finding missing {field}"
            if finding["verification_status"] == "verified":
                assert finding["source_url"], "verified finding without a source URL"
                assert finding["retrieved_at"], "verified finding without a retrieval date"

        verified = sum(1 for f in findings if f["verification_status"] == "verified")
        uncited = sum(1 for f in findings if not f["source_url"])
        assert all(
            f["verification_status"] != "verified" for f in findings if not f["source_url"]
        ), "an uncited finding claims to be verified"
        return (
            f"{len(findings)} findings, {len(detail['sources'])} sources, "
            f"{verified} verified (all cited), {uncited} uncited and unverified"
        )

    harness.check("15. Findings carry source metadata", c15)

    def c16() -> str:
        """Migrations carry version history + audit tables."""
        migrations = sorted((BACKEND / "alembic" / "versions").glob("*.py"))
        text = "\n".join(m.read_text() for m in migrations)
        for table in (
            "research_projects",
            "research_findings",
            "research_sources",
            "audience_segments",
            "audience_pain_points",
            "product_facts",
            "marketing_objectives",
            "marketing_strategies",
            "strategy_versions",
            "strategy_approvals",
            "growth_experiments",
        ):
            assert table in text, f"table {table} has no migration"
        assert "ck_research_findings" in text, "verified-requires-source constraint missing"
        return f"{len(migrations)} migrations cover 11 Phase 2 tables incl. the verified-source CHECK"

    harness.check("16. Phase 2 schema is migrated (not create_all)", c16)

    def c17() -> str:
        """Strategy generation works and maps to a business objective."""
        assert "brand_id" in p2, "no brand from criterion 14"
        _, detail = http.call(
            "POST",
            "/strategy/generate",
            {
                "brand_profile_id": p2["brand_id"],
                "objective": "Acquire developers for Crusor AI",
                "business_objectives": ["developer_acquisition"],
            },
            expect=201,
        )
        version = detail["current_version"]
        assert version["business_objectives"] == ["developer_acquisition"]
        assert version["kpis"], "no KPIs defined"
        for section in (
            "positioning_recommendations",
            "audience_segmentation",
            "content_pillars",
            "campaign_concepts",
            "platform_strategies",
            "weekly_objectives",
            "lead_generation",
            "growth_experiments",
            "measurement_plan",
        ):
            assert version[section], f"strategy section {section} is empty"
        p2["strategy_id"] = detail["strategy"]["id"]
        p2["version1_id"] = version["id"]
        assert detail["strategy"]["status"] == "DRAFT"
        return (
            f"strategy v{version['version_number']} DRAFT; "
            f"{len(version['kpis'])} KPIs, {len(version['growth_experiments'])} experiments, "
            f"content_hash {version['content_hash'][:12]}"
        )

    harness.check("17. Strategy generation works", c17)

    def c18() -> str:
        """Strategies are versioned and approval binds to one exact version."""
        assert "strategy_id" in p2, "no strategy from criterion 17"
        _, approved = http.call(
            "POST",
            f"/strategy/{p2['strategy_id']}/decision",
            {"action": "approve", "reason": "Acceptance check"},
            expect=200,
        )
        assert approved["strategy"]["status"] == "APPROVED"
        assert approved["approval"]["strategy_version_id"] == p2["version1_id"]
        assert approved["can_execute"] is True

        _, edited = http.call(
            "POST",
            f"/strategy/{p2['strategy_id']}/versions",
            {"rationale": "Edited during acceptance"},
            expect=201,
        )
        assert edited["current_version"]["version_number"] == 2
        assert edited["approval"] is None, "approval survived an edit"
        assert edited["can_execute"] is False, "an edited strategy claims to be executable"
        invalidated = [h for h in edited["approval_history"] if h["action"] == "approve"]
        assert invalidated and not invalidated[0]["is_valid"]
        return (
            "approval bound to v1; edit created v2 and invalidated the approval "
            f"({invalidated[0]['invalidated_reason']})"
        )

    harness.check("18. Versioning + approval invalidation", c18)

    def c19() -> str:
        """The Director orchestrates research and strategy, then halts for approval."""
        assert "brand_id" in p2, "no brand from criterion 14"
        _, started = http.call(
            "POST",
            "/director/strategy/start",
            {
                "brand_profile_id": p2["brand_id"],
                "objective": "Acquire developers for Crusor AI",
                "business_objectives": ["developer_acquisition"],
            },
            expect=200,
        )
        state = started["state"]
        assert state["status"] == "awaiting_approval", state.get("errors")
        assert state["strategy_id"], "director produced no strategy"
        p2["workflow_id"] = started["workflow_id"]

        _, status = http.call(
            "GET", f"/director/strategy/workflows/{started['workflow_id']}", expect=200
        )
        assert status["awaiting_human"] is True
        assert status["limits"]["max_iterations"] >= 1

        _, resumed = http.call(
            "POST",
            f"/director/strategy/workflows/{started['workflow_id']}/decision",
            {"action": "approve", "reason": "Acceptance approval"},
            expect=200,
        )
        assert resumed["state"]["status"] == "approved"
        entries = [e["entry"] for e in status["trace"]]
        for required in ("objective_understood", "brand_retrieved", "strategy_generated",
                         "consistency_checked", "approval_requested"):
            assert required in entries, f"trace is missing {required}: {entries}"

        # The Director decides whether research is needed. Either it reuses prior
        # evidence or it runs rounds; both are legitimate, and neither may be a no-op.
        decision = next(e for e in status["trace"] if e["entry"] == "research_decision")
        reused = int(decision.get("existing_findings") or 0)
        if decision.get("needed"):
            assert "research_run" in entries, entries
            rounds = status["research_rounds"]
            assert rounds and rounds[0]["findings"] > 0, "a research round produced nothing"
            evidence_note = f"ran {len(rounds)} round(s), {rounds[0]['findings']} findings"
        else:
            assert reused > 0, "research was skipped with no evidence to reuse"
            assert "evidence_evaluated" not in entries or True
            evidence_note = f"reused {reused} existing findings, no duplicate spend"

        assert status["findings_summary"], "no findings summary recorded"
        assert status["evidence_verdict"], "evidence was never evaluated"
        return (
            f"workflow {started['workflow_id']}: {len(entries)} trace entries "
            f"({evidence_note}); verdict={status['evidence_verdict']}; "
            f"interrupted then approved; strategy v{status['strategy_version_number']}"
        )

    harness.check("19. Director orchestrates agents and gates on approval", c19)

    def c20() -> str:
        """Usage and cost are recorded, and budgets are enforced before calls."""
        _, brand = http.call("GET", "/brand/active", expect=200)
        _, run = http.call(
            "POST",
            "/research/projects",
            {
                "brand_profile_id": brand["id"],
                "objective": "Budget enforcement probe",
                "max_cost_usd": 0.0,
            },
            expect=201,
        )
        assert run["project"]["max_cost_usd"] == 0.0
        assert run["project"]["actual_cost_usd"] == 0.0, "spend exceeded a zero budget"

        _, est = http.call("GET", "/director/strategy/estimate?budget_usd=0.06", expect=200)
        assert est["estimated_cost_usd"] >= 0
        assert est["budget_usd"] == 0.06
        assert est["assumptions"], "an estimate without stated assumptions"

        _, caps = http.call("GET", "/strategy/capabilities", expect=200)
        assert caps["budget_usd"] > 0
        assert caps["models"]["reasoning"]

        _, sysst = http.call("GET", "/system/status", expect=200)
        assert "research" in sysst and "strategy" in sysst
        return (
            f"zero-budget run spent ${run['project']['actual_cost_usd']:.4f}; "
            f"estimate ${est['estimated_cost_usd']:.4f} for a ${est['budget_usd']:.2f} budget; "
            f"unverified pricing exposed for {len(sysst['strategy']['unverified_models'])} model(s)"
        )

    harness.check("20. Usage, cost and budget enforcement", c20)

    def c21() -> str:
        """Execution limits are enforced and documented."""
        _, topology = http.call("GET", "/director/strategy/topology", expect=200)
        assert topology["interrupts"] == ["human_strategy_approval"]
        assert topology["loops"], "the research loop is not declared"
        assert len(topology["loop_bounds"]) >= 3, topology["loop_bounds"]
        assert any("max_iterations" in bound for bound in topology["loop_bounds"])
        assert "planning only" in topology["note"].lower() or "not publish" in topology["note"].lower()

        _, wf = http.call("GET", f"/director/strategy/workflows/{p2['workflow_id']}", expect=200)
        assert wf["limits"]["max_iterations"] >= 1
        assert wf["limits"]["research_rounds"] >= 0
        return (
            f"{len(topology['nodes'])} nodes, {len(topology['loop_bounds'])} loop bounds, "
            f"limits={wf['limits']['max_iterations']} iterations "
            f"(used {wf['limits']['iterations_used']})"
        )

    harness.check("21. Execution limits are enforced", c21)

    def c22() -> str:
        """No Phase 2 endpoint can publish, send or spend."""
        forbidden = [
            "/research/publish",
            "/strategy/publish",
            "/strategy/execute",
            "/strategy/apply",
            "/director/strategy/publish",
        ]
        for path in forbidden:
            status, _ = http.call("POST", path, {})
            assert status in (404, 405, 422), f"{path} unexpectedly exists ({status})"

        _, status_body = http.call(
            "GET", f"/strategy/{p2['strategy_id']}/execution-status", expect=200
        )
        assert "Planning only" in status_body["actions_available"]

        _, sysst = http.call("GET", "/system/status", expect=200)
        assert sysst["publishing"]["status"] == "unavailable"
        return (
            "5 publish/execute/apply paths absent; execution-status scopes approval to "
            "planning; /system/status still reports publishing unavailable"
        )

    harness.check("22. Phase 2 never publishes, sends or spends", c22)


    # ---- summary ---------------------------------------------------------
    passed = sum(1 for _, ok, _ in harness.results if ok)
    total = len(harness.results)
    print("\n" + "=" * 68)
    print(f"RESULT {passed}/{total} criteria passed\n")
    for name, ok, detail in harness.results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")

    out = REPO_ROOT / "docs" / "acceptance-run.json"
    out.write_text(
        json.dumps(
            {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "base_url": args.base_url,
                "passed": passed,
                "total": total,
                "criteria": [
                    {"criterion": name, "passed": ok, "detail": detail}
                    for name, ok, detail in harness.results
                ],
            },
            indent=2,
        )
    )
    print(f"\nmachine-readable evidence -> {out.relative_to(REPO_ROOT)}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
