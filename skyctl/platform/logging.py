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
skyctl.platform.logging
=======================
Structured logs every platform's log backend understands, with trace
correlation in the platform's own field names:

    gcp     severity, logging.googleapis.com/trace, …/spanId   (Cloud Logging parses these)
    aws     level, xray_trace_id; metrics as Embedded Metric Format (CloudWatch extracts them)
    azure   level, operation_Id, operation_ParentId            (Application Insights correlation)
    other   level, trace_id, span_id

Write once with `log_event(...)` / `emit_metric(...)`; the platform decides
the shape. Trace context is a contextvar set per request/invocation.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time

from .base import Platform, TraceContext

_EMPTY_TRACE = TraceContext()  # frozen dataclass: safe to share
_trace: contextvars.ContextVar[TraceContext] = contextvars.ContextVar("skyctl_trace", default=_EMPTY_TRACE)
_platform: Platform | None = None


def set_platform(platform: Platform) -> None:
    global _platform
    _platform = platform


def set_trace(trace: TraceContext):
    return _trace.set(trace)


def current_trace() -> TraceContext:
    return _trace.get()


def _fields(level: str) -> dict:
    if _platform is None:
        t = current_trace()
        return {"level": level, **({"trace_id": t.trace_id} if t.trace_id else {})}
    return _platform.log_fields(level, current_trace())


def format_event(event: str, level: str = "info", **data) -> str:
    record = {
        **_fields(level),
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "message": event,
    }
    record.update(data)
    return json.dumps(record, default=str, ensure_ascii=False)


def log_event(event: str, level: str = "info", stream=None, **data) -> None:
    print(format_event(event, level, **data), file=stream or sys.stdout, flush=True)


def emit_metric(name: str, value: float, unit: str = "Count", stream=None, **dimensions) -> None:
    """One metric data point. CloudWatch EMF on AWS; a structured log line elsewhere."""
    if _platform is not None and _platform.name == "aws":
        doc = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": "skyctl",
                        "Dimensions": [sorted(dimensions)] if dimensions else [[]],
                        "Metrics": [{"Name": name, "Unit": unit}],
                    }
                ],
            },
            name: value,
            **dimensions,
        }
        print(json.dumps(doc, default=str), file=stream or sys.stdout, flush=True)
        return
    log_event("metric", metric=name, value=value, unit=unit, stream=stream, **dimensions)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname.lower()
        data = {"logger": record.name}
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return format_event(record.getMessage(), level, **data)


def configure_logging(platform: Platform, fmt: str = "auto") -> None:
    """JSON on every non-local platform (or when asked); human text on a terminal."""
    set_platform(platform)
    use_json = fmt == "json" or (fmt == "auto" and (platform.name != "local" or not sys.stderr.isatty()))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if use_json else logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
