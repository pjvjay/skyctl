"""
skyctl.adapters.fsspec_store
============================
One ObjectStore for every cloud bucket, via fsspec:

    s3://bucket/prefix     needs s3fs      (pip install skyctl[aws])
    az://container/prefix  needs adlfs     (pip install skyctl[azure]; AZURE_STORAGE_ACCOUNT_NAME)
    gs://bucket/prefix     needs gcsfs     (pip install skyctl[gcp])
    memory://prefix        built in        (tests)
    file:///abs/path       built in

All three cloud filesystems pick up workload identity (task role, managed
identity, service account) through their SDK's default credential chain.

This is the *fallback* store: fsspec has no portable conditional write, so
CAS is emulated (check-then-write) and `atomic_cas` is False. For s3://,
az:// and gs:// the platform layer uses the native stores in
skyctl.adapters.s3 / azure_blob / gcs instead, which have real CAS.
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional
from urllib.parse import urlparse

from ..ports import PreconditionFailed, Versioned


class FsspecObjectStore:
    atomic_cas = False

    def __init__(self, url: str, **storage_options):
        try:
            import fsspec
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "fsspec is not installed. Run: pip install 'skyctl[cloud]' "
                "(or [aws], [azure], [gcp] for the bucket backend you need)."
            ) from e
        parsed = urlparse(url)
        self.scheme = parsed.scheme or "file"
        if self.scheme == "az" and "account_name" not in storage_options:
            account = os.getenv("AZURE_STORAGE_ACCOUNT_NAME", "").strip()
            if account:
                storage_options["account_name"] = account
        self.fs = fsspec.filesystem(self.scheme, **storage_options)
        # fsspec path: "bucket/prefix" (netloc + path), no trailing slash.
        self.base = (parsed.netloc + parsed.path).rstrip("/")
        if self.scheme == "file":
            self.base = os.path.abspath(parsed.path or parsed.netloc)

    def _p(self, key: str) -> str:
        key = key.strip("/")
        if ".." in key.split("/"):
            raise ValueError(f"Key escapes the store root: {key}")
        return f"{self.base}/{key}" if key else self.base

    def read(self, key: str) -> bytes:
        with self.fs.open(self._p(key), "rb") as f:
            return f.read()

    def write(self, key: str, data: bytes) -> None:
        path = self._p(key)
        parent = path.rsplit("/", 1)[0]
        if self.scheme in ("file", "memory"):
            self.fs.makedirs(parent, exist_ok=True)
        with self.fs.open(path, "wb") as f:
            f.write(data)

    def exists(self, key: str) -> bool:
        return self.fs.exists(self._p(key))

    def delete(self, key: str) -> None:
        path = self._p(key)
        if self.fs.exists(path):
            self.fs.rm(path)

    def list(self, prefix: str = "") -> list[str]:
        root = self._p(prefix)
        if not self.fs.exists(root):
            return []
        if self.fs.isfile(root):
            return [prefix.strip("/")]
        found = self.fs.find(root)
        # Backends differ on leading slashes and protocol prefixes; compare normalised.
        base = self.base.strip("/") + "/"
        out = []
        for p in found:
            p = self.fs._strip_protocol(p).lstrip("/") if hasattr(self.fs, "_strip_protocol") else p.lstrip("/")
            if p.startswith(base):
                out.append(p[len(base):])
        return sorted(out)

    def get(self, key: str) -> Optional[Versioned]:
        try:
            data = self.read(key)
        except FileNotFoundError:
            return None
        return Versioned(data, hashlib.sha256(data).hexdigest()[:32])

    def put(self, key: str, data: bytes, *, if_version: Optional[str] = None, if_absent: bool = False) -> str:
        # NOT atomic: another writer can slip in between the check and the write.
        if if_absent or if_version is not None:
            current = self.get(key)
            if if_absent and current is not None:
                raise PreconditionFailed(key)
            if if_version is not None and (current is None or current.version != if_version):
                raise PreconditionFailed(key)
        self.write(key, data)
        return hashlib.sha256(data).hexdigest()[:32]

    def append_line(self, key: str, line: str) -> None:
        from .local import append_line_cas

        append_line_cas(self, key, line)
