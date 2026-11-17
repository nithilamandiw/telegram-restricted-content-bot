import asyncio
from dataclasses import replace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Update, User
from test_access import RecordingSession, message

from telegram_downloader.handlers import make_router
from telegram_downloader.presentation import StatusCards, job_card, status_card
from telegram_downloader.transfer import TransferService


def new_job(store):
    return store.enqueue(101, 101, "https://t.me/channel/1", 5)


async def test_one_card_is_reused_from_queue_through_completion(settings, store, client, bot):
    service = TransferService(settings, store, client, bot)
    job = service.enqueue(101, 101, "https://t.me/channel/1", start=False)
    await service.cards.show(job)
    assert not service.wakeup.is_set()
    original_id = store.get_job(job.id).status_message_id
    await service.execute(store.claim_next())
    assert store.get_job(job.id).state == "done"
    assert store.get_job(job.id).status_message_id == original_id
    bot.send_message.assert_awaited_once()
    assert bot.edit_message_text.await_count >= 3
    assert "Delivered" in bot.edit_message_text.call_args.args[0]
    final_buttons = bot.edit_message_text.call_args.kwargs["reply_markup"]
    assert all(
        not (button.callback_data or "").startswith("cancel:")
        for row in final_buttons.inline_keyboard
        for button in row
    )


async def test_concurrent_card_creation_sends_only_one_message(store, bot):
    job = new_job(store)
    cards = StatusCards(bot, store)
    await asyncio.gather(cards.show(job), cards.show(job), cards.show(job))
    bot.send_message.assert_awaited_once()
    assert bot.edit_message_text.await_count == 2


async def test_deleted_status_message_is_replaced_and_saved(store, bot):
    job = new_job(store)
    cards = StatusCards(bot, store)
    await cards.show(job)
    bot.send_message.return_value.message_id = 99
    bot.edit_message_text.side_effect = TelegramBadRequest(
        method=EditMessageText(text="status", chat_id=101, message_id=88),
        message="Bad Request: message to edit not found",
    )
    await cards.show(job)
    assert store.get_job(job.id).status_message_id == 99
    assert bot.send_message.await_count == 2


async def test_unchanged_status_does_not_send_duplicate(store, bot):
    job = new_job(store)
    cards = StatusCards(bot, store)
    await cards.show(job)
    bot.edit_message_text.side_effect = TelegramBadRequest(
        method=EditMessageText(text="status", chat_id=101, message_id=88),
        message="Bad Request: message is not modified",
    )
    await cards.show(job)
    bot.send_message.assert_awaited_once()


def test_card_escapes_source_data_and_displays_bounded_progress(store):
    job = new_job(store)
    text = job_card(
        job,
        stage="downloading",
        current=512,
        total=1024,
        filename="<b>file&name.pdf",
        detail="Failed <script> & retry",
    )
    assert "▰▰▰▰▰▱▱▱▱▱ 50%" in text
    assert "512 B / 1.0 KB" in text
    assert "&lt;b&gt;file&amp;name.pdf" in text
    assert "&lt;script&gt; &amp; retry" in text
    cancelled = replace(job, state="cancelled")
    assert "Cancelled" in job_card(cancelled, stage="downloading", current=50, total=100)
    assert "50%" not in job_card(cancelled, stage="downloading", current=50, total=100)
    assert "&lt;unsafe&gt;" in status_card([replace(job, state="failed", error="<unsafe>")])


async def test_late_notification_cannot_overwrite_completed_card(store, bot):
    job = new_job(store)
    store.set_state(job.id, "done")
    await StatusCards(bot, store).show(
        job, detail="Still waiting for approval", expected_state="awaiting_approval"
    )
    assert "Delivered" in bot.send_message.call_args.args[1]
    assert "Still waiting" not in bot.send_message.call_args.args[1]


def callback(user_id, data, card_id):
    return CallbackQuery(
        id="button-click",
        from_user=User(id=user_id, is_bot=False, first_name="Test"),
        chat_instance="private-chat",
        data=data,
        message=message(user_id).model_copy(update={"message_id": card_id}),
    )


@pytest.mark.parametrize(
    ("user", "stale", "cancelled"),
    [
        (101, False, True),
        (202, False, False),
        (999, False, False),
        (101, True, False),
    ],
)
async def test_cancel_button_checks_owner_allowlist_and_card(
    settings, store, client, user, stale, cancelled
):
    session = RecordingSession()
    bot = Bot(settings.bot_token, session=session)
    service = TransferService(settings, store, client, bot)
    dispatcher = Dispatcher()
    dispatcher.include_router(make_router(settings, store, service))
    job = new_job(store)
    try:
        await service.cards.show(job)
        card_id = store.get_job(job.id).status_message_id
        await dispatcher.feed_update(
            bot,
            Update(
                update_id=20,
                callback_query=callback(user, f"cancel:{job.id}", card_id + int(stale)),
            ),
        )
        assert (store.get_job(job.id).state == "cancelled") is cancelled
        assert any(isinstance(request, AnswerCallbackQuery) for request in session.requests)
        if cancelled:
            assert isinstance(session.requests[-1], EditMessageText)
            assert "Cancelled" in session.requests[-1].text
    finally:
        await bot.session.close()


async def test_buttons_show_own_jobs_and_refresh_in_place(settings, store, client):
    session = RecordingSession()
    bot = Bot(settings.bot_token, session=session)
    service = TransferService(settings, store, client, bot)
    dispatcher = Dispatcher()
    dispatcher.include_router(make_router(settings, store, service))
    new_job(store)  # owned by 101, not by the user clicking My requests
    try:
        await dispatcher.feed_update(
            bot, Update(update_id=30, callback_query=callback(202, "ui:status", 50))
        )
        page = session.requests[-1]
        assert isinstance(page, SendMessage)
        assert "No requests" in page.text
        await dispatcher.feed_update(
            bot, Update(update_id=31, callback_query=callback(202, "ui:refresh", session.next_id))
        )
        assert isinstance(session.requests[-1], EditMessageText)
        assert "No requests" in session.requests[-1].text
    finally:
        await bot.session.close()


async def test_help_and_queue_are_styled_with_working_buttons(settings, store, client):
    session = RecordingSession()
    bot = Bot(settings.bot_token, session=session)
    service = TransferService(settings, store, client, bot)
    dispatcher = Dispatcher()
    dispatcher.include_router(make_router(settings, store, service))
    try:
        await dispatcher.feed_update(bot, Update(update_id=40, message=message(101, "/start")))
        welcome = session.requests[-1]
        assert welcome.parse_mode == "HTML"
        assert "<b>Telegram Restricted Content Bot</b>" in welcome.text
        assert welcome.reply_markup.inline_keyboard[0][0].callback_data == "ui:status"
        await dispatcher.feed_update(
            bot, Update(update_id=41, message=message(101, "https://t.me/channel/123"))
        )
        job = store.list_for_user(101)[0]
        assert job.status_message_id == session.next_id
        assert "Added to queue" in session.requests[-1].text
        assert (
            session.requests[-1].reply_markup.inline_keyboard[0][0].callback_data
            == f"cancel:{job.id}"
        )
        assert service.wakeup.is_set()
    finally:
        await bot.session.close()
