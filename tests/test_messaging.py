"""WorkQueue adapters against moto (AWS) and SDK-faithful fakes."""

import json

import pytest

from conftest import require
from skyctl.adapters.queues import (
    MemoryQueue,
    PubSubQueue,
    QueueCapabilityError,
    ServiceBusQueue,
    SqsQueue,
    queue_for,
)
from skyctl.ports import WorkQueue
from skyctl.testing.contracts import check_work_queue
from skyctl.testing.fakes import FakePubSub, FakeServiceBus


@pytest.fixture
def aws():
    moto = require("moto")
    boto3 = require("boto3")
    with moto.mock_aws():
        yield boto3


def test_memory_queue_contract():
    check_work_queue(MemoryQueue())


def test_sqs_queue_contract(aws):
    sqs = aws.client("sqs", region_name="us-east-1")
    url = sqs.create_queue(QueueName="skyctl-work")["QueueUrl"]
    q = SqsQueue(url, client=sqs)
    assert isinstance(q, WorkQueue)
    check_work_queue(q)
    with pytest.raises(QueueCapabilityError):
        q.send({"n": 3}, delay_seconds=901)


def test_service_bus_queue_contract():
    q = ServiceBusQueue("ns.servicebus.windows.net", "skyctl", client=FakeServiceBus())
    check_work_queue(q)
    # nack with a delay is emulated as complete + scheduled re-send
    q.send({"n": 9})
    [m] = q.receive()
    q.nack(m, delay_seconds=60)
    assert q.receive() == []


def test_pubsub_queue_contract_without_delay():
    fake = FakePubSub()
    q = PubSubQueue("proj", "skyctl", "skyctl-worker", publisher=fake, subscriber=fake)
    check_work_queue(q, supports_delay=False)
    with pytest.raises(QueueCapabilityError):
        q.send({"n": 1}, delay_seconds=5)


def test_queue_url_factory():
    assert isinstance(queue_for("memory://x"), MemoryQueue)
    assert queue_for("memory://x") is queue_for("memory://x")
    assert queue_for("") is None and queue_for("none") is None
    sqs = queue_for("sqs://sqs.us-east-1.amazonaws.com/123456789012/skyctl")
    assert isinstance(sqs, SqsQueue) and sqs.queue_url == "https://sqs.us-east-1.amazonaws.com/123456789012/skyctl"
    sb = queue_for("servicebus://ns.servicebus.windows.net/skyctl")
    assert (sb.namespace, sb.queue) == ("ns.servicebus.windows.net", "skyctl")
    ps = queue_for("pubsub://proj/skyctl?subscription=workers")
    assert ps.subscription_path == "projects/proj/subscriptions/workers"
    with pytest.raises(ValueError):
        queue_for("kafka://x")


def _storage_queue():
    from skyctl.adapters.queues import AzureStorageQueue
    from skyctl.testing.fakes import FakeStorageQueue

    main, poison = FakeStorageQueue(), FakeStorageQueue()
    return AzureStorageQueue("acct", "skyctl-work", client=main, poison_client=poison), main, poison


def test_storage_queue_contract():
    q, _, _ = _storage_queue()
    check_work_queue(q)


def test_storage_queue_delay_up_to_seven_days_never_expires():
    q, main, _ = _storage_queue()
    q.send({"kind": "post.send", "job_id": "j"}, delay_seconds=7 * 24 * 3600)
    [m] = main.messages.values()
    assert m["ttl"] == -1  # otherwise a 7-day delay would expire first
    with pytest.raises(QueueCapabilityError):
        q.send({"kind": "x"}, delay_seconds=7 * 24 * 3600 + 1)


def test_storage_queue_tracks_the_new_pop_receipt_after_extend():
    q, _, _ = _storage_queue()
    q.send({"kind": "tick"})
    [m] = q.receive(visibility_seconds=30)
    first = m.receipt
    q.extend(m, 60)  # heartbeat during a long run
    assert m.receipt != first
    q.ack(m)  # would fail with the stale receipt
    assert q.receive(visibility_seconds=1) == []


def test_storage_queue_dead_letter_moves_the_message_to_the_poison_queue():
    q, main, poison = _storage_queue()
    q.send({"kind": "unknown-kind"})
    [m] = q.receive()
    q.dead_letter(m, reason="No handler for unknown-kind")
    assert main.messages == {}
    [p] = poison.messages.values()
    dead = json.loads(p["content"])
    assert dead["body"] == {"kind": "unknown-kind"} and "No handler" in dead["reason"]


def test_storage_queue_url():
    from skyctl.adapters.queues import AzureStorageQueue

    q = queue_for("azqueue://skyctlst1234/skyctl-work")
    assert isinstance(q, AzureStorageQueue) and (q.account, q.queue) == ("skyctlst1234", "skyctl-work")
