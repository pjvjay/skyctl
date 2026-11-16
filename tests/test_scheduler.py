from datetime import datetime, timedelta, timezone

import pytest

from skyctl import BskyScheduler, PostTooLongError


def future(hours=1):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


@pytest.fixture
def scheduler(manager, tmp_path):
    return BskyScheduler(manager, queue_file=tmp_path / "queue.json", retry_delay=60)


def test_one_time_job_sends_and_is_marked_sent(scheduler, manager):
    sp = scheduler.schedule("personal", "hello", future())
    scheduler._execute(sp.id)
    assert sp.status == "sent"
    assert manager.client("personal")._client.sent[0]["text"] == "hello"


def test_failing_job_is_retried_then_succeeds(scheduler, manager):
    sp = scheduler.schedule("personal", "hello", future())
    fake = manager.client("personal").login()._client
    fake.fail_sends = 1

    before = datetime.now(timezone.utc)
    scheduler._execute(sp.id)
    assert sp.status == "retrying"
    assert sp.attempts == 1
    assert "network down" in sp.last_error
    retry = scheduler._scheduler.get_job(f"{sp.id}:retry")
    assert retry is not None
    assert retry.trigger.run_date >= before + timedelta(seconds=59)

    scheduler._execute(sp.id)          # the retry fires
    assert sp.status == "sent"
    assert sp.attempts == 0
    assert len(fake.sent) == 1


def test_retry_delay_doubles_each_attempt(scheduler, manager):
    sp = scheduler.schedule("personal", "hello", future())
    manager.client("personal").login()._client.fail_sends = 2
    scheduler._execute(sp.id)
    scheduler._execute(sp.id)
    retry = scheduler._scheduler.get_job(f"{sp.id}:retry")
    assert retry.trigger.run_date >= datetime.now(timezone.utc) + timedelta(seconds=115)


def test_job_fails_for_good_after_max_retries(manager, tmp_path):
    scheduler = BskyScheduler(manager, queue_file=tmp_path / "q.json", max_retries=2)
    sp = scheduler.schedule("personal", "hello", future())
    manager.client("personal").login()._client.fail_sends = 99
    for _ in range(3):                 # first try + 2 retries
        scheduler._execute(sp.id)
    assert sp.status == "failed"


def test_recurring_job_is_persisted_and_listed(scheduler, manager, tmp_path):
    job_id = scheduler.schedule_cron("work", "Daily update", cron="0 9 * * mon-fri")

    # A fresh scheduler (as `skyctl jobs` or `run-scheduler` would create)
    reloaded = BskyScheduler(manager, queue_file=tmp_path / "queue.json")
    (job,) = reloaded.list_jobs()
    assert job["id"] == job_id
    assert job["status"] == "recurring"
    assert job["schedule"] == "0 9 * * mon-fri"
    assert job["account"] == "work"
    assert job["next_run"] not in ("—", "missed")
    assert reloaded._scheduler.get_job(job_id) is not None


def test_recurring_job_stays_recurring_after_a_send(scheduler, manager):
    job_id = scheduler.schedule_cron("work", "Daily update", hour=9, minute=30)
    scheduler._execute(job_id)
    assert scheduler._queue[job_id].status == "recurring"
    assert manager.client("work")._client.sent[0]["text"] == "Daily update"


def test_invalid_cron_expression_is_rejected(scheduler):
    with pytest.raises(ValueError):
        scheduler.schedule_cron("work", "x", cron="not a cron")


def test_too_long_text_is_rejected_at_schedule_time(scheduler):
    with pytest.raises(PostTooLongError):
        scheduler.schedule("personal", "a" * 301, future())


def test_cancel_removes_job_everywhere(scheduler, manager, tmp_path):
    sp = scheduler.schedule("personal", "hello", future())
    assert scheduler.cancel(sp.id) is True
    assert scheduler.list_jobs() == []
    assert BskyScheduler(manager, queue_file=tmp_path / "queue.json").list_jobs() == []


def test_running_scheduler_picks_up_jobs_added_by_another_process(manager, tmp_path):
    queue = tmp_path / "queue.json"
    running = BskyScheduler(manager, queue_file=queue)
    other = BskyScheduler(manager, queue_file=queue)
    sp = other.schedule("personal", "added later", future())

    running._sync_from_disk()
    assert running._scheduler.get_job(sp.id) is not None

    other.cancel(sp.id)
    running._sync_from_disk()
    assert running._scheduler.get_job(sp.id) is None
