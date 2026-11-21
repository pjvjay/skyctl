"""
skyctl.adapters.gcs
===================
Native Google Cloud Storage ObjectStore with real compare-and-swap.

    version token   object generation
    if_absent       if_generation_match=0
    if_version      if_generation_match=<generation>
    lost race       412 google.api_core.exceptions.PreconditionFailed

Reads pin the generation they report, so data and version always agree.
Credentials: Application Default Credentials (Cloud Run service account,
GKE Workload Identity). Needs `skyctl[gcp]`.

    SKYCTL_STATE_URL=gs://bucket/prefix
"""

from __future__ import annotations

from typing import Optional

from ..ports import PreconditionFailed, Versioned


class GcsObjectStore:
    atomic_cas = True

    def __init__(self, bucket: str, prefix: str = "", bucket_client=None, project: Optional[str] = None):
        self.bucket_name = bucket
        self.prefix = prefix.strip("/")
        self._bucket = bucket_client
        self._project = project

    @property
    def bucket(self):
        if self._bucket is None:
            try:
                from google.cloud import storage
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("google-cloud-storage is not installed. Run: pip install 'skyctl[gcp]'") from e
            self._bucket = storage.Client(project=self._project).bucket(self.bucket_name)
        return self._bucket

    def _k(self, key: str) -> str:
        key = key.strip("/")
        if ".." in key.split("/"):
            raise ValueError(f"Key escapes the store root: {key}")
        return f"{self.prefix}/{key}" if self.prefix else key

    def get(self, key: str) -> Optional[Versioned]:
        from google.api_core.exceptions import NotFound, PreconditionFailed as GcsPrecondition

        for _ in range(3):
            blob = self.bucket.get_blob(self._k(key))
            if blob is None:
                return None
            try:
                data = blob.download_as_bytes(if_generation_match=blob.generation)
            except NotFound:
                return None
            except GcsPrecondition:
                continue        # overwritten between metadata and data reads; try again
            return Versioned(data, str(blob.generation))
        raise PreconditionFailed(key)

    def put(self, key: str, data: bytes, *, if_version: Optional[str] = None, if_absent: bool = False) -> str:
        from google.api_core.exceptions import PreconditionFailed as GcsPrecondition

        blob = self.bucket.blob(self._k(key))
        kwargs = {}
        if if_absent:
            kwargs["if_generation_match"] = 0
        elif if_version is not None:
            kwargs["if_generation_match"] = int(if_version)
        try:
            blob.upload_from_string(data, **kwargs)
        except GcsPrecondition as e:
            raise PreconditionFailed(key) from e
        return str(blob.generation)

    def read(self, key: str) -> bytes:
        found = self.get(key)
        if found is None:
            raise FileNotFoundError(key)
        return found.data

    def write(self, key: str, data: bytes) -> None:
        self.put(key, data)

    def exists(self, key: str) -> bool:
        return self.bucket.get_blob(self._k(key)) is not None

    def delete(self, key: str) -> None:
        from google.api_core.exceptions import NotFound

        try:
            self.bucket.blob(self._k(key)).delete()
        except NotFound:
            pass

    def list(self, prefix: str = "") -> list[str]:
        full = self._k(prefix) if prefix else (f"{self.prefix}/" if self.prefix else "")
        strip = len(self.prefix) + 1 if self.prefix else 0
        return sorted(b.name[strip:] for b in self.bucket.list_blobs(prefix=full))

    def append_line(self, key: str, line: str) -> None:
        from .local import append_line_cas

        append_line_cas(self, key, line)
