"""Run with TEST_DATABASE_URL pointing at a disposable database ending in _test."""

import os
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import pytest

from telegram_downloader.store import JobStore, QueueFull


@pytest.fixture
def pg_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL for PostgreSQL integration tests")
    if not urlsplit(url).path.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must select a disposable database ending in _test")
    return url


@pytest.fixture
def pg_store(settings, pg_url):
    database = JobStore(settings.jobs_path, database_url=pg_url)
    database.db.execute("TRUNCATE jobs, invitations, user_invites RESTART IDENTITY")
    yield database
    database.close()


def enqueue(database, user=101):
    return database.enqueue(user, user, "https://t.me/channel/1", 5)


def test_postgres_queue_limits_ownership_and_64bit_ids(pg_store):
    user = 9_000_000_000
    for _ in range(5):
        enqueue(pg_store, user)
    with pytest.raises(QueueFull):
        enqueue(pg_store, user)
    assert pg_store.claim_next().user_id == user
    assert pg_store.cancel(202, 1) == []
    assert len(pg_store.cancel(user)) == 5
    assert pg_store.claim_next() is None


def test_postgres_invites_cards_and_waiting_state_survive_reconnect(pg_store, settings, pg_url):
    pg_store.save_invite("testInvite", "pending", peer_id=-1001234567890, next_check_at=123)
    pg_store.remember_invite(101, "testInvite")
    job = enqueue(pg_store)
    pg_store.save_status_message(job.id, 5000000000)
    pg_store.set_state(job.id, "awaiting_approval", "Waiting for admin")
    pg_store.close()
    reopened = JobStore(settings.jobs_path, database_url=pg_url)
    try:
        assert reopened.recover() == []
        saved = reopened.waiting_jobs()[0]
        assert saved.approval_deadline == job.approval_deadline
        assert saved.invite_hash == "testInvite"
        assert saved.status_message_id == 5000000000
        assert reopened.latest_invite(101).peer_id == -1001234567890
        assert reopened.update_waiting(job.id, "queued")
        assert reopened.claim_next().id == job.id
        reopened.set_state(job.id, "done")
        assert reopened.get_job(job.id).state == "done"
    finally:
        reopened.close()


def test_postgres_single_worker_lock_is_released_on_disconnect(pg_store, settings, pg_url):
    second = JobStore(settings.jobs_path, database_url=pg_url)
    try:
        assert pg_store.acquire_worker_lock()
        assert not second.acquire_worker_lock()
        pg_store.close()
        assert second.acquire_worker_lock()
    finally:
        second.close()


def test_postgres_recovers_transfers_but_preserves_approval_waiters(pg_store):
    active = enqueue(pg_store)
    waiting = enqueue(pg_store)
    pg_store.claim_next()
    pg_store.set_state(waiting.id, "awaiting_approval")
    assert [job.id for job in pg_store.recover()] == [active.id]
    assert pg_store.get_job(waiting.id).state == "awaiting_approval"
    assert pg_store.get_job(active.id).state == "interrupted"


def test_postgres_cancel_cannot_be_undone_by_approval(pg_store):
    job = enqueue(pg_store)
    pg_store.set_state(job.id, "awaiting_approval")
    assert pg_store.cancel(101, job.id) == [job.id]
    assert not pg_store.update_waiting(job.id, "queued")
    assert pg_store.waiting_jobs() == []


def test_postgres_failed_transaction_rolls_back_and_connection_remains_usable(pg_store):
    with pytest.raises(RuntimeError):
        with pg_store.db:
            pg_store.db.execute(
                "INSERT INTO user_invites(user_id,invite_hash) VALUES (?,?)", (101, "testInvite")
            )
            raise RuntimeError("Abort transaction")
    assert (
        pg_store.db.execute("SELECT count(*) AS total FROM user_invites").fetchone()["total"] == 0
    )
    assert enqueue(pg_store).id > 0


def test_postgres_concurrent_enqueues_respect_the_pending_limit(pg_store, settings, pg_url):
    def submit(_):
        database = JobStore(settings.jobs_path, database_url=pg_url)
        try:
            try:
                enqueue(database)
                return True
            except QueueFull:
                return False
        finally:
            database.close()

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(submit, range(8)))
    assert sum(results) == 5
    assert len(pg_store.list_for_user(101)) == 5
