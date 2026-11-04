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
import errno
import hashlib
import json
import os
import re
import stat
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from app.flow.authorization import tree_digest_from_pairs

#: Source/licensing profile: VCS metadata and build caches are noise when the
#: question is "what did the vendor ship".
DIGEST_SOURCE = "sha256-tree-v1"
#: Installed-runtime profile (Phase 3C.2.3). Everything the process can load is
#: included — ``node_modules``, virtualenvs, ``site-packages``, ``__pycache__`` —
#: because a runtime dependency is executable code. Only VCS metadata is skipped.
DIGEST_RUNTIME = "sha256-tree-runtime-v1"
DIGEST_ALGORITHM = DIGEST_RUNTIME
SUPPORTED_DIGEST_ALGORITHMS = (DIGEST_RUNTIME, DIGEST_SOURCE)

LAYOUT_VERSIONED = "versioned"
LAYOUT_DIRECTORY = "directory"
#: A plain directory with no Chrome manifest: an MCP server, the companion, a
#: packaged runtime. Phase 3C.2.3 — without this, a signed manifest that requires
#: the MCP server could never be satisfied, so "required" would have meant
#: "required of the extension only".
LAYOUT_TREE = "tree"
SUPPORTED_LAYOUTS = (LAYOUT_VERSIONED, LAYOUT_DIRECTORY, LAYOUT_TREE)

#: Chrome extension ids are 32 characters drawn from a-p.
CHROME_ID = re.compile(r"^[a-p]{32}$")

#: Per-algorithm directory policy. The runtime profile deliberately includes the
#: dependency trees the source profile skips: a measurement that ignored
#: ``node_modules`` would call two different MCP servers "the same build".
_EXCLUSIONS = {
    DIGEST_SOURCE: frozenset({".git", "__pycache__", "node_modules", ".venv", ".mypy_cache"}),
    DIGEST_RUNTIME: frozenset({".git"}),
}
#: Nothing in this set is ever measured, under either profile.
ALWAYS_EXCLUDED_FILES = frozenset({".DS_Store"})

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

    #: Which digest profile to use. Declared in the signed release manifest too, so
    #: a measurement and an anchor can never silently disagree about semantics.
    digest_algorithm: str = DIGEST_RUNTIME
    max_files: int = 20_000
    max_file_bytes: int = 128 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024
    #: When false, any file that is not manifest.json, the service worker, its
    #: declared scripts, or a declared web-accessible resource fails the report.
    allow_extra_files: bool = True
    require_service_worker: bool = True

    def validate(self) -> "MeasurementPolicy":
        if self.digest_algorithm not in SUPPORTED_DIGEST_ALGORITHMS:
            raise MeasurementError(
                "unsupported_digest_algorithm",
                f"The digest algorithm {self.digest_algorithm!r} is not supported.",
            )
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "digest_algorithm": self.digest_algorithm,
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
        self.policy.validate()
        if self.layout not in SUPPORTED_LAYOUTS:
            raise MeasurementError(
                "unsupported_package_layout",
                f"The installation layout {self.layout!r} is not supported.",
            )
        if self.layout != LAYOUT_TREE and not self.expected_extension_id:
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


#: Phase 3C.2.3.1 — descriptor-relative traversal. Every lookup below happens
#: against a directory file descriptor with ``O_NOFOLLOW``, so a parent directory
#: that is renamed, replaced or turned into a symlink mid-scan cannot redirect a
#: read. Platforms without these primitives are refused rather than downgraded to
#: path-based checks, which cannot make the same guarantee.
#:
#: Phase 3C.2.3.2: the check now names every primitive the guarantee depends on,
#: including ``O_NOFOLLOW``. Using ``getattr(os, "O_NOFOLLOW", 0)`` in the flags was
#: a silent downgrade: on a platform without ``O_NOFOLLOW`` the flag would have been
#: 0 and every open would have *followed* a symlink while the code still claimed
#: descriptor-relative, no-follow traversal.
_DIR_FD_PRIMITIVES = (
    "O_NOFOLLOW",
    "O_DIRECTORY",
)


def _descriptor_traversal_available() -> bool:
    """Every primitive the no-follow descriptor traversal depends on, named.

    Phase 3C.2.3.2: ``O_NOFOLLOW`` is part of the contract, not an optional extra.
    The earlier expression used ``getattr(os, "O_NOFOLLOW", 0)`` inside the open
    flags, so a platform without it would have opened symlinks while the report
    still claimed no-follow traversal.
    """
    if not all(getattr(os, flag, 0) for flag in _DIR_FD_PRIMITIVES):
        return False
    if not (hasattr(os, "supports_dir_fd") and hasattr(os, "supports_fd")):
        return False
    if not (os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd):
        return False
    if os.stat not in os.supports_follow_symlinks or not hasattr(os, "fstat"):
        return False
    if not hasattr(os, "scandir") or os.scandir not in os.supports_fd:
        return False
    return True


DIR_FD_SUPPORTED = _descriptor_traversal_available()

_OPEN_DIR_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_OPEN_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _require_descriptor_traversal() -> None:
    if not DIR_FD_SUPPORTED:
        raise MeasurementError(
            "unsupported_platform",
            "This platform does not offer descriptor-relative lookups "
            "(openat-style os.open/os.stat with dir_fd, plus scandir on a descriptor), "
            "so the traversal race guarantees cannot be made. Crusor refuses to "
            "measure rather than fall back to path-based checks that look equivalent "
            "and are not.",
            details={
                "platform": sys.platform,
                "o_directory": bool(getattr(os, "O_DIRECTORY", 0)),
                "o_nofollow": bool(getattr(os, "O_NOFOLLOW", 0)),
                "scandir_on_fd": bool(hasattr(os, "scandir") and os.scandir in os.supports_fd),
            },
        )


def _open_root_dir(root: Path) -> int:
    """Open the configured root itself, refusing a symlink at that last component.

    Kept for callers that already hold a validated absolute path; :meth:`measure`
    acquires the root with :func:`_open_root_through_anchor` instead, which never
    resolves more than one component per ``openat``.
    """
    try:
        return os.open(str(root), _OPEN_DIR_FLAGS)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise MeasurementError(
                "symlink_in_package",
                f"{root} is a symbolic link; a measurement never follows one.",
                details={"root": str(root)},
            ) from exc
        raise MeasurementError(
            "installed_artifact_not_found",
            f"The configured installation {str(root)!r} could not be opened: {exc.strerror}.",
            details={"root": str(root)},
        ) from exc


def _open_root_through_anchor(root: Path) -> int:
    """Acquire the root one component at a time from the filesystem root.

    Phase 3C.2.3.2. A single ``os.open("/a/b/c", O_NOFOLLOW)`` protects only the
    *last* component: every ancestor is resolved by the kernel from a string the
    process no longer controls. Walking from a trusted anchor (``/``) with one
    ``openat`` per component means a component cannot be swapped for a symlink or a
    different directory between the check and the open — each step is
    descriptor-relative and no-follow, and each verifies that the descriptor it
    got is the entry the parent currently names.
    """
    parts = [part for part in root.parts if part not in ("/", "")]
    if not parts or any(part in (".", "..") for part in parts):
        raise MeasurementError(
            "unsafe_relative_path",
            f"The installation root {str(root)!r} is not a plain path below the "
            "filesystem root, so it cannot be walked safely.",
            details={"root": str(root)},
        )
    try:
        fd = os.open("/", _OPEN_DIR_FLAGS)
    except OSError as exc:  # pragma: no cover - no / means no filesystem
        raise MeasurementError(
            "unsupported_platform",
            f"The filesystem root could not be opened: {exc.strerror}.",
            details={"platform": sys.platform},
        ) from exc

    walked = ""
    try:
        for part in parts:
            walked = f"{walked}/{part}"
            child = _open_child_dir(
                fd, part, walked, missing_code="installed_artifact_not_found"
            )
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise

    # What the walk acquired must still be what the configured path names.
    try:
        named = os.lstat(root)
    except OSError as exc:
        os.close(fd)
        raise MeasurementError(
            "installed_artifact_not_found",
            f"The configured installation {str(root)!r} disappeared while it was opened.",
            details={"root": str(root)},
        ) from exc
    measured = os.fstat(fd)
    if stat.S_ISLNK(named.st_mode) or (named.st_dev, named.st_ino) != (
        measured.st_dev,
        measured.st_ino,
    ):
        os.close(fd)
        raise MeasurementError(
            "root_replaced_during_measurement",
            f"The configured installation {str(root)!r} is not the directory that was "
            "opened by walking down from the filesystem root.",
            details={"root": str(root)},
        )
    return fd


def _open_child_dir(
    parent_fd: int,
    name: str,
    relative: str,
    *,
    missing_code: str = "directory_replaced_during_measurement",
) -> int:
    """Open ``name`` relative to ``parent_fd``; a symlink is never followed."""
    try:
        child = os.open(name, _OPEN_DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise MeasurementError(
                "symlink_in_package",
                f"{relative} is a symbolic link; a measurement never follows one.",
                details={"path": relative},
            ) from exc
        raise MeasurementError(
            missing_code,
            f"{relative} could not be opened as a directory: {exc.strerror}.",
            details={"path": relative},
        ) from exc
    # What we opened must be what the parent currently names.
    try:
        opened = os.fstat(child)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        os.close(child)
        raise MeasurementError(
            "directory_replaced_during_measurement",
            f"{relative} changed while it was being opened.",
            details={"path": relative},
        ) from exc
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        os.close(child)
        raise MeasurementError(
            "directory_replaced_during_measurement",
            f"{relative} was replaced by a different directory during measurement.",
            details={"path": relative},
        )
    return child


def _read_file_at(
    dir_fd: int, name: str, relative: str, *, max_bytes: int
) -> tuple[bytes, os.stat_result]:
    """Read one file by descriptor, relative to an already-open directory.

    * the open is ``openat``-style with ``O_NOFOLLOW``, so no component below the
      directory descriptor is resolved through a path the caller could have swapped;
    * the bytes come from the descriptor, so a rename in the same directory cannot
      mix two files;
    * ``fstat`` before/after plus a final ``stat(name, dir_fd=…)`` catch size, mtime
      and inode changes, and a name that no longer points at the inode we read.
    """
    try:
        fd = os.open(name, _OPEN_FILE_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise MeasurementError(
                "symlink_in_package",
                f"{relative} is a symbolic link; a measurement never follows one.",
                details={"path": relative},
            ) from exc
        raise MeasurementError(
            "file_unreadable",
            f"{relative} could not be opened: {exc.strerror}",
            details={"path": relative},
        ) from exc

    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise MeasurementError(
                "unsupported_file_type",
                f"{relative} is not a regular file.",
                details={"path": relative},
            )
        if before.st_size > max_bytes:
            raise MeasurementError(
                "file_too_large",
                f"{relative} is larger than the per-file measurement limit.",
                details={"path": relative, "size": before.st_size, "limit": max_bytes},
            )
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise MeasurementError(
                "file_mutated_during_measurement",
                f"{relative} grew while it was being measured.",
                details={"path": relative},
            )
        after = os.fstat(fd)
    finally:
        os.close(fd)

    data = b"".join(chunks)
    if len(data) != before.st_size:
        raise MeasurementError(
            "file_mutated_during_measurement",
            f"{relative} returned fewer bytes than its size reported.",
            details={"path": relative},
        )
    if (before.st_size, before.st_mtime_ns, before.st_ino, before.st_dev) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
        after.st_dev,
    ):
        raise MeasurementError(
            "file_mutated_during_measurement",
            f"{relative} changed while it was being measured.",
            details={"path": relative},
        )
    try:
        current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as exc:
        raise MeasurementError(
            "file_replaced_during_measurement",
            f"{relative} disappeared during measurement.",
            details={"path": relative},
        ) from exc
    if stat.S_ISLNK(current.st_mode):
        raise MeasurementError(
            "symlink_in_package",
            f"{relative} became a symbolic link during measurement.",
            details={"path": relative},
        )
    if (current.st_ino, current.st_dev) != (before.st_ino, before.st_dev):
        raise MeasurementError(
            "file_replaced_during_measurement",
            f"{relative} was replaced by a different file during measurement.",
            details={"path": relative},
        )
    return data, before


@dataclass
class _ScanResult:
    """Every regular file read during one descriptor-relative scan."""

    entries: list[tuple[str, bytes, os.stat_result]] = field(default_factory=list)
    total_bytes: int = 0

    @property
    def files(self) -> list[str]:
        return [relative for relative, _, _ in self.entries]


def _scan_directory(
    fd: int,
    *,
    prefix: str,
    exclusions: frozenset[str],
    policy: MeasurementPolicy,
    state: _ScanResult,
) -> None:
    """Walk one directory by descriptor, failing closed on any instability."""
    try:
        with os.scandir(fd) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
            first_names = [entry.name for entry in entries]
            stats: dict[str, os.stat_result] = {}
            for entry in entries:
                try:
                    stats[entry.name] = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise MeasurementError(
                        "enumeration_failed",
                        f"{prefix}{entry.name} could not be inspected: {exc.strerror}.",
                        details={"path": f"{prefix}{entry.name}"},
                    ) from exc
    except OSError as exc:
        raise MeasurementError(
            "enumeration_failed",
            f"The directory {prefix or '.'} could not be enumerated: {exc.strerror}.",
            details={"path": prefix or "."},
        ) from exc

    read_in_this_directory: list[tuple[str, str, os.stat_result]] = []
    for name in first_names:
        if name in ALWAYS_EXCLUDED_FILES:
            continue
        relative = f"{prefix}{name}"
        info = stats[name]
        mode = info.st_mode
        if stat.S_ISLNK(mode):
            raise MeasurementError(
                "symlink_in_package",
                f"{relative} is a symbolic link; a measurement never follows one.",
                details={"path": relative},
            )
        if stat.S_ISDIR(mode):
            if name in exclusions:
                continue
            child = _open_child_dir(fd, name, relative)
            child_stat = os.fstat(child)
            try:
                _scan_directory(
                    child,
                    prefix=f"{relative}/",
                    exclusions=exclusions,
                    policy=policy,
                    state=state,
                )
            finally:
                os.close(child)
            # Phase 3C.2.3.2: the recursive scan read the directory through its own
            # descriptor. That proves the *contents*; it does not prove the parent
            # entry still names the same directory. A same-name replacement keeps the
            # entry list identical, so it can only be caught by comparing identities.
            try:
                current = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError as exc:
                raise MeasurementError(
                    "directory_replaced_during_measurement",
                    f"{relative} disappeared while it was being measured.",
                    details={"path": relative},
                ) from exc
            if (current.st_dev, current.st_ino) != (child_stat.st_dev, child_stat.st_ino):
                raise MeasurementError(
                    "directory_replaced_during_measurement",
                    f"{relative} was replaced by a different directory during measurement.",
                    details={"path": relative},
                )
            continue
        if not stat.S_ISREG(mode):
            raise MeasurementError(
                "unsupported_file_type",
                f"{relative} is not a regular file.",
                details={"path": relative},
            )
        data, fst = _read_file_at(fd, name, relative, max_bytes=policy.max_file_bytes)
        if (fst.st_dev, fst.st_ino) != (info.st_dev, info.st_ino):
            raise MeasurementError(
                "file_replaced_during_measurement",
                f"{relative} was replaced between directory enumeration and reading.",
                details={"path": relative},
            )
        state.total_bytes += len(data)
        if state.total_bytes > policy.max_total_bytes:
            raise MeasurementError(
                "package_too_large",
                "The installation is larger than the measurement limit allows.",
                details={"limit": policy.max_total_bytes},
            )
        state.entries.append((relative, data, fst))
        read_in_this_directory.append((name, relative, data, fst))
        if len(state.entries) > policy.max_files:
            raise MeasurementError(
                "too_many_files",
                "The installation contains more files than the measurement limit allows.",
                details={"limit": policy.max_files},
            )

    # Revalidate every file this directory contributed *after* the whole directory
    # has been read: a same-inode rewrite keeps the inode check happy, so size and
    # mtime are compared again here rather than trusted from the read.
    for name, relative, data, recorded in read_in_this_directory:
        try:
            current = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except OSError as exc:
            raise MeasurementError(
                "file_replaced_during_measurement",
                f"{relative} disappeared before the scan of this directory finished.",
                details={"path": relative},
            ) from exc
        if (current.st_ino, current.st_dev) != (recorded.st_ino, recorded.st_dev):
            raise MeasurementError(
                "file_replaced_during_measurement",
                f"{relative} was replaced by a different file during the scan.",
                details={"path": relative},
            )
        if (
            (current.st_size, current.st_mtime_ns, current.st_ctime_ns)
            != (recorded.st_size, recorded.st_mtime_ns, recorded.st_ctime_ns)
        ):
            raise MeasurementError(
                "file_mutated_during_measurement",
                f"{relative} changed after it was read, before the scan finished.",
                details={"path": relative},
            )
        # Timestamps are not evidence: this filesystem can hand a freed inode back
        # with an unchanged size *and* an unchanged mtime. Re-read the bytes through a
        # fresh descriptor and compare them, so a same-size rewrite cannot pass.
        try:
            again, _ = _read_file_at(fd, name, relative, max_bytes=policy.max_file_bytes)
        except MeasurementError:
            raise
        except OSError as exc:
            raise MeasurementError(
                "file_replaced_during_measurement",
                f"{relative} could not be re-read: {exc.strerror}.",
                details={"path": relative},
            ) from exc
        if again != data:
            raise MeasurementError(
                "file_mutated_during_measurement",
                f"{relative} no longer contains the bytes that were measured.",
                details={"path": relative},
            )

    # The directory's own listing must be unchanged: an entry added or removed while
    # we were reading it means the measured set is not the set that was there.
    try:
        with os.scandir(fd) as iterator:
            second_names = sorted(entry.name for entry in iterator)
    except OSError as exc:
        raise MeasurementError(
            "enumeration_failed",
            f"The directory {prefix or '.'} could not be re-enumerated: {exc.strerror}.",
            details={"path": prefix or "."},
        ) from exc
    if second_names != first_names:
        raise MeasurementError(
            "file_added_during_measurement",
            f"The directory {prefix or '.'} changed while it was being measured.",
            details={
                "path": prefix or ".",
                "added": sorted(set(second_names) - set(first_names))[:16],
                "removed": sorted(set(first_names) - set(second_names))[:16],
            },
        )


def _scan_tree(
    root_fd: int, *, exclusions: frozenset[str], policy: MeasurementPolicy
) -> _ScanResult:
    state = _ScanResult()
    _scan_directory(root_fd, prefix="", exclusions=exclusions, policy=policy, state=state)
    return state


def _assert_root_still_the_same(root: Path, root_fd: int) -> None:
    """The configured root path must still name the descriptor we measured."""
    try:
        named = os.lstat(root)
    except OSError as exc:
        raise MeasurementError(
            "root_replaced_during_measurement",
            f"The installation root {str(root)!r} disappeared during measurement.",
            details={"root": str(root)},
        ) from exc
    if stat.S_ISLNK(named.st_mode):
        raise MeasurementError(
            "symlink_in_package",
            f"The installation root {str(root)!r} became a symbolic link during measurement.",
            details={"root": str(root)},
        )
    measured = os.fstat(root_fd)
    if (named.st_dev, named.st_ino) != (measured.st_dev, measured.st_ino):
        raise MeasurementError(
            "root_replaced_during_measurement",
            f"The installation root {str(root)!r} was replaced by a different directory.",
            details={"root": str(root)},
        )


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


def normalized_relative_path(declared: str, *, field: str = "path") -> str:
    """Validate a manifest-declared path and return it as a POSIX relative name.

    Nothing is resolved and nothing is opened: the declaration is checked for
    emptiness, absoluteness and ``..`` components, then compared against names that
    came from the descriptor scan. A path that needs resolving to be safe is not
    something a measurement should accept.
    """
    text = str(declared or "").strip()
    if not text:
        raise MeasurementError(
            "invalid_package_layout", f"The manifest declared an empty {field}."
        )
    candidate = PurePosixPath(text)
    if candidate.is_absolute() or any(part in {"..", ""} for part in candidate.parts):
        raise MeasurementError(
            "unsafe_relative_path",
            f"The manifest declared an unsafe {field} ({text!r}).",
            details={"path": text},
        )
    return candidate.as_posix()


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
        "digest_algorithm": report.digest_algorithm,
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
        _require_descriptor_traversal()
        root = self._validate_root(spec.root)
        # Phase 3C.2.3.2: the root is acquired one component at a time from the
        # filesystem root, so no ancestor is resolved from a string by the kernel.
        root_fd = _open_root_through_anchor(root)
        try:
            return self._measure_open_root(root=root, root_fd=root_fd, spec=spec)
        finally:
            os.close(root_fd)

    def _measure_open_root(
        self, *, root: Path, root_fd: int, spec: InstallationSpec
    ) -> MeasurementReport:
        started = time.time()
        checks: list[str] = []

        extension_id, directory_version = self._identity_from_layout(root_fd, root, spec)
        checks.append(f"layout_{spec.layout}")

        if spec.layout == LAYOUT_TREE:
            return self._measure_tree(
                root=root, root_fd=root_fd, spec=spec, started=started, checks=checks
            )

        try:
            os.stat("manifest.json", dir_fd=root_fd, follow_symlinks=False)
        except OSError as exc:
            raise MeasurementError(
                "missing_manifest",
                "The installation has no manifest.json, so it cannot be identified.",
                details={"root": str(root)},
            ) from exc
        manifest_bytes, _ = _read_file_at(
            root_fd, "manifest.json", "manifest.json", max_bytes=spec.policy.max_file_bytes
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

        service_worker = self._service_worker_relative(manifest, spec.policy)
        if service_worker:
            checks.append("service_worker_resolved")

        state = _scan_tree(
            root_fd,
            exclusions=_EXCLUSIONS[spec.policy.digest_algorithm],
            policy=spec.policy,
        )
        _assert_root_still_the_same(root, root_fd)
        checks.append("descriptor_relative_traversal")
        checks.append("directory_listings_stable")
        checks.append("files_read_stable")

        pairs = [(relative, data) for relative, data, _ in state.entries]
        files = [
            MeasuredFile(path=relative, size=len(data), sha256=hashlib.sha256(data).hexdigest())
            for relative, data, _ in state.entries
        ]
        if not files:
            raise MeasurementError(
                "unsupported_package_layout", "The installation contains no files to measure."
            )
        self._enforce_file_policy(
            manifest,
            files=files,
            service_worker_relative=service_worker,
            policy=spec.policy,
        )
        checks.append("file_policy_satisfied")

        return MeasurementReport(
            measurement_id=uuid.uuid4().hex,
            component=self.component,
            root=str(root),
            layout=spec.layout,
            digest=tree_digest_from_pairs(pairs),
            digest_algorithm=spec.policy.digest_algorithm,
            extension_id=extension_id,
            extension_version=version,
            manifest_version=int(manifest.get("manifest_version") or 0),
            service_worker=service_worker,
            file_count=len(files),
            total_bytes=state.total_bytes,
            started_at=started,
            finished_at=time.time(),
            files=tuple(files),
            limitations=(
                "local_filesystem_only: this measures files on disk, not the code the "
                "browser has loaded into memory",
                "no_atomic_snapshot: each directory listing and each file is checked for "
                "stability, and the root is re-checked afterwards, but a writer that "
                "restores every inode between checks is not detectable",
                "no_runtime_correlation: no browser interface is used, so the running "
                "extension is not proven to be this directory",
                "host_trust_assumed: a compromised machine with write access to this "
                "directory can present any bytes it likes",
            ),
            checks=tuple(checks),
        )

    def _measure_tree(
        self,
        *,
        root: Path,
        root_fd: int,
        spec: InstallationSpec,
        started: float,
        checks: list[str],
    ) -> MeasurementReport:
        """Measure a directory that carries no Chrome manifest.

        The rules that make a measurement worth having still apply: no symlinks, a
        stable file listing, bounded size, every file hashed. What changes is that
        there is no service worker to resolve and no manifest to police — the
        authority for *what* this directory should contain is the signed release
        manifest, so the digest of the whole tree is the entire comparison.
        """
        state = _scan_tree(
            root_fd,
            exclusions=_EXCLUSIONS[spec.policy.digest_algorithm],
            policy=spec.policy,
        )
        _assert_root_still_the_same(root, root_fd)
        checks.append("descriptor_relative_traversal")
        checks.append("directory_listings_stable")
        checks.append("files_read_stable")
        pairs = [(relative, data) for relative, data, _ in state.entries]
        files = [
            MeasuredFile(path=relative, size=len(data), sha256=hashlib.sha256(data).hexdigest())
            for relative, data, _ in state.entries
        ]
        if not files:
            raise MeasurementError(
                "unsupported_package_layout", "The installation contains no files to measure."
            )
        return MeasurementReport(
            measurement_id=uuid.uuid4().hex,
            component=self.component,
            root=str(root),
            layout=spec.layout,
            digest=tree_digest_from_pairs(pairs),
            digest_algorithm=spec.policy.digest_algorithm,
            extension_id=spec.expected_extension_id,
            extension_version=spec.expected_version,
            manifest_version=0,
            service_worker="",
            file_count=len(files),
            total_bytes=state.total_bytes,
            started_at=started,
            finished_at=time.time(),
            files=tuple(files),
            limitations=(
                "local_filesystem_only: this measures files on disk, not the code the "
                "process has loaded into memory",
                "no_atomic_snapshot: each directory listing and each file is checked for "
                "stability, and the root is re-checked afterwards, but a writer that "
                "restores every inode between checks is not detectable",
                "no_process_correlation: the running component is not proven to be this "
                "directory",
                "host_trust_assumed: a compromised machine with write access to this "
                "directory can present any bytes it likes",
            ),
            checks=tuple(checks) + ("tree_layout",),
        )

    # ---------------------------------------------------------------- helpers
    def _reject_symlinked_ancestors(self, root: Path) -> None:
        """A root reached through a symlinked parent is not the directory it claims."""
        current = root
        while True:
            if current.is_symlink():
                raise MeasurementError(
                    "symlink_in_package",
                    f"{current} is a symbolic link; the installation path must be real.",
                    details={"path": str(current)},
                )
            if current.parent == current:
                return
            current = current.parent

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
        self._reject_symlinked_ancestors(root.absolute())
        resolved = root.resolve()
        if resolved != root.absolute():
            raise MeasurementError(
                "path_outside_root",
                "The configured installation root resolves through a symbolic link.",
                details={"root": str(root), "resolved": str(resolved)},
            )
        return resolved

    def _identity_from_layout(
        self, root_fd: int, root: Path, spec: InstallationSpec
    ) -> tuple[str, str]:
        """Work out which extension this directory claims to be.

        ``versioned`` is Chrome's real layout — ``Extensions/<id>/<version>/`` — so
        the id and version come from the path and are checked against the manifest
        below. ``directory`` has no id in its path, so the manifest ``key`` must
        supply one; without it the installation cannot be identified and is
        rejected rather than guessed.
        """
        if spec.layout == LAYOUT_TREE:
            # Identity is supplied by the signed release manifest; there is nothing
            # in the directory to derive it from, and the directory name is never
            # trusted as an identity.
            return spec.expected_extension_id, ""
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

        key = self._manifest_key_of(root_fd, spec)
        if not key:
            raise MeasurementError(
                "unsupported_package_layout",
                "An unpacked installation must carry a manifest key for its identity to "
                "be derived; without it the directory name would be trusted, which is "
                "exactly what this verifier refuses to do.",
                details={"root": str(root)},
            )
        return chrome_extension_id_from_key(key), ""

    def _manifest_key_of(self, root_fd: int, spec: InstallationSpec) -> str:
        try:
            os.stat("manifest.json", dir_fd=root_fd, follow_symlinks=False)
        except OSError:
            return ""
        data, _ = _read_file_at(
            root_fd, "manifest.json", "manifest.json", max_bytes=spec.policy.max_file_bytes
        )
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

    def _service_worker_relative(
        self, manifest: dict[str, Any], policy: MeasurementPolicy
    ) -> str:
        """The declared service worker as a validated, normalized relative path.

        No filesystem access happens here: the declaration is checked for safety and
        the measured file set decides whether it exists. Comparing a *path object*
        against the scanned set was how a same-named file in a subdirectory could
        stand in for the real worker.
        """
        background = manifest.get("background")
        background = background if isinstance(background, dict) else {}
        declared = str(background.get("service_worker") or "").strip()
        if not declared:
            scripts = background.get("scripts")
            if isinstance(scripts, list) and scripts:
                declared = str(scripts[0]).strip()
        if not declared:
            if policy.require_service_worker:
                raise MeasurementError(
                    "missing_service_worker",
                    "The manifest declares no background service worker or script.",
                )
            return ""
        return normalized_relative_path(declared, field="service_worker")

    def _enforce_file_policy(
        self,
        manifest: dict[str, Any],
        *,
        files: list[MeasuredFile],
        service_worker_relative: str,
        policy: MeasurementPolicy,
    ) -> None:
        names = {item.path for item in files}
        if "manifest.json" not in names:
            raise MeasurementError("missing_manifest", "manifest.json is missing from the package.")
        if service_worker_relative:
            # Exact normalized relative path: a different file with the same
            # basename (`vendor/sw.js` vs `sw.js`) must not satisfy this check.
            relative = service_worker_relative
            if relative not in names:
                raise MeasurementError(
                    "missing_service_worker",
                    f"The declared service worker {relative!r} is absent from the "
                    "measurement, so another file with that name cannot stand in for it.",
                    details={"expected": relative},
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
