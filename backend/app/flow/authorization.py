"""Human-reviewed authorization evidence for third-party source (Phase 3C.2.1).

Phase 3C.2 allowed a single file called ``LICENSE`` — containing anything at all,
including an empty file — to flip the source gate to ``AUTHORIZED``. That is a
filename check pretending to be a legal determination, and it is exactly the kind
of "looks verified" claim this project is not allowed to make.

Phase 3C.2.1 separates two things that were previously conflated:

**Evidence** — files inside the source package that *look* like a licence or a
permission statement. Crusor can describe them (name, size, SHA-256) but cannot
interpret them. The most they can do is move the gate off ``SOURCE_ABSENT``.

**Attestation** — a record, held *outside* the package, in which a named human
states what they reviewed, what rights they concluded apply, and which exact
bytes they reviewed. It is machine-checked for completeness and for binding to
the package's tree digest, but the *decision* it records is a human one.

The gate is authorized only when both exist **and** the attestation is complete,
internally consistent, written by a reviewer, and bound to the digest of the
package that is actually present. Nothing here decides what a licence means; it
only refuses to proceed without a human who did.

Attestation format (``FLOW_AUTHORIZATION_ATTESTATION_PATH``, JSON)::

    {
      "schema": "crusor.flow.authorization.v1",
      "upstream_project_url": "https://github.com/<owner>/<repo>",
      "release": {"tag": "v1.2.1", "commit": "<7-40 hex>"},
      "package": {"sha256": "<64 hex, tree digest of the package>"},
      "licence": {
        "name": "MIT",
        "evidence_reference": "LICENSE",
        "evidence_sha256": "<64 hex of that file>"
      },
      "rights": {
        "modification_permitted": true,
        "redistribution_permitted": true
      },
      "review": {
        "reviewer": "Full Name <email>",
        "reviewed_at": "2026-11-03T18:00:00Z",
        "evidence_reference": "legal-ticket-1234"
      }
    }
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

ATTESTATION_SCHEMA = "crusor.flow.authorization.v1"

SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
#: A licence document must carry *some* text. An empty or whitespace-only file is
#: not evidence of anything, whatever it is called.
MIN_EVIDENCE_BYTES = 8

#: Directories that are never part of a package digest: VCS internals and the
#: artefacts of having run the tooling in the same tree.
_DIGEST_EXCLUDES = {".git", "__pycache__", "node_modules", ".venv", ".mypy_cache"}


def tree_digest_from_pairs(pairs: Iterable[tuple[str, bytes]]) -> str:
    """The single ``sha256-tree-v1`` implementation.

    Sorted by POSIX relative path; each path and payload length-prefixed so moving
    bytes between files cannot produce the same digest. Every producer of a package
    digest — the authorization gate and, since Phase 3C.2.2, the installed-artifact
    verifier — must call this function rather than reimplementing the encoding, or
    two "matching" digests could disagree about what they measured.
    """
    digest = hashlib.sha256()
    for relative, data in sorted(pairs, key=lambda item: item[0]):
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def digest_excluded(relative: PurePosixPath | Path) -> bool:
    """Paths never included in a tree digest: VCS internals and build caches."""
    return bool(set(PurePosixPath(relative).parts) & _DIGEST_EXCLUDES)


def package_tree_digest(root: str | Path) -> str:
    """Deterministic SHA-256 over the package's relative paths and file bytes.

    Symbolic links and anything outside the root are ignored: a package digest
    must describe the files actually in the package, not what they point at.
    """
    base = Path(root).resolve()
    entries: list[tuple[str, bytes]] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(base)
        if digest_excluded(relative):
            continue
        entries.append((relative.as_posix(), path.read_bytes()))
    return tree_digest_from_pairs(entries)


@dataclass(frozen=True, slots=True)
class EvidenceFile:
    """A licence-like file found in the package. Described, never interpreted."""

    name: str
    size: int
    sha256: str

    @property
    def substantive(self) -> bool:
        return self.size >= MIN_EVIDENCE_BYTES

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "size": self.size,
            "sha256": self.sha256,
            "substantive": self.substantive,
        }


def scan_evidence(root: str | Path, names: tuple[str, ...]) -> tuple[EvidenceFile, ...]:
    base = Path(root)
    found: list[EvidenceFile] = []
    for name in names:
        path = base / name
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        found.append(
            EvidenceFile(name=name, size=len(data), sha256=hashlib.sha256(data).hexdigest())
        )
    return tuple(found)


@dataclass(frozen=True, slots=True)
class AuthorizationAttestation:
    """A human-reviewed record that a specific package may be modified.

    ``valid`` means *structurally complete and consistent*; ``bound`` means the
    digest it records is the digest of the package on disk. Only ``valid and
    bound`` may authorize anything.
    """

    present: bool = False
    path: str = ""
    schema: str = ""
    upstream_project_url: str = ""
    release_tag: str = ""
    release_commit: str = ""
    package_sha256: str = ""
    licence_name: str = ""
    evidence_reference: str = ""
    evidence_sha256: str = ""
    modification_permitted: bool = False
    redistribution_permitted: bool = False
    reviewer: str = ""
    reviewed_at: str = ""
    review_reference: str = ""
    problems: tuple[str, ...] = field(default_factory=tuple)
    bound: bool = False
    observed_package_sha256: str = ""

    @property
    def valid(self) -> bool:
        return self.present and not self.problems

    @property
    def authorized(self) -> bool:
        return bool(
            self.valid
            and self.bound
            and self.modification_permitted
            and self.redistribution_permitted
        )

    def as_dict(self) -> dict[str, Any]:
        """The reviewed facts. Never the raw document, never a signature."""
        return {
            "present": self.present,
            "path": self.path,
            "schema": self.schema,
            "valid": self.valid,
            "bound_to_package": self.bound,
            "authorized": self.authorized,
            "upstream_project_url": self.upstream_project_url,
            "release_tag": self.release_tag,
            "release_commit": self.release_commit,
            "package_sha256": self.package_sha256,
            "observed_package_sha256": self.observed_package_sha256,
            "licence_name": self.licence_name,
            "evidence_reference": self.evidence_reference,
            "evidence_sha256": self.evidence_sha256,
            "modification_permitted": self.modification_permitted,
            "redistribution_permitted": self.redistribution_permitted,
            "reviewer": self.reviewer,
            "reviewed_at": self.reviewed_at,
            "review_reference": self.review_reference,
            "problems": list(self.problems),
        }


def _text(value: Any, *, field_name: str, problems: list[str], minimum: int = 1) -> str:
    text = str(value or "").strip()
    if len(text) < minimum:
        problems.append(f"{field_name}_missing")
    return text


def _flag(value: Any, *, field_name: str, problems: list[str]) -> bool:
    # A string "true" is not a reviewer's assertion that they checked the right;
    # only a JSON boolean is. Anything else is treated as absent.
    if value is not True:
        problems.append(f"{field_name}_not_asserted")
        return False
    return True


def _timestamp(value: Any, *, problems: list[str]) -> str:
    text = str(value or "").strip()
    if not text:
        problems.append("reviewed_at_missing")
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        problems.append("reviewed_at_not_iso8601")
        return text
    if parsed.tzinfo is None:
        problems.append("reviewed_at_without_timezone")
    return text


def load_attestation(
    path: str | Path | None,
    *,
    package_root: str | Path | None = None,
    observed_digest: str = "",
    evidence: tuple[EvidenceFile, ...] = (),
) -> AuthorizationAttestation:
    """Read and mechanically check an attestation. Absent is a valid outcome."""
    raw_path = str(path or "").strip()
    if not raw_path:
        return AuthorizationAttestation(present=False, observed_package_sha256=observed_digest)

    location = Path(raw_path).expanduser()
    if not location.is_file():
        return AuthorizationAttestation(
            present=False,
            path=raw_path,
            problems=("attestation_file_missing",),
            observed_package_sha256=observed_digest,
        )

    problems: list[str] = []

    # Evidence must live outside the package: a document shipped inside the thing
    # it authorizes cannot be independently verified.
    if package_root:
        try:
            location.resolve().relative_to(Path(package_root).resolve())
            problems.append("attestation_inside_package")
        except ValueError:
            pass

    try:
        document = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return AuthorizationAttestation(
            present=True,
            path=str(location),
            problems=("attestation_not_json",),
            observed_package_sha256=observed_digest,
        )
    if not isinstance(document, dict):
        return AuthorizationAttestation(
            present=True,
            path=str(location),
            problems=("attestation_not_an_object",),
            observed_package_sha256=observed_digest,
        )

    schema = str(document.get("schema") or "").strip()
    if schema != ATTESTATION_SCHEMA:
        problems.append("attestation_schema_unsupported")

    upstream = str(document.get("upstream_project_url") or "").strip()
    parsed = urlparse(upstream)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        problems.append("upstream_project_url_invalid")

    release = document.get("release") if isinstance(document.get("release"), dict) else {}
    release_commit = str(release.get("commit") or "").strip().lower()
    release_tag = str(release.get("tag") or "").strip()
    if not COMMIT.match(release_commit) and not release_tag:
        problems.append("release_identity_missing")

    package = document.get("package") if isinstance(document.get("package"), dict) else {}
    package_sha256 = str(package.get("sha256") or "").strip().lower()
    if not SHA256_HEX.match(package_sha256):
        problems.append("package_digest_invalid")

    licence = document.get("licence") if isinstance(document.get("licence"), dict) else {}
    licence_name = str(licence.get("name") or "").strip()
    evidence_reference = str(licence.get("evidence_reference") or "").strip()
    evidence_sha256 = str(licence.get("evidence_sha256") or "").strip().lower()
    if not licence_name:
        problems.append("licence_name_missing")
    if not evidence_reference:
        problems.append("licence_evidence_reference_missing")
    if not SHA256_HEX.match(evidence_sha256):
        problems.append("licence_evidence_digest_invalid")

    rights = document.get("rights") if isinstance(document.get("rights"), dict) else {}
    modification = _flag(
        rights.get("modification_permitted"),
        field_name="modification_permitted",
        problems=problems,
    )
    redistribution = _flag(
        rights.get("redistribution_permitted"),
        field_name="redistribution_permitted",
        problems=problems,
    )

    review = document.get("review") if isinstance(document.get("review"), dict) else {}
    reviewer = _text(review.get("reviewer"), field_name="reviewer", problems=problems, minimum=3)
    reviewed_at = _timestamp(review.get("reviewed_at"), problems=problems)
    review_reference = _text(
        review.get("evidence_reference"),
        field_name="review_evidence_reference",
        problems=problems,
        minimum=3,
    )

    # The attestation must name evidence that is actually in this package, with
    # the digest it recorded. If the reviewer reviewed different bytes, the
    # attestation does not describe this package.
    matching = next(
        (item for item in evidence if item.name == evidence_reference), None
    )
    if evidence_reference and matching is None:
        problems.append("licence_evidence_not_in_package")
    elif matching is not None and SHA256_HEX.match(evidence_sha256):
        if matching.sha256 != evidence_sha256:
            problems.append("licence_evidence_digest_mismatch")

    bound = bool(
        observed_digest and SHA256_HEX.match(package_sha256) and package_sha256 == observed_digest
    )
    if observed_digest and SHA256_HEX.match(package_sha256) and not bound:
        problems.append("package_digest_mismatch")

    return AuthorizationAttestation(
        present=True,
        path=str(location),
        schema=schema,
        upstream_project_url=upstream,
        release_tag=release_tag,
        release_commit=release_commit,
        package_sha256=package_sha256,
        licence_name=licence_name,
        evidence_reference=evidence_reference,
        evidence_sha256=evidence_sha256,
        modification_permitted=modification,
        redistribution_permitted=redistribution,
        reviewer=reviewer,
        reviewed_at=reviewed_at,
        review_reference=review_reference,
        problems=tuple(dict.fromkeys(problems)),
        bound=bound,
        observed_package_sha256=observed_digest,
    )
