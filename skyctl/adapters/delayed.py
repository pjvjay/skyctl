# Copyright 2026 The skyctl Authors
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
skyctl.adapters.delayed
=======================
DelayedDelivery: have the platform deliver {"kind": "post.send", ...} to
the worker at a future time. This is the *fast path* for one-time
scheduled posts; the tick reconciler is always the safety net, and
BskyScheduler.send_now() makes duplicate or early deliveries no-ops.

    service                      horizon        cancel        delivers to
    --------------------------   ------------   -----------   -----------------------------
    EventBridge Scheduler at()   unlimited      yes           SQS queue / Lambda (target ARN)
    SQS DelaySeconds             15 minutes     no            the queue
    Service Bus scheduled msg    unlimited      (no-op here)  the queue
    Cloud Tasks HTTP task        30 days        yes           HTTPS endpoint (/invoke), OIDC
    none                         -              -             (tick only)

URLs (SKYCTL_DELAYED_URL):
    none
    queue                                        use SKYCTL_QUEUE_URL's own delay support
    eventbridge://<group>?target=<arn>&role=<arn>
    cloudtasks://<project>/<location>/<queue>?url=<https://…/invoke>&sa=<service-account-email>
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse


def _utc(when: datetime) -> datetime:
    return when.astimezone(timezone.utc) if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _safe_name(key: str, limit: int = 64) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", key)[:limit]


class QueueDelayDelivery:
    """Delay via the work queue itself (SQS ≤15 min, Service Bus unlimited, memory)."""

    def __init__(self, queue):
        self.queue = queue
        self.max_horizon_seconds = getattr(queue, "max_delay_seconds", None)
        if not getattr(queue, "supports_delay", False):
            raise ValueError(f"{type(queue).__name__} cannot delay messages.")

    def schedule(self, key: str, when: datetime, body: dict) -> None:
        delay = max(0, int((_utc(when) - datetime.now(timezone.utc)).total_seconds()))
        self.queue.send(body, delay_seconds=delay, dedupe_id=f"{key}:{int(_utc(when).timestamp())}")

    def cancel(self, key: str) -> None:
        """Queued messages cannot be withdrawn; the stray delivery finds no job."""


class EventBridgeSchedulerDelivery:
    """
    One-shot `at()` schedules that delete themselves after firing. The
    target is usually the worker's SQS queue (or a Lambda ARN). Names are
    deterministic per job, so rescheduling replaces and cancel deletes.
    """

    max_horizon_seconds: int | None = None

    def __init__(self, group: str, target_arn: str, role_arn: str, client=None, region: str | None = None):
        self.group = group
        self.target_arn = target_arn
        self.role_arn = role_arn
        self._client = client
        self._region = region

    @property
    def client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("scheduler", region_name=self._region)
        return self._client

    def _name(self, key: str) -> str:
        return f"skyctl-{_safe_name(key)}"

    def schedule(self, key: str, when: datetime, body: dict) -> None:
        params = {
            "Name": self._name(key),
            "GroupName": self.group,
            "ScheduleExpression": f"at({_utc(when):%Y-%m-%dT%H:%M:%S})",
            "ScheduleExpressionTimezone": "UTC",
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "Target": {"Arn": self.target_arn, "RoleArn": self.role_arn, "Input": json.dumps(body)},
            "ActionAfterCompletion": "DELETE",
        }
        try:
            self.client.create_schedule(**params)
        except Exception as e:
            if "Conflict" not in type(e).__name__ and "ConflictException" not in str(e):
                raise
            self.client.update_schedule(**params)

    def cancel(self, key: str) -> None:
        try:
            self.client.delete_schedule(Name=self._name(key), GroupName=self.group)
        except Exception as e:
            if "NotFound" not in type(e).__name__ and "ResourceNotFound" not in str(e):
                raise


class CloudTasksDelivery:
    """
    An HTTP task per job, POSTed to the service's /invoke with an OIDC token
    minted for `service_account` (so /invoke can verify the caller). Task
    names are deterministic, which also deduplicates; note Cloud Tasks keeps
    deleted names reserved for a while, so a re-schedule right after a
    cancel may be refused, and tick covers it.
    """

    max_horizon_seconds = 30 * 24 * 3600

    def __init__(self, project: str, location: str, queue: str, url: str, service_account: str, client=None):
        self.parent = f"projects/{project}/locations/{location}/queues/{queue}"
        self.url = url
        self.service_account = service_account
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from google.cloud import tasks_v2

            self._client = tasks_v2.CloudTasksClient()
        return self._client

    def _name(self, key: str) -> str:
        return f"{self.parent}/tasks/skyctl-{_safe_name(key, 400)}"

    def schedule(self, key: str, when: datetime, body: dict) -> None:
        from google.protobuf import timestamp_pb2

        ts = timestamp_pb2.Timestamp()
        ts.FromDatetime(_utc(when).replace(tzinfo=None))
        task = {
            "name": self._name(key),
            "schedule_time": ts,
            "http_request": {
                "http_method": "POST",
                "url": self.url,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(body).encode("utf-8"),
                "oidc_token": {"service_account_email": self.service_account, "audience": self.url},
            },
        }
        try:
            self.client.create_task(request={"parent": self.parent, "task": task})
        except Exception as e:
            if "AlreadyExists" not in type(e).__name__:
                raise

    def cancel(self, key: str) -> None:
        try:
            self.client.delete_task(request={"name": self._name(key)})
        except Exception as e:
            if "NotFound" not in type(e).__name__:
                raise


def delayed_for(url: str, queue=None):
    """Build a DelayedDelivery from SKYCTL_DELAYED_URL. None for '' / 'none'."""
    url = (url or "").strip()
    if url in ("", "none"):
        return None
    if url == "queue":
        if queue is None:
            raise ValueError("SKYCTL_DELAYED_URL=queue needs SKYCTL_QUEUE_URL.")
        return QueueDelayDelivery(queue)
    parsed = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    if parsed.scheme == "eventbridge":
        return EventBridgeSchedulerDelivery(parsed.netloc, q["target"], q["role"])
    if parsed.scheme == "cloudtasks":
        location, _, queue_name = parsed.path.strip("/").partition("/")
        return CloudTasksDelivery(parsed.netloc, location, queue_name, q["url"], q["sa"])
    raise ValueError(f"Unknown delayed-delivery URL scheme {parsed.scheme!r} in {url!r}")
