"""
skyctl.ports
============
The interfaces ("ports") through which the core touches its environment.
Cloud SDKs are only ever imported by adapters in skyctl.adapters; the core
depends on these Protocols alone.

Each port states its *semantics*, not just its signature. An adapter
"supports" a port only if it passes the contract tests in
skyctl.testing.contracts.

    ObjectStore     bytes by key, with compare-and-swap (CAS) on a version token
    SessionStore    atproto session strings per account alias
    JobStore        the scheduled-post queue; every mutation is a CAS update
    WorkQueue       at-least-once messages with visibility timeout, ack/nack
    DelayedDelivery deliver a message at a future time (native fast path)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from .models import ScheduledPost


class PreconditionFailed(Exception):
    """A conditional write lost a race: the object changed (or exists) since it was read."""


class ConflictError(RuntimeError):
    """A CAS update kept losing races and gave up."""


@dataclass(frozen=True)
class Versioned:
    """Object bytes plus the opaque version token to pass back to put()."""

    data: bytes
    version: str  # S3/Blob: ETag; GCS: generation; file/memory: content hash/counter


@runtime_checkable
class ObjectStore(Protocol):
    """
    A flat key → bytes store. Keys are relative, '/'-separated paths.

    Contract:
      * Read-after-write consistent for a single key (true of S3, Blob and GCS today).
      * put(if_absent=True) succeeds for exactly one of N concurrent callers.
      * put(if_version=v) succeeds only if the object is still at version v.
      * A losing conditional put raises PreconditionFailed and changes nothing.
      * `atomic_cas` is False for adapters that can only emulate CAS (fsspec
        fallbacks); the JobStore refuses multi-writer use on them.
    """

    atomic_cas: bool

    def read(self, key: str) -> bytes: ...
    def write(self, key: str, data: bytes) -> None: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def list(self, prefix: str = "") -> list[str]: ...

    def get(self, key: str) -> Versioned | None:
        """The object and its version, or None if absent."""
        ...

    def put(self, key: str, data: bytes, *, if_version: str | None = None, if_absent: bool = False) -> str:
        """Write, optionally conditionally. Returns the new version. Raises PreconditionFailed."""
        ...

    def append_line(self, key: str, line: str) -> None:
        """Append one text line (JSONL). Implemented as a CAS loop on object stores."""
        ...


def cas_update(
    store: ObjectStore,
    key: str,
    mutate: Callable[[bytes | None], bytes | None],
    retries: int = 12,
) -> str | None:
    """
    Optimistic read-modify-write. `mutate(current_bytes_or_None)` returns the
    new bytes, or None to leave the object unchanged. Retries on
    PreconditionFailed with jittered backoff. Returns the new version (or the
    current one if unchanged).
    """
    import random
    import time

    for attempt in range(retries):
        current = store.get(key)
        new = mutate(current.data if current else None)
        if new is None:
            return current.version if current else None
        try:
            if current is None:
                return store.put(key, new, if_absent=True)
            return store.put(key, new, if_version=current.version)
        except PreconditionFailed:
            time.sleep(min(0.5, 0.005 * (2**attempt)) * (0.5 + random.random()))
    raise ConflictError(f"Gave up updating {key!r} after {retries} conflicting writes.")


@runtime_checkable
class SessionStore(Protocol):
    def get(self, alias: str) -> str | None: ...
    def set(self, alias: str, session: str) -> None: ...
    def clear(self, alias: str) -> None: ...


@runtime_checkable
class JobStore(Protocol):
    """
    Persistence for ScheduledPost objects keyed by job id.

    Contract: update() applies `mutate` atomically with respect to every other
    update() (CAS under the hood), so the CLI adding a job and a tick sending
    one can never lose each other's changes.
    """

    def load(self) -> dict[str, ScheduledPost]: ...
    def save(self, jobs: dict[str, ScheduledPost]) -> None: ...

    def update(self, mutate: Callable[[dict[str, ScheduledPost]], Any]) -> Any:
        """Atomically apply mutate(jobs) (which edits the dict in place); returns its result."""
        ...

    def lease(self, owner: str, seconds: int) -> bool:
        """Take a short exclusive lease for a tick. False if someone holds it."""
        ...

    def release(self, owner: str) -> None: ...


# ----------------------------------------------------------------------
# Messaging
# ----------------------------------------------------------------------


@dataclass
class QueueMessage:
    id: str
    body: dict
    receipt: Any  # adapter-specific handle for ack/nack
    attempt: int = 1  # delivery count, 1-based (where the service reports it)


@runtime_checkable
class WorkQueue(Protocol):
    """
    At-least-once work queue.

    Contract: a received message is invisible to other receivers for
    `visibility_seconds`; ack() removes it; nack(delay) makes it visible again
    after `delay`; a message not acked within the window is redelivered.
    Handlers must be idempotent. Maps to SQS, Service Bus (peek-lock),
    Pub/Sub (ack deadline) and Redis/NATS on Kubernetes.
    """

    def send(self, body: dict, delay_seconds: int = 0, dedupe_id: str | None = None) -> str: ...
    def receive(
        self, max_messages: int = 1, visibility_seconds: int = 300, wait_seconds: int = 0
    ) -> list[QueueMessage]: ...
    def ack(self, message: QueueMessage) -> None: ...
    def nack(self, message: QueueMessage, delay_seconds: int = 0) -> None: ...

    def extend(self, message: QueueMessage, seconds: int) -> None:
        """
        Keep an in-flight message hidden for `seconds` more. Long handlers
        heartbeat with this, because some services cap the window far below a
        long handler run (Service Bus locks: 5 min; Pub/Sub ack deadline: 10 min).
        """
        ...


@runtime_checkable
class DelayedDelivery(Protocol):
    """
    Have the cloud platform deliver `body` back to your handler at `when`.

    Contract: best-effort fast path, at-least-once. `max_horizon_seconds` is
    how far ahead it can schedule (None = unlimited). The tick reconciler is
    always the safety net, so a missed or duplicated delivery is harmless.
    """

    max_horizon_seconds: int | None

    def schedule(self, key: str, when: datetime, body: dict) -> None: ...
    def cancel(self, key: str) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
