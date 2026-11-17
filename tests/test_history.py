import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from telegram_downloader import history
from telegram_downloader import main as bot_main
from telegram_downloader.store import FINISHED, PENDING, JobStore
from telegram_downloader.transfer import TransferService

NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)


@pytest.fixture(params=["store", "pg_store"], ids=["sqlite", "postgres"])
def history_store(request):
    return request.getfixturevalue(request.param)


def dated_job(store, state, age, *, now=NOW):
    job = store.enqueue(101, 101, "https://t.me/channel/1", 5)
    store.set_state(job.id, state)
    created, updated = now - timedelta(days=2), now - age
    if not store.postgres:
        created = created.strftime("%Y-%m-%d %H:%M:%S")
        updated = updated.strftime("%Y-%m-%d %H:%M:%S")
    with store.db:
        store.db.execute(
            "UPDATE jobs SET created_at=?, updated_at=? WHERE id=?", (created, updated, job.id)
        )
    return job


@pytest.mark.parametrize("state", [*FINISHED, *PENDING])
def test_two_hour_cutoff_preserves_pending_and_recently_finished(history_store, state):
    old = dated_job(history_store, state, timedelta(hours=3))
    boundary = dated_job(history_store, state, timedelta(hours=2))
    recent = dated_job(history_store, state, timedelta(hours=2) - timedelta(seconds=1))
    just_finished = dated_job(history_store, state, timedelta(seconds=5))
    removed = history_store.prune_history(now=NOW)
    assert removed == (2 if state in FINISHED else 0)
    assert (history_store.get_job(old.id) is None) == (state in FINISHED)
    assert (history_store.get_job(boundary.id) is None) == (state in FINISHED)
    assert history_store.get_job(recent.id).state == state
    assert history_store.get_job(just_finished.id).state == state
    assert history_store.prune_history(now=NOW) == 0


def test_history_cleanup_preserves_invites_and_does_not_reuse_request_ids(history_store):
    history_store.save_invite("testInvite", "joined", peer_id=-1001234567890)
    history_store.remember_invite(101, "testInvite")
    old = dated_job(history_store, "done", timedelta(hours=3))
    assert history_store.prune_history(now=NOW) == 1
    assert history_store.list_for_user(101) == []
    assert history_store.latest_invite(101).peer_id == -1001234567890
    new = history_store.enqueue(101, 101, "https://t.me/channel/2", 5)
    assert new.id > old.id
    assert new.invite_hash == "testInvite"


def test_existing_sqlite_history_is_cleaned_after_reopening(settings):
    database = JobStore(settings.jobs_path)
    old = dated_job(database, "done", timedelta(hours=3))
    pending = dated_job(database, "awaiting_approval", timedelta(hours=3))
    database.close()
    reopened = JobStore(settings.jobs_path)
    try:
        assert reopened.prune_history(now=NOW) == 1
        assert reopened.get_job(old.id) is None
        assert reopened.get_job(pending.id).state == "awaiting_approval"
    finally:
        reopened.close()


async def test_cleanup_runs_immediately_retries_safely_and_can_be_cancelled(
    store, monkeypatch, caplog
):
    checked_twice = asyncio.Event()
    calls = []

    def prune():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("private-database-credential")
        checked_twice.set()
        return 0

    monkeypatch.setattr(store, "prune_history", prune)
    monkeypatch.setattr(history, "CHECK_INTERVAL_SECONDS", 0)
    task = asyncio.create_task(history.cleanup_history(store))
    try:
        await asyncio.wait_for(checked_twice.wait(), timeout=2)
        assert len(calls) >= 2
        assert "RuntimeError" in caplog.text
        assert "private-database-credential" not in caplog.text
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_cleanup_continues_during_a_blocked_upload(settings, store, client, bot, monkeypatch):
    old = dated_job(store, "done", timedelta(hours=3), now=datetime.now(UTC))
    service = TransferService(settings, store, client, bot)
    active = service.enqueue(101, 101, "https://t.me/channel/2")
    uploading, release_upload, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def upload(**kwargs):
        uploading.set()
        await release_upload.wait()

    prune = store.prune_history

    def cleanup():
        result = prune()
        cleaned.set()
        return result

    bot.send_document.side_effect = upload
    monkeypatch.setattr(store, "prune_history", cleanup)
    transfer = asyncio.create_task(service.execute(store.claim_next()))
    cleanup_task = None
    try:
        await asyncio.wait_for(uploading.wait(), timeout=2)
        cleanup_task = asyncio.create_task(history.cleanup_history(store))
        await asyncio.wait_for(cleaned.wait(), timeout=2)
        assert store.get_job(old.id) is None
        assert store.get_job(active.id).state == "uploading"
        release_upload.set()
        await asyncio.wait_for(transfer, timeout=2)
        assert store.get_job(active.id).state == "done"
    finally:
        for task in (transfer, cleanup_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


async def test_bot_starts_and_stops_the_history_task_with_polling(
    settings, store, client, bot, monkeypatch
):
    checked, stopped = asyncio.Event(), asyncio.Event()

    async def cleanup(database):
        assert database is store
        checked.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def poll(*args, **kwargs):
        await asyncio.wait_for(checked.wait(), timeout=2)

    settings.session_path.touch()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=True)
    client.get_me = AsyncMock(return_value=SimpleNamespace(bot=False))
    bot.get_me, bot.delete_webhook, bot.set_my_commands = AsyncMock(), AsyncMock(), AsyncMock()
    bot.session = SimpleNamespace(close=AsyncMock())
    dispatcher = SimpleNamespace(
        include_router=Mock(), start_polling=poll, stop_polling=AsyncMock()
    )
    monkeypatch.setattr(bot_main, "account_client", lambda _: client)
    monkeypatch.setattr(bot_main, "make_bot", lambda _: bot)
    monkeypatch.setattr(bot_main, "JobStore", lambda *args, **kwargs: store)
    monkeypatch.setattr(bot_main, "Dispatcher", lambda: dispatcher)
    monkeypatch.setattr(bot_main, "cleanup_history", cleanup)
    await asyncio.wait_for(bot_main.serve(settings, handle_signals=False), timeout=2)
    assert checked.is_set()
    assert stopped.is_set()
    client.disconnect.assert_awaited_once()
    bot.session.close.assert_awaited_once()
