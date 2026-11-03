"""Object storage seam: interface, local + S3 implementations, capability report."""

from app.storage.base import (
    DisabledStorage,
    LocalFilesystemStorage,
    S3Storage,
    StorageBackend,
    StorageBackendName,
    StoredObject,
    get_storage,
    resolve_backend_name,
    storage_key,
    storage_status,
)

__all__ = [
    "DisabledStorage",
    "LocalFilesystemStorage",
    "S3Storage",
    "StorageBackend",
    "StorageBackendName",
    "StoredObject",
    "get_storage",
    "resolve_backend_name",
    "storage_key",
    "storage_status",
]
