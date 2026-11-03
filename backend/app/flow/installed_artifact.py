"""Independent verification of the *installed* DeepX extension (Phase 3C.2.2).

Phase 3C.2.1 established that a HELLO's digest is a claim, not a measurement, and
that trust needs an anchor Crusor already holds. That left one gap: nothing ever
looked at the bytes actually installed on the machine. A signed release manifest
proves what *should* be installed; it proves nothing about what *is*.

This module closes that gap on the local side. It reads the extension directory
that an operator configured, measures every file with a stable read, derives the
same ``sha256-tree-v1`` digest the authorization gate uses, and reports what it
found. It never scans for a "likely" installation, never follows a symbolic link
out of the root, and never decides that a measurement is trustworthy on its own —
comparison against the signed release happens in :mod:`app.flow.trust`.

What this proves, and what it does not:

* it proves that the files under the configured root, at measurement time, hash to
  a particular digest;
* it does **not** prove that the browser is running those files (a profile could
  load a different unpacked copy, memory could be patched, another process with the
  same privileges could have written them a moment earlier). Those limits are
  reported as ``limitations`` on every report and are repeated in
  ``docs/16-PHASE3C22-INDEPENDENT-ARTIFACT-VERIFICATION.md``.

TOCTOU handling: every file is opened with ``O_NOFOLLOW``, read from the file
descriptor, and ``fstat``-compared before and after the read; the path is then
``lstat``-ed to confirm it still names the same inode. The directory listing is
taken twice, and a list that changed mid-measurement fails the whole report rather
than producing a digest of a package that never existed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from app.flow.authorization import digest_excluded, tree_digest_from_pairs

DIGEST_ALGORITHM = "sha256-tree-v1"

LAYOUT_VERSIONED = "versioned"
LAYOUT_DIRECTORY = "directory"
SUPPORTED_LAYOUTS = (LAYOUT_VERSIONED, LAYOUT_DIRECTORY)

#: Chrome extension ids are 32 characters drawn from a-p.
CHROME_ID = re.compile(r"^[a-p]{32}$")

#: Directories never included in a measurement (identical to the gate's policy).
EXCLUDED_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv", ".mypy_cache"})

#: Files that must exist for an installation to be measurable at all.
REQUIRED_FILES = ("manifest.json",)


class MeasurementError(Exception):
    """The installation could not be measured. Always fails closed."""

    code = "flow_artifact_measurement_failed"

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": dict(self.details)}


def chrome_extension_id_from_key(key_b64: str) -> str:
    """Derive Chrome's extension id from a manifest ``key`` (base64 DER SPKI).

    Chrome takes the first 16 bytes of SHA-256 over the public key and maps each
    nibble to ``a``-``p``. Reproducing it lets the verifier check the *actual*
    identity of a directory install, instead of trusting the directory's name.
    """
    try:
        raw = base64.b64decode(key_b64, validate=True)
    except Exception as exc:  # noqa: BLE001 - malformed key is a rejection, not a crash
        raise MeasurementError(
            "invalid_extension_key", "The manifest key is not valid base64."
        ) from exc
    if len(raw) < 32:
        raise MeasurementError(
            "invalid_extension_key", "The manifest key is too short to identify an extension."
        )
    digest = hashlib.sha256(raw).hexdigest()[:32]
    return "".join(chr(ord("a") + int(char, 16)) for char in digest)


@dataclass(frozen=True, slots=True)
class MeasurementPolicy:
    """Bounds and inclusion rules for one measurement."""

    max_files: int = 20_000
    max_file_bytes: int = 128 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024
    #: When false, any file that is not manifest.json, the service worker, its
    #: declared scripts, or a declared web-accessible resource fails the report.
    allow_extra_files: bool = True
    require_service_worker: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_files": self.max_files,
            "max_file_bytes": self.max_file_bytes,
            "max_total_bytes": self.max_total_bytes,
            "allow_extra_files": self.allow_extra_files,
            "require_service_worker": self.require_service_worker,
        }


@dataclass(frozen=True, slots=True)
class InstallationSpec:
    """Exactly where to look. Never inferred by the verifier."""

    root: Path
    layout: str = LAYOUT_VERSIONED
    expected_extension_id: str = ""
    expected_version: str = ""
    policy: MeasurementPolicy = field(default_factory=MeasurementPolicy)

    def validate(self) -> "InstallationSpec":
        if self.layout not in SUPPORTED_LAYOUTS:
            raise MeasurementError(
                "unsupported_package_layout",
                f"The installation layout {self.layout!r} is not supported.",
            )
        if not self.expected_extension_id:
            raise MeasurementError(
                "extension_id_not_configured",
                "No expected extension id is configured, so the directory cannot be "
                "identified. Discovery is explicit, never guessed.",
            )
        return self


@dataclass(frozen=True, slots=True)
class MeasuredFile:
    path: str
    size: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class MeasurementReport:
    """What the local filesystem actually contains, measured once."""

    measurement_id: str
    component: str
    root: str
    layout: str
    digest: str
    digest_algorithm: str
    extension_id: str
    extension_version: str
    manifest_version: int
    service_worker: str
    file_count: int
    total_bytes: int
    started_at: float
    finished_at: float
    files: tuple[MeasuredFile, ...] = ()
    limitations: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()

    @property
    def duration_ms(self) -> float:
        return round((self.finished_at - self.started_at) * 1000.0, 3)

    @property
    def measured_at(self) -> str:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(self.finished_at, tz=timezone.utc).isoformat()

    def as_dict(self) -> dict[str, Any]:
        return {
            "measurement_id": self.measurement_id,
            "component": self.component,
            "root": self.root,
            "layout": self.layout,
            "digest": self.digest,
            "digest_algorithm": self.digest_algorithm,
            "extension_id": self.extension_id,
            "extension_version": self.extension_version,
            "manifest_version": self.manifest_version,
            "service_worker": self.service_worker,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "measured_at": self.measured_at,
            "duration_ms": self.duration_ms,
            "files": [item.as_dict() for item in self.files],
            "limitations": list(self.limitations),
            "checks": list(self.checks),
        }

    def summary(self) -> dict[str, Any]:
        """The report without the per-file list, for status payloads."""
        data = self.as_dict()
        data.pop("files", None)
        return data


def _safe_open_read(path: Path, root: Path, *, max_bytes: int) -> tuple[bytes, os.stat_result]:
    """Read a file so that a concurrent change cannot be mistaken for content.

    * ``O_NOFOLLOW`` refuses a symlink at the final component;
    * the bytes come from the descriptor, not the path, so a rename/replace during
      the read cannot mix two files;
    * ``fstat`` before/after and ``lstat`` after catch size, mtime and inode
      changes, and the path no longer naming the inode that was read.
    """
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(root)
    except (ValueError, OSError) as exc:
        raise MeasurementError(
            "path_outside_root",
            f"{path} does not resolve inside the configured root.",
            details={"path": str(path)},
        ) from exc

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if os.path.islink(path):
            raise MeasurementError(
                "symlink_in_package",
                f"{path} is a symbolic link; a measurement never follows one.",
                details={"path": str(path)},
            ) from exc
        raise MeasurementError(
            "file_unreadable", f"{path} could not be opened: {exc.strerror}", details={"path": str(path)}
        ) from exc

    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise MeasurementError(
                "unsupported_file_type",
                f"{path} is not a regular file.",
                details={"path": str(path)},
            )
        if before.st_size > max_bytes:
            raise MeasurementError(
                "file_too_large",
                f"{path} is larger than the per-file measurement limit.",
                details={"path": str(path), "size": before.st_size, "limit": max_bytes},
            )
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        # A file that grew is a file that changed mid-read.
        if os.read(fd, 1):
            raise MeasurementError(
                "file_mutated_during_measurement",
                f"{path} grew while it was being measured.",
                details={"path": str(path)},
            )
        after = os.fstat(fd)
    finally:
        os.close(fd)

    data = b"".join(chunks)
    if len(data) != before.st_size:
        raise MeasurementError(
            "file_mutated_during_measurement",
            f"{path} returned fewer bytes than its size reported.",
            details={"path": str(path)},
        )
    if (
        before.st_size,
        before.st_mtime_ns,
        before.st_ino,
        before.st_dev,
    ) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
        after.st_dev,
    ):
        raise MeasurementError(
            "file_mutated_during_measurement",
            f"{path} changed while it was being measured.",
            details={"path": str(path)},
        )
    try:
        current = os.lstat(path)
    except OSError as exc:
        raise MeasurementError(
            "file_replaced_during_measurement",
            f"{path} disappeared during measurement.",
            details={"path": str(path)},
        ) from exc
    if stat.S_ISLNK(current.st_mode):
        raise MeasurementError(
            "symlink_in_package",
            f"{path} became a symbolic link during measurement.",
            details={"path": str(path)},
        )
    if (current.st_ino, current.st_dev) != (before.st_ino, before.st_dev):
        raise MeasurementError(
            "file_replaced_during_measurement",
            f"{path} was replaced by a different file during measurement.",
            details={"path": str(path)},
        )
    return data, before


def _walk_regular_files(root: Path, *, max_files: int) -> list[Path]:
    """Every regular file under ``root``, refusing symlinks and special files."""
    found: list[Path] = []
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(current)
        for name in sorted(dirnames):
            candidate = here / name
            if os.path.islink(candidate):
                raise MeasurementError(
                    "symlink_in_package",
                    f"{candidate} is a symbolic link; a measurement never follows one.",
                    details={"path": str(candidate)},
                )
            if name in EXCLUDED_DIRS:
                dirnames.remove(name)
        dirnames.sort()
        for name in sorted(filenames):
            candidate = here / name
            mode = os.lstat(candidate).st_mode
            if stat.S_ISLNK(mode):
                raise MeasurementError(
                    "symlink_in_package",
                    f"{candidate} is a symbolic link; a measurement never follows one.",
                    details={"path": str(candidate)},
                )
            if not stat.S_ISREG(mode):
                raise MeasurementError(
                    "unsupported_file_type",
                    f"{candidate} is not a regular file.",
                    details={"path": str(candidate)},
                )
            found.append(candidate)
            if len(found) > max_files:
                raise MeasurementError(
                    "too_many_files",
                    "The installation contains more files than the measurement limit allows.",
                    details={"limit": max_files},
                )
    return found


def declared_files(manifest: dict[str, Any]) -> set[str]:
    """Every file path the manifest names, as POSIX paths relative to the root."""
    names: set[str] = {"manifest.json"}

    background = manifest.get("background") if isinstance(manifest.get("background"), dict) else {}
    for key in ("service_worker", "page"):
        value = str(background.get(key) or "").strip()
        if value:
            names.add(PurePosixPath(value).as_posix())
    for script in background.get("scripts") or []:
        names.add(PurePosixPath(str(script)).as_posix())

    for entry in manifest.get("content_scripts") or []:
        if not isinstance(entry, dict):
            continue
        for key in ("js", "css"):
            for script in entry.get(key) or []:
                names.add(PurePosixPath(str(script)).as_posix())

    resources = manifest.get("web_accessible_resources") or []
    for entry in resources:
        if isinstance(entry, str):
            names.add(PurePosixPath(entry).as_posix())
        elif isinstance(entry, dict):
            for resource in entry.get("resources") or []:
                names.add(PurePosixPath(str(resource)).as_posix())

    for key in ("action", "browser_action", "page_action", "options_page"):
        value = manifest.get(key)
        if isinstance(value, dict):
            page = str(value.get("default_popup") or "").strip()
            if page:
                names.add(PurePosixPath(page).as_posix())
        elif isinstance(value, str) and value.strip():
            names.add(PurePosixPath(value.strip()).as_posix())

    for icon in (manifest.get("icons") or {}).values():
        names.add(PurePosixPath(str(icon)).as_posix())
    return names


def _relative_name(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    if any(part in {"..", ""} for part in relative.parts) or relative.is_absolute():
        raise MeasurementError(
            "unsafe_relative_path",
            f"{path} does not have a safe path relative to the installation root.",
            details={"path": str(path)},
        )
    return PurePosixPath(relative).as_posix()


def _resolve_declared_file(root: Path, declared: str) -> Path:
    """Validate a manifest-declared path before touching it."""
    text = str(declared or "").strip()
    if not text:
        raise MeasurementError("invalid_package_layout", "The manifest declared an empty path.")
    candidate = PurePosixPath(text)
    if candidate.is_absolute() or any(part in {"..", ""} for part in candidate.parts):
        raise MeasurementError(
            "unsafe_relative_path",
            f"The manifest declared an unsafe path ({text!r}).",
            details={"path": text},
        )
    resolved = (root / candidate).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise MeasurementError(
            "path_outside_root",
            f"The manifest path {text!r} resolves outside the installation root.",
            details={"path": text},
        ) from exc
    return resolved


def component_frame(report: MeasurementReport) -> dict[str, Any]:
    """The companion's protocol frame for one measured component.

    The companion measures; Crusor compares. Nothing in this frame is trusted until
    a challenge binds it to a session and the digest matches the signed release.
    """
    return {
        "name": report.component,
        "digest": report.digest,
        "extension_id": report.extension_id,
        "version": report.extension_version,
        "measurement_id": report.measurement_id,
        "measured_at": report.finished_at,
        "file_count": report.file_count,
        "limitations": list(report.limitations),
        "checks": list(report.checks),
    }


def measure_components(specs: dict[str, InstallationSpec]) -> list[dict[str, Any]]:
    """Measure every configured component, independently. One failure fails all."""
    frames: list[dict[str, Any]] = []
    for name, spec in specs.items():
        report = InstalledArtifactVerifier(component=name).measure(spec)
        frames.append(component_frame(report))
    return frames


class InstalledArtifactVerifier:
    """Measures one installed component. Deterministic, local, read-only."""

    def __init__(self, component: str = "chrome_extension") -> None:
        self.component = component

    # ------------------------------------------------------------------ public
    def measure(self, spec: InstallationSpec) -> MeasurementReport:
        spec = spec.validate()
        root = self._validate_root(spec.root)
        started = time.time()
        checks: list[str] = []

        extension_id, directory_version = self._identity_from_layout(root, spec)
        checks.append(f"layout_{spec.layout}")

        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            raise MeasurementError(
                "missing_manifest",
                "The installation has no manifest.json, so it cannot be identified.",
                details={"root": str(root)},
            )
        manifest_bytes, _ = _safe_open_read(
            manifest_path, root, max_bytes=spec.policy.max_file_bytes
        )
        manifest = self._parse_manifest(manifest_bytes)
        checks.append("manifest_present")

        version = str(manifest.get("version") or "").strip()
        if not version:
            raise MeasurementError("unsupported_package_layout", "The manifest has no version.")
        if spec.expected_version and version != spec.expected_version:
            raise MeasurementError(
                "unexpected_version",
                f"The installed version {version!r} is not the expected "
                f"{spec.expected_version!r}.",
                details={"installed": version, "expected": spec.expected_version},
            )
        if directory_version and directory_version != version:
            raise MeasurementError(
                "unexpected_version",
                f"The directory says {directory_version} but the manifest says {version}.",
                details={"directory": directory_version, "manifest": version},
            )
        checks.append("version_matches_expected")

        declared_id = self._identity_from_manifest(manifest)
        if declared_id and declared_id != extension_id:
            raise MeasurementError(
                "extension_identity_mismatch",
                f"The manifest identifies {declared_id} but the installation is {extension_id}.",
                details={"manifest_id": declared_id, "installation_id": extension_id},
            )
        if spec.expected_extension_id and extension_id != spec.expected_extension_id:
            raise MeasurementError(
                "extension_identity_mismatch",
                f"The installed extension {extension_id} is not the expected "
                f"{spec.expected_extension_id}.",
                details={
                    "installed": extension_id,
                    "expected": spec.expected_extension_id,
                },
            )
        checks.append("extension_identity_matches_expected")

        service_worker = self._service_worker_path(root, manifest, spec.policy)
        if service_worker:
            checks.append("service_worker_resolved")

        before_listing = sorted(
            _relative_name(path, root) for path in _walk_regular_files(
                root, max_files=spec.policy.max_files
            )
        )
        pairs: list[tuple[str, bytes]] = []
        files: list[MeasuredFile] = []
        total = 0
        for path in _walk_regular_files(root, max_files=spec.policy.max_files):
            relative = _relative_name(path, root)
            if digest_excluded(relative):
                continue
            data, st = _safe_open_read(path, root, max_bytes=spec.policy.max_file_bytes)
            total += len(data)
            if total > spec.policy.max_total_bytes:
                raise MeasurementError(
                    "package_too_large",
                    "The installation is larger than the measurement limit allows.",
                    details={"limit": spec.policy.max_total_bytes},
                )
            pairs.append((relative, data))
            files.append(
                MeasuredFile(path=relative, size=len(data), sha256=hashlib.sha256(data).hexdigest())
            )

        after_listing = sorted(
            _relative_name(path, root) for path in _walk_regular_files(
                root, max_files=spec.policy.max_files
            )
        )
        if before_listing != after_listing:
            added = sorted(set(after_listing) - set(before_listing))
            removed = sorted(set(before_listing) - set(after_listing))
            raise MeasurementError(
                "file_added_during_measurement",
                "The installation changed while it was being measured.",
                details={"added": added[:16], "removed": removed[:16]},
            )
        checks.append("listing_stable")
        checks.append("files_read_stable")

        if not files:
            raise MeasurementError(
                "unsupported_package_layout", "The installation contains no files to measure."
            )
        self._enforce_file_policy(
            manifest,
            files=files,
            service_worker=service_worker,
            policy=spec.policy,
        )
        checks.append("file_policy_satisfied")

        return MeasurementReport(
            measurement_id=uuid.uuid4().hex,
            component=self.component,
            root=str(root),
            layout=spec.layout,
            digest=tree_digest_from_pairs(pairs),
            digest_algorithm=DIGEST_ALGORITHM,
            extension_id=extension_id,
            extension_version=version,
            manifest_version=int(manifest.get("manifest_version") or 0),
            service_worker=str(service_worker.relative_to(root).as_posix()) if service_worker else "",
            file_count=len(files),
            total_bytes=total,
            started_at=started,
            finished_at=time.time(),
            files=tuple(files),
            limitations=(
                "local_filesystem_only: this measures files on disk, not the code the "
                "browser has loaded into memory",
                "no_runtime_correlation: no browser interface is used, so the running "
                "extension is not proven to be this directory",
                "host_trust_assumed: a compromised machine with write access to this "
                "directory can present any bytes it likes",
            ),
            checks=tuple(checks),
        )

    # ---------------------------------------------------------------- helpers
    def _validate_root(self, configured: Path) -> Path:
        root = Path(configured).expanduser()
        if not str(root):
            raise MeasurementError(
                "installed_artifact_not_configured",
                "No extension installation directory is configured.",
            )
        if root.is_symlink():
            raise MeasurementError(
                "symlink_in_package",
                "The configured installation root is itself a symbolic link.",
                details={"root": str(root)},
            )
        if not root.exists() or not root.is_dir():
            raise MeasurementError(
                "installed_artifact_not_found",
                f"The configured installation {str(root)!r} does not exist.",
                details={"root": str(root)},
            )
        resolved = root.resolve()
        if resolved != root.absolute():
            raise MeasurementError(
                "path_outside_root",
                "The configured installation root resolves through a symbolic link.",
                details={"root": str(root), "resolved": str(resolved)},
            )
        return resolved

    def _identity_from_layout(self, root: Path, spec: InstallationSpec) -> tuple[str, str]:
        """Work out which extension this directory claims to be.

        ``versioned`` is Chrome's real layout — ``Extensions/<id>/<version>/`` — so
        the id and version come from the path and are checked against the manifest
        below. ``directory`` has no id in its path, so the manifest ``key`` must
        supply one; without it the installation cannot be identified and is
        rejected rather than guessed.
        """
        if spec.layout == LAYOUT_VERSIONED:
            version = root.name
            extension_id = root.parent.name
            if not CHROME_ID.match(extension_id):
                raise MeasurementError(
                    "unsupported_package_layout",
                    f"{root} is not an Extensions/<id>/<version> directory.",
                    details={"root": str(root)},
                )
            if not version:
                raise MeasurementError(
                    "unsupported_package_layout", "The version directory has no name."
                )
            return extension_id, version

        key = self._manifest_key_of(root, spec)
        if not key:
            raise MeasurementError(
                "unsupported_package_layout",
                "An unpacked installation must carry a manifest key for its identity to "
                "be derived; without it the directory name would be trusted, which is "
                "exactly what this verifier refuses to do.",
                details={"root": str(root)},
            )
        return chrome_extension_id_from_key(key), ""

    def _manifest_key_of(self, root: Path, spec: InstallationSpec) -> str:
        path = root / "manifest.json"
        if not path.is_file():
            return ""
        data, _ = _safe_open_read(path, root, max_bytes=spec.policy.max_file_bytes)
        return str(self._parse_manifest(data).get("key") or "").strip()

    def _parse_manifest(self, data: bytes) -> dict[str, Any]:
        try:
            manifest = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise MeasurementError(
                "invalid_manifest", "manifest.json is not valid JSON."
            ) from exc
        if not isinstance(manifest, dict):
            raise MeasurementError("invalid_manifest", "manifest.json is not a JSON object.")
        version = manifest.get("manifest_version")
        if version not in (2, 3):
            raise MeasurementError(
                "unsupported_package_layout",
                f"manifest_version {version!r} is not supported.",
                details={"manifest_version": version},
            )
        return manifest

    def _identity_from_manifest(self, manifest: dict[str, Any]) -> str:
        key = str(manifest.get("key") or "").strip()
        return chrome_extension_id_from_key(key) if key else ""

    def _service_worker_path(
        self, root: Path, manifest: dict[str, Any], policy: MeasurementPolicy
    ) -> Path | None:
        background = manifest.get("background")
        background = background if isinstance(background, dict) else {}
        declared = str(background.get("service_worker") or "").strip()
        if not declared:
            scripts = background.get("scripts")
            if isinstance(scripts, list) and scripts:
                declared = str(scripts[0])
        if not declared:
            if policy.require_service_worker:
                raise MeasurementError(
                    "missing_service_worker",
                    "The manifest declares no background service worker or script.",
                )
            return None
        path = _resolve_declared_file(root, declared)
        if path.is_symlink():
            raise MeasurementError(
                "symlink_in_package",
                "The declared service worker is a symbolic link.",
                details={"path": declared},
            )
        if not path.is_file():
            raise MeasurementError(
                "missing_service_worker",
                f"The declared service worker {declared!r} is not present.",
                details={"path": declared},
            )
        return path

    def _enforce_file_policy(
        self,
        manifest: dict[str, Any],
        *,
        files: list[MeasuredFile],
        service_worker: Path | None,
        policy: MeasurementPolicy,
    ) -> None:
        names = {item.path for item in files}
        if "manifest.json" not in names:
            raise MeasurementError("missing_manifest", "manifest.json is missing from the package.")
        if service_worker is not None:
            relative = service_worker.name
            if not any(name == relative or name.endswith("/" + relative) for name in names):
                raise MeasurementError(
                    "missing_service_worker",
                    "The service worker was resolved but is absent from the measurement.",
                )
        if policy.allow_extra_files:
            return
        # A strict policy is for builds where the shipped file set is known: any
        # executable the manifest never mentions could be code nobody reviewed.
        allowed = {"manifest.json"} | declared_files(manifest)
        unexpected = sorted(name for name in names if name not in allowed)
        if unexpected:
            raise MeasurementError(
                "unexpected_file_in_package",
                "The installation contains files the manifest does not declare, and the "
                "policy forbids them.",
                details={"files": unexpected[:32]},
            )
