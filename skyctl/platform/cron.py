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
skyctl.platform.cron
====================
One schedule syntax in the deployment spec (5-field Unix cron + an IANA
time zone), translated per target. The dialects really differ:

    target                         syntax                       time zone
    ----------------------------   --------------------------   --------------------------
    EventBridge Scheduler          cron(m h dom mon dow year)   ScheduleExpressionTimezone
                                   one of dom/dow must be '?';
                                   dow is 1-7 with 1 = SUN
    Cloud Scheduler                unix cron                    time_zone
    Container Apps Jobs            unix cron                    UTC only
    Kubernetes CronJob             unix cron                    spec.timeZone (GA in 1.27)
    APScheduler daemon (compose)   unix cron                    any
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_DOW_NAMES = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]


class CronError(ValueError):
    pass


@dataclass(frozen=True)
class CronSpec:
    expr: str
    timezone: str = "UTC"

    def fields(self) -> list[str]:
        parts = self.expr.split()
        if len(parts) != 5:
            raise CronError(f"Expected 5 cron fields (m h dom mon dow), got {len(parts)}: {self.expr!r}")
        return parts

    @property
    def is_utc(self) -> bool:
        return self.timezone.upper() in ("UTC", "ETC/UTC", "Z", "GMT", "ETC/GMT")

    def min_interval_seconds(self) -> int:
        minute = self.fields()[0]
        if minute == "*":
            return 60
        m = re.fullmatch(r"(?:\*|0)/(\d+)", minute)
        return int(m.group(1)) * 60 if m else 3600


def _dow_to_eventbridge(token: str) -> str:
    """Unix 0-7 (0/7 = SUN) → EventBridge 1-7 (1 = SUN); names pass through."""

    def one(v: str) -> str:
        if v.isdigit():
            n = int(v) % 7
            return str(n + 1)
        if v.upper()[:3] in _DOW_NAMES:
            return v.upper()
        raise CronError(f"Bad day-of-week value {v!r}")

    out = []
    for part in token.split(","):
        step = ""
        if "/" in part:
            part, step = part.split("/", 1)
            step = "/" + step
        if part == "*":
            out.append("*" + step)
        elif "-" in part:
            a, b = part.split("-", 1)
            out.append(f"{one(a)}-{one(b)}{step}")
        else:
            out.append(one(part) + step)
    return ",".join(out)


def _step_base(token: str, base: str) -> str:
    return re.sub(r"^\*/", f"{base}/", token)


def to_eventbridge(spec: CronSpec) -> str:
    m, h, dom, mon, dow = spec.fields()
    if dom != "*" and dow != "*":
        raise CronError(
            f"{spec.expr!r} restricts both day-of-month and day-of-week (Unix ORs them); "
            "EventBridge cannot express that. Split it into two schedules."
        )
    if dow != "*":
        dom, dow = "?", _dow_to_eventbridge(dow)
    else:
        dow = "?"
    return f"cron({_step_base(m, '0')} {_step_base(h, '0')} {_step_base(dom, '1')} {mon.upper()} {dow} *)"


def previous_fire(cron: str, timezone_name: str = "UTC", now=None):
    """
    The most recent fire time of `cron` at or before `now`: the "slot" a
    scheduled run belongs to. Retries of the same run land in the same slot
    on every platform, which makes it a portable idempotency key (no need
    for EventBridge's scheduled-time or any other platform-specific context).
    """
    from datetime import datetime, timedelta
    from datetime import timezone as tz

    from apscheduler.triggers.cron import CronTrigger

    now = now or datetime.now(tz.utc)
    trigger = CronTrigger.from_crontab(cron, timezone=timezone_name)
    for lookback in (timedelta(hours=1), timedelta(days=1), timedelta(days=8), timedelta(days=32), timedelta(days=370)):
        fire = trigger.get_next_fire_time(None, now - lookback)
        if fire is None or fire > now:
            continue
        while True:  # walk forward to the last fire <= now
            nxt = trigger.get_next_fire_time(fire, fire)
            if nxt is None or nxt > now:
                return fire
            fire = nxt
    return None
