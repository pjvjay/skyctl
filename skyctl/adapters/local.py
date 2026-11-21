"""
skyctl.adapters.local
=====================
Adapters that need nothing but the standard library (plus python-dotenv):
process/.env secrets, file and in-memory object stores, and the session
and job stores built on top of any ObjectStore.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from dotenv import dotenv_values

from ..models import PostResult, ScheduledPost
from ..ports import PreconditionFailed, Versioned, cas_update


# ----------------------------------------------------------------------
# Secrets
# ----------------------------------------------------------------------

def parse_secret_payload(text: str) -> dict[str, str]:
    """
    A cloud secret holds the whole BSKY_* map as either a JSON object or
    dotenv text. Accept both so operators can paste their .env straight in.
    """
    text = text.strip()
    if not text:
        return {}
    if text.startswith("{"):
        data = json.loads(text)
        return {str(k): str(v) for k, v in data.items() if v is not None}
    return {k: v for k, v in dotenv_values(stream=io.StringIO(text)).items() if v is not None}


class EnvSecretStore:
    """Secrets from a .env file (if present). The process env always wins."""

    def __init__(self, env_file: str = ".env"):
        self.env_file = env_file

    def load(self) -> dict[str, str]:
        if not self.env_file or not Path(self.env_file).exists():
            return {}
        return {k: v for k, v in dotenv_values(self.env_file).items() if v is not None}


class StaticSecretStore:
    """A fixed map; handy for tests and embedding."""

    def __init__(self, values: dict[str, str]):
        self.values = dict(values)

    def load(self) -> dict[str, str]:
        return dict(self.values)


# ----------------------------------------------------------------------
# Object stores
# ----------------------------------------------------------------------

class FileObjectStore:
    """
    Keys map to files under a root directory. Secret-ish files are 0600.

    CAS uses a per-key lock file (O_CREAT|O_EXCL, portable) and a content
    hash as the version, so it is atomic between processes on one host or
    one shared POSIX volume (compose, a VM, a k8s ReadWriteOnce PVC).
    """

    atomic_cas = True
    LOCK_STALE_SECONDS = 30

    def __init__(self, root: str | Path):
        self.root = Path(root)

    @contextmanager
    def _locked(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.with_name(path.name + ".lock")
        deadline = time.monotonic() + 10
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > self.LOCK_STALE_SECONDS:
                        lock.unlink(missing_ok=True)   # holder died
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Could not lock {path}")
                time.sleep(0.005)
        try:
            yield
        finally:
            lock.unlink(missing_ok=True)

    @staticmethod
    def _version(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()[:32]

    def get(self, key: str) -> Optional[Versioned]:
        path = self._path(key)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        return Versioned(data, self._version(data))

    def put(self, key: str, data: bytes, *, if_version: Optional[str] = None, if_absent: bool = False) -> str:
        path = self._path(key)
        if if_version is None and not if_absent:
            self.write(key, data)
            return self._version(data)
        with self._locked(path):
            current = self.get(key)
            if if_absent and current is not None:
                raise PreconditionFailed(key)
            if if_version is not None and (current is None or current.version != if_version):
                raise PreconditionFailed(key)
            self._write_unlocked(path, data)
        return self._version(data)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents and path != self.root.resolve():
            raise ValueError(f"Key escapes the store root: {key}")
        return path

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def write(self, key: str, data: bytes) -> None:
        self._write_unlocked(self._path(key), data)

    @staticmethod
    def _write_unlocked(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def list(self, prefix: str = "") -> list[str]:
        base = self._path(prefix) if prefix else self.root
        if not base.exists():
            return []
        if base.is_file():
            return [prefix]
        return sorted(
            str(p.relative_to(self.root)).replace(os.sep, "/")
            for p in base.rglob("*")
            if p.is_file() and not p.name.endswith((".tmp", ".lock"))
        )

    def append_line(self, key: str, line: str) -> None:
        path = self._path(key)
        with self._locked(path):
            with path.open("a", encoding="utf-8") as f:
                f.write(line.rstrip("\n") + "\n")


def append_line_cas(store, key: str, line: str) -> None:
    """JSONL append for object stores: a CAS loop, so concurrent appends never lose lines."""
    encoded = (line.rstrip("\n") + "\n").encode("utf-8")
    cas_update(store, key, lambda current: (current or b"") + encoded)


def _check_key(key: str) -> str:
    if ".." in key.split("/"):
        raise ValueError(f"Key escapes the store root: {key}")
    return key


class MemoryObjectStore:
    """In-memory store for tests and ephemeral runs. Versions are a counter."""

    atomic_cas = True

    def __init__(self):
        self._data: dict[str, bytes] = {}
        self._versions: dict[str, int] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def _bump(self, key: str) -> str:
        self._counter += 1
        self._versions[key] = self._counter
        return str(self._counter)

    def get(self, key: str) -> Optional[Versioned]:
        with self._lock:
            key = _check_key(key)
            if key not in self._data:
                return None
            return Versioned(self._data[key], str(self._versions[key]))

    def put(self, key: str, data: bytes, *, if_version: Optional[str] = None, if_absent: bool = False) -> str:
        with self._lock:
            key = _check_key(key)
            exists = key in self._data
            if if_absent and exists:
                raise PreconditionFailed(key)
            if if_version is not None and (not exists or str(self._versions[key]) != if_version):
                raise PreconditionFailed(key)
            self._data[key] = bytes(data)
            return self._bump(key)

    def read(self, key: str) -> bytes:
        try:
            return self._data[_check_key(key)]
        except KeyError:
            raise FileNotFoundError(key) from None

    def write(self, key: str, data: bytes) -> None:
        self.put(key, data)

    def exists(self, key: str) -> bool:
        return _check_key(key) in self._data

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(_check_key(key), None)
            self._versions.pop(key, None)

    def list(self, prefix: str = "") -> list[str]:
        return sorted(k for k in self._data if k.startswith(prefix))

    def append_line(self, key: str, line: str) -> None:
        append_line_cas(self, key, line)


# ----------------------------------------------------------------------
# Sessions
# ----------------------------------------------------------------------

class ObjectSessionStore:
    """Session strings under <prefix><alias>.session in any ObjectStore."""

    def __init__(self, store, prefix: str = "sessions/"):
        self.store = store
        self.prefix = prefix

    def _key(self, alias: str) -> str:
        return f"{self.prefix}{alias}.session"

    def get(self, alias: str) -> Optional[str]:
        key = self._key(alias)
        if not self.store.exists(key):
            return None
        try:
            return self.store.read(key).decode("utf-8").strip() or None
        except Exception:
            return None

    def set(self, alias: str, session: str) -> None:
        self.store.write(self._key(alias), session.encode("utf-8"))

    def clear(self, alias: str) -> None:
        self.store.delete(self._key(alias))


class FileSessionStore(ObjectSessionStore):
    """The pre-existing layout: <dir>/<alias>.session, owner-only files."""

    def __init__(self, directory: str | Path):
        super().__init__(FileObjectStore(directory), prefix="")


# ----------------------------------------------------------------------
# Jobs
# ----------------------------------------------------------------------

def job_to_dict(sp: ScheduledPost) -> dict:
    return {
        "id": sp.id,
        "account": sp.account,
        "text": sp.text,
        "run_at": sp.run_at.isoformat(),
        "reply_to_uri": sp.reply_to_uri,
        "image_paths": sp.image_paths,
        "status": sp.status,
        "cron": sp.cron,
        "attempts": sp.attempts,
        "last_error": sp.last_error,
        "last_run": sp.last_run.isoformat() if sp.last_run else None,
        "retry_at": sp.retry_at.isoformat() if sp.retry_at else None,
        "claimed_by": sp.claimed_by,
        "claimed_at": sp.claimed_at.isoformat() if sp.claimed_at else None,
        "claimed_until": sp.claimed_until.isoformat() if sp.claimed_until else None,
        "sent_uri": sp.result.uri if sp.result is not None and sp.result.success else None,
    }


def job_from_dict(d: dict) -> ScheduledPost:
    return ScheduledPost(
        id=d["id"],
        account=d["account"],
        text=d["text"],
        run_at=datetime.fromisoformat(d["run_at"]),
        reply_to_uri=d.get("reply_to_uri"),
        image_paths=d.get("image_paths", []),
        status=d.get("status", "pending"),
        cron=d.get("cron"),
        attempts=d.get("attempts", 0),
        last_error=d.get("last_error"),
        last_run=datetime.fromisoformat(d["last_run"]) if d.get("last_run") else None,
        retry_at=datetime.fromisoformat(d["retry_at"]) if d.get("retry_at") else None,
        claimed_by=d.get("claimed_by"),
        claimed_at=datetime.fromisoformat(d["claimed_at"]) if d.get("claimed_at") else None,
        claimed_until=datetime.fromisoformat(d["claimed_until"]) if d.get("claimed_until") else None,
        result=(
            PostResult(account=d["account"], uri=d["sent_uri"], cid="", url="")
            if d.get("sent_uri") else None
        ),
    )


class ObjectJobStore:
    """
    The queue as one JSON document in an ObjectStore.

    Every mutation goes through update(), a CAS loop on the document's
    version, so concurrent writers (CLI, tick, worker, HTTP) never lose
    each other's changes. The lease is a separate document acquired by CAS;
    its version is a fencing token (see docs/CLOUD_FRAMEWORK.md §5).
    """

    def __init__(self, store, key: str = "queue.json", lease_key: Optional[str] = None):
        self.store = store
        self.key = key
        self.lease_key = lease_key or f"{key}.lease"
        self.fencing_token: Optional[str] = None

    @staticmethod
    def _decode(raw: Optional[bytes]) -> dict[str, ScheduledPost]:
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8") or "{}")
        return {job_id: job_from_dict(d) for job_id, d in data.items()}

    @staticmethod
    def _encode(jobs: dict[str, ScheduledPost]) -> bytes:
        return json.dumps({job_id: job_to_dict(sp) for job_id, sp in jobs.items()}, indent=2).encode("utf-8")

    def load(self) -> dict[str, ScheduledPost]:
        current = self.store.get(self.key)
        return self._decode(current.data if current else None)

    def save(self, jobs: dict[str, ScheduledPost]) -> None:
        """Unconditional overwrite. Prefer update(); kept for migrations and tests."""
        self.store.put(self.key, self._encode(jobs))

    def update(self, mutate: Callable[[dict[str, ScheduledPost]], Any]) -> Any:
        result: dict[str, Any] = {}

        def apply(raw: Optional[bytes]) -> Optional[bytes]:
            jobs = self._decode(raw)
            before = self._encode(jobs)
            result["value"] = mutate(jobs)
            after = self._encode(jobs)
            return None if after == before else after

        cas_update(self.store, self.key, apply)
        return result.get("value")

    # -- lease -----------------------------------------------------------

    def lease(self, owner: str, seconds: int) -> bool:
        now = datetime.now(timezone.utc)
        payload = json.dumps({"owner": owner, "expires": (now + timedelta(seconds=seconds)).isoformat()}).encode("utf-8")
        current = self.store.get(self.lease_key)
        try:
            if current is None:
                self.fencing_token = self.store.put(self.lease_key, payload, if_absent=True)
                return True
            try:
                held = json.loads(current.data)
                expired = datetime.fromisoformat(held["expires"]) <= now
                mine = held.get("owner") == owner
            except Exception:
                expired, mine = True, False            # corrupt lease: take it
            if not (expired or mine):
                return False
            self.fencing_token = self.store.put(self.lease_key, payload, if_version=current.version)
            return True
        except PreconditionFailed:
            return False                               # someone else won the race

    def release(self, owner: str) -> None:
        current = self.store.get(self.lease_key)
        if current is None:
            return
        try:
            if json.loads(current.data).get("owner") != owner:
                return
            # Expire it in place (conditional), rather than delete, so a racing
            # acquirer's CAS is never confused by a recreated object.
            expired = json.dumps({"owner": None, "expires": datetime.now(timezone.utc).isoformat()}).encode("utf-8")
            self.store.put(self.lease_key, expired, if_version=current.version)
        except (PreconditionFailed, ValueError):
            pass
        finally:
            self.fencing_token = None


class FileJobStore(ObjectJobStore):
    """The pre-existing layout: a single JSON file at a path of your choosing."""

    def __init__(self, path: str | Path):
        path = Path(path)
        super().__init__(FileObjectStore(path.parent), key=path.name)
