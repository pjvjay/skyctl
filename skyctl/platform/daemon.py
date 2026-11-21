"""
skyctl.platform.daemon
======================
The substitution for platforms without scheduled jobs (Compose, a VM,
local): one long-lived process that runs the same Invocations the clouds
would deliver.

    * every schedule from SKYCTL_SCHEDULES_JSON, as cron (any time zone)
    * a queue worker thread, if SKYCTL_QUEUE_URL is set

SKYCTL_SCHEDULES_JSON is rendered by `skyctl deploy render --target compose`:
    [{"name": "tick", "cron": "*/5 * * * *", "timezone": "UTC", "invoke": {"kind": "tick"}}, ...]
"""

from __future__ import annotations

import json
import os
import threading
from typing import Optional

from .invocation import Dispatcher, Invocation, run_worker
from .logging import log_event


def load_schedules(raw: Optional[str] = None) -> list[dict]:
    raw = raw if raw is not None else os.getenv("SKYCTL_SCHEDULES_JSON", "")
    if not raw.strip():
        return [{"name": "tick", "cron": "* * * * *", "timezone": "UTC", "invoke": {"kind": "tick"}}]
    schedules = json.loads(raw)
    for s in schedules:
        if not {"name", "cron", "invoke"} <= set(s):
            raise ValueError(f"Schedule needs name, cron and invoke: {s}")
    return schedules


def build_scheduler(dispatcher: Dispatcher, schedules: list[dict], blocking: bool = True):
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    sched = (BlockingScheduler if blocking else BackgroundScheduler)(timezone="UTC")
    for s in schedules:
        def fire(s=s):
            inv = Invocation.from_payload(s["invoke"], source=f"daemon:{s['name']}")
            outcome = dispatcher.dispatch(inv)
            log_event("schedule.fired", name=s["name"], kind=inv.kind, ok=outcome.ok, error=outcome.error)

        sched.add_job(fire, CronTrigger.from_crontab(s["cron"], timezone=s.get("timezone", "UTC")),
                      id=s["name"], max_instances=1, coalesce=True, misfire_grace_time=300)
    return sched


def run_daemon(dispatcher: Dispatcher, queue=None, schedules: Optional[list[dict]] = None) -> None:
    schedules = schedules if schedules is not None else load_schedules()
    stop = threading.Event()
    if queue is not None:
        threading.Thread(
            target=lambda: run_worker(queue, dispatcher, idle_exit_seconds=None, stop=stop.is_set),
            name="skyctl-worker", daemon=True,
        ).start()
    log_event("daemon.start", schedules=[s["name"] for s in schedules], worker=queue is not None)
    try:
        build_scheduler(dispatcher, schedules).start()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        stop.set()
