"""
skyctl.scheduler
================
Schedule posts to go out at a specific time, optionally recurring.
Built on APScheduler with a simple persistent JSON job store.

Every job (one-time and recurring) is saved to the queue file, so jobs
created by one process (e.g. `skyctl schedule`) are picked up by another
(e.g. `skyctl run-scheduler`). Failed sends are retried with backoff.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .manager import BskyManager
from .models import ScheduledPost, PostResult
from .richtext import validate_length


QUEUE_FILE = Path(".skyctl_queue.json")
ACTIVE_STATUSES = ("pending", "retrying", "recurring", "sending")
SYNC_JOB_ID = "__skyctl_sync__"
SYNC_SECONDS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BskyScheduler:
    """
    Schedule posts across any managed account.

    Usage:
        manager = BskyManager.from_env()
        scheduler = BskyScheduler(manager)

        # One-shot post at a specific time
        scheduler.schedule(
            alias="personal",
            text="Good morning Bluesky!",
            run_at=datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc),
        )

        # Recurring daily post
        scheduler.schedule_cron(
            alias="work",
            text="Daily update!",
            hour=9, minute=0,
        )

        scheduler.start()   # blocks until Ctrl+C

    Args:
        max_retries: How many times a failed send is retried.
        retry_delay: Seconds before the first retry; doubles each attempt.
    """

    def __init__(
        self,
        manager: BskyManager,
        queue_file: Path = QUEUE_FILE,
        max_retries: int = 3,
        retry_delay: float = 60,
    ):
        self.manager = manager
        self.queue_file = Path(queue_file)
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._queue: dict[str, ScheduledPost] = {}
        self._scheduler = BackgroundScheduler(timezone="UTC")
        self._load_queue()
        for sp in self._queue.values():
            self._register(sp)

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def schedule(
        self,
        alias: str,
        text: str,
        run_at: datetime,
        reply_to_uri: Optional[str] = None,
        image_paths: Optional[list[str]] = None,
    ) -> ScheduledPost:
        """
        Schedule a one-time post.

        Args:
            alias:        Account alias to post from.
            text:         Post text.
            run_at:       UTC datetime to send the post.
            reply_to_uri: If set, sends as a reply to this AT URI.
            image_paths:  Optional list of local image paths.

        Returns:
            ScheduledPost object with a unique ID.
        """
        validate_length(text)
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        self._sync_from_disk()
        job_id = str(uuid.uuid4())[:8]
        sp = ScheduledPost(
            id=job_id,
            account=alias,
            text=text,
            run_at=run_at,
            reply_to_uri=reply_to_uri,
            image_paths=image_paths or [],
        )
        self._queue[job_id] = sp
        self._save_queue()
        self._register(sp)
        return sp

    def schedule_cron(
        self,
        alias: str,
        text: str,
        hour: int = 9,
        minute: int = 0,
        day_of_week: str = "*",
        job_id: Optional[str] = None,
        cron: Optional[str] = None,
    ) -> str:
        """
        Schedule a recurring post.

        Args:
            alias:        Account alias.
            text:         Post text (same text each time).
            hour:         UTC hour (0-23).
            minute:       Minute (0-59).
            day_of_week:  Day string e.g. "mon,wed,fri" or "*".
            job_id:       Optional stable ID (auto-generated if omitted).
            cron:         A 5-field crontab expression in UTC, e.g.
                          "0 9 * * mon-fri". Overrides hour/minute/day_of_week.
                          Use day names rather than numbers for the weekday.

        Returns:
            Job ID string.
        """
        validate_length(text)
        expr = cron or f"{minute} {hour} * * {day_of_week}"
        CronTrigger.from_crontab(expr, timezone="UTC")  # raises ValueError if invalid
        self._sync_from_disk()
        if job_id is None:
            job_id = (
                f"cron_{str(uuid.uuid4())[:8]}"
                if cron
                else f"cron_{alias}_{hour:02d}{minute:02d}"
            )
        sp = ScheduledPost(
            id=job_id,
            account=alias,
            text=text,
            run_at=_now(),
            status="recurring",
            cron=expr,
        )
        self._queue[job_id] = sp
        self._save_queue()
        self._register(sp)
        return job_id

    def cancel(self, job_id: str) -> bool:
        """Cancel a scheduled job by ID. Returns True if removed."""
        self._sync_from_disk()
        self._unregister(job_id)
        if job_id in self._queue:
            del self._queue[job_id]
            self._save_queue()
            return True
        return False

    def list_jobs(self) -> list[dict]:
        """Return a summary of all scheduled jobs."""
        self._sync_from_disk()
        jobs = []
        for sp in self._queue.values():
            preview = sp.text[:60] + "…" if len(sp.text) > 60 else sp.text
            jobs.append({
                "id": sp.id,
                "next_run": self._next_run(sp),
                "account": sp.account,
                "text_preview": preview,
                "status": sp.status,
                "schedule": sp.cron or sp.run_at.isoformat(),
                "attempts": sp.attempts,
                "last_error": sp.last_error,
            })
        return jobs

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start(self, blocking: bool = True):
        """
        Start the scheduler.

        Args:
            blocking: If True (default), runs until Ctrl+C.
                      If False, runs in background thread.
        """
        if blocking:
            self._scheduler = BlockingScheduler(timezone="UTC")
            for sp in self._queue.values():
                self._register(sp)
        self._scheduler.add_job(
            func=self._sync_from_disk,
            trigger="interval",
            seconds=SYNC_SECONDS,
            id=SYNC_JOB_ID,
            replace_existing=True,
        )
        if blocking:
            try:
                print("Scheduler running. Press Ctrl+C to stop.")
                self._scheduler.start()
            except (KeyboardInterrupt, SystemExit):
                print("\nScheduler stopped.")
        else:
            self._scheduler.start()

    def stop(self):
        """Stop the background scheduler."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _trigger_for(self, sp: ScheduledPost):
        if sp.cron:
            return CronTrigger.from_crontab(sp.cron, timezone="UTC")
        run_at = sp.run_at
        if sp.status == "retrying":
            # A retry that was pending when the process stopped runs promptly.
            run_at = max(run_at, _now() + timedelta(seconds=5))
        return DateTrigger(run_date=run_at)

    def _register(self, sp: ScheduledPost):
        """Add a queued post to APScheduler if it still needs to run."""
        if sp.status not in ACTIVE_STATUSES:
            return
        try:
            self._add_job(sp.id, sp.id, self._trigger_for(sp))
        except Exception as e:
            print(f"[scheduler] Warning: could not register job {sp.id}: {e}")

    def _add_job(self, scheduler_id: str, job_id: str, trigger):
        """Add a job, replacing any existing one with the same ID."""
        # replace_existing alone is not applied until the scheduler starts,
        # so drop the old job explicitly first.
        try:
            self._scheduler.remove_job(scheduler_id)
        except Exception:
            pass
        self._scheduler.add_job(
            func=self._execute,
            trigger=trigger,
            id=scheduler_id,
            kwargs={"job_id": job_id},
            misfire_grace_time=300,  # 5 min grace window
            replace_existing=True,
        )

    def _unregister(self, job_id: str):
        for jid in (job_id, f"{job_id}:retry"):
            try:
                self._scheduler.remove_job(jid)
            except Exception:
                pass

    def _next_run(self, sp: ScheduledPost) -> str:
        if sp.status not in ACTIVE_STATUSES:
            return "—"
        job = self._scheduler.get_job(f"{sp.id}:retry") or self._scheduler.get_job(sp.id)
        if job is None:
            return "—"
        nxt = job.trigger.get_next_fire_time(None, _now())
        return nxt.isoformat() if nxt else "missed"

    def _send(self, sp: ScheduledPost) -> PostResult:
        try:
            client = self.manager.client(sp.account)
            if sp.reply_to_uri:
                return client.reply(sp.text, parent_uri=sp.reply_to_uri)
            return client.post(sp.text, image_paths=sp.image_paths or None)
        except Exception as e:
            return PostResult(
                account=sp.account, uri="", cid="", url="", error=str(e)
            )

    def _execute(self, job_id: str):
        """Called by APScheduler when a job (or one of its retries) fires."""
        self._sync_from_disk()
        sp = self._queue.get(job_id)
        if not sp:
            return

        result = self._send(sp)
        sp.result = result

        if result.success:
            sp.attempts = 0
            sp.last_error = None
            sp.status = "recurring" if sp.cron else "sent"
        else:
            sp.attempts += 1
            sp.last_error = result.error
            if sp.attempts <= self.max_retries:
                delay = self.retry_delay * (2 ** (sp.attempts - 1))
                if not sp.cron:
                    sp.status = "retrying"
                self._add_job(
                    f"{job_id}:retry",
                    job_id,
                    DateTrigger(run_date=_now() + timedelta(seconds=delay)),
                )
                print(
                    f"[scheduler] Job {job_id} failed (attempt {sp.attempts}): "
                    f"{result.error}. Retrying in {delay:.0f}s."
                )
            else:
                # Out of retries. Recurring jobs wait for their next slot.
                print(f"[scheduler] Job {job_id} failed after {sp.attempts} attempts: {result.error}")
                sp.attempts = 0
                sp.status = "recurring" if sp.cron else "failed"
        self._save_queue()

    # ------------------------------------------------------------------
    # Queue persistence
    # ------------------------------------------------------------------

    def _save_queue(self):
        data = {}
        for job_id, sp in self._queue.items():
            data[job_id] = {
                "id": sp.id,
                "account": sp.account,
                "text": sp.text,
                "run_at": sp.run_at.isoformat(),
                "reply_to_uri": sp.reply_to_uri,
                "image_paths": sp.image_paths,
                "status": sp.status,
                "cron": sp.cron,
                "attempts": sp.attempts,
                "last_error": sp.last_error,
            }
        self.queue_file.write_text(json.dumps(data, indent=2))

    def _read_queue_file(self) -> Optional[dict[str, ScheduledPost]]:
        if not self.queue_file.exists():
            return {}
        try:
            data = json.loads(self.queue_file.read_text())
            return {
                job_id: ScheduledPost(
                    id=d["id"],
                    account=d["account"],
                    text=d["text"],
                    run_at=datetime.fromisoformat(d["run_at"]),
                    reply_to_uri=d.get("reply_to_uri"),
                    image_paths=d.get("image_paths", []),
                    status=d.get("status", "pending"),
                    cron=d.get("cron"),
                    attempts=d.get("attempts", 0),
                    last_error=d.get("last_error"),
                )
                for job_id, d in data.items()
            }
        except Exception as e:
            print(f"[scheduler] Warning: could not load queue file: {e}")
            return None

    def _load_queue(self):
        self._queue = self._read_queue_file() or {}

    def _sync_from_disk(self):
        """
        Pick up jobs another process added to, or cancelled from, the queue
        file, so a running scheduler and the CLI do not overwrite each other.
        """
        on_disk = self._read_queue_file()
        if on_disk is None:
            return
        for job_id, sp in on_disk.items():
            if job_id not in self._queue:
                self._queue[job_id] = sp
                self._register(sp)
        for job_id in list(self._queue):
            if job_id not in on_disk:
                del self._queue[job_id]
                self._unregister(job_id)
