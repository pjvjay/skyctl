"""WorkQueue and DelayedDelivery adapters against moto (AWS) and SDK-faithful fakes."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from skyctl.adapters.delayed import (
    CloudTasksDelivery,
    EventBridgeSchedulerDelivery,
    QueueDelayDelivery,
    delayed_for,
)
from skyctl.adapters.queues import (
    MemoryQueue,
    PubSubQueue,
    QueueCapabilityError,
    ServiceBusQueue,
    SqsQueue,
    queue_for,
)
from skyctl.ports import DelayedDelivery, WorkQueue
from skyctl.testing.contracts import check_work_queue
from skyctl.testing.fakes import FakeCloudTasks, FakePubSub, FakeServiceBus


@pytest.fixture
def aws():
    moto = pytest.importorskip("moto")
    boto3 = pytest.importorskip("boto3")
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


# ----------------------------------------------------------------------
# Delayed delivery
# ----------------------------------------------------------------------

def test_eventbridge_scheduler_one_shot(aws):
    client = aws.client("scheduler", region_name="us-east-1")
    client.create_schedule_group(Name="skyctl")
    d = EventBridgeSchedulerDelivery("skyctl", "arn:aws:sqs:us-east-1:123456789012:skyctl-work",
                                     "arn:aws:iam::123456789012:role/scheduler", client=client)
    assert isinstance(d, DelayedDelivery) and d.max_horizon_seconds is None
    when = datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc)
    d.schedule("job/1", when, {"kind": "post.send", "job_id": "job/1"})
    got = client.get_schedule(Name="skyctl-job-1", GroupName="skyctl")
    assert got["ScheduleExpression"] == "at(2030-01-01T09:00:00)"
    assert got["ActionAfterCompletion"] == "DELETE"
    assert json.loads(got["Target"]["Input"]) == {"kind": "post.send", "job_id": "job/1"}
    d.schedule("job/1", when + timedelta(hours=1), {"kind": "post.send", "job_id": "job/1"})   # reschedule replaces
    assert client.get_schedule(Name="skyctl-job-1", GroupName="skyctl")["ScheduleExpression"] == "at(2030-01-01T10:00:00)"
    d.cancel("job/1")
    d.cancel("job/1")   # idempotent
    assert client.list_schedules(GroupName="skyctl")["Schedules"] == []


def test_sqs_delay_delivery_has_a_15_minute_horizon(aws):
    sqs = aws.client("sqs", region_name="us-east-1")
    q = SqsQueue(sqs.create_queue(QueueName="d")["QueueUrl"], client=sqs)
    d = QueueDelayDelivery(q)
    assert d.max_horizon_seconds == 900
    d.schedule("j", datetime.now(timezone.utc) + timedelta(seconds=30), {"kind": "post.send", "job_id": "j"})
    assert q.receive(visibility_seconds=1) == []        # not yet visible


def test_cloud_tasks_delivery():
    fake = FakeCloudTasks()
    d = CloudTasksDelivery("proj", "europe-west1", "skyctl", "https://svc.run.app/invoke", "invoker@proj.iam.gserviceaccount.com", client=fake)
    assert d.max_horizon_seconds == 30 * 24 * 3600
    when = datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc)
    d.schedule("abc", when, {"kind": "post.send", "job_id": "abc"})
    d.schedule("abc", when, {"kind": "post.send", "job_id": "abc"})     # duplicate is absorbed
    [(name, task)] = fake.tasks.items()
    assert name == "projects/proj/locations/europe-west1/queues/skyctl/tasks/skyctl-abc"
    assert task["http_request"]["oidc_token"]["audience"] == "https://svc.run.app/invoke"
    assert json.loads(task["http_request"]["body"]) == {"kind": "post.send", "job_id": "abc"}
    assert task["schedule_time"].ToDatetime() == datetime(2030, 1, 1, 9, 0)
    d.cancel("abc")
    d.cancel("abc")
    assert fake.tasks == {}


def test_delayed_url_factory():
    assert delayed_for("none") is None
    with pytest.raises(ValueError):
        delayed_for("queue")
    with pytest.raises(ValueError):
        delayed_for("queue", queue=PubSubQueue("p", "t", "s", publisher=FakePubSub(), subscriber=FakePubSub()))
    assert isinstance(delayed_for("queue", queue=MemoryQueue()), QueueDelayDelivery)
    eb = delayed_for("eventbridge://skyctl?target=arn:aws:sqs:x&role=arn:aws:iam::1:role/r")
    assert (eb.group, eb.target_arn, eb.role_arn) == ("skyctl", "arn:aws:sqs:x", "arn:aws:iam::1:role/r")
    ct = delayed_for("cloudtasks://proj/europe-west1/skyctl?url=https://x/invoke&sa=a@b")
    assert ct.parent == "projects/proj/locations/europe-west1/queues/skyctl"
