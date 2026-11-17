import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from telethon import errors, functions, types
from test_access import RecordingSession, message

from telegram_downloader.errors import TransferError
from telegram_downloader.handlers import make_router
from telegram_downloader.store import JobStore, QueueFull
from telegram_downloader.transfer import TransferService


class InviteClient:
    def __init__(self, base):
        self.get_entity = base.get_entity
        self.get_messages = base.get_messages
        self.download_media = base.download_media
        self.session = SimpleNamespace(process_entities=Mock())
        self.approved = False
        self.channel = base.get_entity.return_value
        self.preview = types.ChatInvite(
            title="Private test",
            photo=types.PhotoEmpty(id=1),
            participants_count=1,
            color=0,
            request_needed=True,
        )
        self.rpc = AsyncMock(side_effect=self.reply)

    async def __call__(self, request):
        return await self.rpc(request)

    async def reply(self, request):
        if isinstance(request, functions.messages.CheckChatInviteRequest):
            return types.ChatInviteAlready(self.channel) if self.approved else self.preview
        if isinstance(request, functions.messages.ImportChatInviteRequest):
            raise errors.InviteRequestSentError(request=None)
        raise AssertionError(type(request).__name__)

    @property
    def import_count(self):
        return sum(
            isinstance(call.args[0], functions.messages.ImportChatInviteRequest)
            for call in self.rpc.await_args_list
        )


@pytest.fixture
def invite_client(client):
    return InviteClient(client)


async def waiting_job(service, invite_client, *, user=101, link="https://t.me/c/555/12"):
    # The account is a nonmember until an admin approves its request.
    invite_client.get_entity.side_effect = errors.ChannelPrivateError(request=None)
    job = service.enqueue(user, user, link)
    await service.execute(service.store.claim_next())
    assert service.store.list_for_user(user)[0].state == "awaiting_approval"
    return job


def due(store, invite_hash="testHASH"):
    invite = store.get_invite(invite_hash)
    store.save_invite(invite_hash, invite.state, peer_id=invite.peer_id, next_check_at=0)


async def approve(service, invite_client, store):
    invite_client.approved = True
    invite_client.get_entity.side_effect = None
    due(store)
    await service.joins.check_pending()


async def test_pending_join_approval_resumes_download_and_caches_entity(
    settings, store, invite_client, bot
):
    service = TransferService(settings, store, invite_client, bot)
    assert "Join request sent" in await service.joins.join(101, "testHASH")
    job = await waiting_job(service, invite_client)
    assert invite_client.import_count == 1
    invite_client.download_media.assert_not_called()
    await approve(service, invite_client, store)
    assert store.list_for_user(101)[0].state == "queued"
    invite_client.session.process_entities.assert_called_once()
    await service.execute(store.claim_next())
    assert store.list_for_user(101)[0].state == "done"
    bot.send_document.assert_awaited_once()
    assert any(
        "approval received" in call.args[0] and f"#{job.id}" in call.args[0]
        for call in bot.edit_message_text.await_args_list
    )


async def test_pending_approval_also_resumes_text_posts(settings, store, invite_client, bot):
    service = TransferService(settings, store, invite_client, bot)
    await service.joins.join(101, "testHASH")
    await waiting_job(service, invite_client)
    invite_client.get_messages.return_value = SimpleNamespace(
        photo=None, document=None, file=None, media=None, message="A private text post 🙂"
    )
    await approve(service, invite_client, store)
    await service.execute(store.claim_next())
    assert store.list_for_user(101)[0].state == "done"
    assert any(
        call.kwargs.get("text") == "A private text post 🙂"
        for call in bot.send_message.await_args_list
    )
    invite_client.download_media.assert_not_called()


async def test_immediate_join_and_already_joined(settings, store, invite_client, bot):
    async def reply(request):
        if isinstance(request, functions.messages.ImportChatInviteRequest):
            invite_client.approved = True
            return types.Updates(updates=[], users=[], chats=[], date=None, seq=0)
        return await invite_client.reply(request)

    invite_client.rpc.side_effect = reply
    service = TransferService(settings, store, invite_client, bot)
    assert "Joined" in await service.joins.join(101, "testHASH")
    assert "already joined" in await service.joins.join(101, "testHASH")
    assert invite_client.import_count == 1
    assert store.get_invite("testHASH").peer_id == -1000000000555


async def test_existing_membership_does_not_import(settings, store, invite_client, bot):
    invite_client.approved = True
    service = TransferService(settings, store, invite_client, bot)
    assert "member" in await service.joins.join(101, "testHASH")
    assert invite_client.import_count == 0


async def test_duplicate_pending_invites_reuse_request_after_restart(
    settings, store, invite_client, bot
):
    service = TransferService(settings, store, invite_client, bot)
    await service.joins.join(101, "testHASH")
    await asyncio.gather(service.joins.join(101, "testHASH"), service.joins.join(202, "testHASH"))
    restarted = TransferService(settings, store, invite_client, bot)
    due(store)
    await restarted.joins.join(101, "testHASH")
    assert invite_client.import_count == 1
    assert store.latest_invite(202).invite_hash == "testHASH"


async def test_paid_invite_is_rejected_without_import(settings, store, invite_client, bot):
    invite_client.preview.subscription_pricing = types.StarsSubscriptionPricing(
        period=2592000, amount=5
    )
    service = TransferService(settings, store, invite_client, bot)
    with pytest.raises(TransferError, match="paid invite"):
        await service.joins.join(101, "paidHASH")
    assert invite_client.import_count == 0


@pytest.mark.parametrize(
    ("exception", "text"),
    [
        (errors.InviteHashExpiredError(request=None), "expired"),
        (errors.InviteHashInvalidError(request=None), "invalid"),
        (errors.UserBannedInChannelError(request=None), "banned"),
        (errors.ChannelsTooMuchError(request=None), "membership limit"),
        (errors.UsersTooMuchError(request=None), "member limit"),
    ],
)
async def test_invite_errors_are_clear(settings, store, invite_client, bot, exception, text):
    invite_client.rpc.side_effect = exception
    service = TransferService(settings, store, invite_client, bot)
    with pytest.raises(TransferError, match=text):
        await service.joins.join(101, "badHASH")
    assert store.get_invite("badHASH").state == "failed"


async def test_lost_join_response_is_not_imported_twice(settings, store, invite_client, bot):
    async def reply(request):
        if isinstance(request, functions.messages.ImportChatInviteRequest):
            raise ConnectionError("secret invitation must not be logged")
        return invite_client.preview

    invite_client.rpc.side_effect = reply
    service = TransferService(settings, store, invite_client, bot)
    assert "response was lost" in await service.joins.join(101, "testHASH")
    due(store)
    await service.joins.join(101, "testHASH")
    assert invite_client.import_count == 1


async def test_wrong_channel_is_not_downloaded_after_approval(settings, store, invite_client, bot):
    service = TransferService(settings, store, invite_client, bot)
    await service.joins.join(101, "testHASH")
    await waiting_job(service, invite_client, link="https://t.me/c/999/12")
    await approve(service, invite_client, store)
    result = store.list_for_user(101)[0]
    assert result.state == "failed"
    assert "does not belong" in result.error
    invite_client.download_media.assert_not_called()


async def test_accessible_post_ignores_unrelated_pending_invite(
    settings, store, invite_client, bot
):
    service = TransferService(settings, store, invite_client, bot)
    await service.joins.join(101, "testHASH")
    service.enqueue(101, 101, "https://t.me/channel/123")
    await service.execute(store.claim_next())
    assert store.list_for_user(101)[0].state == "done"


async def test_waiting_jobs_do_not_block_other_transfers(settings, store, invite_client, bot):
    service = TransferService(settings, store, invite_client, bot)
    await service.joins.join(101, "testHASH")
    await waiting_job(service, invite_client)
    invite_client.get_entity.side_effect = None
    service.enqueue(202, 202, "https://t.me/channel/123")
    await service.execute(store.claim_next())
    assert store.list_for_user(202)[0].state == "done"
    assert store.list_for_user(101)[0].state == "awaiting_approval"


async def test_wait_expiration_does_not_call_telegram(settings, store, invite_client, bot):
    service = TransferService(settings, store, invite_client, bot)
    await service.joins.join(101, "testHASH")
    job = await waiting_job(service, invite_client)
    calls = invite_client.rpc.await_count
    deadline = store.list_for_user(101)[0].approval_deadline
    with patch("telegram_downloader.joins.time.time", return_value=deadline + 1):
        await service.joins.check_pending()
    assert store.list_for_user(101)[0].state == "expired"
    assert invite_client.rpc.await_count == calls
    assert str(job.id) in bot.send_message.call_args.args[1]


async def test_cancelling_last_waiter_stops_approval_checks(settings, store, invite_client, bot):
    service = TransferService(settings, store, invite_client, bot)
    await service.joins.join(101, "testHASH")
    job = await waiting_job(service, invite_client)
    calls = invite_client.rpc.await_count
    assert service.cancel(202, job.id) == []
    assert service.cancel(101, job.id) == [job.id]
    due(store)
    await service.joins.check_pending()
    assert invite_client.rpc.await_count == calls


async def test_cancel_during_approval_rpc_cannot_resurrect_job(settings, store, invite_client, bot):
    service = TransferService(settings, store, invite_client, bot)
    await service.joins.join(101, "testHASH")
    job = await waiting_job(service, invite_client)
    entered, release = asyncio.Event(), asyncio.Event()

    async def check(request):
        entered.set()
        await release.wait()
        return types.ChatInviteAlready(invite_client.channel)

    invite_client.rpc.side_effect = check
    due(store)
    task = asyncio.create_task(service.joins.check_pending())
    await asyncio.wait_for(entered.wait(), timeout=2)
    service.cancel(101, job.id)
    release.set()
    await task
    assert store.list_for_user(101)[0].state == "cancelled"
    assert store.claim_next() is None


def test_invites_are_per_user_and_snapshotted_at_submission(store):
    for name in ("inviteA", "inviteB"):
        store.save_invite(name, "pending")
    store.remember_invite(101, "inviteA")
    first = store.enqueue(101, 101, "https://t.me/channel/1", 5)
    store.remember_invite(101, "inviteB")
    second = store.enqueue(101, 101, "https://t.me/channel/2", 5)
    store.remember_invite(202, "inviteA")
    assert first.invite_hash == "inviteA"
    assert second.invite_hash == "inviteB"
    assert store.latest_invite(202).invite_hash == "inviteA"
    assert store.latest_invite(101).invite_hash == "inviteB"


def test_waiting_jobs_count_towards_limit(store):
    store.save_invite("inviteA", "pending")
    store.remember_invite(101, "inviteA")
    for _ in range(5):
        job = store.enqueue(101, 101, "https://t.me/channel/1", 5)
        store.set_state(job.id, "awaiting_approval")
    with pytest.raises(QueueFull):
        store.enqueue(101, 101, "https://t.me/channel/1", 5)


async def test_waiting_jobs_resume_after_database_reopen(settings, store, invite_client, bot):
    service = TransferService(settings, store, invite_client, bot)
    await service.joins.join(101, "testHASH")
    await waiting_job(service, invite_client)
    deadline = store.list_for_user(101)[0].approval_deadline
    store.close()
    reopened = JobStore(settings.jobs_path)
    try:
        service = TransferService(settings, reopened, invite_client, bot)
        await service.recover()
        assert reopened.list_for_user(101)[0].approval_deadline == deadline
        assert reopened.list_for_user(101)[0].state == "awaiting_approval"
        await approve(service, invite_client, reopened)
        assert reopened.list_for_user(101)[0].state == "queued"
        assert invite_client.import_count == 1
    finally:
        reopened.close()


def test_additive_migration_preserves_old_history(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.executescript("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL, link TEXT NOT NULL, state TEXT NOT NULL,
            error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO jobs(user_id,chat_id,link,state) VALUES (101,101,'https://t.me/channel/1','done');
    """)
    legacy.close()
    migrated = JobStore(path)
    try:
        job = migrated.list_for_user(101)[0]
        assert job.state == "done"
        assert job.invite_hash is None
        assert job.approval_deadline is None
    finally:
        migrated.close()


async def test_flood_wait_defers_further_invite_checks(settings, store, invite_client, bot):
    service = TransferService(settings, store, invite_client, bot)
    await service.joins.join(101, "testHASH")
    await waiting_job(service, invite_client)
    due(store)
    invite_client.rpc.side_effect = errors.FloodWaitError(request=None, capture=600)
    await service.joins.check_pending()
    calls = invite_client.rpc.await_count
    assert store.get_invite("testHASH").next_check_at > 0
    await service.joins.check_pending()
    assert invite_client.rpc.await_count == calls
    assert store.list_for_user(101)[0].state == "awaiting_approval"


async def test_router_accepts_invites_and_blocks_unauthorized_join(settings, store, invite_client):
    session = RecordingSession()
    bot = Bot(settings.bot_token, session=session)
    service = TransferService(settings, store, invite_client, bot)
    dispatcher = Dispatcher()
    dispatcher.include_router(make_router(settings, store, service))
    try:
        await dispatcher.feed_update(
            bot, Update(update_id=1, message=message(999, "https://t.me/+testHASH"))
        )
        invite_client.rpc.assert_not_called()
        await dispatcher.feed_update(
            bot, Update(update_id=2, message=message(101, "https://t.me/+testHASH"))
        )
        assert "Join request sent" in session.requests[-1].text
        assert invite_client.import_count == 1
        assert store.list_for_user(101) == []
        assert store.latest_invite(101).invite_hash == "testHASH"
    finally:
        await bot.session.close()
