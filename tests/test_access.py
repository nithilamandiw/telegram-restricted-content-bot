from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Chat, Message, Update, User

from telegram_downloader.handlers import AccessMiddleware, make_router
from telegram_downloader.transfer import TransferService


def message(user_id=101, text="/status", chat_type="private"):
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        text=text,
        chat=Chat(id=user_id, type=chat_type),
        from_user=User(id=user_id, is_bot=False, first_name="Test"),
    )


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
async def test_non_private_chat_is_ignored(chat_type):
    handler = AsyncMock()
    await AccessMiddleware(frozenset({101}))(handler, message(chat_type=chat_type), {})
    handler.assert_not_called()


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.next_id = 100

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, SendMessage):
            self.next_id += 1
            return message(user_id=method.chat_id, text=method.text).model_copy(
                update={"message_id": self.next_id}
            )
        if isinstance(method, EditMessageText):
            return message(user_id=method.chat_id, text=method.text).model_copy(
                update={"message_id": method.message_id}
            )
        if isinstance(method, AnswerCallbackQuery):
            return True
        raise AssertionError(type(method).__name__)

    async def stream_content(self, *args, **kwargs):
        yield b""


async def test_all_commands_and_downloads_enforce_allowlist(settings, store, client):
    session = RecordingSession()
    bot = Bot(settings.bot_token, session=session)
    service = TransferService(settings, store, client, bot)
    dispatcher = Dispatcher()
    dispatcher.include_router(make_router(settings, store, service))
    owner_job = service.enqueue(101, 101, "https://t.me/channel/1")
    try:
        for index, text in enumerate(
            [
                "/start",
                "/help",
                "/status",
                f"/cancel {owner_job.id}",
                "https://t.me/channel/2",
            ]
        ):
            await dispatcher.feed_update(bot, Update(update_id=index, message=message(999, text)))
        assert len(session.requests) == 5
        assert all("private" in request.text for request in session.requests)
        assert store.list_for_user(999) == []
        assert store.list_for_user(101)[0].state == "queued"
        session.requests.clear()
        await dispatcher.feed_update(bot, Update(update_id=20, message=message(202, "/status")))
        assert "No requests" in session.requests[-1].text
        await dispatcher.feed_update(bot, Update(update_id=21, message=message(101, "/status")))
        assert f"#{owner_job.id}" in session.requests[-1].text
        assert "Added to queue" in session.requests[-1].text
        await dispatcher.feed_update(
            bot, Update(update_id=22, message=message(101, "https://t.me/channel/3"))
        )
        assert len(store.list_for_user(101)) == 2
        await dispatcher.feed_update(
            bot, Update(update_id=23, message=message(202, f"/cancel {owner_job.id}"))
        )
        assert "No matching" in session.requests[-1].text
        await dispatcher.feed_update(
            bot, Update(update_id=24, message=message(101, "/cancel 9999999999999999999999"))
        )
        assert "Use /cancel" in session.requests[-1].text
    finally:
        await bot.session.close()
