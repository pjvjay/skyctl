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
skyctl.platform.invocation
==========================
The invocation plane: every way a platform can call skyctl is decoded into
one `Invocation` and handled by one `Dispatcher`.

    trigger                                   arrives as                          decoder
    ---------------------------------------   ---------------------------------   --------------------
    container job (ECS RunTask, ACA Job,      `skyctl invoke '<json>'`            decode_json
      Cloud Run Job, k8s CronJob)
    Cloud Tasks / EventBridge API destination POST /invoke, plain JSON body        decode_http
    Pub/Sub push subscription                 POST /invoke, {"message": {...}}    decode_http
    Eventarc / Event Grid (CloudEvents)       POST /invoke, binary or structured  decode_http
    Event Grid (its own schema)               POST /invoke, [{"eventType": ...}]  decode_http
    Lambda: SQS event source mapping          {"Records": [...]}                  decode_lambda_event
    Lambda: EventBridge Scheduler / rule      the target Input / {"detail": …}    decode_lambda_event
    Lambda: function URL                      {"requestContext": …, "body": …}    decode_lambda_event
    WorkQueue consumer (`skyctl worker`)      QueueMessage.body                   decode_json

Payload kinds (the canonical vocabulary):

    {"kind": "tick"}
    {"kind": "post.send", "job_id": "..."}
    {"kind": "task.run", "task": "<name>" | {TaskSpec fields}, "run_id": "...", "overrides": {...},
     "slot": {"cron": "0 9 * * mon-fri", "timezone": "UTC"}}      # added to scheduled runs
    {"kind": "task.approve", "run_id": "...", "call_ids": [...]}
    {"kind": "plan.execute", "plan_id": "...", "plan_hash": "sha256:..."}   # an already-approved plan

Delivery is at-least-once on every platform, so every handler is
idempotent (claim protocol for posts, write ledger for agent runs).
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .base import TraceContext, _header


class InvocationError(ValueError):
    """The event could not be decoded into an Invocation."""


@dataclass
class Invocation:
    kind: str
    payload: dict
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    source: str = "direct"
    attempt: int = 1
    trace: TraceContext = field(default_factory=TraceContext)

    @classmethod
    def from_payload(cls, payload: Any, **kw) -> Invocation:
        if not isinstance(payload, dict) or not isinstance(payload.get("kind"), str):
            raise InvocationError(f"Payload must be a JSON object with a 'kind': {str(payload)[:200]}")
        return cls(kind=payload["kind"], payload=payload, **kw)


# ----------------------------------------------------------------------
# Decoders
# ----------------------------------------------------------------------


def decode_json(body: Any, source: str = "direct", attempt: int = 1, msg_id: str | None = None) -> Invocation:
    if isinstance(body, (bytes, str)):
        try:
            body = json.loads(body)
        except ValueError as e:
            raise InvocationError(f"Invocation body is not JSON: {e}") from e
    kw = {"source": source, "attempt": attempt}
    if msg_id:
        kw["id"] = msg_id
    return Invocation.from_payload(body, **kw)


def _b64json(data: str) -> Any:
    try:
        return json.loads(base64.b64decode(data))
    except Exception as e:
        raise InvocationError(f"Could not decode base64 JSON data: {e}") from e


def _pubsub_envelope(envelope: dict, source: str) -> Invocation:
    msg = envelope.get("message") or {}
    attempt = int(envelope.get("deliveryAttempt") or 1)
    return decode_json(_b64json(msg.get("data", "")), source=source, attempt=attempt, msg_id=msg.get("messageId"))


def decode_http(headers: Mapping[str, str], body: bytes, platform=None) -> list[Invocation]:
    """Decode a POST /invoke request into one or more Invocations."""
    trace = platform.trace_from_headers(headers) if platform is not None else TraceContext()
    ctype = _header(headers, "content-type").lower()

    def done(invs: list[Invocation]) -> list[Invocation]:
        for inv in invs:
            inv.trace = trace
        return invs

    # CloudEvents, binary content mode: attributes in ce-* headers, data in the body.
    if _header(headers, "ce-specversion"):
        ce_type = _header(headers, "ce-type")
        data = json.loads(body or b"null")
        if ce_type == "google.cloud.pubsub.topic.v1.messagePublished":
            return done([_pubsub_envelope(data, "eventarc-pubsub")])
        return done([decode_json(data, source=f"cloudevents:{ce_type}", msg_id=_header(headers, "ce-id") or None)])

    try:
        doc = json.loads(body or b"null")
    except ValueError as e:
        raise InvocationError(f"Body is not JSON: {e}") from e

    # CloudEvents, structured content mode.
    if "application/cloudevents" in ctype or (isinstance(doc, dict) and "specversion" in doc and "type" in doc):
        events = doc if isinstance(doc, list) else [doc]  # batch mode is a JSON array
        out = []
        for ev in events:
            data = ev.get("data")
            if data is None and "data_base64" in ev:
                data = _b64json(ev["data_base64"])
            if ev.get("type") == "google.cloud.pubsub.topic.v1.messagePublished":
                out.append(_pubsub_envelope(data, "eventarc-pubsub"))
            else:
                out.append(decode_json(data, source=f"cloudevents:{ev.get('type')}", msg_id=ev.get("id")))
        return done(out)

    # Pub/Sub push subscription envelope.
    if isinstance(doc, dict) and isinstance(doc.get("message"), dict) and "subscription" in doc:
        return done([_pubsub_envelope(doc, "pubsub-push")])

    # Azure Event Grid schema: a JSON array of events.
    if isinstance(doc, list) and doc and isinstance(doc[0], dict) and "eventType" in doc[0]:
        out = []
        for ev in doc:
            if ev["eventType"] == "Microsoft.EventGrid.SubscriptionValidationEvent":
                out.append(Invocation("eventgrid.validate", ev.get("data") or {}, source="eventgrid"))
            else:
                out.append(decode_json(ev.get("data"), source=f"eventgrid:{ev['eventType']}", msg_id=ev.get("id")))
        return done(out)

    # Plain JSON: Cloud Tasks, EventBridge API destinations, curl.
    return done([decode_json(doc, source="http")])


def decode_lambda_event(event: Any, platform=None) -> tuple[str, list[tuple[str | None, Invocation]]]:
    """
    Returns (shape, [(item_id, invocation)]). shape is "sqs" (partial batch
    failures apply), "http" (function URL / API Gateway) or "direct".
    """
    if isinstance(event, dict) and isinstance(event.get("Records"), list):
        items = []
        for rec in event["Records"]:
            if rec.get("eventSource") != "aws:sqs":
                raise InvocationError(f"Unsupported record source {rec.get('eventSource')!r}")
            attempt = int((rec.get("attributes") or {}).get("ApproximateReceiveCount", 1))
            items.append(
                (rec["messageId"], decode_json(rec["body"], source="sqs", attempt=attempt, msg_id=rec["messageId"]))
            )
        return "sqs", items
    if isinstance(event, dict) and "requestContext" in event and "body" in event:
        body = event.get("body") or ""
        raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode("utf-8")
        return "http", [(None, inv) for inv in decode_http(event.get("headers") or {}, raw, platform)]
    if isinstance(event, dict) and "detail-type" in event and "detail" in event:
        return "direct", [(None, decode_json(event["detail"], source=f"eventbridge:{event.get('source')}"))]
    return "direct", [(None, decode_json(event, source="lambda"))]


# ----------------------------------------------------------------------
# Dispatch
# ----------------------------------------------------------------------


class RetryableError(RuntimeError):
    """The handler failed transiently; the platform should redeliver."""


@dataclass
class Outcome:
    ok: bool
    kind: str
    result: Any = None
    error: str | None = None
    retry: bool = False

    def to_dict(self) -> dict:
        return {"ok": self.ok, "kind": self.kind, "result": self.result, "error": self.error, "retry": self.retry}


Handler = Callable[[Invocation], Any]


class Dispatcher:
    def __init__(self, handlers: dict[str, Handler] | None = None):
        self.handlers: dict[str, Handler] = dict(handlers or {})

    def register(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler

    def dispatch(self, inv: Invocation) -> Outcome:
        handler = self.handlers.get(inv.kind)
        if handler is None:
            # Unknown kinds are permanent failures: retrying will not help.
            return Outcome(
                False, inv.kind, error=f"No handler for kind {inv.kind!r}. Known: {', '.join(sorted(self.handlers))}"
            )
        try:
            return Outcome(True, inv.kind, result=handler(inv))
        except RetryableError as e:
            return Outcome(False, inv.kind, error=str(e), retry=True)
        except (InvocationError, ValueError, PermissionError, KeyError) as e:
            return Outcome(False, inv.kind, error=f"{type(e).__name__}: {e}")
        except Exception as e:  # unexpected: let the platform retry (bounded by its DLQ policy)
            return Outcome(False, inv.kind, error=f"{type(e).__name__}: {e}", retry=True)


def deployment_policy(settings):
    """The ToolPolicy implied by a deployment's ceilings (SKYCTL_AGENT_*)."""
    from ..tools import ToolPolicy

    d = settings.agent
    return ToolPolicy(
        accounts=d.accounts, allow_writes=d.allow_writes, allow_delete=d.allow_delete, max_writes=max(d.max_writes, 1)
    )


def default_dispatcher(runtime=None, manager=None, scheduler=None, runner=None) -> Dispatcher:
    """The standard handlers, built lazily from the Runtime."""
    state: dict[str, Any] = {"manager": manager, "scheduler": scheduler, "runner": runner, "runtime": runtime}

    def rt():
        if state["runtime"] is None:
            from .runtime import get_runtime

            state["runtime"] = get_runtime()
        return state["runtime"]

    def mgr():
        if state["manager"] is None:
            from ..manager import BskyManager

            state["manager"] = BskyManager.from_settings(rt().settings)
        return state["manager"]

    def sched():
        if state["scheduler"] is None:
            from ..scheduler import BskyScheduler

            state["scheduler"] = BskyScheduler.from_settings(mgr(), rt().settings)
        return state["scheduler"]

    def run():
        if state["runner"] is None:
            from ..agent import AgentRunner

            state["runner"] = AgentRunner.from_settings(mgr(), rt().settings)
        return state["runner"]

    def tick(inv: Invocation):
        return sched().tick().to_dict()

    def post_send(inv: Invocation):
        job_id = inv.payload.get("job_id")
        if not job_id:
            raise InvocationError("post.send needs job_id")
        result = sched().send_now(job_id)
        return {
            "job_id": job_id,
            "sent": bool(result and result.success),
            "noop": result is None,
            "error": result.error if result is not None else None,
        }

    def task_run(inv: Invocation):
        from ..agent import TaskSpec, apply_deployment_ceilings, load_tasks

        spec = inv.payload.get("task")
        if isinstance(spec, str):
            task = load_tasks(state=run().state).get(spec.lower())
            if task is None:
                raise InvocationError(f"Unknown task {spec!r}")
        elif isinstance(spec, dict):
            task = TaskSpec.from_dict(spec)
        else:
            raise InvocationError("task.run needs 'task': a name or a TaskSpec object")
        for key, value in (inv.payload.get("overrides") or {}).items():
            if key not in TaskSpec.__dataclass_fields__:
                raise InvocationError(f"Unknown override {key!r}")
            setattr(task, key, value)
        if task.idempotency_key is None:
            # Retries of the same logical run must not repeat writes:
            #   scheduled run → task @ cron slot (same across platform retries and overlaps)
            #   queued run    → its run_id;  anything else → the delivery id
            slot = inv.payload.get("slot")
            if isinstance(slot, dict) and slot.get("cron"):
                from .cron import previous_fire

                fired = previous_fire(slot["cron"], slot.get("timezone", "UTC"))
                task.idempotency_key = f"{task.name or 'task'}@{fired.isoformat() if fired else 'unscheduled'}"
            else:
                task.idempotency_key = inv.payload.get("run_id") or inv.id
        apply_deployment_ceilings(task, rt().settings.agent)
        return run().run(task, run_id=inv.payload.get("run_id")).to_dict()

    def task_approve(inv: Invocation):
        return run().approve(inv.payload["run_id"], inv.payload.get("call_ids"))

    def plan_execute(inv: Invocation):
        """Execute an approved plan with the deployment's identity (e.g. after `skyctl run-cloud-task` approval)."""
        from ..planning import ApprovalRequired, PlanError, PlanStore, execute_plan
        from ..tools import ToolContext, registry
        from ..tools.plan_tools import _detail

        plan_id, plan_hash = inv.payload.get("plan_id"), inv.payload.get("plan_hash")
        if not plan_id or not plan_hash:
            raise InvocationError("plan.execute needs plan_id and plan_hash")
        ctx = ToolContext(mgr(), policy=deployment_policy(rt().settings), scheduler=sched())
        ctx.plans = PlanStore(rt().object_store())
        ctx.principal = f"worker:{inv.source}"
        try:
            return _detail(execute_plan(ctx.plans, plan_id, plan_hash, ctx, registry))
        except ApprovalRequired as e:
            raise PermissionError(str(e)) from None  # permanent: nobody approved it
        except PlanError as e:
            if "already executing" in str(e):
                raise RetryableError(str(e)) from None  # a duplicate delivery; try again later
            raise ValueError(str(e)) from None

    def eventgrid_validate(inv: Invocation):
        return {"validationResponse": inv.payload.get("validationCode")}

    return Dispatcher(
        {
            "tick": tick,
            "post.send": post_send,
            "task.run": task_run,
            "task.approve": task_approve,
            "plan.execute": plan_execute,
            "eventgrid.validate": eventgrid_validate,
        }
    )


# ----------------------------------------------------------------------
# Queue worker
# ----------------------------------------------------------------------


@dataclass
class WorkerStats:
    processed: int = 0
    succeeded: int = 0
    retried: int = 0
    dropped: int = 0

    def to_dict(self) -> dict:
        return vars(self).copy()


class _Heartbeat:
    """Extends a message's visibility in the background while it is being handled."""

    def __init__(self, queue, message, visibility_seconds: int, every: float | None):
        import threading

        self.queue, self.message = queue, message
        self.visibility, self.every = visibility_seconds, every
        self._stop = threading.Event()
        self._thread = None
        self.beats = 0

    def _run(self):
        while not self._stop.wait(self.every):
            try:
                self.queue.extend(self.message, self.visibility)
                self.beats += 1
            except Exception as e:
                print(json.dumps({"event": "heartbeat.failed", "message_id": self.message.id, "error": str(e)}))

    def __enter__(self):
        import threading

        if self.every and hasattr(self.queue, "extend"):
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        return False


def run_worker(
    queue,
    dispatcher: Dispatcher,
    *,
    max_messages: int = 1,
    visibility_seconds: int = 900,
    idle_exit_seconds: float | None = 30,
    max_attempts: int = 5,
    retry_base_seconds: int = 30,
    stop: Callable[[], bool] | None = None,
    wait_seconds: int = 10,
    heartbeat_seconds: float | None = 60,
) -> WorkerStats:
    """
    Drain a WorkQueue. Exits after `idle_exit_seconds` with nothing to do
    (the shape event-driven jobs want: ACA event jobs, KEDA ScaledJobs,
    scale-to-zero ECS services); pass None to run forever.

    success → ack; retryable failure → nack with exponential backoff;
    permanent failure or too many attempts → dead_letter() where the adapter
    has one (Storage Queues: "<queue>-poison"), else ack and log (SQS,
    Service Bus and Pub/Sub dead-letter policies are the second line).

    While a handler runs, a heartbeat extends the message's visibility every
    `heartbeat_seconds`, so a 20-minute agent run is not redelivered to a
    second worker when a Service Bus lock (5 min) or Pub/Sub ack deadline
    (10 min) would otherwise expire.
    """
    stats = WorkerStats()
    idle_since = time.monotonic()
    while not (stop and stop()):
        messages = queue.receive(
            max_messages=max_messages, visibility_seconds=visibility_seconds, wait_seconds=wait_seconds
        )
        if not messages:
            if idle_exit_seconds is not None and time.monotonic() - idle_since >= idle_exit_seconds:
                break
            continue
        idle_since = time.monotonic()
        for msg in messages:
            stats.processed += 1
            try:
                inv = decode_json(msg.body, source="queue", attempt=msg.attempt, msg_id=msg.id)
                with _Heartbeat(queue, msg, visibility_seconds, heartbeat_seconds):
                    outcome = dispatcher.dispatch(inv)
            except InvocationError as e:
                outcome = Outcome(False, "?", error=str(e))
            if outcome.ok:
                stats.succeeded += 1
                queue.ack(msg)
            elif outcome.retry and msg.attempt < max_attempts:
                stats.retried += 1
                queue.nack(msg, delay_seconds=min(retry_base_seconds * 2 ** (msg.attempt - 1), 900))
            else:
                stats.dropped += 1
                print(
                    json.dumps(
                        {
                            "event": "invocation.dropped",
                            "kind": outcome.kind,
                            "error": outcome.error,
                            "attempt": msg.attempt,
                            "message_id": msg.id,
                        }
                    )
                )
                if hasattr(queue, "dead_letter"):
                    queue.dead_letter(msg, outcome.error or "")  # services without a native DLQ
                else:
                    queue.ack(msg)
    return stats
