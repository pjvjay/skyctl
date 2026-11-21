"""
In-memory stand-ins for cloud SDK clients, faithful to the calls skyctl
makes and raising the SDKs' own exception types. Used where no in-process
emulator exists (moto covers AWS; these cover Azure Blob, GCS, Service Bus,
Pub/Sub, Cloud Tasks). They are test doubles, not emulators: behaviour
outside what skyctl uses is not modelled.
"""

from __future__ import annotations

import itertools
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Optional

# ----------------------------------------------------------------------
# Azure Blob (azure-storage-blob ContainerClient)
# ----------------------------------------------------------------------


class FakeAzureContainer:
    def __init__(self):
        self.blobs: dict[str, tuple[bytes, str]] = {}
        self.lock = threading.Lock()
        self._n = itertools.count(1)

    def get_blob_client(self, name: str) -> "FakeAzureBlob":
        return FakeAzureBlob(self, name)

    def list_blobs(self, name_starts_with: str = ""):
        return [SimpleNamespace(name=n) for n in sorted(self.blobs) if n.startswith(name_starts_with)]


class FakeAzureBlob:
    def __init__(self, container: FakeAzureContainer, name: str):
        self.c, self.name = container, name

    def download_blob(self):
        from azure.core.exceptions import ResourceNotFoundError

        with self.c.lock:
            if self.name not in self.c.blobs:
                raise ResourceNotFoundError("BlobNotFound")
            data, etag = self.c.blobs[self.name]
        return SimpleNamespace(readall=lambda: data, properties=SimpleNamespace(etag=etag))

    def upload_blob(self, data, overwrite=False, etag=None, match_condition=None, **_):
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceExistsError, ResourceModifiedError

        with self.c.lock:
            current = self.c.blobs.get(self.name)
            if not overwrite and current is not None:
                raise ResourceExistsError("BlobAlreadyExists")
            if match_condition == MatchConditions.IfNotModified and (current is None or current[1] != etag):
                raise ResourceModifiedError("ConditionNotMet")
            new_etag = f'"0x{next(self.c._n):08X}"'
            self.c.blobs[self.name] = (bytes(data), new_etag)
            return {"etag": new_etag}

    def exists(self) -> bool:
        return self.name in self.c.blobs

    def delete_blob(self):
        from azure.core.exceptions import ResourceNotFoundError

        with self.c.lock:
            if self.name not in self.c.blobs:
                raise ResourceNotFoundError("BlobNotFound")
            del self.c.blobs[self.name]


# ----------------------------------------------------------------------
# Google Cloud Storage (google-cloud-storage Bucket / Blob)
# ----------------------------------------------------------------------


class FakeGcsBucket:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, int]] = {}
        self.lock = threading.Lock()
        self._gen = itertools.count(1_700_000_000_000_000)

    def blob(self, name: str) -> "FakeGcsBlob":
        return FakeGcsBlob(self, name)

    def get_blob(self, name: str) -> Optional["FakeGcsBlob"]:
        with self.lock:
            if name not in self.objects:
                return None
            b = FakeGcsBlob(self, name)
            b.generation = self.objects[name][1]
            return b

    def list_blobs(self, prefix: str = ""):
        return [SimpleNamespace(name=n) for n in sorted(self.objects) if n.startswith(prefix)]


class FakeGcsBlob:
    def __init__(self, bucket: FakeGcsBucket, name: str):
        self.b, self.name, self.generation = bucket, name, None

    def download_as_bytes(self, if_generation_match=None):
        from google.api_core.exceptions import NotFound, PreconditionFailed

        with self.b.lock:
            if self.name not in self.b.objects:
                raise NotFound("No such object")
            data, gen = self.b.objects[self.name]
            if if_generation_match is not None and gen != if_generation_match:
                raise PreconditionFailed("generation mismatch")
            return data

    def upload_from_string(self, data, if_generation_match=None, **_):
        from google.api_core.exceptions import PreconditionFailed

        with self.b.lock:
            current = self.b.objects.get(self.name)
            if if_generation_match is not None:
                current_gen = current[1] if current else 0
                if current_gen != if_generation_match:
                    raise PreconditionFailed("generation mismatch")
            gen = next(self.b._gen)
            self.b.objects[self.name] = (data if isinstance(data, bytes) else data.encode(), gen)
            self.generation = gen

    def delete(self):
        from google.api_core.exceptions import NotFound

        with self.b.lock:
            if self.name not in self.b.objects:
                raise NotFound("No such object")
            del self.b.objects[self.name]


# ----------------------------------------------------------------------
# Azure Service Bus (azure-servicebus ServiceBusClient, peek-lock)
# ----------------------------------------------------------------------


class FakeServiceBus:
    """One queue. Lock duration is a queue property, as in the real service."""

    def __init__(self, lock_seconds: float = 30.0, clock=time.monotonic):
        self.lock_seconds = lock_seconds
        self.clock = clock
        self.lock = threading.Lock()
        self.messages: dict[str, dict] = {}   # id -> {body, visible_at, delivery_count, locked_until}

    def get_queue_sender(self, queue_name: str):
        return SimpleNamespace(send_messages=self._send)

    def get_queue_receiver(self, queue_name: str):
        return SimpleNamespace(
            receive_messages=self._receive,
            complete_message=self._complete,
            abandon_message=self._abandon,
            renew_message_lock=self._renew,
        )

    def _send(self, msg):
        from datetime import datetime, timezone

        visible_at = self.clock()
        if msg.scheduled_enqueue_time_utc is not None:
            visible_at += max(0.0, (msg.scheduled_enqueue_time_utc - datetime.now(timezone.utc)).total_seconds())
        with self.lock:
            self.messages[msg.message_id or uuid.uuid4().hex] = {
                "body": str(msg).encode("utf-8"), "visible_at": visible_at, "delivery_count": 0, "locked_until": 0.0,
            }

    def _receive(self, max_message_count=1, max_wait_time=None):
        now = self.clock()
        out = []
        with self.lock:
            for msg_id, m in self.messages.items():
                if len(out) >= max_message_count:
                    break
                if m["visible_at"] <= now and m["locked_until"] <= now:
                    m["locked_until"] = now + self.lock_seconds
                    out.append(SimpleNamespace(message_id=msg_id, body=iter([m["body"]]), delivery_count=m["delivery_count"]))
                    m["delivery_count"] += 1
        return out

    def _complete(self, received):
        with self.lock:
            self.messages.pop(received.message_id, None)

    def _abandon(self, received):
        with self.lock:
            if received.message_id in self.messages:
                self.messages[received.message_id]["locked_until"] = 0.0

    def _renew(self, received):
        with self.lock:
            if received.message_id in self.messages:
                self.messages[received.message_id]["locked_until"] = self.clock() + self.lock_seconds


# ----------------------------------------------------------------------
# Google Pub/Sub (PublisherClient / SubscriberClient, synchronous pull)
# ----------------------------------------------------------------------


class FakePubSub:
    def __init__(self, ack_deadline: float = 10.0, clock=time.monotonic):
        self.ack_deadline = ack_deadline
        self.clock = clock
        self.lock = threading.Lock()
        self.messages: dict[str, dict] = {}   # message_id -> {data, visible_at, attempts, ack_id}
        self._ids = itertools.count(1)

    # publisher side
    def publish(self, topic, data, **attrs):
        msg_id = str(next(self._ids))
        with self.lock:
            self.messages[msg_id] = {"data": data, "visible_at": 0.0, "attempts": 0, "ack_id": None, "attrs": attrs}
        return SimpleNamespace(result=lambda: msg_id)

    # subscriber side
    def pull(self, request, timeout=None):
        now = self.clock()
        out = []
        with self.lock:
            for msg_id, m in self.messages.items():
                if len(out) >= request["max_messages"]:
                    break
                if m["visible_at"] <= now:
                    m["attempts"] += 1
                    m["ack_id"] = f"ack-{msg_id}-{m['attempts']}"
                    m["visible_at"] = now + self.ack_deadline
                    out.append(SimpleNamespace(
                        ack_id=m["ack_id"],
                        message=SimpleNamespace(message_id=msg_id, data=m["data"]),
                        delivery_attempt=0,        # 0 unless a dead-letter policy is set
                    ))
        return SimpleNamespace(received_messages=out)

    def _by_ack(self, ack_id):
        return next((k for k, m in self.messages.items() if m["ack_id"] == ack_id), None)

    def acknowledge(self, request):
        with self.lock:
            for ack_id in request["ack_ids"]:
                key = self._by_ack(ack_id)
                if key:
                    del self.messages[key]

    def modify_ack_deadline(self, request):
        with self.lock:
            for ack_id in request["ack_ids"]:
                key = self._by_ack(ack_id)
                if key:
                    self.messages[key]["visible_at"] = self.clock() + request["ack_deadline_seconds"]


# ----------------------------------------------------------------------
# Google Cloud Tasks (CloudTasksClient)
# ----------------------------------------------------------------------


class FakeCloudTasks:
    def __init__(self):
        self.tasks: dict[str, dict] = {}

    def create_task(self, request):
        from google.api_core.exceptions import AlreadyExists

        task = request["task"]
        if task["name"] in self.tasks:
            raise AlreadyExists("task exists")
        self.tasks[task["name"]] = task
        return SimpleNamespace(name=task["name"])

    def delete_task(self, request):
        from google.api_core.exceptions import NotFound

        if request["name"] not in self.tasks:
            raise NotFound("no task")
        del self.tasks[request["name"]]
