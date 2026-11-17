from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    TelegramObject,
)

from .config import Settings
from .errors import TransferError
from .links import InvalidLink, parse_invite_link
from .presentation import HELP, home_buttons, notice, status_card
from .store import JobStore, QueueFull
from .transfer import TransferService, bot_request

logger = logging.getLogger(__name__)


async def answer(
    message: Message, text: str, markup: InlineKeyboardMarkup | None = None
) -> Message:
    return await bot_request(
        lambda: message.answer(
            text,
            parse_mode="HTML",
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    )


async def edit(message: Message, message_id: int, text: str, markup=None) -> None:
    try:
        await bot_request(
            lambda: message.bot.edit_message_text(
                text,
                chat_id=message.chat.id,
                message_id=message_id,
                parse_mode="HTML",
                reply_markup=markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in exc.message.lower():
            raise


class CallbackAccessMiddleware(BaseMiddleware):
    def __init__(self, allowed: frozenset[int]):
        self.allowed = allowed

    async def __call__(self, handler, event: CallbackQuery, data):
        try:
            if (
                event.from_user.id not in self.allowed
                or not isinstance(event.message, Message)
                or event.message.chat.type != "private"
            ):
                await event.answer("This bot is private.", show_alert=True)
                return None
            return await handler(event, data)
        except (TelegramAPIError, TelegramNetworkError) as exc:
            logger.warning("Could not handle a button (%s)", type(exc).__name__)
            return None


class AccessMiddleware(BaseMiddleware):
    def __init__(self, allowed: frozenset[int]):
        self.allowed = allowed

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        # Apply before filters so no command, status lookup or enqueue can bypass access.
        if event.chat.type != "private" or event.from_user is None:
            return None
        if event.from_user.id not in self.allowed:
            try:
                await answer(
                    event, "This bot is private. Ask its owner to add your Telegram user ID."
                )
            except (TelegramAPIError, TelegramNetworkError):
                pass
            return None
        try:
            return await handler(event, data)
        except (TelegramAPIError, TelegramNetworkError) as exc:
            logger.warning("Could not reply to a bot command (%s)", type(exc).__name__)
            return None


def make_router(settings: Settings, store: JobStore, service: TransferService) -> Router:
    router = Router()
    router.message.outer_middleware(AccessMiddleware(settings.allowed_user_ids))
    router.callback_query.outer_middleware(CallbackAccessMiddleware(settings.allowed_user_ids))

    @router.message(Command("start", "help"))
    async def help_command(message: Message):
        await answer(message, HELP, home_buttons())

    @router.message(Command("status"))
    async def status_command(message: Message):
        jobs = store.list_for_user(message.from_user.id)
        await answer(message, status_card(jobs), home_buttons(refresh=True))

    @router.message(Command("cancel"))
    async def cancel_command(message: Message):
        parts = message.text.split()
        if len(parts) > 2 or (len(parts) == 2 and not parts[1].isascii()):
            await answer(message, "Use /cancel or /cancel JOB_ID.")
            return
        try:
            job_id = int(parts[1]) if len(parts) == 2 else None
            if job_id is not None and not 0 < job_id <= 2**63 - 1:
                raise ValueError
        except ValueError:
            await answer(message, "Use /cancel or /cancel JOB_ID.")
            return
        ids = service.cancel(message.from_user.id, job_id)
        for cancelled_id in ids:
            await service.cards.show(store.get_job(cancelled_id))
        await answer(
            message,
            (
                "🛑 <b>Cancelled</b>\n\n"
                + ", ".join(f"#{job_id}" for job_id in ids)
                + "\n\nContent already accepted by Telegram may still arrive. "
                "Channel membership is unchanged."
            )
            if ids
            else notice("📋 Nothing to cancel", "No matching pending requests in your account."),
        )

    @router.message(F.text)
    async def link_message(message: Message):
        if message.text.startswith("/"):
            await answer(message, "Unknown command. Use /help.")
            return
        try:
            invite = parse_invite_link(message.text)
            if invite is not None:
                card = await answer(
                    message, notice("🔎 Checking invite", "Connecting to the channel…")
                )
                try:
                    result = await service.joins.join(message.from_user.id, invite.invite_hash)
                except TransferError as exc:
                    await edit(
                        message,
                        card.message_id,
                        notice("⚠️ Could not join", str(exc)),
                        home_buttons(),
                    )
                    return
                state = store.get_invite(invite.invite_hash).state
                title = "✅ Channel connected" if state == "joined" else "👥 Join request sent"
                await edit(message, card.message_id, notice(title, result), home_buttons())
                return
            job = service.enqueue(
                message.from_user.id, message.chat.id, message.text.strip(), start=False
            )
        except (InvalidLink, QueueFull, TransferError) as exc:
            await answer(message, notice("⚠️ Request not added", str(exc)), home_buttons())
            return
        try:
            await service.cards.show(job)
        finally:
            service.wakeup.set()

    @router.callback_query(F.data.in_({"ui:status", "ui:refresh", "ui:help"}))
    async def navigation(callback: CallbackQuery):
        await callback.answer()
        if callback.data == "ui:help":
            await answer(callback.message, HELP, home_buttons())
            return
        text = status_card(store.list_for_user(callback.from_user.id))
        if callback.data == "ui:refresh":
            await edit(
                callback.message, callback.message.message_id, text, home_buttons(refresh=True)
            )
        else:
            await answer(callback.message, text, home_buttons(refresh=True))

    @router.callback_query(F.data.startswith("cancel:"))
    async def cancel_button(callback: CallbackQuery):
        raw_id = callback.data.partition(":")[2]
        if not raw_id.isascii() or not raw_id.isdigit() or len(raw_id) > 19:
            await callback.answer("This action is unavailable.")
            return
        job_id = int(raw_id)
        job = store.get_job(job_id) if 0 < job_id <= 2**63 - 1 else None
        if (
            job is None
            or job.user_id != callback.from_user.id
            or job.chat_id != callback.message.chat.id
            or job.status_message_id != callback.message.message_id
        ):
            await callback.answer("This request is unavailable. Use /status.", show_alert=True)
            return
        cancelled = service.cancel(callback.from_user.id, job_id)
        await callback.answer(
            "Request cancelled." if cancelled else "This request has already finished."
        )
        await service.cards.show(job)

    @router.callback_query()
    async def unknown_button(callback: CallbackQuery):
        await callback.answer("This button has expired. Use /help.")

    @router.message()
    async def unsupported(message: Message):
        await answer(
            message, "Send a Telegram invite or post link as text. Use /help for examples."
        )

    return router
