from __future__ import annotations

import asyncio
from html import escape
from weakref import WeakValueDictionary

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from .store import PENDING, Job, JobStore

STATES = {
    "queued": ("⏳", "Added to queue", "Your request will start shortly."),
    "reading post": ("🔎", "Reading your post", "Checking the source…"),
    "downloading": ("⬇️", "Downloading", "Saving the original file…"),
    "uploading": ("⬆️", "Sending file", "Delivering it to this chat…"),
    "sending text": ("💬", "Sending text", "Copying the post to this chat…"),
    "awaiting_approval": (
        "👥",
        "Waiting for approval",
        "I’ll resume automatically after "
        "an admin approves access, for up to 24 hours from your request.",
    ),
    "done": ("✅", "Delivered", "Your content is ready in this chat."),
    "failed": ("⚠️", "Request could not be completed", "Please check the source and try again."),
    "cancelled": (
        "🛑",
        "Cancelled",
        "This request has stopped. Content already accepted by "
        "Telegram may still arrive. Channel membership is unchanged.",
    ),
    "expired": ("⌛", "Approval wait expired", "Send the invite and post again to retry."),
    "interrupted": (
        "↻",
        "Interrupted by restart",
        "Check this chat, then resend the post if it did not arrive.",
    ),
}

HELP = (
    "📥 <b>Your media helper</b>\n\n"
    "Send a <b>Telegram post link</b> to get its text or original file.\n\n"
    "🔐 <b>From a private channel?</b>\n"
    "<b>1.</b> Send the channel’s invite link.\n"
    "<b>2.</b> I join or request admin approval.\n"
    "<b>3.</b> Send the post link — I’ll take it from there.\n\n"
    "<b>Supported</b>\n"
    "💬 Text · 🖼 Photos · 🎬 Videos · 📎 Files up to 2 GB\n\n"
    "I wait up to 24 hours for approval. Telegram may block protected posts.\n"
    "Use the buttons below, or /status and /cancel."
)


def notice(title: str, detail: str) -> str:
    return f"<b>{escape(title)}</b>\n\n{escape(detail)}"


def home_buttons(*, refresh: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↻ Refresh" if refresh else "📋 My requests",
                    callback_data="ui:refresh" if refresh else "ui:status",
                ),
                InlineKeyboardButton(text="ℹ️ Help", callback_data="ui:help"),
            ]
        ]
    )


def job_buttons(job: Job) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="↗ Open post", url=job.link)]]
    if job.state in PENDING:
        rows[0].insert(0, InlineKeyboardButton(text="✕ Cancel", callback_data=f"cancel:{job.id}"))
    rows.append([InlineKeyboardButton(text="📋 My requests", callback_data="ui:status")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def byte_size(value: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    raise AssertionError("Unreachable")


def job_card(
    job: Job,
    *,
    stage: str | None = None,
    detail: str | None = None,
    current: int = 0,
    total: int = 0,
    filename: str | None = None,
) -> str:
    # Old progress callbacks must not paint over a terminal or waiting state.
    stage = stage if job.state in ("queued", "downloading", "uploading") else job.state
    icon, title, hint = STATES.get(stage or job.state, STATES["queued"])
    lines = [f"{icon} <b>{title}</b>", f"Request <code>#{job.id}</code>"]
    if filename:
        lines.append(escape(filename[:160]))
    lines.append("")
    if total > 0 and stage in ("downloading", "uploading"):
        percent = min(100, max(0, current * 100 // total))
        filled = percent // 10
        lines.extend(
            [
                f"<code>{'▰' * filled}{'▱' * (10 - filled)} {percent}%</code>",
                f"{byte_size(max(0, current))} / {byte_size(total)}",
                "",
            ]
        )
    lines.append(escape(detail or job.error or hint))
    return "\n".join(lines)


def status_card(jobs: list[Job]) -> str:
    if not jobs:
        return notice(
            "📋 Your requests", "No requests yet. Send a Telegram post link to get started."
        )
    lines = ["📋 <b>Your recent requests</b>", ""]
    for job in jobs:
        icon, title, _ = STATES.get(job.state, STATES["queued"])
        lines.append(f"{icon} <code>#{job.id}</code> · {escape(title)}")
        if job.error:
            lines.append(escape(job.error[:160]))
        lines.append("")
    lines.append("Use /cancel followed by the request number to stop a pending request.")
    return "\n".join(lines)


class StatusCards:
    """Serialize edits to each persistent status card, including handler/worker races."""

    def __init__(self, bot: Bot, store: JobStore):
        self.bot, self.store = bot, store
        self.locks = WeakValueDictionary()

    async def show(self, job: Job, **view) -> None:
        expected_state = view.pop("expected_state", None)
        lock = self.locks.get(job.id)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[job.id] = lock
        async with lock:
            job = self.store.get_job(job.id)
            if job is None:
                return
            if expected_state is not None and job.state != expected_state:
                view.pop("detail", None)
            text, buttons = job_card(job, **view), job_buttons(job)
            if job.status_message_id is not None:
                try:
                    await self.bot.edit_message_text(
                        text,
                        chat_id=job.chat_id,
                        message_id=job.status_message_id,
                        parse_mode="HTML",
                        reply_markup=buttons,
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    )
                    return
                except TelegramBadRequest as exc:
                    reason = exc.message.lower()
                    if "message is not modified" in reason:
                        return
                    if "message to edit not found" not in reason:
                        raise
            message = await self.bot.send_message(
                job.chat_id,
                text,
                parse_mode="HTML",
                reply_markup=buttons,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            self.store.save_status_message(job.id, message.message_id)
