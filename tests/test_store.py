import pytest

from telegram_downloader.store import QueueFull


def enqueue(store, user=101, limit=5):
    return store.enqueue(user, user, "https://t.me/channel/1", limit)


def test_queue_limit_includes_active_job_and_is_per_user(store):
    enqueue(store)
    store.claim_next()
    for _ in range(4):
        enqueue(store)
    with pytest.raises(QueueFull):
        enqueue(store)
    assert enqueue(store, user=202).user_id == 202
    store.set_state(1, "done")
    assert enqueue(store).state == "queued"


def test_fifo_and_owner_scoped_cancellation(store):
    first = enqueue(store)
    second = enqueue(store, user=202)
    assert store.claim_next().id == first.id
    assert store.cancel(202, first.id) == []
    assert store.cancel(202) == [second.id]
    assert store.claim_next() is None
    assert [job.id for job in store.list_for_user(101)] == [first.id]


def test_cancelled_job_cannot_be_marked_done(store):
    job = enqueue(store)
    store.claim_next()
    store.cancel(101, job.id)
    store.set_state(job.id, "done")
    assert store.list_for_user(101)[0].state == "cancelled"


def test_recovery_interrupts_all_pending_without_replaying(store):
    jobs = [enqueue(store) for _ in range(3)]
    store.claim_next()
    store.set_state(jobs[0].id, "uploading")
    assert len(store.recover()) == 3
    assert {job.state for job in store.list_for_user(101)} == {"interrupted"}
    assert store.claim_next() is None
    assert store.recover() == []


def test_jobs_survive_reopening(settings):
    from telegram_downloader.store import JobStore

    first = JobStore(settings.jobs_path)
    job = enqueue(first)
    first.close()
    second = JobStore(settings.jobs_path)
    try:
        assert second.list_for_user(101)[0].id == job.id
        assert second.recover()[0].id == job.id
    finally:
        second.close()
