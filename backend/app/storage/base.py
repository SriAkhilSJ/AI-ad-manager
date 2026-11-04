"""Object storage abstraction.

Creative and generated media must live in durable object storage, not in the
database. This module defines the seam, two real implementations (local
filesystem for development, S3-compatible for production) and an explicit
"disabled" backend. It reports capability honestly instead of pretending a
bucket is configured when it is not.

Selection (``STORAGE_BACKEND=auto``):

1. ``s3``       — ``S3_BUCKET`` plus credentials/endpoint present.
2. ``local``    — development fallback under ``backend/.data/storage``.
3. ``disabled`` — explicit opt-out (``STORAGE_BACKEND=disabled``).

The database stores only the object ``key`` (plus an optional public URL and the
SHA-256 checksum); bytes never enter a table.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Any, Literal, Protocol

from app.core.config import Settings, get_settings
from app.core.errors import IntegrationUnavailableError

StorageBackendName = Literal["s3", "local", "none"]

PREFIX = "creatives"


class StoredObject(str):
    """The object key, plus the metadata the database needs to persist.

    It *is* the key string, so `await storage.get(stored)` works directly, while
    `stored.checksum` and friends remain available to the caller.
    """

    checksum: str
    size_bytes: int
    content_type: str
    backend: str
    public_url: str | None
    metadata: dict[str, Any]

    def __new__(
        cls,
        key: str,
        *,
        checksum: str = "",
        size_bytes: int = 0,
        content_type: str = "",
        backend: str = "local",
        public_url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "StoredObject":
        obj = super().__new__(cls, key)
        obj.checksum = checksum
        obj.size_bytes = size_bytes
        obj.content_type = content_type
        obj.backend = backend
        obj.public_url = public_url
        obj.metadata = metadata or {}
        return obj

    @property
    def key(self) -> str:
        return str(self)


class StorageBackend(Protocol):
    """The contract every backend satisfies."""

    name: str
    available: bool

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> bool: ...

    async def exists(self, key: str) -> bool: ...


def storage_key(filename: str, prefix: str = PREFIX) -> str:
    """Build a collision-free key that keeps the original filename readable."""
    stem = Path(filename).name.replace(" ", "-")[:80] or "asset"
    return f"{prefix.strip('/')}/{uuid.uuid4().hex}/{stem}"


class LocalFilesystemStorage:
    """Development backend: real bytes on a local disk, honestly non-durable.

    Suitable for a single-process dev server and for tests. It is *not* shared
    between replicas and is not a backup, which ``storage_status`` states.
    """

    name = "local"
    available = True

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        cleaned = key.replace("..", "_").lstrip("/\\")
        target = (self.root / cleaned).resolve()
        if not str(target).startswith(str(self.root.resolve())):
            raise IntegrationUnavailableError("Storage key escapes the storage root.")
        return target

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        checksum = hashlib.sha256(data).hexdigest()
        return StoredObject(
            key=key,
            size_bytes=len(data),
            checksum=checksum,
            content_type=content_type or "",
            backend=self.name,
            metadata={"checksum": checksum, **(metadata or {})},
        )

    async def get(self, key: str) -> bytes:
        target = self._resolve(key)
        if not target.is_file():
            raise IntegrationUnavailableError(f"Object '{key}' is not present in local storage.")
        return target.read_bytes()

    async def delete(self, key: str) -> bool:
        target = self._resolve(key)
        if target.is_file():
            target.unlink()
            return True
        return False

    async def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()


class S3Storage:
    """S3-compatible backend (AWS S3, Cloudflare R2, MinIO, Supabase Storage)."""

    name = "s3"
    available = True

    def __init__(self, settings: Settings) -> None:
        import boto3  # local import: dev installs never need the SDK at import time

        self.bucket = settings.s3_bucket or ""
        self.public_base = (settings.s3_public_base_url or "").rstrip("/")
        kwargs: dict[str, Any] = {"region_name": settings.s3_region}
        if settings.s3_endpoint_url:
            kwargs["endpoint_url"] = settings.s3_endpoint_url
        if settings.s3_access_key_id and settings.s3_secret_access_key:
            kwargs["aws_access_key_id"] = settings.s3_access_key_id
            kwargs["aws_secret_access_key"] = settings.s3_secret_access_key
        self._client = boto3.client("s3", **kwargs)

    def _url(self, key: str) -> str | None:
        return f"{self.public_base}/{key}" if self.public_base else None

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        extra: dict[str, Any] = {}
        if content_type:
            extra["ContentType"] = content_type
        if metadata:
            extra["Metadata"] = metadata
        try:
            self._client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)
        except Exception as exc:  # noqa: BLE001 - normalise vendor errors, never leak them
            raise IntegrationUnavailableError(
                f"S3 upload failed ({type(exc).__name__})."
            ) from exc
        checksum = hashlib.sha256(data).hexdigest()
        return StoredObject(
            key=key,
            size_bytes=len(data),
            checksum=checksum,
            content_type=content_type or "",
            backend=self.name,
            public_url=self._url(key),
            metadata={"checksum": checksum},
        )

    async def get(self, key: str) -> bytes:
        try:
            return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as exc:  # noqa: BLE001
            raise IntegrationUnavailableError(
                f"S3 download failed ({type(exc).__name__})."
            ) from exc

    async def delete(self, key: str) -> bool:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001 - deletion is best effort
            return False

    async def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001
            return False


class DisabledStorage:
    """Explicit opt-out: writes fail with a typed 503 rather than silently no-op."""

    name = "none"
    available = False

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        raise IntegrationUnavailableError(
            "Object storage is disabled (STORAGE_BACKEND=disabled)."
        )

    async def get(self, key: str) -> bytes:
        raise IntegrationUnavailableError("Object storage is disabled.")

    async def delete(self, key: str) -> bool:
        return False

    async def exists(self, key: str) -> bool:
        return False


def _s3_configured(settings: Settings) -> bool:
    if not settings.s3_bucket:
        return False
    return bool(settings.s3_access_key_id and settings.s3_secret_access_key) or bool(
        settings.s3_endpoint_url
    )


def resolve_backend_name(settings: Settings | None = None) -> StorageBackendName:
    settings = settings or get_settings()
    if settings.storage_backend == "disabled":
        return "none"
    if settings.storage_backend == "local":
        return "local"
    if settings.storage_backend == "s3":
        return "s3" if _s3_configured(settings) else "none"
    return "s3" if _s3_configured(settings) else "local"


def get_storage(settings: Settings | None = None) -> StorageBackend:
    settings = settings or get_settings()
    backend = resolve_backend_name(settings)
    if backend == "s3":
        return S3Storage(settings)
    if backend == "local":
        return LocalFilesystemStorage(settings.storage_root)
    return DisabledStorage()


def storage_status(settings: Settings | None = None) -> dict[str, Any]:
    """Capability report for ``/system/status``. Never raises."""
    settings = settings or get_settings()
    backend = resolve_backend_name(settings)

    if backend == "s3":
        return {
            "backend": "s3",
            "status": "available",
            "available": True,
            "configured": True,
            "bucket": settings.s3_bucket,
            "endpoint": settings.s3_endpoint_url or "aws",
            "public_urls": bool(settings.s3_public_base_url),
            "note": "S3-compatible bucket configured for creative assets.",
        }

    if backend == "local":
        root = settings.storage_root
        return {
            "backend": "local",
            "status": "degraded",
            "available": True,
            "configured": False,
            "root": str(root),
            "writable": os.access(root if root.exists() else root.parent, os.W_OK),
            "note": (
                "Development filesystem storage: real writes, but not shared across "
                "replicas and not durable. Configure S3_BUCKET before production."
            ),
        }

    return {
        "backend": "none",
        "status": "unavailable",
        "available": False,
        "configured": False,
        "note": "STORAGE_BACKEND=disabled: creative uploads are refused with 503.",
    }
