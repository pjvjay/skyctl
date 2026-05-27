"""
skyctl.scheduler
================
Schedule posts to go out at a specific time, optionally recurring.
Built on APScheduler with a simple persistent JSON job store.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .manager import BskyManager
from .models import ScheduledPost, PostResult


QUEUE_FILE = Path(".skyctl_queue.json")


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
    """

    def __init__(self, manager: BskyManager, queue_file: Path = QUEUE_FILE):
        self.manager = manager
        self.queue_file = queue_file
        self._queue: dict[str, ScheduledPost] = {}
        self._scheduler = BackgroundScheduler(timezone="UTC")
        self._load_queue()

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

        self._scheduler.add_job(
            func=self._execute,
            trigger=DateTrigger(run_date=run_at),
            id=job_id,
            kwargs={"job_id": job_id},
            misfire_grace_time=300,  # 5 min grace window
        )
        return sp

    def schedule_cron(
        self,
        alias: str,
        text: str,
        hour: int = 9,
        minute: int = 0,
        day_of_week: str = "*",
        job_id: Optional[str] = None,
    ) -> str:
        """
        Schedule a recurring post using a cron expression.

        Args:
            alias:        Account alias.
            text:         Post text (same text each time).
            hour:         UTC hour (0-23).
            minute:       Minute (0-59).
            day_of_week:  Cron day string e.g. "mon,wed,fri" or "*".
            job_id:       Optional stable ID (auto-generated if omitted).

        Returns:
            Job ID string.
        """
        job_id = job_id or f"cron_{alias}_{hour:02d}{minute:02d}"
        self._scheduler.add_job(
            func=self._execute_cron,
            trigger=CronTrigger(hour=hour, minute=minute, day_of_week=day_of_week),
            id=job_id,
            kwargs={"alias": alias, "text": text},
            replace_existing=True,
        )
        return job_id

    def cancel(self, job_id: str) -> bool:
        """Cancel a scheduled job by ID. Returns True if removed."""
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            pass
        if job_id in self._queue:
            del self._queue[job_id]
            self._save_queue()
            return True
        return False

    def list_jobs(self) -> list[dict]:
        """Return a summary of all scheduled jobs."""
        jobs = []
        for job in self._scheduler.get_jobs():
            sp = self._queue.get(job.id)
            jobs.append({
                "id": job.id,
                "next_run": str(job.next_run_time),
                "account": sp.account if sp else "cron",
                "text_preview": (sp.text[:60] + "…") if sp and len(sp.text) > 60 else (sp.text if sp else "—"),
                "status": sp.status if sp else "recurring",
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
            sched = BlockingScheduler(timezone="UTC")
            # Copy jobs over to blocking scheduler
            for job in self._scheduler.get_jobs():
                sched.add_job(
                    func=job.func,
                    trigger=job.trigger,
                    id=job.id,
                    kwargs=job.kwargs,
                    misfire_grace_time=getattr(job, "misfire_grace_time", None),
                )
            try:
                print("Scheduler running. Press Ctrl+C to stop.")
                sched.start()
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

    def _execute(self, job_id: str):
        """Called by APScheduler when a one-shot job fires."""
        sp = self._queue.get(job_id)
        if not sp:
            return

        sp.status = "sending"
        try:
            client = self.manager[sp.account]
            if sp.reply_to_uri:
                result = client.reply(sp.text, parent_uri=sp.reply_to_uri)
            else:
                result = client.post(sp.text, image_paths=sp.image_paths or None)

            sp.result = result
            sp.status = "sent" if result.success else "failed"
        except Exception as e:
            sp.status = "failed"
            sp.result = PostResult(
                account=sp.account, uri="", cid="", url="", error=str(e)
            )
        finally:
            self._save_queue()

    def _execute_cron(self, alias: str, text: str):
        """Called by APScheduler for recurring cron jobs."""
        try:
            self.manager[alias].post(text)
        except Exception as e:
            print(f"[scheduler] Cron job failed for {alias}: {e}")

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
            }
        self.queue_file.write_text(json.dumps(data, indent=2))

    def _load_queue(self):
        if not self.queue_file.exists():
            return
        try:
            data = json.loads(self.queue_file.read_text())
            for job_id, d in data.items():
                self._queue[job_id] = ScheduledPost(
                    id=d["id"],
                    account=d["account"],
                    text=d["text"],
                    run_at=datetime.fromisoformat(d["run_at"]),
                    reply_to_uri=d.get("reply_to_uri"),
                    image_paths=d.get("image_paths", []),
                    status=d.get("status", "pending"),
                )
        except Exception as e:
            print(f"[scheduler] Warning: could not load queue file: {e}")
