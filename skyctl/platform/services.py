"""
skyctl.platform.services
========================
URL scheme → adapter factory, per service kind. This is the service axis of
the framework; it is independent of the compute platform.

    kind      schemes (built in)
    state     file, memory, s3, az, gs          (+ any fsspec scheme as a non-atomic fallback)
    secrets   env, aws-sm, azure-kv, gcp-sm
    queue     memory, sqs, servicebus, azqueue, pubsub
    delayed   none, queue, eventbridge, cloudtasks

Plugins add schemes via the `skyctl.services` entry point group; the entry
point name is "<kind>:<scheme>", e.g. "state:oci" = "skyctl_oci:make_store".
"""

from __future__ import annotations

from typing import Callable, Optional
from urllib.parse import urlparse

_FACTORIES: dict[tuple[str, str], Callable] = {}
_LOADED = False


def register_scheme(kind: str, scheme: str, factory: Callable) -> None:
    _FACTORIES[(kind, scheme)] = factory


def _load_plugins() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        from importlib.metadata import entry_points

        for ep in entry_points(group="skyctl.services"):
            kind, _, scheme = ep.name.partition(":")
            try:
                register_scheme(kind, scheme, ep.load())
            except Exception as e:
                print(f"[skyctl] Warning: service plugin {ep.name!r} failed to load: {e}")
    except Exception:
        pass


def schemes(kind: str) -> list[str]:
    _load_plugins()
    return sorted(s for k, s in _FACTORIES if k == kind)


def build(kind: str, url: str, **kwargs):
    _load_plugins()
    scheme = (url.split("://", 1)[0] if "://" in url else url).strip() or "none"
    factory = _FACTORIES.get((kind, scheme))
    if factory is None:
        if kind == "state":
            from ..adapters.fsspec_store import FsspecObjectStore

            return FsspecObjectStore(url)       # generic fallback; atomic_cas=False
        raise ValueError(f"No {kind} adapter for scheme {scheme!r} (known: {', '.join(schemes(kind))}).")
    return factory(url, **kwargs)


# ----------------------------------------------------------------------
# Built-ins
# ----------------------------------------------------------------------

def _bucket_and_prefix(url: str) -> tuple[str, str]:
    p = urlparse(url)
    return p.netloc, p.path.strip("/")


def _state_file(url, **_):
    from ..adapters import object_store_for

    return object_store_for(url)


def _state_memory(url, **_):
    from ..adapters.local import MemoryObjectStore

    return MemoryObjectStore()


def _state_s3(url, **_):
    from ..adapters.s3 import S3ObjectStore

    return S3ObjectStore(*_bucket_and_prefix(url))


def _state_az(url, **_):
    from ..adapters.azure_blob import AzureBlobObjectStore

    return AzureBlobObjectStore(*_bucket_and_prefix(url))


def _state_gs(url, **_):
    from ..adapters.gcs import GcsObjectStore

    return GcsObjectStore(*_bucket_and_prefix(url))


def _secrets(url, env_file: str = ".env", **_):
    from ..adapters import secret_store_for

    return secret_store_for(url, env_file=env_file)


def _queue(url, **_):
    from ..adapters.queues import queue_for

    return queue_for(url)


def _delayed(url, queue=None, **_):
    from ..adapters.delayed import delayed_for

    return delayed_for(url, queue=queue)


for _s, _f in (("file", _state_file), ("memory", _state_memory), ("s3", _state_s3), ("az", _state_az), ("gs", _state_gs)):
    register_scheme("state", _s, _f)
for _s in ("env", "aws-sm", "azure-kv", "gcp-sm"):
    register_scheme("secrets", _s, _secrets)
for _s in ("memory", "sqs", "servicebus", "azqueue", "pubsub", "none"):
    register_scheme("queue", _s, _queue)
for _s in ("none", "queue", "eventbridge", "cloudtasks"):
    register_scheme("delayed", _s, _delayed)
