"""
skyctl.scheduler
================
Schedule posts to go out at a specific time, optionally recurring.
Built on APScheduler with a simple persistent JSON job store.

Every job (one-time and recurring) is saved to a JobStore (by default the
JSON queue file), so jobs created by one process (e.g. `skyctl schedule`)
are picked up by another (e.g. `skyctl run-scheduler`). Failed sends are
retried with backoff.

Two ways to run:

* Daemon: `start()` runs APScheduler in-process (VMs, docker-compose).
* Tick:   `tick()` does one pass — send everything that is due, record
          retries, exit — for cron and scheduled cloud jobs (EventBridge
          Scheduler, Container Apps Jobs, Cloud Scheduler).
"""

from __future__ import annotations

import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .adapters.local import FileJobStore
from .manager import BskyManager
from .models import PostResult, ScheduledPost
from .richtext import validate_length

QUEUE_FILE = Path(".skyctl_queue.json")
ACTIVE_STATUSES = ("pending", "retrying", "recurring", "sending")
SYNC_JOB_ID = "__skyctl_sync__"
SYNC_SECONDS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class TickResult:
    """What one scheduler pass did."""

    at: datetime
    sent: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: str | None = None  # reason the tick did nothing at all

    def to_dict(self) -> dict:
        return {
            "at": self.at.isoformat(),
            "sent": self.sent,
            "failed": self.failed,
            "skipped": self.skipped,
        }


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
        job_store=None,
        clock: Callable[[], datetime] = _now,
        delayed=None,
        claim_seconds: int = 300,
    ):
        """
        Args:
            queue_file: Legacy JSON queue path; wrapped in a FileJobStore.
            job_store:  Any JobStore (see skyctl.ports); overrides queue_file.
            clock:      Returns the current UTC time; injectable for tests.
            delayed:    Optional DelayedDelivery: the native fast path that
                        delivers a `post.send` at run_at (EventBridge Scheduler,
                        Service Bus scheduled messages, Cloud Tasks). tick()
                        stays the safety net either way.
            claim_seconds: How long a worker may hold a job in "sending"
                        before another may recover it.
        """
        self.manager = manager
        self.queue_file = Path(queue_file)
        self.job_store = job_store or FileJobStore(self.queue_file)
        self.delayed = delayed
        self.claim_seconds = claim_seconds
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._clock = clock
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
        reply_to_uri: str | None = None,
        image_paths: list[str] | None = None,
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
        job_id = str(uuid.uuid4())[:8]
        sp = ScheduledPost(
            id=job_id,
            account=alias,
            text=text,
            run_at=run_at,
            reply_to_uri=reply_to_uri,
            image_paths=image_paths or [],
        )
        self.job_store.update(lambda jobs: jobs.__setitem__(job_id, sp))
        self._queue[job_id] = sp  # the returned object stays live in this process
        self._sync_from_disk()
        self._register(sp)
        self._notify_scheduled(sp)
        return sp

    def schedule_cron(
        self,
        alias: str,
        text: str,
        hour: int = 9,
        minute: int = 0,
        day_of_week: str = "*",
        job_id: str | None = None,
        cron: str | None = None,
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
        if job_id is None:
            job_id = f"cron_{str(uuid.uuid4())[:8]}" if cron else f"cron_{alias}_{hour:02d}{minute:02d}"
        sp = ScheduledPost(
            id=job_id,
            account=alias,
            text=text,
            run_at=self._clock(),
            status="recurring",
            cron=expr,
        )
        self.job_store.update(lambda jobs: jobs.__setitem__(job_id, sp))
        self._queue[job_id] = sp
        self._sync_from_disk()
        self._register(sp)
        return job_id

    def cancel(self, job_id: str) -> bool:
        """Cancel a scheduled job by ID. Returns True if removed."""
        self._unregister(job_id)
        removed = self.job_store.update(lambda jobs: jobs.pop(job_id, None) is not None)
        self._queue.pop(job_id, None)
        if removed and self.delayed is not None:
            try:
                self.delayed.cancel(job_id)
            except Exception:
                pass  # the fast path is best-effort; a stray delivery finds no job
        return bool(removed)

    def list_jobs(self) -> list[dict]:
        """Return a summary of all scheduled jobs."""
        self._sync_from_disk()
        jobs = []
        for sp in self._queue.values():
            preview = sp.text[:60] + "…" if len(sp.text) > 60 else sp.text
            jobs.append(
                {
                    "id": sp.id,
                    "next_run": self._next_run(sp),
                    "account": sp.account,
                    "text_preview": preview,
                    "status": sp.status,
                    "schedule": sp.cron or sp.run_at.isoformat(),
                    "attempts": sp.attempts,
                    "last_error": sp.last_error,
                }
            )
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
            run_at = max(run_at, self._clock() + timedelta(seconds=5))
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
        nxt = job.trigger.get_next_fire_time(None, self._clock())
        return nxt.isoformat() if nxt else "missed"

    def _send(self, sp: ScheduledPost) -> PostResult:
        try:
            client = self.manager.client(sp.account)
            if sp.reply_to_uri:
                return client.reply(sp.text, parent_uri=sp.reply_to_uri)
            return client.post(sp.text, image_paths=sp.image_paths or None)
        except Exception as e:
            return PostResult(account=sp.account, uri="", cid="", url="", error=str(e))

    def _apply_result(self, sp: ScheduledPost, result: PostResult, now: datetime) -> float | None:
        """
        Update a job after a send attempt. Returns the retry delay in
        seconds when the job should be retried, else None. Shared by the
        daemon (which registers a retry job) and tick mode (which stores
        retry_at).
        """
        sp.result = result
        sp.retry_at = None
        if result.success:
            sp.attempts = 0
            sp.last_error = None
            sp.last_run = now
            sp.status = "recurring" if sp.cron else "sent"
            return None

        sp.attempts += 1
        sp.last_error = result.error
        if sp.attempts <= self.max_retries:
            delay = self.retry_delay * (2 ** (sp.attempts - 1))
            if not sp.cron:
                sp.status = "retrying"
            print(f"[scheduler] Job {sp.id} failed (attempt {sp.attempts}): {result.error}. Retrying in {delay:.0f}s.")
            return delay
        # Out of retries. Recurring jobs wait for their next slot.
        print(f"[scheduler] Job {sp.id} failed after {sp.attempts} attempts: {result.error}")
        sp.attempts = 0
        sp.last_run = now if sp.cron else sp.last_run
        sp.status = "recurring" if sp.cron else "failed"
        return None

    def _execute(self, job_id: str):
        """Called by APScheduler when a job (or one of its retries) fires."""
        owner = f"daemon:{socket.gethostname()}:{uuid.uuid4().hex[:6]}"
        now = self._clock()
        claimed = self._claim(owner, now, only=job_id, require_due=False)
        for sp in claimed:
            result = self._deliver(sp)
            delay = self._complete(owner, sp, result, self._clock())
            if delay is not None:
                self._add_job(
                    f"{job_id}:retry",
                    job_id,
                    DateTrigger(run_date=self._clock() + timedelta(seconds=delay)),
                )
        self._sync_from_disk()

    # ------------------------------------------------------------------
    # Claim → send → complete (shared by tick, daemon and post.send)
    # ------------------------------------------------------------------

    def _claimable(self, sp: ScheduledPost, now: datetime, require_due: bool, grace: timedelta) -> bool:
        if sp.status == "sending":
            return sp.claimed_until is not None and sp.claimed_until <= now  # stale claim
        if sp.status not in ACTIVE_STATUSES:
            return False
        return self.is_due(sp, now, grace) if require_due else True

    def _claim(
        self,
        owner: str,
        now: datetime,
        only: str | None = None,
        require_due: bool = True,
        grace: timedelta = timedelta(days=1),
    ) -> list[ScheduledPost]:
        """
        Atomically mark due jobs as "sending" by `owner` (one CAS update).
        Returned copies carry `claimed_by` so completion can be fenced.
        A job whose previous claim went stale keeps `claimed_at` of the old
        claim in `_recovering_since`, so delivery can check for a post the
        crashed worker may already have made.
        """

        def mutate(jobs: dict[str, ScheduledPost]) -> list[ScheduledPost]:
            out = []
            for job_id, sp in jobs.items():
                if only is not None and job_id != only:
                    continue
                if not self._claimable(sp, now, require_due, grace):
                    continue
                recovering = sp.claimed_at if sp.status == "sending" else None
                sp.status = "sending"
                sp.claimed_by = owner
                sp.claimed_at = now
                sp.claimed_until = now + timedelta(seconds=self.claim_seconds)
                copy = ScheduledPost(**{k: getattr(sp, k) for k in sp.__dataclass_fields__})
                copy._recovering_since = recovering  # type: ignore[attr-defined]
                out.append(copy)
            return out

        return self.job_store.update(mutate) or []

    def _deliver(self, sp: ScheduledPost) -> PostResult:
        """Send, unless recovering a stale claim whose post already went out."""
        since = getattr(sp, "_recovering_since", None)
        if since is not None:
            found = self._find_existing_post(sp, since)
            if found is not None:
                print(f"[scheduler] Job {sp.id}: recovered a send that completed before a crash ({found.uri}).")
                return found
        return self._send(sp)

    def _find_existing_post(self, sp: ScheduledPost, since: datetime) -> PostResult | None:
        """
        Outbox verification: did a crashed worker already publish this post?
        Looks for the same text on the account's own repo, created at or
        after the stale claim (minus clock skew). Reads the PDS directly, so
        it also works on a private PDS with no AppView.
        """
        skew = timedelta(minutes=2)
        try:
            client = self.manager.client(sp.account)
            for post in client.list_posts(limit=25):
                created = post.get("created_at") or ""
                try:
                    created_at = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if post.get("text") == sp.text and created_at >= since - skew:
                    return PostResult(
                        account=sp.account, uri=post["uri"], cid=post.get("cid", ""), url=post.get("url", "")
                    )
        except Exception as e:
            print(f"[scheduler] Job {sp.id}: could not verify a previous send ({e}); sending.")
        return None

    def _complete(self, owner: str, claimed: ScheduledPost, result: PostResult, now: datetime) -> float | None:
        """
        Record the outcome, fenced by the claim: if another worker has since
        re-claimed the job (ours went stale), our result is discarded rather
        than overwriting theirs. Returns the retry delay, if any.
        """

        def mutate(jobs: dict[str, ScheduledPost]) -> float | None:
            sp = jobs.get(claimed.id)
            if sp is None or sp.claimed_by != owner:
                return None  # cancelled, or fenced out
            sp.status = "recurring" if sp.cron else ("retrying" if sp.attempts else "pending")
            sp.claimed_by = sp.claimed_at = sp.claimed_until = None
            delay = self._apply_result(sp, result, now)
            if delay is not None:
                sp.retry_at = now + timedelta(seconds=delay)
            return delay

        return self.job_store.update(mutate)

    # ------------------------------------------------------------------
    # Tick mode (cloud cron)
    # ------------------------------------------------------------------

    def is_due(self, sp: ScheduledPost, now: datetime, grace: timedelta = timedelta(days=1)) -> bool:
        """
        Whether a job should be sent in a tick at `now`.

        One-time jobs are due once run_at has passed (or retry_at, when
        retrying). Cron jobs are due when a slot has passed since the last
        run; at most one send per tick, so a long outage does not burst.
        A late post beats no post, so overdue jobs are still sent.
        """
        if sp.status not in ACTIVE_STATUSES or sp.status == "sending":
            return False
        if sp.retry_at is not None:
            return sp.retry_at <= now
        if sp.cron:
            trigger = CronTrigger.from_crontab(sp.cron, timezone="UTC")
            previous = sp.last_run or sp.run_at
            next_slot = trigger.get_next_fire_time(previous, now)
            return next_slot is not None and next_slot <= now
        return sp.run_at <= now

    def tick(
        self,
        now: datetime | None = None,
        grace: timedelta = timedelta(days=1),
        lease_seconds: int = 120,
        owner: str | None = None,
    ) -> TickResult:
        """
        One reconciliation pass: claim every due (or stale) job, send, record.

        Correctness comes from the claim protocol (CAS), not the lease: two
        overlapping ticks can never claim the same job. The lease only saves
        an overlapping tick the work of trying.
        """
        now = now or self._clock()
        owner = owner or f"tick:{socket.gethostname()}:{uuid.uuid4().hex[:6]}"
        result = TickResult(at=now)
        if not self.job_store.lease(owner, lease_seconds):
            result.skipped = "lease held"
            return result
        try:
            for sp in self._claim(owner, now, require_due=True, grace=grace):
                outcome = self._deliver(sp)
                self._complete(owner, sp, outcome, now)
                (result.sent if outcome.success else result.failed).append(sp.id)
        finally:
            self.job_store.release(owner)
        self._sync_from_disk()
        return result

    def send_now(self, job_id: str, owner: str | None = None) -> PostResult | None:
        """
        Deliver one job if it is due: the handler for a native delayed
        delivery (`post.send`). Duplicate or early deliveries are no-ops.
        """
        owner = owner or f"delivery:{socket.gethostname()}:{uuid.uuid4().hex[:6]}"
        now = self._clock()
        claimed = self._claim(owner, now, only=job_id, require_due=True)
        if not claimed:
            return None
        outcome = self._deliver(claimed[0])
        self._complete(owner, claimed[0], outcome, now)
        return outcome

    def _notify_scheduled(self, sp: ScheduledPost) -> None:
        """Register the native fast path for a one-time job, if configured."""
        if self.delayed is None:
            return
        horizon = self.delayed.max_horizon_seconds
        if horizon is not None and (sp.run_at - self._clock()).total_seconds() > horizon:
            return  # beyond what the service can schedule; tick will send it
        try:
            self.delayed.schedule(sp.id, sp.run_at, {"kind": "post.send", "job_id": sp.id})
        except Exception as e:
            print(f"[scheduler] Native scheduling failed for {sp.id} ({e}); tick will send it.")

    # ------------------------------------------------------------------
    # Queue persistence
    # ------------------------------------------------------------------

    def _read_queue_file(self) -> dict[str, ScheduledPost] | None:
        try:
            return self.job_store.load()
        except Exception as e:
            print(f"[scheduler] Warning: could not load queue: {e}")
            return None

    def _load_queue(self):
        self._queue = self._read_queue_file() or {}

    def _sync_from_disk(self):
        """
        Mirror the store into this process: pick up jobs another process
        added or cancelled, and refresh the ones we hold in place, so objects
        returned by schedule() reflect their current status.
        """
        on_disk = self._read_queue_file()
        if on_disk is None:
            return
        for job_id, sp in on_disk.items():
            mine = self._queue.get(job_id)
            if mine is None:
                self._queue[job_id] = sp
                self._register(sp)
            else:
                result = (
                    mine.result
                    if (mine.result is not None and sp.result is not None and mine.result.uri == sp.result.uri)
                    else sp.result
                )
                for name in sp.__dataclass_fields__:
                    setattr(mine, name, getattr(sp, name))
                mine.result = result
        for job_id in list(self._queue):
            if job_id not in on_disk:
                del self._queue[job_id]
                self._unregister(job_id)
