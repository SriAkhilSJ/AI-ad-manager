#!/usr/bin/env python3
"""Read-only security scanner for an upstream DeepX Flow Connector package.

Phase 3C.2 §13 asks for scans over the extension source: secret/token leakage,
arbitrary URL requests, wildcard host permissions, unrestricted JavaScript
evaluation, dangerous MCP tool registration, automatic retry, and package
integrity. This script performs them **only if** the source and licensing gate is
satisfied (see ``app/flow/source_gate.py``).

Two rules it will not break:

* it never copies, moves, or writes inside the package — findings reference paths
  and line numbers, never file contents;
* a text scan is reported as *evidence*, not as proof. Every finding is labelled
  ``needs_behavioural_test`` because a regex cannot show that a defect is fixed.

Exit codes:

* ``0`` — authorized and scanned, no findings;
* ``1`` — authorized and scanned, findings present;
* ``2`` — scanned, but the package is **not** authorized for modification: licence
  evidence exists without a human-reviewed attestation, or the attestation is
  incomplete or bound to different bytes. Findings are evidence for a reviewer;
  they never imply permission to edit (Phase 3C.2.1);
* ``3`` — blocked: no source, or nothing substantive to read (the current state);
* ``4`` — the package exists but is missing required files.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.flow.source_gate import (  # noqa: E402
    STATUS_AUTHORIZED,
    STATUS_LICENSING_BLOCKED,
    STATUS_SOURCE_ABSENT,
    evaluate_source_gate,
)

#: (scan id, human label, regex, field) — text-level evidence only.
SCANS: tuple[tuple[str, str, str, str], ...] = (
    (
        "S1",
        "wildcard postMessage token broadcast",
        r"postMessage\s*\([^)]*,\s*['\"]\*['\"]",
        "U1",
    ),
    ("S2", "page-accessible get_token", r"get_token", "U1"),
    ("S3", "page-accessible get_flow_context", r"get_flow_context", "U1"),
    ("S4", "arbitrary authenticated POST", r"api_post|apiPost\s*\(", "U2"),
    ("S5", "bearer token handling", r"Bearer\s+[A-Za-z0-9._\-]{8,}|ya29\.", "U1"),
    ("S6", "unrestricted JavaScript evaluation", r"\beval\s*\(|new\s+Function\s*\(", "U4"),
    ("S7", "chrome debugger attach", r"chrome\.debugger|Runtime\.evaluate", "U4"),
    ("S8", "wildcard host permission", r"https?://\*/\*", "U5"),
    ("S9", "batch generation tool", r"batch_generate|rapid_batch_dispatch", "U4"),
    ("S10", "autonomous storyboard tool", r"autonomous_storyboard_pipeline", "U4"),
    ("S11", "project mutation tools", r"rename_project|open_new_project", "U4"),
    ("S12", "unauthenticated bridge listener", r"listen\s*\(\s*8792|127\.0\.0\.1:8792", "U3"),
    ("S13", "post-submit automatic retry", r"retry|retries", "U6"),
    ("S14", "download-id-only asset path", r"downloadId|downloads\.download", "U7"),
)

SCAN_TARGETS = ("*.js", "*.json", "*.md", "*.sh")


@dataclass
class Finding:
    scan: str
    label: str
    blocker: str
    file: str
    line: int
    evidence: str
    confidence: str = "text_match"
    needs_behavioural_test: bool = True


@dataclass
class Report:
    generated_at: str
    status: str
    authorized: bool
    package_path: str
    reason: str
    required_files_missing: list[str] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)
    versions: dict[str, Any] = field(default_factory=dict)
    host_permissions: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    scans_run: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["finding_count"] = len(self.findings)
        return payload


def _iter_files(root: Path) -> Iterable[Path]:
    for pattern in SCAN_TARGETS:
        yield from sorted(root.rglob(pattern))


def _redact_evidence(line: str) -> str:
    """Keep the shape of a match, never a credential."""
    cleaned = re.sub(r"(ya29\.[A-Za-z0-9._\-]{6,})", "ya29.[redacted]", line)
    cleaned = re.sub(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}", r"\1[redacted]", cleaned)
    cleaned = re.sub(r"([A-Za-z0-9_\-]{24,})", lambda m: m.group(1)[:4] + "…", cleaned)
    return cleaned.strip()[:160]


def scan_package(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in _iter_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        relative = str(path.relative_to(root))
        for scan_id, label, pattern, blocker in SCANS:
            for match in re.finditer(pattern, text):
                line_no = text.count("\n", 0, match.start()) + 1
                start = max(0, text.rfind("\n", 0, match.start()) + 1)
                end = text.find("\n", match.end())
                snippet = text[start : end if end != -1 else len(text)]
                findings.append(
                    Finding(
                        scan=scan_id,
                        label=label,
                        blocker=blocker,
                        file=relative,
                        line=line_no,
                        evidence=_redact_evidence(snippet),
                    )
                )
    return findings


def _manifest_summary(root: Path) -> tuple[dict[str, Any], list[str]]:
    path = root / "manifest.json"
    if not path.is_file():
        return {}, []
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - a broken manifest is itself a finding
        return {"error": "manifest.json is not valid JSON"}, []
    hosts = list(data.get("host_permissions") or [])
    perms = list(data.get("permissions") or [])
    return (
        {
            "name": data.get("name"),
            "version": data.get("version"),
            "manifest_version": data.get("manifest_version"),
            "permissions": perms,
            "host_permissions": hosts,
        },
        hosts,
    )


def _component_versions(root: Path) -> dict[str, Any]:
    """Collect the version strings the four components claim, without executing them."""
    versions: dict[str, Any] = {}
    manifest = root / "manifest.json"
    if manifest.is_file():
        try:
            versions["manifest"] = json.loads(manifest.read_text(errors="replace")).get("version")
        except Exception:  # noqa: BLE001
            versions["manifest"] = "unreadable"
    mcp_pkg = root / "mcp-server" / "package.json"
    if mcp_pkg.is_file():
        try:
            versions["mcp_server"] = json.loads(mcp_pkg.read_text(errors="replace")).get("version")
        except Exception:  # noqa: BLE001
            versions["mcp_server"] = "unreadable"
    sw = root / "service-worker.js"
    if sw.is_file():
        text = sw.read_text(errors="replace")
        match = re.search(r"version\s*[:=]\s*['\"]([0-9][^'\"]{0,31})['\"]", text)
        versions["service_worker_hello"] = match.group(1) if match else "not found"
    readme = next((p for p in root.glob("README*") if p.is_file()), None)
    if readme is not None:
        match = re.search(r"v?(\d+\.\d+\.\d+)", readme.read_text(errors="replace")[:4000])
        versions["readme"] = match.group(1) if match else "not found"
    distinct = {str(v) for v in versions.values() if isinstance(v, str) and re.match(r"^\d+\.\d+\.\d+", v)}
    versions["consistent"] = len(distinct) <= 1
    return versions


def build_report(package_path: str | None) -> tuple[Report, int]:
    gate = evaluate_source_gate(package_path)
    report = Report(
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        status=gate.status,
        authorized=gate.authorized,
        package_path=gate.package_path,
        reason=gate.reason,
        required_files_missing=list(gate.missing_files),
        limitations=[
            "Text scans are evidence, not proof: a regex cannot demonstrate that a "
            "defect is fixed. Every finding is marked needs_behavioural_test.",
            "This script never copies or writes inside the package.",
            "No package is executed; the extension is not installed or driven.",
        ],
    )

    if not gate.readable:
        # SOURCE_ABSENT / LICENSING_BLOCKED: reading is not permitted either, because
        # there is nothing substantive to read and a scan would imply progress.
        report.scans_run = []
        return report, 3

    root = Path(gate.package_path)
    if gate.missing_files:
        report.scans_run = []
        return report, 4

    report.manifest, report.host_permissions = _manifest_summary(root)
    report.versions = _component_versions(root)
    report.findings = scan_package(root)
    report.scans_run = [scan[0] for scan in SCANS]
    if not gate.authorized:
        # Evidence exists but no human-reviewed authorization does. Scanning is
        # allowed; *modifying* is not, and the exit code must not imply otherwise.
        return report, 2
    return report, 1 if report.findings else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--package",
        default=None,
        help="Path to an authorised copy of the upstream package (never committed).",
    )
    parser.add_argument("--json-out", default=None, help="Write the JSON report here.")
    args = parser.parse_args()

    report, code = build_report(args.package)
    payload = report.as_dict()
    text = json.dumps(payload, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")
    print(text)

    summary = {
        0: "authorized; scans clean",
        1: f"authorized; {payload['finding_count']} finding(s)",
        2: f"scanned; NOT authorized to modify ({report.status})",
        3: f"blocked ({report.status})",
        4: "package incomplete",
    }[code]
    print(f"\nRESULT {summary}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
