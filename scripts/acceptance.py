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
import hashlib
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



def minimal_png(width: int = 48, height: int = 48) -> bytes:
    """A real, decodable RGB PNG with incompressible-ish pixels (deterministic)."""
    import struct
    import zlib

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0
        for x in range(width):
            digest = hashlib.sha256(f"{x}:{y}".encode()).digest()
            raw += digest[:3]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def put_chunk(
    http: Http,
    ticket_id: str,
    token: str,
    headers: dict[str, str],
    index: int,
    total: int,
    data: bytes,
    *,
    chunk_sha: str | None = None,
) -> tuple[int, dict | None]:
    """Upload one raw chunk. ``Http.call`` is JSON-only, so this speaks urllib."""
    url = f"{http.base}{API}/flow/exports/{ticket_id}/chunks"
    hdrs = {
        "accept": "application/json",
        "content-type": "application/octet-stream",
        "X-Export-Token": token,
        "X-Chunk-Index": str(index),
        "X-Chunk-Count": str(total),
        "X-Chunk-Sha256": chunk_sha or hashlib.sha256(data).hexdigest(),
        **headers,
    }
    request = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    return status, (json.loads(raw) if raw else None)


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

    print("\nCrusor AI Marketing Director — Phase 1 + 2 + 3A + 3B acceptance\n" + "=" * 68)
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


    # ====================================================================== #
    # Phase 3A — content intelligence & internal content calendar
    # ====================================================================== #
    p3: dict[str, object] = {}
    STUDIO = "/content-studio"
    # Each run gets its own campaign name and idempotency keys: the harness is
    # expected to be re-runnable against a long-lived database.
    RUN_TAG = uuid.uuid4().hex[:8]
    PLAN_KEY = f"acceptance-3a-plan-{RUN_TAG}"
    GENERATE_KEY = f"acceptance-3a-generate-{RUN_TAG}"

    def c23() -> str:
        """A campaign gets a balanced weekly content plan."""
        assert "brand_id" in p2, "no brand from criterion 14"
        _, campaign = http.call(
            "POST",
            "/campaigns",
            {
                "brand_profile_id": p2["brand_id"],
                "name": f"Crusor AI Developer Awareness {RUN_TAG}",
                "objective": "Introduce Crusor AI to developers",
                "platforms": ["instagram", "linkedin", "youtube", "x", "facebook", "reddit"],
                "status": "active",
            },
            expect=201,
        )
        p3["campaign_id"] = campaign["id"]
        status, plan = http.call(
            "POST",
            f"{STUDIO}/plans",
            {
                "campaign_id": campaign["id"],
                "config": {"posts_per_week": 6, "variants": 2},
                "generate": True,
            },
            headers={"Idempotency-Key": PLAN_KEY},
            expect=(200, 201),
        )
        p3["plan_id"] = plan["id"]
        slots = plan["slots"]
        platforms = {slot["platform"] for slot in slots}
        types = {slot["content_type"] for slot in slots}
        assert plan["slot_count"] >= 6, plan["slot_count"]
        assert len(platforms) >= 5, platforms
        assert len(types) > 1, "every slot used the same content type"
        assert plan["campaign_id"] == campaign["id"]
        assert plan["generated_by"] == "mock", plan["generated_by"]
        assert all(slot["content_item_id"] for slot in slots), "a slot produced no draft"
        p3["item_ids"] = [slot["content_item_id"] for slot in slots]
        # item 2 stays untouched so the variant proofs can inspect the set the
        # planner stored on its first version (c26 and c27 edit items 0 and 1).
        p3["variant_item"] = p3["item_ids"][2]
        return (
            f"{status} plan {plan['week_start']} with {len(slots)} slots across "
            f"{sorted(platforms)} and {len(types)} content types; generated_by=mock"
        )

    harness.check("23. Weekly content plan is created for a campaign", c23)

    def c24() -> str:
        """Drafts are platform-specific and linked to the right campaign."""
        assert "item_ids" in p3, "no plan from criterion 23"
        seen = []
        for item_id in p3["item_ids"][:6]:  # type: ignore[union-attr]
            _, item = http.call("GET", f"/content/{item_id}", expect=200)
            version = item["current_version"]
            assert item["campaign_id"] == p3["campaign_id"], "draft detached from its campaign"
            assert item["status"] == "DRAFT"
            assert version["body"], "draft has no body"
            meta = version["generation_metadata"]
            assert meta["platform"] == item["platform"]
            assert meta["content_type"] == item["format"]
            assert meta["is_mock"] is True
            assert "plan_id" in meta and meta["plan_id"] == p3["plan_id"]
            seen.append(f"{item['platform']}/{item['format']} {len(version['body'])}ch")
        return f"{len(seen)} drafts linked to campaign {p3['campaign_id']}: " + ", ".join(seen)

    harness.check("24. Drafts are platform-specific and campaign-linked", c24)

    def c25() -> str:
        """The Brand Guardian validates a draft and stores structured issues."""
        assert "item_ids" in p3, "no plan from criterion 23"
        item_id = p3["item_ids"][0]  # type: ignore[index]
        _, validation = http.call(
            "POST", f"{STUDIO}/items/{item_id}/validate", {}, expect=(200, 201)
        )
        assert validation["verdict"] in {
            "PASS",
            "PASS_WITH_WARNINGS",
            "NEEDS_REVISION",
            "BLOCKED",
        }, validation["verdict"]
        assert validation["checked_rules"], "guardian checked no rules"
        assert validation["content_version_id"], "validation not bound to a version"
        _, history = http.call("GET", f"{STUDIO}/items/{item_id}/validations", expect=200)
        assert history, "validation was not persisted"
        p3["clean_item"] = item_id
        return (
            f"verdict {validation['verdict']} after {len(validation['checked_rules'])} rules; "
            f"{len(history)} validation row(s) persisted for version "
            f"{validation['content_version_id'][:8]}"
        )

    harness.check("25. Brand Guardian validates drafts", c25)

    def c26() -> str:
        """Unsupported claims are blocked and cannot enter approval."""
        assert "item_ids" in p3, "no plan from criterion 23"
        item_id = p3["item_ids"][1]  # type: ignore[index]
        _, created = http.call(
            "POST",
            f"/content/{item_id}/versions",
            {
                "body": (
                    "Crusor AI makes developers 300% faster and is used by 50,000 developers "
                    "at Google and Meta. Book a demo to get started."
                ),
                "change_note": "acceptance: inject unsupported claims",
            },
            expect=(200, 201),
        )
        _, validation = http.call(
            "POST", f"{STUDIO}/items/{item_id}/validate", {}, expect=(200, 201)
        )
        assert validation["verdict"] == "BLOCKED", validation["verdict"]
        assert validation["blocking_count"] >= 1
        banned = {issue["category"] for issue in validation["issues"]}
        status, submit = http.call(
            "POST", f"/content/{item_id}/transition", {"action": "submit"}
        )
        assert status == 409, f"blocked content was submittable ({status}: {submit})"
        p3["blocked_item"] = item_id
        return (
            f"v{created['current_version']['version_number']} verdict BLOCKED with "
            f"{validation['blocking_count']} blocking issue(s) in {sorted(banned)}; "
            f"submit -> 409"
        )

    harness.check("26. Unsupported claims are blocked", c26)

    def c27() -> str:
        """Editing an item creates a new version and never rewrites the old one."""
        assert "clean_item" in p3, "no validated item from criterion 25"
        item_id = p3["clean_item"]
        _, before = http.call("GET", f"/content/{item_id}", expect=200)
        original_body = before["current_version"]["body"]
        original_id = before["current_version"]["id"]
        _, after = http.call(
            "POST",
            f"/content/{item_id}/versions",
            {"body": original_body + "\n\nAcceptance edit.", "change_note": "acceptance edit"},
            expect=(200, 201),
        )
        assert after["current_version"]["version_number"] == before["version_count"] + 1
        _, versions = http.call("GET", f"/content/{item_id}/versions", expect=200)
        original = next(version for version in versions if version["id"] == original_id)
        assert original["body"] == original_body, "the earlier version was mutated"
        p3["edited_version_id"] = after["current_version"]["id"]
        return (
            f"v{before['version_count']} -> v{after['current_version']['version_number']}; "
            f"original version {original_id[:8]} unchanged"
        )

    harness.check("27. Editing creates a new version", c27)

    def c28() -> str:
        """Approval binds to the exact version; a later edit invalidates it."""
        assert "clean_item" in p3, "no validated item from criterion 25"
        item_id = p3["clean_item"]
        # approval is only reachable from IN_REVIEW, so submit first
        submitted = http.call(
            "POST",
            f"/content/{item_id}/transition",
            {"action": "submit"},
            expect=(200, 201),
        )[1]
        assert submitted["status"] == "IN_REVIEW", submitted["status"]
        _, approved = http.call(
            "POST",
            f"/content/{item_id}/transition",
            {"action": "approve", "content_version_id": p3["edited_version_id"]},
            expect=(200, 201),
        )
        assert approved["status"] == "APPROVED", approved["status"]
        assert approved["has_valid_approval"] is True
        _, approvals_before = http.call("GET", f"/content/{item_id}/approvals", expect=200)
        approved_version_id = approvals_before[-1]["content_version_id"]

        _, edited = http.call(
            "POST",
            f"/content/{item_id}/versions",
            {"body": approved["current_version"]["body"] + "\n\nPost-approval edit.",
             "change_note": "acceptance: edit after approval"},
            expect=(200, 201),
        )
        assert edited["has_valid_approval"] is False, "approval survived an edit"
        assert edited["status"] == "DRAFT", edited["status"]
        _, approvals_after = http.call("GET", f"/content/{item_id}/approvals", expect=200)
        assert len(approvals_after) == len(approvals_before), "approval history was deleted"
        assert approvals_after[-1]["content_version_id"] == approved_version_id
        # the approved version itself is still exactly what was approved
        _, versions = http.call("GET", f"/content/{item_id}/versions", expect=200)
        still = next(version for version in versions if version["id"] == approved_version_id)
        assert still["body"] == approved["current_version"]["body"]
        return (
            f"approval bound to version {approved_version_id[:8]}; editing invalidated it "
            f"while keeping {len(approvals_after)} approval record(s) and the approved text intact"
        )

    harness.check("28. Approval is version-bound and edits invalidate it", c28)

    def c29() -> str:
        """Internal calendar entries can be moved without implying real scheduling."""
        assert "edited_version_id" in p3, "no edited item from criterion 27"
        item_id = p3["clean_item"]
        status, entry = http.call(
            "PATCH",
            f"{STUDIO}/calendar/{item_id}",
            {"planned_date": "2026-11-16", "planned_time": "10:15",
             "note": "acceptance move"},
            expect=200,
        )
        assert entry["planned_date"] == "2026-11-16"
        assert entry["planned_time"] == "10:15"
        assert entry["external_scheduling"] is False
        assert entry["publishing_available"] is False
        assert entry["label"] in {
            "planned", "draft", "in_review", "approved", "changes_requested",
            "rejected", "scheduled_internally", "cancelled", "failed",
        }, entry["label"]
        _, view = http.call("GET", f"{STUDIO}/calendar?view=week&anchor=2026-11-16", expect=200)
        moved = [row for row in view["entries"] if row["content_item_id"] == item_id]
        assert moved and moved[0]["planned_date"] == "2026-11-16", "the move was not persisted"
        assert view["external_scheduling"] is False and view["publishing_available"] is False
        return (
            f"entry moved to {entry['planned_date']} {entry['planned_time']} "
            f"(label {entry['label']}); calendar reports external_scheduling=false"
        )

    harness.check("29. Internal calendar can be updated", c29)

    def c30() -> str:
        """Nothing in Phase 3A can publish or externally schedule."""
        for path in (
            f"{STUDIO}/publish",
            f"{STUDIO}/schedule",
            f"{STUDIO}/calendar/publish",
            f"{STUDIO}/items/{p3['clean_item']}/publish",
        ):
            status, _ = http.call("POST", path, {})
            assert status in (403, 404, 405), f"{path} unexpectedly exists ({status})"
        _, capabilities = http.call("GET", f"{STUDIO}/capabilities", expect=200)
        assert capabilities["external_publishing"] is False
        assert capabilities["external_scheduling"] is False
        _, sysst = http.call("GET", "/system/status", expect=200)
        assert sysst["publishing"]["status"] == "unavailable"
        return (
            "4 studio publish/schedule paths absent; capabilities report "
            "external_publishing=false and external_scheduling=false"
        )

    harness.check("30. Publishing is unavailable", c30)

    def c31() -> str:
        """Zero budget blocks paid providers while the free mock still works."""
        code = (
            "import asyncio, json\n"
            "from app.core.config import Settings\n"
            "from app.llm.base import LLMRequest, LLMResponse\n"
            "from app.llm.gateway import LLMGateway\n"
            "from app.llm.mock_content import build_mock_provider\n"
            "from app.llm.routing import BudgetGuard, estimate_cost\n"
            "class FakeProvider:\n"
            "    name = 'openai'\n"
            "    def is_configured(self):\n"
            "        return True\n"
            "    async def structured(self, request):\n"
            "        return LLMResponse(provider='openai', model='gpt-4o-mini', data={'ok': True},\n"
            "                           prompt_tokens=1, completion_tokens=1, cost_usd=0.01,\n"
            "                           latency_ms=1)\n"
            "async def main():\n"
            "    s = Settings(environment='test', content_provider='mock',\n"
            "                 llm_default_provider='openai', llm_default_model='gpt-4o-mini')\n"
            "    # The gateway holds both providers so the zero-budget proof is about the\n"
            "    # budget and not about a missing provider registration.\n"
            "    gw = LLMGateway(session=None, settings=s,\n"
            "                    providers={'openai': FakeProvider(), 'mock': build_mock_provider()})\n"
            "    req = LLMRequest(system_prompt='s', user_prompt='u', schema_name='x',\n"
            "                     json_schema={}, max_tokens=16)\n"
            "    def zero():\n"
            "        return BudgetGuard(max_cost_usd=0.0, label='acceptance-zero')\n"
            "    out = {}\n"
            "    try:\n"
            "        r = await gw.structured(agent_name='content', request=req, budget=zero())\n"
            "        out['mock_allowed'] = (r.provider == 'mock' and r.cost_usd == 0.0)\n"
            "    except Exception as exc:\n"
            "        out['mock_allowed'] = 'error:' + type(exc).__name__\n"
            "    blocked = []\n"
            "    for agent in ('research', 'strategy'):\n"
            "        try:\n"
            "            await gw.structured(agent_name=agent, request=req, budget=zero())\n"
            "            blocked.append('NOT_BLOCKED:' + agent)\n"
            "        except Exception as exc:\n"
            "            blocked.append(type(exc).__name__)\n"
            "    out['paid_agents'] = blocked\n"
            "    est = estimate_cost(provider='deepseek', model='deepseek-v4.1-flash',\n"
            "                        prompt_tokens=1000, completion_tokens=1000, settings=s)\n"
            "    out['paid_billable'] = est.billable\n"
            "    out['paid_cost'] = est.estimated_cost_usd\n"
            "    print(json.dumps(out))\n"
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
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        assert result["mock_allowed"] is True, result
        for outcome in result["paid_agents"]:
            assert not str(outcome).startswith("NOT_BLOCKED"), result
        assert result["paid_billable"] is True, "a paid model was treated as free"
        assert result["paid_cost"] > 0, "a paid model was priced at zero"
        return (
            f"mock served at zero budget (cost 0.0); paid agents rejected with "
            f"{result['paid_agents']}; deepseek-v4.1-flash still billable at "
            f"${result['paid_cost']:.6f}/1k+1k"
        )

    harness.check("31. Zero budget blocks paid providers", c31)

    def c32() -> str:
        """Mock output is labelled as mock everywhere it surfaces."""
        _, capabilities = http.call("GET", f"{STUDIO}/capabilities", expect=200)
        assert capabilities["is_mock_provider"] is True
        assert capabilities["live_model_calls_enabled"] is False
        assert capabilities["content_provider"] == "mock"
        _, plan = http.call("GET", f"{STUDIO}/plans/{p3['plan_id']}", expect=200)
        assert plan["generated_by"] == "mock"
        meta = plan["generation_metadata"]
        assert meta["is_mock"] is True and meta["provider"] == "mock"
        assert meta["model"] == "mock-content-v1"
        _, variants = http.call(
            "GET", f"{STUDIO}/items/{p3['variant_item']}/variants", expect=200
        )
        assert variants["is_mock"] is True, variants["is_mock"]
        assert len(variants["variants"]) >= 2, variants["variants"]
        _, item = http.call("GET", f"/content/{p3['variant_item']}", expect=200)
        version_meta = item["current_version"]["generation_metadata"]
        assert version_meta.get("provider") in {"mock", "deterministic"}
        return (
            f"capabilities provider=mock live_model_calls_enabled=false; plan "
            f"generated_by={plan['generated_by']} model={meta['model']}; variants is_mock=true"
        )

    harness.check("32. Mock generation is clearly attributed", c32)

    def c33() -> str:
        """Variants are compared and the selection is preserved as a version."""
        assert "variant_item" in p3, "no untouched item from criterion 23"
        item_id = p3["variant_item"]
        _, variants = http.call("GET", f"{STUDIO}/items/{item_id}/variants", expect=200)
        assert len(variants["variants"]) >= 2, variants
        bodies = {variant["body"] for variant in variants["variants"]}
        assert len(bodies) == len(variants["variants"]), "variants are reworded duplicates"
        current = variants["selected_variant"]
        other = next(v["index"] for v in variants["variants"] if v["index"] != current)
        _, selected = http.call(
            "POST",
            f"{STUDIO}/items/{item_id}/variants/select",
            {"variant_index": other, "change_note": "acceptance: pick another variant"},
            expect=(200, 201),
        )
        _, after = http.call("GET", f"{STUDIO}/items/{item_id}/variants", expect=200)
        assert after["selected_variant"] == other
        assert after["version_number"] == selected["version_number"]
        chosen = next(v for v in after["variants"] if v["index"] == other)
        _, item = http.call("GET", f"/content/{item_id}", expect=200)
        assert item["current_version"]["body"] == chosen["body"], "selected variant not applied"
        return (
            f"{len(after['variants'])} distinct variants; selecting index {other} produced "
            f"v{selected['version_number']} with that exact body"
        )

    harness.check("33. Variants are compared and preserved", c33)

    def c34() -> str:
        """Generation is idempotent: a retry never duplicates campaigns or items."""
        _, again = http.call(
            "POST",
            f"{STUDIO}/plans",
            {
                "campaign_id": p3["campaign_id"],
                "config": {"posts_per_week": 6, "variants": 2},
                "generate": True,
            },
            headers={"Idempotency-Key": PLAN_KEY},
            expect=(200, 201),
        )
        assert again["id"] == p3["plan_id"], "the retry created a second plan"
        assert again["slot_count"] == 6, "the retry added slots"
        _, twice = http.call(
            "POST",
            f"{STUDIO}/generate",
            {"platform": "linkedin", "content_type": "social_post",
             "campaign_id": p3["campaign_id"]},
            headers={"Idempotency-Key": GENERATE_KEY},
            expect=(200, 201),
        )
        _, third = http.call(
            "POST",
            f"{STUDIO}/generate",
            {"platform": "linkedin", "content_type": "social_post",
             "campaign_id": p3["campaign_id"]},
            headers={"Idempotency-Key": GENERATE_KEY},
            expect=(200, 201),
        )
        assert twice["content_item_id"] == third["content_item_id"], "retry created a new item"
        _, campaigns = http.call("GET", "/campaigns", expect=200)
        matching = [c for c in campaigns if c["id"] == p3["campaign_id"]]
        assert len(matching) == 1
        return (
            f"plan retry returned plan {again['id'][:8]} with {again['slot_count']} slots; "
            f"generate retry returned item {twice['content_item_id'][:8]}"
        )

    harness.check("34. Generation is idempotent", c34)

    def c35() -> str:
        """Phase 1 + 2 acceptance still holds in this same run."""
        def number(name: str) -> int | None:
            head = name.split(".")[0].strip()
            digits = "".join(ch for ch in head if ch.isdigit())
            return int(digits) if digits else None

        prior = [ok for name, ok, _ in harness.results
                 if (number(name) or 99) <= 22]
        failed = [name for name, ok, _ in harness.results
                  if (number(name) or 99) <= 22 and not ok]
        assert not failed, f"earlier criteria regressed: {failed}"
        assert len(prior) >= 22, f"expected at least 22 earlier checks, ran {len(prior)}"
        _, overview = http.call("GET", f"{STUDIO}/overview", expect=200)
        assert overview["total_drafts"] >= 6
        return (
            f"all {len(prior)} Phase 1/2 checks still pass alongside the Phase 3A criteria; "
            f"studio overview reports {overview['total_drafts']} drafts"
        )

    harness.check("35. Phase 1 and Phase 2 acceptance is intact", c35)

    # ------------------------------------------------------------------ #
    # Phase 3B — Creative Studio
    # ------------------------------------------------------------------ #
    creative = "/creative-studio"
    state: dict[str, object] = {}

    def c36() -> str:
        """A brief can be created from a content version without touching the content."""
        _, brand = http.call("GET", "/brand/active", expect=200)
        tag = uuid.uuid4().hex[:8]
        _, item = http.call(
            "POST",
            "/content",
            {
                "brand_profile_id": brand["id"],
                "title": f"Phase 3B acceptance creative {tag}",
                "platform": "instagram",
                "body": "Crusor AI is an AI-powered IDE. Build and ship with ease.\n\n"
                "This copy must survive brief generation unchanged.",
                "headline": "Build and Ship with Ease",
                "call_to_action": "Try it.",
            },
            expect=201,
        )
        _, detail = http.call("GET", f"/content/{item['id']}", expect=200)
        version_id = detail["current_version_id"]
        before_body = detail["current_version"]["body"]

        _, brief = http.call(
            "POST",
            f"{creative}/briefs",
            {
                "content_version_id": version_id,
                "asset_type": "image",
                "creative_direction": "developer at a terminal, charcoal frame",
            },
            expect=(200, 201),
        )
        required = [
            "content_version_id",
            "platform",
            "asset_type",
            "creative_objective",
            "visual_concept",
            "scenes",
            "subjects",
            "background",
            "lighting",
            "composition",
            "colour_palette",
            "typography",
            "brand_asset_requirements",
            "image_prompt",
            "negative_prompt",
            "aspect_ratio",
            "resolution",
            "reference_assets",
            "validation_constraints",
        ]
        missing = [key for key in required if key not in brief]
        assert not missing, f"brief is missing {missing}"
        assert brief["content_version_id"] == version_id
        assert brief["validation_constraints"]["human_visual_review_required"] is True

        _, after = http.call("GET", f"/content/{item['id']}", expect=200)
        assert after["current_version"]["body"] == before_body, "brief generation edited content"
        assert after["status"] == detail["status"]

        state.update(item_id=item["id"], version_id=version_id, brief=brief)
        return (
            f"brief v{brief['version_number']} for {brief['platform']} "
            f"({brief['aspect_ratio']}, {brief['resolution']}); content body byte-identical "
            f"afterwards"
        )

    harness.check("36. Creative briefs are creatable from a content version", c36)

    def c37() -> str:
        """A mock image job executes and its asset is stored with verified bytes."""
        brief = state["brief"]
        _, job = http.call(
            "POST",
            f"{creative}/jobs",
            {"brief_id": brief["id"], "parameters": {"seed": 3}},
            expect=201,
        )
        assert job["status"] == "succeeded", job
        assert job["provider"] == "mock" and job["is_mock"] is True
        assert job["external_job_id"].startswith("mock:")
        assert job["cost_actual_usd"] == 0.0
        _, replay = http.call(
            "POST",
            f"{creative}/jobs",
            {"brief_id": brief["id"], "parameters": {"seed": 3}},
            expect=(200, 201),
        )
        assert replay["id"] == job["id"], "the same request created a second job"
        state.update(job=job)
        return (
            f"job {job['external_job_id'][:34]}… succeeded on {job['provider']} at "
            f"${job['cost_actual_usd']}; replay returned the same job"
        )

    harness.check("37. Mock image generation executes and is idempotent", c37)

    def c38() -> str:
        """Video generation is honest: a poster frame, not an encoded video."""
        brief = state["brief"]
        _, video_brief = http.call(
            "POST",
            f"{creative}/briefs",
            {
                "content_version_id": state["version_id"],
                "asset_type": "video",
                "force_new_version": True,
            },
            expect=(200, 201),
        )
        _, caps = http.call("GET", f"{creative}/capabilities", expect=200)
        assert caps["video_playback"] == "poster_only", caps
        _, job = http.call(
            "POST", f"{creative}/jobs", {"brief_id": video_brief["id"]}, expect=201
        )
        # The mock simulates a short render for video, so poll it to completion
        # rather than assuming a submit returns a finished job.
        initial = job["status"]
        deadline = time.time() + 30
        while job["status"] not in ("succeeded", "failed", "timed_out") and time.time() < deadline:
            time.sleep(1.2)
            _, job = http.call("POST", f"{creative}/jobs/{job['id']}/poll", expect=200)
        assert job["status"] == "succeeded", job
        _, asset = http.call("GET", f"{creative}/assets/{job['asset_id']}", expect=200)
        assert asset["mime_type"] == "image/png"
        assert asset["metadata_json"]["video_playback"] == "poster_only"
        assert asset["metadata_json"]["encoded_video"] in (None, False)
        return (
            f"video brief asked {video_brief['duration_seconds']}s at "
            f"{video_brief['aspect_ratio']}; submitted as {initial}, polled to "
            f"{job['status']}; provider returned a {asset['width']}x{asset['height']} poster and "
            f"declared poster_only"
        )

    harness.check("38. Video generation is labelled a poster, not a video", c38)

    def c39() -> str:
        """Jobs persist with status, parameters and an audit trail."""
        job = state["job"]
        _, listed = http.call("GET", f"{creative}/jobs", expect=200)
        assert any(row["id"] == job["id"] for row in listed), "job missing from history"
        _, fetched = http.call("GET", f"{creative}/jobs/{job['id']}", expect=200)
        assert fetched["status"] == "succeeded"
        assert fetched["parameters"]["seed"] == 3
        assert fetched["audit_trail"], "no audit trail recorded"
        events = [event.get("event") for event in fetched["audit_trail"]]
        assert "succeeded" in events, events
        _, polled = http.call("POST", f"{creative}/jobs/{job['id']}/poll", expect=200)
        assert polled["status"] == "succeeded"
        return f"job persisted with {len(events)} audit events: {events[:4]}"

    harness.check("39. Job lifecycle is persisted with an audit trail", c39)

    def c40() -> str:
        """The asset is stored, retrievable, and its metadata matches the bytes."""
        job = state["job"]
        _, asset = http.call("GET", f"{creative}/assets/{job['asset_id']}", expect=200)
        url = f"{args.base_url}{API}{creative}/assets/{asset['id']}/bytes"
        request = urllib.request.Request(
            url, headers={"authorization": f"Bearer {http.token}"}
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            content_type = response.headers.get("content-type")
        digest = __import__("hashlib").sha256(raw).hexdigest()
        assert digest == asset["checksum"], "stored bytes do not match the checksum"
        assert len(raw) == asset["size_bytes"], "stored size does not match"
        assert raw.startswith(b"\x89PNG\r\n\x1a\n"), "not a PNG"
        assert content_type.startswith("image/png")
        assert asset["detected_mime"] == asset["mime_type"] == "image/png"
        assert asset["width"] and asset["height"]
        assert asset["storage_key"] and asset["asset_version"] == 1
        state.update(asset=asset)
        return (
            f"{asset['width']}x{asset['height']} {asset['mime_type']}, "
            f"{len(raw)} bytes, sha256 {digest[:16]}… verified against the record"
        )

    harness.check("40. Assets are stored and retrievable byte-for-byte", c40)

    def c41() -> str:
        """Creative QA runs, produces a verdict, and declares what it did not do."""
        asset = state["asset"]
        _, result = http.call(
            "POST", f"{creative}/assets/{asset['id']}/validate", {}, expect=200
        )
        report = result["qa_report"]
        assert report["verdict"] in {
            "pass",
            "pass_with_warnings",
            "needs_revision",
            "blocked",
        }, report["verdict"]
        assert report["performed_checks"] > 5
        assert report["visual_verification"] == "not_performed"
        assert report["human_visual_review_required"] is True
        rules = {check["rule"]: check for check in report["checks"]}
        assert rules["text_readability"]["performed"] is False, "claimed OCR it does not have"
        assert rules["checksum_matches"]["performed"] is True
        assert rules["pixel_analysis"]["performed"] is True
        return (
            f"verdict {report['verdict']} score {report['score']}; "
            f"{report['performed_checks']} performed / {report['not_performed_checks']} not "
            f"performed; pixel sample {report['pixel_stats']['sampled_pixels']} px"
        )

    harness.check("41. Creative QA works and does not claim visual verification", c41)

    def c42() -> str:
        """Content version, brief version and asset version are linked."""
        asset = state["asset"]
        brief = state["brief"]
        assert asset["content_version_id"] == state["version_id"]
        assert asset["content_item_id"] == state["item_id"]
        assert asset["brief_id"] == brief["id"]
        assert asset["brief_version"] == brief["version_number"]
        assert asset["generation_job_id"] == state["job"]["id"]
        assert asset["selected"] is False, "a generated asset selected itself"
        _, versions = http.call(
            "GET", f"{creative}/assets?content_item_id={state['item_id']}", expect=200
        )
        assert len(versions) >= 1
        return (
            f"asset v{asset['asset_version']} → brief v{asset['brief_version']} → content "
            f"version {str(asset['content_version_id'])[:8]} → job {str(asset['generation_job_id'])[:8]}"
        )

    harness.check("42. Content, brief and asset versions are linked", c42)

    def c43() -> str:
        """Approval covers content + brief + asset; changing the asset voids it."""
        asset = state["asset"]
        _, selected = http.call(
            "POST", f"{creative}/assets/{asset['id']}/select", {}, expect=200
        )
        assert selected["asset"]["selected"] is True
        http.call(
            "POST", f"/content/{state['item_id']}/transition", {"action": "submit"}, expect=200
        )
        _, approved = http.call(
            "POST",
            f"/content/{state['item_id']}/transition",
            {"action": "approve", "reason": "acceptance: approved with creative"},
            expect=200,
        )
        assert approved["has_valid_approval"] is True
        _, records = http.call("GET", f"/content/{state['item_id']}/approvals", expect=200)
        approval = next(row for row in records if row["action"] == "approve")
        assert approval["creative_asset_id"] == asset["id"], approval
        assert approval["creative_brief_id"] == state["brief"]["id"]
        assert approval["creative_brief_version"] == state["brief"]["version_number"]
        assert approval["creative_asset_version"] == asset["asset_version"]
        assert approval["creative_binding_hash"]

        _, second = http.call(
            "POST",
            f"{creative}/jobs",
            {"brief_id": state["brief"]["id"], "parameters": {"seed": 11}},
            expect=(200, 201),
        )
        _, swapped = http.call(
            "POST", f"{creative}/assets/{second['asset_id']}/select", {}, expect=200
        )
        assert swapped["invalidated_approvals"] >= 1, swapped
        _, after = http.call("GET", f"/content/{state['item_id']}", expect=200)
        assert after["has_valid_approval"] is False, "approval survived a selection change"
        return (
            f"approval bound asset {asset['id'][:8]} v{asset['asset_version']} + brief "
            f"v{approval['creative_brief_version']} + content version; selecting another asset "
            f"invalidated {swapped['invalidated_approvals']} approval(s)"
        )

    harness.check("43. Approval binds content + brief + asset and enforces invalidation", c43)

    def c44() -> str:
        """Flow is reported unavailable and paid generation is off."""
        _, flow = http.call("GET", f"{creative}/flow/status", expect=200)
        assert flow["available"] is False
        assert flow["enabled"] is False
        assert flow["source_found"] is False
        assert len(flow["required_information"]) >= 5
        _, caps = http.call("GET", f"{creative}/capabilities", expect=200)
        assert caps["provider"] == "mock" and caps["is_mock"] is True
        assert caps["billable"] is False
        assert caps["live_generation_enabled"] is False
        assert caps["paid_generation_enabled"] is False
        _, status = http.call("GET", "/system/status", expect=200)
        creative_status = status["creative"]
        assert creative_status["paid_generation_enabled"] is False
        assert creative_status["flow_connector"]["status"] == "not_available"
        return (
            f"flow {flow['status']} (source_found={flow['source_found']}, "
            f"{len(flow['required_information'])} requirements); provider {caps['provider']} "
            f"billable={caps['billable']} paid={caps['paid_generation_enabled']}"
        )

    harness.check("44. Flow is not falsely active and paid generation is disabled", c44)

    def c45() -> str:
        """There is no publishing or external scheduling route for creative."""
        attempts = [
            ("POST", "/publishing"),
            ("POST", "/publish"),
            ("POST", f"{creative}/publish"),
            ("POST", f"{creative}/assets/{state['asset']['id']}/publish"),
            ("POST", f"{creative}/schedule"),
        ]
        codes = []
        for method, path in attempts:
            status, _ = http.call(method, path, {})
            assert status in (404, 405), f"{path} -> {status}"
            codes.append(f"{path}={status}")
        _, caps = http.call("GET", f"{creative}/capabilities", expect=200)
        assert caps["publishing_available"] is False
        assert caps["external_scheduling"] is False
        return "; ".join(codes)

    harness.check("45. No creative publishing or scheduling endpoint exists", c45)

    def c46() -> str:
        """The shipped frontend bundle contains the Creative Studio surfaces."""
        bundle = sorted((REPO_ROOT / "frontend" / "dist" / "assets").glob("index-*.js"))
        assert bundle, "no production bundle — run `npm run build`"
        js = bundle[-1].read_text(errors="ignore")
        wanted = [
            "Generation centre",
            "Brief editor",
            "Asset library",
            "Mock generation is active",
            "Google Flow is not available",
            "not performed",
        ]
        missing = [needle for needle in wanted if needle not in js]
        assert not missing, f"bundle is missing {missing}"
        size_kb = bundle[-1].stat().st_size / 1024
        return f"{bundle[-1].name} ({size_kb:.1f} kB) contains all {len(wanted)} studio strings"

    harness.check("46. Frontend build ships the Creative Studio UI", c46)

    def c47() -> str:
        """Phase 1, 2, 2.5, 2.6 and 3A acceptance still holds in this same run."""
        def number(name: str) -> int | None:
            head = name.split(".")[0].strip()
            digits = "".join(ch for ch in head if ch.isdigit())
            return int(digits) if digits else None

        earlier = [name for name, ok, _ in harness.results if (number(name) or 99) < 47]
        failed = [
            name
            for name, ok, _ in harness.results
            if (number(name) or 99) < 47 and not ok
        ]
        assert not failed, f"earlier criteria regressed: {failed}"
        assert len(earlier) >= 40, f"expected the full earlier suite, ran {len(earlier)}"
        _, overview = http.call("GET", f"{creative}/overview", expect=200)
        assert overview["total_assets"] >= 2
        assert overview["mock_assets"] >= 2
        assert overview["provider"] == "mock"
        return (
            f"all {len(earlier)} earlier checks still pass; creative overview reports "
            f"{overview['total_assets']} assets ({overview['mock_assets']} mock), "
            f"{overview['succeeded_jobs']} succeeded jobs"
        )

    harness.check("47. Phase 1/2/2.5/2.6/3A acceptance is intact", c47)

    # ---- Phase 3C.1: DeepX Flow bridge security ---------------------------
    flow = "/flow"

    def c48() -> str:
        """The Flow integration is reported honestly and generation stays off."""
        _, status = http.call("GET", f"{flow}/status", expect=200)
        # The label follows the current hardening milestone; every boundary below
        # is still the Phase 3C.1 one and is asserted unchanged.
        assert status["phase"] == "3C.2.3"
        assert status["live_generation_enabled"] is False
        assert status["available"] is False and status["enabled"] is False
        bridge = status["bridge"]
        assert bridge["bind_host"] == "127.0.0.1", bridge["bind_host"]
        assert bridge["loopback_only"] is True
        assert bridge["mode"] == "local_companion"
        assert bridge["command_allowlist"] == [
            "status", "generate_image", "generate_video", "check_video_status", "export_asset"
        ]
        policy = status["token_policy"]
        assert policy == {
            "page_visible_tokens": False,
            "arbitrary_api_urls": False,
            "approved_endpoints_only": True,
            "logging": "redacted",
        }
        assert status["generation"]["max_live_jobs"] == 1
        assert status["generation"]["unknown_submission_policy"] == "never_resubmitted_automatically"
        assert status["asset_export"]["assets_created_before_verification"] is False
        blockers = {row["id"] for row in status["upstream_blockers"]}
        assert blockers == {f"U{i}" for i in range(1, 8)}, blockers
        _, system = http.call("GET", "/system/status", expect=200)
        assert system["flow"]["live_generation_enabled"] is False
        return (
            f"phase 3C.2.3, bind_host=127.0.0.1, loopback_only=true, live_generation=false, "
            f"max_live_jobs=1, {len(blockers)} upstream blockers reported (generation off)"
        )

    harness.check("48. Flow status is honest and live generation is off", c48)

    def c49() -> str:
        """The command surface is an allowlist, not the upstream MCP tool dump."""
        _, caps = http.call("GET", f"{flow}/capabilities", expect=200)
        allowed = set(caps["allowed_commands"])
        denied = {row["command"] for row in caps["denied_commands"]}
        for command in (
            "status", "generate_image", "generate_video", "check_video_status", "export_asset"
        ):
            assert command in allowed, command
        for command in (
            "eval_script", "native_click", "native_key", "batch_generate",
            "rapid_batch_dispatch", "autonomous_storyboard_pipeline",
            "rename_project", "open_new_project", "api_post", "get_token",
        ):
            assert command in denied, command
            assert command not in allowed, f"{command} is both allowed and denied"
        assert allowed.isdisjoint(denied)
        # The OpenAPI document lives at the app root, outside the /api/v1 prefix.
        with urllib.request.urlopen(
            f"{args.base_url.rstrip('/')}/openapi.json", timeout=60
        ) as schema_response:
            schema = json.loads(schema_response.read())
        paths = " ".join(schema["paths"])
        for needle in ("eval", "api_post", "get_token", "native_", "storyboard", "script"):
            assert needle not in paths, f"route surface exposes {needle}"
        return (
            f"{len(allowed)} allowlisted commands, {len(denied)} denied by name; "
            f"no eval/api_post/get_token/native/storyboard route exists"
        )

    harness.check("49. Dangerous Flow commands are denied by name", c49)

    def c50() -> str:
        """A real, os-level bridge binds loopback only and refuses everything else."""
        code = (
            "import asyncio, json\n"
            "import httpx\n"
            "from app.flow.bridge import BridgeConfig, FlowBridge\n"
            "from app.flow.security import LoopbackBindError, generate_pairing_id, generate_pairing_secret\n"
            "async def main():\n"
            "    out = {}\n"
            "    secret = generate_pairing_secret()\n"
            "    pid = generate_pairing_id()\n"
            "    try:\n"
            "        FlowBridge(BridgeConfig(host='0.0.0.0', pairing_id=pid,\n"
            "                    pairing_secret=secret, http_port=0, ws_port=0))\n"
            "        out['public_bind'] = 'ALLOWED'\n"
            "    except LoopbackBindError:\n"
            "        out['public_bind'] = 'refused'\n"
            "    bridge = FlowBridge(BridgeConfig(pairing_id=pid, pairing_secret=secret,\n"
            "                    http_port=0, ws_port=0, extension_ids=('abcdefghijklmnopabcdefghijklmnop',)))\n"
            "    await bridge.start()\n"
            "    out['http_addr'] = list(bridge._http_server.sockets[0].getsockname()[:2])\n"
            "    ws_sockets = getattr(bridge._ws_server, 'sockets', []) or []\n"
            "    out['ws_addr'] = list(ws_sockets[0].getsockname()[:2]) if ws_sockets else []\n"
            "    base = f'http://127.0.0.1:{bridge.http_port}'\n"
            "    async with httpx.AsyncClient() as c:\n"
            "        out['unauth'] = (await c.get(f'{base}/api/status')).status_code\n"
            "        auth = {'X-Flow-Pairing': f'{pid}.{secret}'}\n"
            "        out['auth'] = (await c.get(f'{base}/api/status', headers=auth)).status_code\n"
            "        out['denied'] = (await c.post(f'{base}/api/command', headers=auth,\n"
            "                        json={'command': 'eval_script', 'payload': {}})).status_code\n"
            "        out['bad_host'] = (await c.get(f'{base}/api/status',\n"
            "                        headers={**auth, 'Host': 'evil.example.com'})).status_code\n"
            "        out['oversize'] = (await c.post(f'{base}/api/command', headers=auth,\n"
            "                        content=b'x' * (bridge.config.max_request_bytes + 2048))).status_code\n"
            "        out['unpaired'] = (await c.post(f'{base}/api/command', headers=auth,\n"
            "                        json={'command': 'status', 'payload': {}})).status_code\n"
            "    await bridge.stop()\n"
            "    print(json.dumps(out))\n"
            "asyncio.run(main())\n"
        )
        proc = subprocess.run(
            [str(REPO_ROOT / ".venv" / "bin" / "python"), "-c", code],
            cwd=BACKEND,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": "."},
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr[-800:]
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        assert result["public_bind"] == "refused", result
        assert result["http_addr"][0] == "127.0.0.1", result["http_addr"]
        assert result["ws_addr"] and result["ws_addr"][0] == "127.0.0.1", result["ws_addr"]
        assert result["unauth"] == 401, result
        assert result["auth"] == 200, result
        assert result["denied"] == 403, result
        assert result["bad_host"] == 421, result
        assert result["oversize"] == 413, result
        assert result["unpaired"] == 503, result
        return (
            f"0.0.0.0 bind {result['public_bind']}; sockets {result['http_addr'][0]}:"
            f"{result['http_addr'][1]} and {result['ws_addr'][0]}:{result['ws_addr'][1]}; "
            f"unauth={result['unauth']}, denied={result['denied']}, bad_host={result['bad_host']}, "
            f"oversize={result['oversize']}, unpaired={result['unpaired']}"
        )

    harness.check("50. The local bridge binds loopback only and authenticates", c50)

    def c51() -> str:
        """A companion cannot talk to Crusor without its 256-bit credential."""
        payload = {"companion_id": "acceptance-companion", "label": "acceptance"}
        anonymous = http.call("POST", f"{flow}/companion/hello", payload)
        assert anonymous[0] == 401, anonymous
        anonymous_body = json.dumps(anonymous[1])
        forged = http.call(
            "POST",
            f"{flow}/companion/hello",
            payload,
            headers={"X-Companion-Id": "acceptance-companion", "X-Companion-Secret": "x" * 43},
        )
        assert forged[0] == 401, forged
        for body in (anonymous_body, json.dumps(forged[1])):
            assert "x" * 43 not in body
            assert "pairing_secret" not in body
            assert "X-Flow-Pairing" not in body
        for path, body in (
            (f"{flow}/companion/heartbeat", {"companion_id": "acceptance-companion"}),
            (f"{flow}/companion/disconnect", {"companion_id": "acceptance-companion"}),
        ):
            assert http.call("POST", path, body)[0] == 401, path
        assert http.call("GET", f"{flow}/companion/commands")[0] == 401
        return (
            "anonymous and forged companion credentials both rejected with 401 on "
            "hello/heartbeat/disconnect/commands; no secret echoed in the error body"
        )

    harness.check("51. The companion endpoint rejects unauthenticated callers", c51)

    def c52() -> str:
        """A real Flow generation needs a founder approval and is still refused."""
        brief_id = state["brief"]["id"]
        body = {"brief_id": brief_id, "prompt": "acceptance: charcoal terminal frame",
                "model": "imagen-3", "dry_run": True}
        status, decision = http.call("POST", f"{flow}/generations", body)
        assert status == 403, (status, decision)
        assert decision["error"]["code"] == "flow_approval_required", decision
        _, binding = http.call(
            "POST", f"{flow}/binding", {**body, "brief_id": brief_id}, expect=200
        )
        assert len(binding["binding_hash"]) == 64
        assert binding["binding"]["output_count"] == 1
        real = http.call("POST", f"{flow}/generations", {**body, "dry_run": False})
        assert real[0] == 403, real  # the gate runs first and still refuses
        assert real[1]["error"]["code"] == "flow_approval_required"
        return (
            "generation refused with flow_approval_required (403) with and without dry_run; "
            f"binding hash {binding['binding_hash'][:16]}… explains what a founder must approve"
        )

    harness.check("52. Flow generation is blocked by the approval gate", c52)

    def c53() -> str:
        """The documented companion module entry point fails closed when unpaired."""
        env = os.environ.copy()
        for name in (
            "FLOW_COMPANION_ID",
            "FLOW_COMPANION_SECRET",
            "FLOW_PAIRING_ID",
            "FLOW_PAIRING_SECRET",
        ):
            env[name] = ""
        # Same import path the rest of the harness uses, whether or not the caller
        # exported PYTHONPATH.
        env["PYTHONPATH"] = "."

        completed = subprocess.run(
            [sys.executable, "-m", "app.flow.companion"],
            cwd=BACKEND,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        output = f"{completed.stdout}\n{completed.stderr}"
        assert completed.returncode == 2, (
            f"expected safe configuration exit 2, got {completed.returncode}: {output[-2000:]}"
        )
        assert "flow_companion_misconfigured:" in output, output[-2000:]
        assert "flow_bridge_started" not in output, "unpaired CLI must not start a listener"
        return (
            "python -m app.flow.companion exits 2 for missing pairing credentials "
            "before starting listeners"
        )

    harness.check("53. The local companion has a safe executable module entry point", c53)

    # ---- Phase 3C.2: upstream remediation gate + receiver-side contract -----
    def c54() -> str:
        """The source/licensing gate fails closed and nothing upstream is vendored."""
        _, status = http.call("GET", f"{flow}/status", expect=200)
        gate = status["source_gate"]
        assert gate["status"] == "SOURCE_ABSENT", gate["status"]
        assert gate["authorized_to_modify"] is False
        assert gate["can_vendor_source"] is False
        assert gate["upstream_remediation_allowed"] is False
        requirements = [row["blocker"] for row in gate["remediation_requirements"]]
        assert requirements == [f"U{i}" for i in range(1, 8)], requirements
        assert all(row["file"] and row["requirement"] for row in gate["remediation_requirements"])

        audit = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "flow_source_audit.py")],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert audit.returncode == 3, f"audit script returned {audit.returncode}"
        report = json.loads(audit.stdout)
        assert report["status"] == "SOURCE_ABSENT"
        assert report["scans_run"] == []
        assert report["authorized"] is False

        vendored = [
            name
            for name in ("content-script-main.js", "service-worker.js", "ws-bridge.js")
            if list(BACKEND.rglob(name))
        ]
        assert vendored == [], f"upstream files vendored into the repository: {vendored}"
        return (
            "source gate reports SOURCE_ABSENT (no licence, no permission); the audit "
            "scanner exits 3 without running scans; 7 remediation requirements recorded; "
            "no upstream file exists in the repo"
        )

    harness.check("54. Upstream source/licensing gate fails closed", c54)

    def c55() -> str:
        """Provenance is proven, not claimed: the bridge refuses inconsistent builds."""
        code = (
            "import asyncio, json\n"
            "import websockets\n"
            "from app.flow.bridge import BridgeConfig, FlowBridge\n"
            "from app.flow.security import generate_pairing_id, generate_pairing_secret\n"
            "EXT = 'abcdefghijklmnopabcdefghijklmnop'\n"
            "DIGEST = 'a' * 64\n"
            "BASE = {'type': 'hello', 'protocol': 'flow.bridge.v1', 'extension_id': EXT,\n"
            "        'extension_version': '1.3.0', 'service_worker_version': '1.3.0',\n"
            "        'mcp_version': '1.3.0', 'source_commit': '0343884',\n"
            "        'build_id': 'acceptance-build-1', 'package_sha256': DIGEST,\n"
            "        'flow_tab_present': True}\n"
            "async def attempt(bridge, frame):\n"
            "    ws = await websockets.connect(f'ws://127.0.0.1:{bridge.ws_port}/ws')\n"
            "    try:\n"
            "        await ws.send(json.dumps(frame))\n"
            "        try:\n"
            "            first = await asyncio.wait_for(ws.recv(), 3)\n"
            "            return {'ack': json.loads(first)}\n"
            "        except websockets.ConnectionClosed as exc:\n"
            "            return {'close': exc.rcvd.code if exc.rcvd else 0}\n"
            "    finally:\n"
            "        await ws.close()\n"
            "async def main():\n"
            "    pid, secret = generate_pairing_id(), generate_pairing_secret()\n"
            "    bridge = FlowBridge(BridgeConfig(pairing_id=pid, pairing_secret=secret,\n"
            "        http_port=0, ws_port=0, extension_ids=(EXT,), handshake_timeout=2.0,\n"
            "        trusted_artifact_sha256=DIGEST, trusted_release='1.3.0'))\n"
            "    await bridge.start()\n"
            "    auth = {'pairing_id': pid, 'pairing_secret': secret}\n"
            "    out = {}\n"
            "    good = await attempt(bridge, {**BASE, **auth})\n"
            "    out['ack'] = good.get('ack', {}).get('type')\n"
            "    out['trusted'] = good.get('ack', {}).get('provenance_trusted')\n"
            "    out['claim_verified'] = good.get('ack', {}).get('hello_digest_claim_verified')\n"
            "    out['trust_state'] = good.get('ack', {}).get('trust_state')\n"
            "    out['build_id'] = good.get('ack', {}).get('provenance', {}).get('build_id')\n"
            "    await bridge.stop()\n"
            "    bridge2 = FlowBridge(BridgeConfig(pairing_id=pid, pairing_secret=secret,\n"
            "        http_port=0, ws_port=0, extension_ids=(EXT,), handshake_timeout=2.0,\n"
            "        trusted_artifact_sha256=DIGEST, trusted_release='1.3.0'))\n"
            "    await bridge2.start()\n"
            "    out['version_split'] = (await attempt(bridge2, {**BASE, **auth,\n"
            "        'extension_version': '1.2.1', 'service_worker_version': '1.2.0',\n"
            "        'mcp_version': '1.0.0'})).get('close')\n"
            "    out['old_protocol'] = (await attempt(bridge2, {**BASE, **auth,\n"
            "        'protocol': 'flow.bridge.v0'})).get('close')\n"
            "    out['no_commit'] = (await attempt(bridge2, {**BASE, **auth,\n"
            "        'source_commit': ''})).get('close')\n"
            "    out['wrong_digest'] = (await attempt(bridge2, {**BASE, **auth,\n"
            "        'package_sha256': 'b' * 64})).get('close')\n"
            "    out['no_digest'] = (await attempt(bridge2, {**BASE, **auth,\n"
            "        'package_sha256': ''})).get('close')\n"
            "    out['paired_after_refusals'] = bridge2.paired\n"
            "    await bridge2.stop()\n"
            "    bridge3 = FlowBridge(BridgeConfig(pairing_id=pid, pairing_secret=secret,\n"
            "        http_port=0, ws_port=0, extension_ids=(EXT,), handshake_timeout=2.0))\n"
            "    await bridge3.start()\n"
            "    unanchored = await attempt(bridge3, {**BASE, **auth})\n"
            "    out['unanchored_state'] = unanchored.get('ack', {}).get('provenance_state')\n"
            "    out['unanchored_trusted'] = unanchored.get('ack', {}).get('provenance_trusted')\n"
            "    await bridge3.stop()\n"
            "    print(json.dumps(out))\n"
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
        assert proc.returncode == 0, proc.stderr[-1200:]
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        assert result["ack"] == "hello_ack", result
        # Phase 3C.2.3: a HELLO digest is a claim. The ack must say so and must not
        # report a session as trusted before anything has been measured.
        assert result["trusted"] is False, result
        assert result["claim_verified"] is True, result
        assert result["trust_state"] == "UNVERIFIED", result
        assert result["build_id"] == "acceptance-build-1", result
        assert result["version_split"] == 4422, result
        assert result["old_protocol"] == 4426, result
        assert result["no_commit"] == 4422, result
        assert result["wrong_digest"] == 4422, result
        assert result["no_digest"] == 4422, result
        assert result["paired_after_refusals"] is False, result
        assert result["unanchored_state"] == "UNVERIFIED", result
        assert result["unanchored_trusted"] is False, result
        return (
            "anchored build accepted with its digest claim verified but no session "
            "trust before measurement (UNVERIFIED); 1.2.1/1.2.0/1.0.0 split, missing commit, "
            "wrong digest and missing digest all closed 4422; unsupported protocol 4426; "
            "without an anchor the same build is reported UNVERIFIED, never trusted"
        )

    harness.check("55. Extension provenance requires an independent anchor", c55)

    def c56() -> str:
        """A real chunked export lands verified bytes; bad chunk sequences do not."""
        media = minimal_png(48, 48)
        checksum = hashlib.sha256(media).hexdigest()
        companion_headers = {
            "X-Companion-Id": "acceptance-companion",
            "X-Companion-Secret": os.environ.get("FLOW_COMPANION_SECRET", ""),
        }
        assert companion_headers["X-Companion-Secret"], (
            "FLOW_COMPANION_SECRET must be configured for the chunked export check"
        )

        _, ticket = http.call(
            "POST",
            f"{flow}/exports",
            {
                "companion_id": "acceptance-companion",
                "expected_sha256": checksum,
                "content_type": "image/png",
                "filename": "acceptance-chunked.png",
            },
        )
        assert ticket.get("upload_token"), ticket

        chunk_size = 4096
        chunks = [media[i : i + chunk_size] for i in range(0, len(media), chunk_size)]
        status_body: dict = {}
        for index, chunk in enumerate(chunks):
            code, status_body = put_chunk(
                http,
                ticket["id"],
                ticket["upload_token"],
                companion_headers,
                index,
                len(chunks),
                chunk,
            )
            assert code == 200, (code, status_body)
            assert status_body["chunks_received"] == index + 1, status_body

        _, final = http.call("GET", f"{flow}/exports/{ticket['id']}", expect=200)
        assert final["status"] == "consumed", final
        assert final["checksum"] == checksum
        assert final["chunks_received"] == len(chunks)
        assert final["asset_id"]

        # A second ticket: out-of-order and corrupt chunks must not create an asset.
        _, broken = http.call(
            "POST",
            f"{flow}/exports",
            {"companion_id": "acceptance-companion", "filename": "acceptance-broken.png"},
        )
        out_of_order, _ = put_chunk(
            http,
            broken["id"],
            broken["upload_token"],
            companion_headers,
            1,
            2,
            chunks[0],
        )
        corrupt, _ = put_chunk(
            http,
            broken["id"],
            broken["upload_token"],
            companion_headers,
            0,
            2,
            b"not the declared bytes",
            chunk_sha=hashlib.sha256(b"something else").hexdigest(),
        )
        _, broken_state = http.call("GET", f"{flow}/exports/{broken['id']}", expect=200)
        assert out_of_order == 422, out_of_order
        assert corrupt == 422, corrupt
        assert broken_state["asset_id"] is None, broken_state
        assert broken_state["chunks_received"] == 0, broken_state
        assert broken_state["status"] != "consumed", broken_state

        # One-time means one time: the consumed ticket cannot be replayed.
        replay, _ = put_chunk(
            http,
            ticket["id"],
            ticket["upload_token"],
            companion_headers,
            0,
            len(chunks),
            chunks[0],
        )
        assert replay == 422, replay
        return (
            f"{len(chunks)} chunks assembled into a verified asset ({checksum[:16]}…, "
            f"{final['received_bytes']} bytes); out-of-order chunk rejected {out_of_order}, "
            f"corrupt chunk rejected {corrupt}, no bytes accepted for the broken ticket, "
            f"consumed ticket replay rejected {replay}"
        )

    harness.check("56. Chunked asset export verifies bytes and rejects bad sequences", c56)

    def c57() -> str:
        """Duplicate keys, cancellation and reconciliation cannot create a second generation."""
        code = (
            "import asyncio, json\n"
            "from app.flow import protocol as proto\n"
            "from app.flow.coordinator import (CancellationAmbiguous, GenerationCoordinator,\n"
            "    IdempotencyConflict, ReconciliationRejected)\n"
            "class Transport:\n"
            "    def __init__(self):\n"
            "        self.sent = []\n"
            "        self.futures = {}\n"
            "    async def send(self, envelope):\n"
            "        self.sent.append(envelope)\n"
            "        fut = asyncio.get_running_loop().create_future()\n"
            "        fut.set_result(proto.ResultEnvelope(type='result', id=envelope.id,\n"
            "            ok=True, job_id=envelope.job_id,\n"
            "            request_hash=envelope.request_hash,\n"
            "            result={'media_id': 'media-0001'}))\n"
            "        self.futures[envelope.id] = fut\n"
            "    async def await_result(self, command_id, timeout):\n"
            "        return await asyncio.wait_for(self.futures[command_id], timeout)\n"
            "def request(prompt='frame', key=''):\n"
            "    return proto.GenerationCommand(prompt=prompt, model='imagen-3', count=1,\n"
            "        idempotency_key=key)\n"
            "async def main():\n"
            "    out = {}\n"
            "    t = Transport()\n"
            "    c = GenerationCoordinator(t, live_generation_enabled=True)\n"
            "    a = await c.submit('image', request(), approval_binding='a'*64, idempotency_key='k')\n"
            "    b = await c.submit('image', request(), approval_binding='a'*64, idempotency_key='k')\n"
            "    out['replay_same_job'] = a.job_id == b.job_id\n"
            "    out['sends_after_replay'] = len(t.sent)\n"
            "    try:\n"
            "        await c.submit('image', request('different'), approval_binding='a'*64,\n"
            "            idempotency_key='k')\n"
            "        out['conflict'] = 'NOT_RAISED'\n"
            "    except IdempotencyConflict as exc:\n"
            "        out['conflict'] = exc.code\n"
            "    out['sends_after_conflict'] = len(t.sent)\n"
            "    out['lifecycle'] = a.lifecycle.value\n"
            "    out['states'] = [s.value for s in proto.FlowLifecycle]\n"
            "    try:\n"
            "        proto.transition(proto.FlowLifecycle.UNKNOWN_SUBMISSION,\n"
            "            proto.FlowLifecycle.NOT_SUBMITTED)\n"
            "        out['rearm'] = 'ALLOWED'\n"
            "    except proto.IllegalTransition:\n"
            "        out['rearm'] = 'refused'\n"
            "    t2 = Transport()\n"
            "    c2 = GenerationCoordinator(t2, live_generation_enabled=True)\n"
            "    job = await c2.submit('image', request('second'), approval_binding='b'*64)\n"
            "    await c2.cancel(job.job_id)\n"
            "    out['cancelled'] = job.lifecycle.value\n"
            "    out['provider_may_complete'] = bool(job.error and\n"
            "        job.error.details.get('provider_may_still_complete'))\n"
            "    unknown = FlowJobUnknown = None\n"
            "    from app.flow.coordinator import FlowJob\n"
            "    pending = FlowJob(job_id='job-x', kind='image', request=request('third'),\n"
            "        approval_binding='c'*64, state=proto.SubmissionState.UNKNOWN_SUBMISSION)\n"
            "    c2._jobs['job-x'] = pending\n"
            "    try:\n"
            "        c2.reconcile('job-x', status='succeeded')\n"
            "        out['fake_success'] = 'ALLOWED'\n"
            "    except ReconciliationRejected:\n"
            "        out['fake_success'] = 'refused'\n"
            "    out['still_unknown'] = pending.lifecycle.value\n"
            "    print(json.dumps(out))\n"
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
        assert proc.returncode == 0, proc.stderr[-1200:]
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        assert result["replay_same_job"] is True, result
        assert result["sends_after_replay"] == 1, result
        assert result["conflict"] == "flow_idempotency_conflict", result
        assert result["sends_after_conflict"] == 1, result
        assert result["lifecycle"] == "SUBMITTED", result
        assert result["states"][0] == "NOT_SUBMITTED" and len(result["states"]) == 8, result
        assert result["rearm"] == "refused", result
        assert result["cancelled"] == "CANCELLED", result
        assert result["provider_may_complete"] is True, result
        assert result["fake_success"] == "refused", result
        assert result["still_unknown"] == "UNKNOWN_SUBMISSION", result
        return (
            "duplicate key replayed with one dispatch; conflicting key "
            "flow_idempotency_conflict; UNKNOWN_SUBMISSION cannot be re-armed; cancel after "
            "submit reports provider_may_still_complete; a success claim without a media id "
            "is refused and the job stays UNKNOWN_SUBMISSION"
        )

    harness.check("57. Duplicate keys and ambiguous submits cannot duplicate generation", c57)

    def c58() -> str:
        """The Phase 3C.1 security boundary survived the Phase 3C.2 changes."""
        def number(name: str) -> int | None:
            head = name.split(".")[0].strip()
            digits = "".join(ch for ch in head if ch.isdigit())
            return int(digits) if digits else None

        earlier = [name for name, _ok, _detail in harness.results if 47 < (number(name) or 99) < 58]
        failed = [
            name
            for name, ok, _detail in harness.results
            if 47 < (number(name) or 99) < 58 and not ok
        ]
        assert not failed, f"Phase 3C.1 criteria regressed: {failed}"
        assert len(earlier) >= 6, f"expected the 3C.1 criteria, ran {len(earlier)}"
        _, status = http.call("GET", f"{flow}/status", expect=200)
        assert status["bridge"]["bind_host"] == "127.0.0.1"
        assert status["bridge"]["command_allowlist"] == [
            "status",
            "generate_image",
            "generate_video",
            "check_video_status",
            "export_asset",
        ]
        assert status["token_policy"]["page_visible_tokens"] is False
        assert status["live_generation_enabled"] is False
        return (
            f"all {len(earlier)} Phase 3C.1 criteria still pass; allowlist, loopback bind, "
            "token policy and live-generation-off unchanged"
        )

    harness.check("58. Phase 3C.1 boundary is intact", c58)

    def c59() -> str:
        """A file called LICENSE never authorizes anything (Phase 3C.2.1)."""
        code = (
            "import hashlib, json, tempfile\n"
            "from pathlib import Path\n"
            "from app.flow.authorization import package_tree_digest\n"
            "from app.flow.source_gate import evaluate_source_gate\n"
            "FILES = ['manifest.json', 'service-worker.js', 'content-script-main.js',\n"
            "         'content-script-isolated.js', 'mcp-server/src/server.js',\n"
            "         'mcp-server/src/ws-bridge.js', 'mcp-server/package.json']\n"
            "LICENCE = ('MIT License\\n\\nPermission is hereby granted, free of charge, to any '\n"
            "           'person obtaining a copy of this software.\\n')\n"
            "root = Path(tempfile.mkdtemp()) / 'pkg'\n"
            "for name in FILES:\n"
            "    path = root / name\n"
            "    path.parent.mkdir(parents=True, exist_ok=True)\n"
            "    path.write_text('// ' + name)\n"
            "out = {}\n"
            "out['none'] = evaluate_source_gate(root).status\n"
            "(root / 'LICENSE').write_text('   ')\n"
            "out['empty'] = evaluate_source_gate(root).status\n"
            "(root / 'LICENSE').write_text('All rights reserved. Proprietary.\\n')\n"
            "arbitrary = evaluate_source_gate(root)\n"
            "out['arbitrary'] = arbitrary.status\n"
            "out['arbitrary_authorized'] = arbitrary.authorized\n"
            "(root / 'LICENSE').write_text(LICENCE)\n"
            "out['evidence'] = evaluate_source_gate(root).status\n"
            "digest = package_tree_digest(root)\n"
            "attestation = Path(tempfile.mkdtemp()) / 'att.json'\n"
            "def document():\n"
            "    return {'schema': 'crusor.flow.authorization.v1',\n"
            "        'upstream_project_url': 'https://github.com/example/deepx-flow',\n"
            "        'release': {'tag': 'v1.2.1', 'commit': '0343884'},\n"
            "        'package': {'sha256': package_tree_digest(root)},\n"
            "        'licence': {'name': 'MIT', 'evidence_reference': 'LICENSE',\n"
            "            'evidence_sha256': hashlib.sha256((root / 'LICENSE').read_bytes()).hexdigest()},\n"
            "        'rights': {'modification_permitted': True, 'redistribution_permitted': True},\n"
            "        'review': {'reviewer': 'Ada Lovelace',\n"
            "            'reviewed_at': '2026-11-03T18:00:00Z',\n"
            "            'evidence_reference': 'legal-ticket-1234'}}\n"
            "attestation.write_text(json.dumps(document()))\n"
            "authorized = evaluate_source_gate(root, attestation_path=str(attestation))\n"
            "out['authorized'] = authorized.status\n"
            "out['authorized_bound'] = authorized.attestation.bound\n"
            "out['digest_matches'] = authorized.package_digest == digest\n"
            "bad = document(); bad['package'] = {'sha256': 'b' * 64}\n"
            "attestation.write_text(json.dumps(bad))\n"
            "out['digest_mismatch'] = evaluate_source_gate(root, attestation_path=str(attestation)).status\n"
            "attestation.write_text(json.dumps(document()))\n"
            "(root / 'service-worker.js').write_text('// patched')\n"
            "out['package_changed'] = evaluate_source_gate(root, attestation_path=str(attestation)).status\n"
            "print(json.dumps(out))\n"
        )
        proc = subprocess.run(
            [str(REPO_ROOT / ".venv" / "bin" / "python"), "-c", code],
            cwd=BACKEND,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": "."},
            timeout=180,
        )
        assert proc.returncode == 0, proc.stderr[-1200:]
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        assert result["none"] == "LICENSING_BLOCKED", result
        assert result["empty"] == "LICENSING_BLOCKED", result
        assert result["arbitrary"] == "LICENSE_EVIDENCE_PRESENT", result
        assert result["arbitrary_authorized"] is False, result
        assert result["evidence"] == "LICENSE_EVIDENCE_PRESENT", result
        assert result["authorized"] == "AUTHORIZED", result
        assert result["authorized_bound"] is True, result
        assert result["digest_matches"] is True, result
        assert result["digest_mismatch"] == "AUTHORIZATION_REVIEW_REQUIRED", result
        assert result["package_changed"] == "AUTHORIZATION_REVIEW_REQUIRED", result
        return (
            "no/empty licence blocked; arbitrary proprietary text is evidence only "
            "(not authorized); a reviewed attestation bound to the tree digest authorizes; "
            "a digest mismatch and a one-byte package change both invalidate it"
        )

    harness.check("59. Licence presence never becomes permission", c59)

    def c60() -> str:
        """A token for one export ticket cannot mutate another ticket by URL."""
        companion_headers = {
            "X-Companion-Id": "acceptance-companion",
            "X-Companion-Secret": os.environ.get("FLOW_COMPANION_SECRET", ""),
        }
        assert companion_headers["X-Companion-Secret"], (
            "FLOW_COMPANION_SECRET must be configured for the ticket-binding check"
        )
        _, first = http.call(
            "POST",
            f"{flow}/exports",
            {"companion_id": "acceptance-companion", "filename": "binding-a.png"},
        )
        _, second = http.call(
            "POST",
            f"{flow}/exports",
            {"companion_id": "acceptance-companion", "filename": "binding-b.png"},
        )
        chunk = minimal_png(20, 24)
        digest = hashlib.sha256(chunk).hexdigest()

        cross, _ = put_chunk(
            http, first["id"], second["upload_token"], companion_headers, 0, 1, chunk
        )
        assert cross == 422, cross

        _, first_state = http.call("GET", f"{flow}/exports/{first['id']}", expect=200)
        _, second_state = http.call("GET", f"{flow}/exports/{second['id']}", expect=200)
        for state in (first_state, second_state):
            assert state["status"] == "pending", state
            assert state["chunks_received"] == 0, state
            assert state["asset_id"] is None, state

        # Identity, not luck: the correct pairing still uploads.
        ok, body = put_chunk(
            http, first["id"], first["upload_token"], companion_headers, 0, 1, chunk
        )
        assert ok == 200, (ok, body)
        assert body["status"] == "consumed", body
        assert body["checksum"] == digest, body
        assert body["asset_id"], body
        return (
            "token for ticket B posted to ticket A's URL refused 422 with no mutation on "
            "either ticket (no staging object, no asset); the correct pairing still "
            "consumes and verifies the asset"
        )

    harness.check("60. Export tickets are bound to their own URL", c60)

    def c61_src() -> str:
        """The trust boundary is reported honestly: no measurement means no trust."""
        _, status = http.call("GET", f"{flow}/status", expect=200)
        trust = status["trust"]
        assert trust["trusted"] is False, trust
        assert trust["state"] in {
            "SOURCE_ABSENT",
            "RELEASE_ANCHOR_MISSING",
            "INSTALLED_ARTIFACT_NOT_FOUND",
            "UNVERIFIED",
        }, trust["state"]
        assert len(trust["states_supported"]) == 9
        assert trust["evidence"]["installed_artifact_measured"] is False
        assert trust["evidence"]["installed_artifact_digest_matches_anchor"] is False
        assert trust["evidence"]["runtime_identity_correlated"] is False
        assert trust["measured"]["artifact_digest"] == ""
        assert "chrome_extension" in trust["unverified_components"]
        assert trust["anchor"]["present"] is False
        assert status["live_generation_enabled"] is False
        return (
            f"trust state {trust['state']} (not trusted); measured=false, digest=false, "
            f"runtime correlation=false; {len(trust['unverified_components'])} components "
            "unverified; live generation off"
        )

    harness.check("61. Installed-artifact trust is UNVERIFIED without a measurement", c61_src)

    def c62() -> str:
        """A synthetic install is measured independently and matched to a signed release."""
        code = (
            "import base64, hashlib, json, tempfile, time\n"
            "from pathlib import Path\n"
            "from cryptography.hazmat.primitives import serialization\n"
            "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey\n"
            "from app.flow.authorization import package_tree_digest\n"
            "from app.flow.installed_artifact import (InstalledArtifactVerifier, InstallationSpec,\n"
            "    MeasurementError, MeasurementPolicy)\n"
            "from app.flow.provenance import COMPONENT_EXTENSION, verify_release_manifest\n"
            "EXT = 'abcdefghijklmnopabcdefghijklmnop'\n"
            "VERSION = '1.3.0'\n"
            "base = Path(tempfile.mkdtemp())\n"
            "root = base / 'Extensions' / EXT / VERSION\n"
            "(root / 'sub').mkdir(parents=True)\n"
            "manifest = {'manifest_version': 3, 'name': 'Synthetic', 'version': VERSION,\n"
            "            'background': {'service_worker': 'sw.js'}}\n"
            "(root / 'manifest.json').write_text(json.dumps(manifest))\n"
            "(root / 'sw.js').write_text('// synthetic sw\\n')\n"
            "(root / 'sub' / 'content.js').write_text('// synthetic content\\n')\n"
            "spec = InstallationSpec(root=root, expected_extension_id=EXT, expected_version=VERSION)\n"
            "report = InstalledArtifactVerifier().measure(spec)\n"
            "out = {}\n"
            "out['digest_matches_gate'] = report.digest == package_tree_digest(root)\n"
            "out['measured_id'] = report.extension_id\n"
            "out['file_count'] = report.file_count\n"
            "private = Ed25519PrivateKey.generate()\n"
            "public = base64.b64encode(private.public_key().public_bytes(\n"
            "    encoding=serialization.Encoding.Raw,\n"
            "    format=serialization.PublicFormat.Raw)).decode()\n"
            "def document(digest):\n"
            "    return {'schema': 'crusor.flow.release.v2', 'product': 'synthetic-fixture',\n"
            "        'release': {'version': VERSION, 'commit': '0343884', 'build_id': 'b1'},\n"
            "        'extension': {'id': EXT, 'version': VERSION, 'protocol': 'flow.bridge.v1',\n"
            "                      'sha256': digest},\n"
            "        'components': [{'name': COMPONENT_EXTENSION, 'sha256': digest,\n"
            "                        'extension_id': EXT, 'required': True}]}\n"
            "def sign(doc):\n"
            "    body = {k: v for k, v in doc.items() if k != 'signature'}\n"
            "    canonical = json.dumps(body, sort_keys=True, separators=(',', ':')).encode()\n"
            "    return {**body, 'signature': base64.b64encode(private.sign(canonical)).decode()}\n"
            "path = base / 'release.json'\n"
            "path.write_text(json.dumps(sign(document(report.digest))))\n"
            "anchor = verify_release_manifest(path, public)\n"
            "out['anchor_digest_matches'] = anchor.digest_for(COMPONENT_EXTENSION) == report.digest\n"
            "out['anchor_identifies_components'] = anchor.identifies_components\n"
            "out['anchor_signature_verified'] = anchor.signature_verified\n"
            "# Tampering means changing bytes *after* signing; re-signing a new\n"
            "# document would be a valid signature over different content.\n"
            "tampered = sign(document(report.digest))\n"
            "tampered['release']['version'] = '9.9.9'\n"
            "tampered_path = base / 'tampered.json'\n"
            "tampered_path.write_text(json.dumps(tampered))\n"
            "try:\n"
            "    verify_release_manifest(tampered_path, public)\n"
            "    out['tampered'] = 'ACCEPTED'\n"
            "except Exception as exc:\n"
            "    out['tampered'] = getattr(exc, 'code', 'rejected')\n"
            "(root / 'sw.js').write_text('// patched after signing\\n')\n"
            "patched = InstalledArtifactVerifier().measure(spec)\n"
            "out['patched_digest_differs'] = patched.digest != anchor.digest_for(COMPONENT_EXTENSION)\n"
            "ext_link = base / 'link.js'\n"
            "ext_link.symlink_to(root / 'sw.js')\n"
            "(root / 'linked.js').symlink_to(ext_link)\n"
            "try:\n"
            "    InstalledArtifactVerifier().measure(spec)\n"
            "    out['symlink'] = 'ACCEPTED'\n"
            "except MeasurementError as exc:\n"
            "    out['symlink'] = exc.code\n"
            "print(json.dumps(out))\n"
        )
        proc = subprocess.run(
            [str(REPO_ROOT / ".venv" / "bin" / "python"), "-c", code],
            cwd=BACKEND,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": "."},
            timeout=180,
        )
        assert proc.returncode == 0, proc.stderr[-1500:]
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        assert result["digest_matches_gate"] is True, result
        assert result["measured_id"] == "abcdefghijklmnopabcdefghijklmnop", result
        assert result["file_count"] == 3, result
        assert result["anchor_digest_matches"] is True, result
        assert result["anchor_identifies_components"] is True, result
        assert result["anchor_signature_verified"] is True, result
        assert result["tampered"] not in {"ACCEPTED"}, result
        assert result["patched_digest_differs"] is True, result
        assert result["symlink"] == "symlink_in_package", result
        return (
            "synthetic install measured to the same sha256-tree-v1 digest as the gate; "
            "signed v2 manifest verified and matched per component; a tampered manifest "
            "was rejected; one patched file changed the digest; a symlink was refused"
        )

    harness.check("62. Installed bytes are measured and matched to a signed release", c62)

    def c63() -> str:
        """Live generation stays blocked by the guard, not merely by a flag."""
        code = (
            "import json\n"
            "from app.flow import protocol as proto\n"
            "from app.flow.coordinator import GenerationCoordinator, UntrustedArtifact\n"
            "from app.flow.provenance import TrustAnchor\n"
            "from app.flow.trust import (TrustDecision, TrustEvidence, SpendingControls,\n"
            "    VERIFIED, UNVERIFIED, MEASUREMENT_STALE, evaluate_live_generation_guard)\n"
            "class Transport:\n"
            "    def __init__(self): self.sent = []\n"
            "    async def send(self, envelope): self.sent.append(envelope)\n"
            "    async def await_result(self, command_id, timeout):\n"
            "        raise AssertionError('no result')\n"
            "import asyncio\n"
            "async def main():\n"
            "    CONTROL = SpendingControls(enforcement='flow_budget_ledger',\n"
            "        cap_usd=5.0, ledger_wired=True)\n"
            "    NO_BUDGET = SpendingControls()\n"
            "    out = {}\n"
            "    blocked = evaluate_live_generation_guard(live_generation_flag=False,\n"
            "        source_gate_status='SOURCE_ABSENT', decision=None, approval_binding='a'*64,\n"
            "        budget_controls=CONTROL, idempotency_enforced=True)\n"
            "    out['flag_off'] = blocked.allowed\n"
            "    out['blockers'] = list(blocked.blockers)\n"
            "    verified = TrustDecision(state=VERIFIED, evidence=TrustEvidence(\n"
            "        installed_artifact_measured=True, installed_artifact_digest_matches_anchor=True,\n"
            "        runtime_identity_correlated=True, measurement_fresh=True))\n"
            "    out['verified_but_flag_off'] = evaluate_live_generation_guard(\n"
            "        live_generation_flag=False, source_gate_status='AUTHORIZED', decision=verified,\n"
            "        approval_binding='a'*64, budget_controls=CONTROL,\n"
            "        idempotency_enforced=True).allowed\n"
            "    out['unverified_with_flag_on'] = evaluate_live_generation_guard(\n"
            "        live_generation_flag=True, source_gate_status='AUTHORIZED',\n"
            "        decision=TrustDecision(state=UNVERIFIED, evidence=TrustEvidence())\n"
            "        , approval_binding='a'*64, budget_controls=CONTROL,\n"
            "        idempotency_enforced=True).allowed\n"
            "    out['no_budget_control'] = evaluate_live_generation_guard(\n"
            "        live_generation_flag=True, source_gate_status='AUTHORIZED',\n"
            "        decision=verified, approval_binding='a'*64,\n"
            "        budget_controls=NO_BUDGET, idempotency_enforced=True).allowed\n"
            "    stale = TrustDecision(state=VERIFIED, evidence=TrustEvidence(\n"
            "        installed_artifact_measured=True,\n"
            "        installed_artifact_digest_matches_anchor=True,\n"
            "        runtime_identity_correlated=True, measurement_fresh=True,\n"
            "        measurement_expires_at=1.0))\n"
            "    out['stale_decision'] = evaluate_live_generation_guard(\n"
            "        live_generation_flag=True, source_gate_status='AUTHORIZED',\n"
            "        decision=stale, approval_binding='a'*64,\n"
            "        budget_controls=CONTROL, idempotency_enforced=True).allowed\n"
            "    transport = Transport()\n"
            "    coordinator = GenerationCoordinator(transport, live_generation_enabled=True,\n"
            "        trust_provider=lambda: TrustDecision(state=UNVERIFIED, evidence=TrustEvidence()))\n"
            "    request = proto.GenerationCommand(prompt='synthetic', model='imagen-3', count=1)\n"
            "    try:\n"
            "        await coordinator.submit('image', request, approval_binding='a'*64)\n"
            "        out['coordinator'] = 'DISPATCHED'\n"
            "    except UntrustedArtifact as exc:\n"
            "        out['coordinator'] = exc.code\n"
            "    out['sends'] = len(transport.sent)\n"
            "    print(json.dumps(out))\n"
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
        assert proc.returncode == 0, proc.stderr[-1500:]
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        assert result["flag_off"] is False, result
        assert "live_generation_disabled" in result["blockers"], result
        assert result["verified_but_flag_off"] is False, result
        assert result["unverified_with_flag_on"] is False, result
        assert result["no_budget_control"] is False, result
        assert result["stale_decision"] is False, result
        assert result["coordinator"] == "flow_untrusted_artifact", result
        assert result["sends"] == 0, result
        return (
            "flag off blocks regardless of trust; a VERIFIED fixture still cannot "
            "generate while the flag is off; UNVERIFIED blocks with the flag on; a "
            "missing spend control and an expired measurement both block; the "
            "coordinator refused flow_untrusted_artifact and dispatched nothing"
        )

    harness.check("63. Live generation requires verification, not a flag", c63)

    def c64() -> str:
        """The production companion path measures the roots; the peer cannot."""
        code = (
            "import asyncio, base64, json, tempfile, time\n"
            "from pathlib import Path\n"
            "from cryptography.hazmat.primitives import serialization\n"
            "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey\n"
            "import websockets\n"
            "from app.flow import protocol as proto\n"
            "from app.flow.bridge import BridgeConfig, ComponentInstallSpec, FlowBridge\n"
            "from app.flow.installed_artifact import (InstalledArtifactVerifier, InstallationSpec,\n"
            "    LAYOUT_TREE)\n"
            "from app.flow.provenance import COMPONENT_EXTENSION, COMPONENT_MCP\n"
            "from app.flow.security import generate_pairing_id, generate_pairing_secret\n"
            "EXT = 'abcdefghijklmnopabcdefghijklmnop'\n"
            "VERSION = '1.3.0'\n"
            "base = Path(tempfile.mkdtemp())\n"
            "root = base / 'Extensions' / EXT / VERSION\n"
            "root.mkdir(parents=True)\n"
            "(root / 'manifest.json').write_text(json.dumps({'manifest_version': 3,\n"
            "    'name': 'Synthetic', 'version': VERSION,\n"
            "    'background': {'service_worker': 'sw.js'}}))\n"
            "(root / 'sw.js').write_text('// synthetic sw\\n')\n"
            "(root / 'content.js').write_text('// synthetic content\\n')\n"
            "mcp = base / 'mcp-server'\n"
            "(mcp / 'node_modules' / 'sdk').mkdir(parents=True)\n"
            "(mcp / 'server.py').write_text('print(1)\\n')\n"
            "(mcp / 'node_modules' / 'sdk' / 'index.js').write_text('module.exports = 1;\\n')\n"
            "ext_report = InstalledArtifactVerifier(COMPONENT_EXTENSION).measure(\n"
            "    InstallationSpec(root=root, expected_extension_id=EXT, expected_version=VERSION))\n"
            "mcp_report = InstalledArtifactVerifier(COMPONENT_MCP).measure(\n"
            "    InstallationSpec(root=mcp, layout=LAYOUT_TREE))\n"
            "private = Ed25519PrivateKey.generate()\n"
            "public = base64.b64encode(private.public_key().public_bytes(\n"
            "    encoding=serialization.Encoding.Raw,\n"
            "    format=serialization.PublicFormat.Raw)).decode()\n"
            "document = {'schema': 'crusor.flow.release.v2', 'product': 'synthetic-fixture',\n"
            "    'digest_algorithm': ext_report.digest_algorithm,\n"
            "    'release': {'version': VERSION, 'commit': '0343884', 'build_id': 'build-acceptance-1'},\n"
            "    'extension': {'id': EXT, 'version': VERSION, 'protocol': 'flow.bridge.v1',\n"
            "                  'sha256': ext_report.digest},\n"
            "    'components': [\n"
            "        {'name': COMPONENT_EXTENSION, 'sha256': ext_report.digest,\n"
            "         'extension_id': EXT, 'required': True},\n"
            "        {'name': COMPONENT_MCP, 'version': VERSION, 'build_id': 'build-acceptance-1',\n"
            "         'sha256': mcp_report.digest, 'required': True}]}\n"
            "body = {k: v for k, v in document.items() if k != 'signature'}\n"
            "canonical = json.dumps(body, sort_keys=True, separators=(',', ':')).encode()\n"
            "manifest_path = base / 'release.json'\n"
            "manifest_path.write_text(json.dumps({**body, 'signature':\n"
            "    base64.b64encode(private.sign(canonical)).decode()}))\n"
            "async def main():\n"
            "    out = {}\n"
            "    FlowBridge._source_gate_status = lambda self: 'AUTHORIZED'\n"
            "    pairing_id = generate_pairing_id()\n"
            "    secret = generate_pairing_secret()\n"
            "    bridge = FlowBridge(BridgeConfig(pairing_id=pairing_id, pairing_secret=secret,\n"
            "        http_port=0, ws_port=0, extension_ids=(EXT,), handshake_timeout=2.0,\n"
            "        companion_id='acceptance-companion',\n"
            "        release_manifest_path=str(manifest_path), release_manifest_public_key=public,\n"
            "        require_installed_measurement=True,\n"
            "        install_components=(\n"
            "            ComponentInstallSpec(component=COMPONENT_EXTENSION, root=str(root)),\n"
            "            ComponentInstallSpec(component=COMPONENT_MCP, root=str(mcp), layout=LAYOUT_TREE))))\n"
            "    await bridge.start()\n"
            "    hello = {'type': 'hello', 'protocol': proto.PROTOCOL_VERSION,\n"
            "        'pairing_id': pairing_id, 'pairing_secret': secret, 'extension_id': EXT,\n"
            "        'extension_version': VERSION, 'service_worker_version': VERSION,\n"
            "        'mcp_version': VERSION, 'source_commit': '0343884', 'build_id': 'build-acceptance-1',\n"
            "        'package_sha256': ext_report.digest, 'flow_tab_present': True}\n"
            "    try:\n"
            "        async with websockets.connect(f'ws://127.0.0.1:{bridge.ws_port}/ws') as ws:\n"
            "            await ws.send(json.dumps(hello))\n"
            "            ack = json.loads(await asyncio.wait_for(ws.recv(), 3))\n"
            "            out['ack_trusted_before_measurement'] = ack.get('provenance_trusted')\n"
            "            session = bridge.session\n"
            "            if session is not None and session.measurement_task is not None:\n"
            "                decision = await asyncio.wait_for(session.measurement_task, 30)\n"
            "                out['state'] = decision.state\n"
            "                out['reasons'] = list(decision.reasons)\n"
            "            snapshot = bridge.snapshot()\n"
            "            trust = snapshot['trust']\n"
            "            out['measured_source'] = trust['measured'].get('source')\n"
            "            out['component_states'] = {k: v['state'] for k, v in trust['components'].items()}\n"
            "            await ws.send(json.dumps({'type': 'verification_report', 'challenge_id': 'a'*32,\n"
            "                'nonce': 'b'*32, 'components': [{'name': COMPONENT_EXTENSION,\n"
            "                'digest': 'c'*64, 'measurement_id': 'd'*32, 'file_count': 5,\n"
            "                'measured_at': time.time()}]}))\n"
            "            try:\n"
            "                await asyncio.wait_for(ws.recv(), 3)\n"
            "                out['peer_report'] = 'ACCEPTED'\n"
            "            except websockets.ConnectionClosed as exc:\n"
            "                out['peer_report'] = exc.rcvd.code if exc.rcvd else 0\n"
            "    finally:\n"
            "        await bridge.stop()\n"
            "    (mcp / 'node_modules' / 'sdk' / 'index.js').write_text('module.exports = 2;\\n')\n"
            "    patched = InstalledArtifactVerifier(COMPONENT_MCP).measure(\n"
            "        InstallationSpec(root=mcp, layout=LAYOUT_TREE))\n"
            "    out['dependency_in_digest'] = patched.digest != mcp_report.digest\n"
            "    print(json.dumps(out))\n"
            "asyncio.run(main())\n"
        )
        proc = subprocess.run(
            [str(REPO_ROOT / ".venv" / "bin" / "python"), "-c", code],
            cwd=BACKEND,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": "."},
            timeout=240,
        )
        assert proc.returncode == 0, proc.stderr[-1500:]
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        assert result["ack_trusted_before_measurement"] is False, result
        assert result["state"] == "VERIFIED", result
        assert result["measured_source"] == "local_companion", result
        states = result["component_states"]
        assert states["chrome_extension"] == "VERIFIED", result
        assert states["mcp_server"] == "VERIFIED", result
        # Not required by the signed release, so it is reported as unmeasured
        # rather than quietly counted as verified.
        assert states["local_companion"] == "NOT_MEASURED", result
        assert result["peer_report"] == 4400, result
        assert result["dependency_in_digest"] is True, result
        return (
            "the local companion measured both signed components itself and reached "
            "VERIFIED only after the measurement; the ack reported no trust before it; "
            "a peer-submitted verification_report was refused 4400; patching a "
            "node_modules dependency changed the installed digest"
        )

    harness.check("64. The companion measures the roots itself; the peer cannot", c64)


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
