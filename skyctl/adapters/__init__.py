"""
skyctl.adapters
===============
Factories that turn a storage URL into an adapter instance.
Only the adapter you select is imported, so cloud SDKs stay optional.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from .local import (
    FileJobStore,
    FileObjectStore,
    FileSessionStore,
    MemoryObjectStore,
    ObjectJobStore,
    ObjectSessionStore,
)

__all__ = [
    "FileJobStore",
    "FileObjectStore",
    "FileSessionStore",
    "MemoryObjectStore",
    "ObjectJobStore",
    "ObjectSessionStore",
    "job_store_for",
    "object_store_for",
    "session_store_for",
]


def object_store_for(url: str):
    """
    file:// and memory:// use the stdlib; s3://, az:// and gs:// use the
    native stores with real CAS; any other fsspec scheme falls back to
    FsspecObjectStore (atomic_cas=False).
    """
    url = (url or "").strip() or "file://./.skyctl"
    parsed = urlparse(url)
    scheme = parsed.scheme or "file"
    if scheme == "file":
        path = parsed.netloc + parsed.path if parsed.netloc in ("", ".") else parsed.path
        return FileObjectStore(Path(path or ".skyctl"))
    if scheme == "memory":
        return MemoryObjectStore()
    bucket, prefix = parsed.netloc, parsed.path.strip("/")
    if scheme == "s3":
        from .s3 import S3ObjectStore

        return S3ObjectStore(bucket, prefix)
    if scheme == "az":
        from .azure_blob import AzureBlobObjectStore

        return AzureBlobObjectStore(bucket, prefix)
    if scheme == "gs":
        from .gcs import GcsObjectStore

        return GcsObjectStore(bucket, prefix)
    from .fsspec_store import FsspecObjectStore

    return FsspecObjectStore(url)  # generic fallback; atomic_cas=False


def session_store_for(store) -> ObjectSessionStore:
    return ObjectSessionStore(store, prefix="sessions/")


def job_store_for(store) -> ObjectJobStore:
    return ObjectJobStore(store, key="queue.json")
