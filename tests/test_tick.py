"""Tick mode: one scheduler pass driven by a fake clock."""

from datetime import datetime, timedelta, timezone

from skyctl import BskyScheduler
from skyctl.adapters import MemoryObjectStore, ObjectJobStore

T0 = datetime(2026, 3, 1, 8, 59, 30, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


def make(manager, clock, store=None, **kwargs):
    store = store or ObjectJobStore(MemoryObjectStore())
    return BskyScheduler(manager, job_store=store, clock=clock, **kwargs), store


def test_one_time_job_is_sent_once_when_due(manager, fake_atproto):
    clock = Clock()
    scheduler, store = make(manager, clock)
    sp = scheduler.schedule("personal", "Morning!", run_at=T0 + timedelta(minutes=1))

    early = scheduler.tick()
    assert early.sent == [] and early.failed == []

    clock.advance(minutes=1)
    due = scheduler.tick()
    assert due.sent == [sp.id]
    assert store.load()[sp.id].status == "sent"

    again = scheduler.tick()
    assert again.sent == []
    assert [s["text"] for s in fake_atproto.instances[0].sent] == ["Morning!"]


def test_cron_job_fires_once_per_slot_and_never_bursts(manager, fake_atproto):
    clock = Clock()
    scheduler, store = make(manager, clock)
    job_id = scheduler.schedule_cron("work", "Daily", cron="0 9 * * *")

    assert scheduler.tick().sent == []          # 08:59:30, slot not reached
    clock.advance(seconds=30)                   # 09:00:00
    assert scheduler.tick().sent == [job_id]
    assert scheduler.tick().sent == []          # same slot, already run
    assert store.load()[job_id].status == "recurring"

    clock.advance(days=3)                       # three slots missed: one catch-up send, not three
    assert scheduler.tick().sent == [job_id]
    assert scheduler.tick().sent == []
    work = [c for c in fake_atproto.instances if c.login_calls and c.login_calls[0]["login"] == "work.test"][0]
    assert len(work.sent) == 2


def test_failed_send_is_retried_with_backoff_then_marked_failed(manager, fake_atproto):
    clock = Clock()
    scheduler, store = make(manager, clock, max_retries=2, retry_delay=60)
    sp = scheduler.schedule("personal", "Hi", run_at=T0)
    fake_atproto.instances[0].fail_sends = 3

    first = scheduler.tick()
    assert first.failed == [sp.id]
    job = store.load()[sp.id]
    assert job.status == "retrying" and job.attempts == 1
    assert job.retry_at == T0 + timedelta(seconds=60)

    clock.advance(seconds=30)
    assert scheduler.tick().failed == []        # not yet
    clock.advance(seconds=30)
    assert scheduler.tick().failed == [sp.id]   # attempt 2, delay doubles
    assert store.load()[sp.id].retry_at == clock.now + timedelta(seconds=120)

    clock.advance(seconds=120)
    assert scheduler.tick().failed == [sp.id]   # attempt 3 > max_retries
    assert store.load()[sp.id].status == "failed"
    assert scheduler.tick().failed == []        # dead jobs stay dead


def test_tick_respects_lease(manager):
    clock = Clock()
    scheduler, store = make(manager, clock)
    scheduler.schedule("personal", "x", run_at=T0)
    assert store.lease("someone-else", 300)
    result = scheduler.tick()
    assert result.skipped == "lease held" and result.sent == []
    store.release("someone-else")
    assert scheduler.tick().sent != []


def test_tick_sees_jobs_added_by_another_process(manager):
    clock = Clock()
    shared = ObjectJobStore(MemoryObjectStore())
    cli_side, _ = make(manager, clock, store=shared)
    ticker, _ = make(manager, clock, store=shared)
    sp = cli_side.schedule("personal", "from the CLI", run_at=T0)
    assert ticker.tick().sent == [sp.id]


def test_legacy_queue_file_still_used_by_default(manager, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    scheduler = BskyScheduler(manager, queue_file=tmp_path / "q.json", clock=Clock())
    scheduler.schedule("personal", "x", run_at=T0 + timedelta(hours=1))
    assert (tmp_path / "q.json").exists()
    assert "x" in (tmp_path / "q.json").read_text()


# ----------------------------------------------------------------------
# Claim protocol: the correctness core shared by tick, daemon and post.send
# ----------------------------------------------------------------------

def test_concurrent_claimers_never_get_the_same_job(manager):
    import threading

    clock = Clock()
    shared = ObjectJobStore(MemoryObjectStore())
    for i in range(20):
        make(manager, clock, store=shared)[0].schedule("personal", f"p{i}", run_at=T0)
    claimed: dict[str, list[str]] = {}

    def claim(owner):
        scheduler, _ = make(manager, clock, store=shared)
        claimed[owner] = [sp.id for sp in scheduler._claim(owner, T0)]

    threads = [threading.Thread(target=claim, args=(f"w{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    all_ids = [j for ids in claimed.values() for j in ids]
    assert len(all_ids) == 20 and len(set(all_ids)) == 20


def test_crash_after_send_is_recovered_without_a_duplicate_post(manager, monkeypatch):
    clock = Clock()
    scheduler, store = make(manager, clock, claim_seconds=60)
    sp = scheduler.schedule("personal", "exactly once please", run_at=T0)

    # Worker A claims and sends, then "crashes" before recording the result.
    [claimed] = scheduler._claim("worker-a", T0)
    sent = manager.client("personal").post(claimed.text)
    assert store.load()[sp.id].status == "sending"

    # The repo now holds the post; list_posts is what recovery consults.
    monkeypatch.setattr(
        manager.client("personal"), "list_posts",
        lambda limit=25: [{"uri": sent.uri, "cid": sent.cid, "url": sent.url,
                           "text": "exactly once please", "created_at": T0.isoformat()}],
    )
    clock.advance(seconds=30)
    assert scheduler.tick().sent == []                 # claim still fresh: hands off
    clock.advance(seconds=60)
    recovered = scheduler.tick()                       # claim stale: verify, don't resend
    assert recovered.sent == [sp.id]
    assert store.load()[sp.id].status == "sent"
    assert store.load()[sp.id].result.uri == sent.uri
    assert [p["text"] for p in manager.client("personal")._client.sent] == ["exactly once please"]


def test_crash_before_send_is_retried(manager, monkeypatch):
    clock = Clock()
    scheduler, store = make(manager, clock, claim_seconds=60)
    sp = scheduler.schedule("personal", "never sent", run_at=T0)
    scheduler._claim("worker-a", T0)                   # crashes before sending
    monkeypatch.setattr(manager.client("personal"), "list_posts", lambda limit=25: [])
    clock.advance(seconds=61)
    assert scheduler.tick().sent == [sp.id]
    assert len(manager.client("personal")._client.sent) == 1


def test_stale_worker_result_is_fenced_out(manager):
    clock = Clock()
    scheduler, store = make(manager, clock, claim_seconds=60)
    sp = scheduler.schedule("personal", "x", run_at=T0)
    [slow] = scheduler._claim("slow-worker", T0)
    clock.advance(seconds=61)
    [fast] = scheduler._claim("fast-worker", clock.now)   # re-claims the stale job
    from skyctl.models import PostResult

    # The slow worker finally reports a failure; it must not clobber the new claim.
    assert scheduler._complete("slow-worker", slow, PostResult("personal", "", "", "", error="late"), clock.now) is None
    job = store.load()[sp.id]
    assert job.status == "sending" and job.claimed_by == "fast-worker" and job.attempts == 0


def test_send_now_is_idempotent_and_respects_due_time(manager):
    clock = Clock()
    scheduler, store = make(manager, clock)
    sp = scheduler.schedule("personal", "fast path", run_at=T0 + timedelta(minutes=5))
    assert scheduler.send_now(sp.id) is None           # early delivery: no-op
    clock.advance(minutes=5)
    assert scheduler.send_now(sp.id).success
    assert scheduler.send_now(sp.id) is None           # duplicate delivery: no-op
    assert scheduler.send_now("unknown") is None
    assert len(manager.client("personal")._client.sent) == 1


def test_delayed_delivery_fast_path_is_registered_and_cancelled(manager):
    class Recorder:
        max_horizon_seconds = 3600

        def __init__(self):
            self.scheduled, self.cancelled = {}, []

        def schedule(self, key, when, body):
            self.scheduled[key] = (when, body)

        def cancel(self, key):
            self.cancelled.append(key)

    clock = Clock()
    delayed = Recorder()
    scheduler, _ = make(manager, clock, delayed=delayed)
    near = scheduler.schedule("personal", "soon", run_at=T0 + timedelta(minutes=10))
    far = scheduler.schedule("personal", "later", run_at=T0 + timedelta(days=3))
    assert delayed.scheduled[near.id][1] == {"kind": "post.send", "job_id": near.id}
    assert far.id not in delayed.scheduled             # beyond the horizon: tick handles it
    scheduler.cancel(near.id)
    assert delayed.cancelled == [near.id]


def test_schedule_during_tick_is_not_lost(manager):
    """The CLI adding a job while a tick is mid-flight must not be overwritten."""
    clock = Clock()
    shared = ObjectJobStore(MemoryObjectStore())
    ticker, _ = make(manager, clock, store=shared)
    cli, _ = make(manager, clock, store=shared)
    ticker.schedule("personal", "due", run_at=T0)
    original_send = ticker._send

    def send_and_meanwhile_schedule(sp):
        cli.schedule("work", "added mid-tick", run_at=T0 + timedelta(hours=1))
        return original_send(sp)

    ticker._send = send_and_meanwhile_schedule
    ticker.tick()
    texts = sorted(sp.text for sp in shared.load().values())
    assert texts == ["added mid-tick", "due"]
