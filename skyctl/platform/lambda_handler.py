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
skyctl.platform.lambda_handler
==============================
AWS Lambda entry point for the same container image:

    image_config {
      entry_point = ["python", "-m", "awslambdaric"]
      command     = ["skyctl.platform.lambda_handler.handler"]
    }

Handles SQS batches (with ReportBatchItemFailures, so one bad message does
not redeliver the whole batch), EventBridge Scheduler / rule targets and
function-URL requests to /invoke. Built once per execution environment;
secrets are loaded at init (Lambda has no native secret injection).
"""

from __future__ import annotations

import json
from typing import Any

from .invocation import decode_lambda_event, default_dispatcher

_dispatcher = None


def _get_dispatcher():
    global _dispatcher
    if _dispatcher is None:
        from .runtime import get_runtime

        _dispatcher = default_dispatcher(get_runtime())
    return _dispatcher


def handler(event: Any, context: Any = None, dispatcher=None) -> Any:
    from .base import get_platform

    dispatcher = dispatcher or _get_dispatcher()
    shape, items = decode_lambda_event(event, get_platform("aws"))
    outcomes = [(item_id, dispatcher.dispatch(inv)) for item_id, inv in items]
    for _, o in outcomes:
        print(json.dumps({"event": "invocation", "kind": o.kind, "ok": o.ok, "error": o.error}))

    if shape == "sqs":
        # Retryable failures go back to the queue; permanent ones are dropped
        # (reported in the log) instead of poisoning the batch until the DLQ.
        return {"batchItemFailures": [{"itemIdentifier": i} for i, o in outcomes if not o.ok and o.retry]}
    if shape == "http":
        status = 200 if all(o.ok for _, o in outcomes) else (500 if any(o.retry for _, o in outcomes) else 400)
        return {
            "statusCode": status,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps([o.to_dict() for _, o in outcomes], default=str),
        }
    [(_, only)] = outcomes
    if not only.ok and only.retry:
        raise RuntimeError(only.error)  # async invokes (Scheduler) retry on exceptions
    return only.to_dict()
