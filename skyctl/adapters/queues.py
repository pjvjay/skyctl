"""
skyctl.adapters.queues
======================
WorkQueue adapters. One contract (at-least-once, visibility window,
ack / nack(delay)), four very different services underneath:

    service         visibility model                 per-message delay      redelivery count
    -------------   ------------------------------   --------------------   ----------------------
    memory          per-receive window               yes                    yes
    SQS             VisibilityTimeout per receive    ≤ 900 s (DelaySeconds) ApproximateReceiveCount
    Storage Queue   visibility per receive; every    ≤ 7 days (initial      dequeue_count
                    update returns a NEW pop receipt visibility; TTL -1)    (no DLQ: <queue>-poison)
    Service Bus     peek-lock; lock duration is a    yes (scheduled         delivery_count
                    queue setting, renewable         enqueue time)
    Pub/Sub         ack deadline; modAckDeadline     no                     delivery_attempt (only
                    extends / 0 redelivers now                              with a dead-letter policy)

Where a service cannot honour part of the contract the adapter says so
(`supports_delay`, `max_delay_seconds`) rather than silently diverging; the
platform's capability set (skyctl.platform) advertises the same facts.

URLs (SKYCTL_QUEUE_URL):
    memory://name
    sqs://<queue-url-without-scheme>            e.g. sqs://sqs.us-east-1.amazonaws.com/123456789012/skyctl
    azqueue://<storage-account>/<queue>         Azure Storage Queue (Entra auth)
    servicebus://<namespace>.servicebus.windows.net/<queue>
    pubsub://<project>/<topic>?subscription=<subscription>
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qs, urlparse

from ..ports import QueueMessage


class QueueCapabilityError(ValueError):
    """The queue service cannot do what was asked (e.g. a delay on Pub/Sub)."""


# ----------------------------------------------------------------------
# Memory
# ----------------------------------------------------------------------

class MemoryQueue:
    supports_delay = True
    max_delay_seconds: Optional[int] = None

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}      # id -> {body, visible_at, attempts}

    def send(self, body: dict, delay_seconds: int = 0, dedupe_id: Optional[str] = None) -> str:
        msg_id = dedupe_id or uuid.uuid4().hex
        with self._lock:
            if dedupe_id and dedupe_id in self._items:
                return dedupe_id
            self._items[msg_id] = {"body": json.loads(json.dumps(body)), "visible_at": self._clock() + delay_seconds, "attempts": 0}
        return msg_id

    def receive(self, max_messages: int = 1, visibility_seconds: int = 300, wait_seconds: int = 0) -> list[QueueMessage]:
        deadline = self._clock() + wait_seconds
        while True:
            with self._lock:
                now = self._clock()
                out = []
                for msg_id, item in self._items.items():
                    if len(out) >= max_messages:
                        break
                    if item["visible_at"] <= now:
                        item["visible_at"] = now + visibility_seconds
                        item["attempts"] += 1
                        out.append(QueueMessage(msg_id, json.loads(json.dumps(item["body"])), msg_id, item["attempts"]))
            if out or self._clock() >= deadline:
                return out
            time.sleep(0.05)

    def ack(self, message: QueueMessage) -> None:
        with self._lock:
            self._items.pop(message.receipt, None)

    def nack(self, message: QueueMessage, delay_seconds: int = 0) -> None:
        with self._lock:
            if message.receipt in self._items:
                self._items[message.receipt]["visible_at"] = self._clock() + delay_seconds

    def extend(self, message: QueueMessage, seconds: int) -> None:
        self.nack(message, delay_seconds=seconds)

    def __len__(self) -> int:
        return len(self._items)


# ----------------------------------------------------------------------
# AWS SQS
# ----------------------------------------------------------------------

class SqsQueue:
    supports_delay = True
    max_delay_seconds = 900          # SQS DelaySeconds ceiling
    MAX_VISIBILITY = 43_200          # 12 hours

    def __init__(self, queue_url: str, client=None, region: Optional[str] = None):
        self.queue_url = queue_url
        self.fifo = queue_url.endswith(".fifo")
        self._client = client
        self._region = region

    @property
    def client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("sqs", region_name=self._region)
        return self._client

    def send(self, body: dict, delay_seconds: int = 0, dedupe_id: Optional[str] = None) -> str:
        if delay_seconds > self.max_delay_seconds:
            raise QueueCapabilityError(f"SQS delays are limited to {self.max_delay_seconds}s; use DelayedDelivery for longer.")
        kwargs = {"QueueUrl": self.queue_url, "MessageBody": json.dumps(body)}
        if self.fifo:
            # FIFO queues ignore per-message delays; dedupe and group ids are required.
            kwargs["MessageGroupId"] = body.get("kind", "skyctl")
            kwargs["MessageDeduplicationId"] = dedupe_id or uuid.uuid4().hex
        elif delay_seconds:
            kwargs["DelaySeconds"] = int(delay_seconds)
        return self.client.send_message(**kwargs)["MessageId"]

    def receive(self, max_messages: int = 1, visibility_seconds: int = 300, wait_seconds: int = 0) -> list[QueueMessage]:
        resp = self.client.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=max(1, min(max_messages, 10)),
            VisibilityTimeout=min(visibility_seconds, self.MAX_VISIBILITY),
            WaitTimeSeconds=min(wait_seconds, 20),
            MessageSystemAttributeNames=["ApproximateReceiveCount"],
        )
        return [
            QueueMessage(
                id=m["MessageId"],
                body=json.loads(m["Body"]),
                receipt=m["ReceiptHandle"],
                attempt=int(m.get("Attributes", {}).get("ApproximateReceiveCount", 1)),
            )
            for m in resp.get("Messages", [])
        ]

    def ack(self, message: QueueMessage) -> None:
        self.client.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message.receipt)

    def nack(self, message: QueueMessage, delay_seconds: int = 0) -> None:
        self.client.change_message_visibility(
            QueueUrl=self.queue_url, ReceiptHandle=message.receipt,
            VisibilityTimeout=min(delay_seconds, self.MAX_VISIBILITY),
        )

    def extend(self, message: QueueMessage, seconds: int) -> None:
        self.nack(message, delay_seconds=seconds)


# ----------------------------------------------------------------------
# Azure Service Bus
# ----------------------------------------------------------------------

class ServiceBusQueue:
    """
    Peek-lock receive. The lock duration is a property of the queue (set in
    IaC, max 5 minutes), so `visibility_seconds` is advisory: long handlers
    must call extend(). A receiver stays open for the adapter's lifetime
    because a message can only be settled on the link that received it.
    nack(delay>0) is emulated: complete, then re-send as a scheduled message
    (at-least-once is preserved; a crash in between can duplicate, never lose).
    """

    supports_delay = True
    max_delay_seconds: Optional[int] = None

    def __init__(self, namespace: str, queue: str, client=None):
        self.namespace = namespace
        self.queue = queue
        self._client = client
        self._receiver = None
        self._sender = None

    @property
    def client(self):
        if self._client is None:
            from azure.identity import DefaultAzureCredential
            from azure.servicebus import ServiceBusClient

            self._client = ServiceBusClient(self.namespace, credential=DefaultAzureCredential())
        return self._client

    @property
    def sender(self):
        if self._sender is None:
            self._sender = self.client.get_queue_sender(queue_name=self.queue)
        return self._sender

    @property
    def receiver(self):
        if self._receiver is None:
            self._receiver = self.client.get_queue_receiver(queue_name=self.queue)
        return self._receiver

    def send(self, body: dict, delay_seconds: int = 0, dedupe_id: Optional[str] = None) -> str:
        from azure.servicebus import ServiceBusMessage

        msg_id = dedupe_id or uuid.uuid4().hex
        kwargs = {"message_id": msg_id, "content_type": "application/json"}
        if delay_seconds:
            kwargs["scheduled_enqueue_time_utc"] = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        self.sender.send_messages(ServiceBusMessage(json.dumps(body), **kwargs))
        return msg_id

    def receive(self, max_messages: int = 1, visibility_seconds: int = 300, wait_seconds: int = 0) -> list[QueueMessage]:
        msgs = self.receiver.receive_messages(max_message_count=max_messages, max_wait_time=max(wait_seconds, 1))
        out = []
        for m in msgs:
            body = b"".join(m.body) if not isinstance(m.body, (bytes, str)) else m.body
            out.append(QueueMessage(id=m.message_id, body=json.loads(body), receipt=m, attempt=int(m.delivery_count or 0) + 1))
        return out

    def ack(self, message: QueueMessage) -> None:
        self.receiver.complete_message(message.receipt)

    def nack(self, message: QueueMessage, delay_seconds: int = 0) -> None:
        if delay_seconds <= 0:
            self.receiver.abandon_message(message.receipt)
            return
        self.send(message.body, delay_seconds=delay_seconds)
        self.receiver.complete_message(message.receipt)

    def extend(self, message: QueueMessage, seconds: int = 0) -> None:
        """Renews the lock for another full lock duration (a queue setting; `seconds` is ignored)."""
        self.receiver.renew_message_lock(message.receipt)


# ----------------------------------------------------------------------
# Azure Storage Queues
# ----------------------------------------------------------------------

class AzureStorageQueue:
    """
    Azure Storage Queue with Entra ID auth (DefaultAzureCredential; role
    "Storage Queue Data Contributor").

    * Delay = initial visibility timeout, at most 7 days. A message's
      visibility cannot outlast its time-to-live (default 7 days), so every
      message is sent with TTL -1 (never expires).
    * update_message() returns a NEW pop receipt and invalidates the old
      one, so extend()/nack() write it back into message.receipt.
    * There is no dead-letter queue: dead_letter() copies the message to
      "<queue>-poison" (the convention Azure Functions uses) and deletes it.
    """

    supports_delay = True
    max_delay_seconds = 7 * 24 * 3600
    MAX_VISIBILITY = 7 * 24 * 3600

    def __init__(self, account: str, queue: str, client=None, poison_client=None):
        self.account = account
        self.queue = queue
        self._client = client
        self._poison = poison_client

    def _make(self, name: str):
        from azure.identity import DefaultAzureCredential
        from azure.storage.queue import QueueClient

        return QueueClient(f"https://{self.account}.queue.core.windows.net", name, credential=DefaultAzureCredential())

    @property
    def client(self):
        if self._client is None:
            self._client = self._make(self.queue)
        return self._client

    @property
    def poison(self):
        if self._poison is None:
            self._poison = self._make(f"{self.queue}-poison")
        return self._poison

    def send(self, body: dict, delay_seconds: int = 0, dedupe_id: Optional[str] = None) -> str:
        if delay_seconds > self.max_delay_seconds:
            raise QueueCapabilityError("Storage Queue delays are limited to 7 days; the tick reconciler covers longer.")
        msg = self.client.send_message(json.dumps(body), visibility_timeout=int(delay_seconds) or None, time_to_live=-1)
        return msg.id

    def receive(self, max_messages: int = 1, visibility_seconds: int = 300, wait_seconds: int = 0) -> list[QueueMessage]:
        # Storage Queues have no long polling; emulate a short wait.
        deadline = time.monotonic() + wait_seconds
        while True:
            msgs = list(self.client.receive_messages(
                max_messages=max(1, min(max_messages, 32)),
                visibility_timeout=min(visibility_seconds, self.MAX_VISIBILITY),
            ))
            if msgs or time.monotonic() >= deadline:
                break
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        return [
            QueueMessage(id=m.id, body=json.loads(m.content), receipt=m.pop_receipt, attempt=int(m.dequeue_count or 1))
            for m in msgs
        ]

    def ack(self, message: QueueMessage) -> None:
        self.client.delete_message(message.id, message.receipt)

    def nack(self, message: QueueMessage, delay_seconds: int = 0) -> None:
        updated = self.client.update_message(
            message.id, message.receipt, visibility_timeout=min(max(delay_seconds, 0), self.MAX_VISIBILITY)
        )
        message.receipt = updated.pop_receipt        # the old receipt is now invalid

    def extend(self, message: QueueMessage, seconds: int) -> None:
        self.nack(message, delay_seconds=seconds)

    def dead_letter(self, message: QueueMessage, reason: str = "") -> None:
        self.poison.send_message(json.dumps({"body": message.body, "reason": reason, "attempt": message.attempt}),
                                 time_to_live=-1)
        self.ack(message)


# ----------------------------------------------------------------------
# Google Pub/Sub
# ----------------------------------------------------------------------

class PubSubQueue:
    """
    Synchronous pull. Pub/Sub has no per-message delay (use Cloud Tasks via
    DelayedDelivery), so send(delay>0) raises QueueCapabilityError. Pull
    subscriptions suit jobs/workers; for Cloud Run services prefer a push
    subscription into /invoke (skyctl.platform.invocation decodes it).
    """

    supports_delay = False
    max_delay_seconds = 0
    MAX_ACK_DEADLINE = 600

    def __init__(self, project: str, topic: str, subscription: str, publisher=None, subscriber=None):
        self.project = project
        self.topic_path = f"projects/{project}/topics/{topic}"
        self.subscription_path = f"projects/{project}/subscriptions/{subscription}"
        self._publisher = publisher
        self._subscriber = subscriber

    @property
    def publisher(self):
        if self._publisher is None:
            from google.cloud import pubsub_v1

            self._publisher = pubsub_v1.PublisherClient()
        return self._publisher

    @property
    def subscriber(self):
        if self._subscriber is None:
            from google.cloud import pubsub_v1

            self._subscriber = pubsub_v1.SubscriberClient()
        return self._subscriber

    def send(self, body: dict, delay_seconds: int = 0, dedupe_id: Optional[str] = None) -> str:
        if delay_seconds:
            raise QueueCapabilityError("Pub/Sub has no delayed delivery; use Cloud Tasks (DelayedDelivery).")
        attrs = {"dedupe_id": dedupe_id} if dedupe_id else {}
        return self.publisher.publish(self.topic_path, json.dumps(body).encode("utf-8"), **attrs).result()

    def receive(self, max_messages: int = 1, visibility_seconds: int = 300, wait_seconds: int = 0) -> list[QueueMessage]:
        resp = self.subscriber.pull(
            request={"subscription": self.subscription_path, "max_messages": max_messages},
            timeout=max(wait_seconds, 1),
        )
        received = list(resp.received_messages)
        if received:
            self.subscriber.modify_ack_deadline(request={
                "subscription": self.subscription_path,
                "ack_ids": [r.ack_id for r in received],
                "ack_deadline_seconds": min(visibility_seconds, self.MAX_ACK_DEADLINE),
            })
        return [
            QueueMessage(
                id=r.message.message_id,
                body=json.loads(r.message.data),
                receipt=r.ack_id,
                attempt=int(getattr(r, "delivery_attempt", 0) or 1),
            )
            for r in received
        ]

    def ack(self, message: QueueMessage) -> None:
        self.subscriber.acknowledge(request={"subscription": self.subscription_path, "ack_ids": [message.receipt]})

    def nack(self, message: QueueMessage, delay_seconds: int = 0) -> None:
        self.subscriber.modify_ack_deadline(request={
            "subscription": self.subscription_path,
            "ack_ids": [message.receipt],
            "ack_deadline_seconds": min(max(delay_seconds, 0), self.MAX_ACK_DEADLINE),
        })

    def extend(self, message: QueueMessage, seconds: int) -> None:
        self.nack(message, delay_seconds=seconds)


# ----------------------------------------------------------------------
# Factory
# ----------------------------------------------------------------------

_MEMORY_QUEUES: dict[str, MemoryQueue] = {}


def queue_for(url: str):
    """Build a WorkQueue from SKYCTL_QUEUE_URL. Returns None for '' / 'none'."""
    url = (url or "").strip()
    if url in ("", "none"):
        return None
    parsed = urlparse(url)
    scheme = parsed.scheme
    if scheme == "memory":
        return _MEMORY_QUEUES.setdefault(parsed.netloc or "default", MemoryQueue())
    if scheme == "sqs":
        return SqsQueue(f"https://{parsed.netloc}{parsed.path}")
    if scheme == "servicebus":
        return ServiceBusQueue(parsed.netloc, parsed.path.strip("/"))
    if scheme == "azqueue":
        return AzureStorageQueue(parsed.netloc, parsed.path.strip("/"))
    if scheme == "pubsub":
        topic = parsed.path.strip("/")
        sub = parse_qs(parsed.query).get("subscription", [f"{topic}-worker"])[0]
        return PubSubQueue(parsed.netloc, topic, sub)
    raise ValueError(f"Unknown queue URL scheme {scheme!r} in {url!r}")
