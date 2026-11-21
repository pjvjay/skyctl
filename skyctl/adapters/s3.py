"""
skyctl.adapters.s3
==================
Native S3 ObjectStore with real compare-and-swap.

    version token   ETag
    if_absent       PutObject If-None-Match: *      (S3 conditional writes, GA 2024)
    if_version      PutObject If-Match: <etag>
    lost race       HTTP 412 PreconditionFailed, or 409 ConditionalRequestConflict
                    when two conditional writes overlap

ETags are content hashes, so a version can recur (A→B→A). That is harmless
for whole-document read-modify-write (the stale writer's base equals the
current content); fencing tokens are taken from documents that embed a
unique timestamp, so they never recur.

Credentials: the boto3 default chain (ECS task role, Lambda role, EKS Pod
Identity / IRSA, instance profile, local profile). Needs `skyctl[aws]`.

    object_store_for("s3://bucket/prefix")
"""

from __future__ import annotations

from ..ports import PreconditionFailed, Versioned

_CONFLICT_CODES = {"PreconditionFailed", "ConditionalRequestConflict", "412", "409"}


class S3ObjectStore:
    atomic_cas = True

    def __init__(self, bucket: str, prefix: str = "", client=None, region: str | None = None):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = client
        self._region = region

    @property
    def client(self):
        if self._client is None:
            try:
                import boto3
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("boto3 is not installed. Run: pip install 'skyctl[aws]'") from e
            self._client = boto3.client("s3", region_name=self._region)
        return self._client

    def _k(self, key: str) -> str:
        key = key.strip("/")
        if ".." in key.split("/"):
            raise ValueError(f"Key escapes the store root: {key}")
        return f"{self.prefix}/{key}" if self.prefix else key

    @staticmethod
    def _code(error) -> str:
        resp = getattr(error, "response", {}) or {}
        return str(resp.get("Error", {}).get("Code") or resp.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))

    def _is_missing(self, error) -> bool:
        return self._code(error) in ("NoSuchKey", "404", "NotFound")

    # -- CAS -------------------------------------------------------------

    def get(self, key: str) -> Versioned | None:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=self._k(key))
        except Exception as e:
            if self._is_missing(e):
                return None
            raise
        return Versioned(resp["Body"].read(), resp["ETag"])

    def put(self, key: str, data: bytes, *, if_version: str | None = None, if_absent: bool = False) -> str:
        kwargs = {"Bucket": self.bucket, "Key": self._k(key), "Body": data}
        if if_absent:
            kwargs["IfNoneMatch"] = "*"
        if if_version is not None:
            kwargs["IfMatch"] = if_version
        try:
            return self.client.put_object(**kwargs)["ETag"]
        except Exception as e:
            if self._code(e) in _CONFLICT_CODES:
                raise PreconditionFailed(key) from e
            if if_version is not None and self._is_missing(e):
                raise PreconditionFailed(key) from e  # deleted since we read it: a lost race
            raise

    # -- plain operations ------------------------------------------------

    def read(self, key: str) -> bytes:
        found = self.get(key)
        if found is None:
            raise FileNotFoundError(key)
        return found.data

    def write(self, key: str, data: bytes) -> None:
        self.put(key, data)

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._k(key))
            return True
        except Exception as e:
            if self._is_missing(e):
                return False
            raise

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._k(key))

    def list(self, prefix: str = "") -> list[str]:
        full = self._k(prefix) if prefix else (f"{self.prefix}/" if self.prefix else "")
        strip = len(self.prefix) + 1 if self.prefix else 0
        keys = []
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=full):
            keys += [obj["Key"][strip:] for obj in page.get("Contents", [])]
        return sorted(keys)

    def append_line(self, key: str, line: str) -> None:
        from .local import append_line_cas

        append_line_cas(self, key, line)
