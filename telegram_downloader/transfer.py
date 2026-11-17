from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import FSInputFile, LinkPreviewOptions
from telethon import TelegramClient, errors, types

from .config import Settings
from .errors import SourceAccessError, TransferError, user_error
from .joins import JoinService
from .links import parse_message_link
from .presentation import StatusCards
from .requests import account_request, bot_request
from .store import Job, JobStore

logger = logging.getLogger(__name__)


def is_text_post(message) -> bool:
    return bool(getattr(message, "message", "")) and (
        isinstance(getattr(message, "media", None), types.MessageMediaWebPage)
        or (
            getattr(message, "media", None) is None
            and not (getattr(message, "photo", None) or getattr(message, "document", None))
        )
    )


def text_chunks(text: str):
    """Keep every character while respecting Telegram's limit and UTF-16 boundaries."""
    chunk, units = [], 0
    for character in text:
        width = 2 if ord(character) > 0xFFFF else 1
        if units + width > 4096:
            yield "".join(chunk)
            chunk, units = [], 0
        chunk.append(character)
        units += width
    if chunk:
        yield "".join(chunk)


def caption_text(text: str | None) -> str | None:
    if not text:
        return None
    # Telegram counts UTF-16 code units. Never split a surrogate pair.
    return text.encode("utf-16-le")[:2048].decode("utf-16-le", errors="ignore")


def safe_filename(name: str | None, extension: str | None) -> str:
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if c.isprintable() and c not in "\x00/")
    if name in {"", ".", ".."}:
        name = "media"
    # Bound encoded filename length for common filesystems, preserving ordinary extensions.
    full_suffix = Path(name).suffix
    stem = name[: -len(full_suffix)] if full_suffix else name
    if not full_suffix:
        ext = extension or ".bin"
        full_suffix = (
            ext
            if ext.startswith(".") and all(c.isascii() and (c.isalnum() or c == ".") for c in ext)
            else ".bin"
        )
    suffix = full_suffix.encode("utf-8")[:20].decode("utf-8", errors="ignore")
    stem = stem.encode("utf-8")[:180].decode("utf-8", errors="ignore")
    return (stem or "media") + suffix


class Progress:
    def __init__(self, cards: StatusCards, job: Job, interval: int):
        self.cards = cards
        self.job = job
        self.interval = interval
        self.filename: str | None = None
        self.last_update = 0.0

    async def update(self, stage: str, current: int = 0, total: int = 0, *, force=False):
        now = time.monotonic()
        if not force and now - self.last_update < self.interval:
            return
        self.last_update = now
        try:
            # Progress is best effort; rate limits should never stall the media stream.
            await self.cards.show(
                self.job, stage=stage, current=current, total=total, filename=self.filename
            )
        except TelegramRetryAfter as exc:
            self.last_update = now + exc.retry_after
        except (TelegramAPIError, TelegramNetworkError):
            pass


class ProgressFile(FSInputFile):
    def __init__(self, path: Path, progress: Progress):
        super().__init__(path, chunk_size=256 * 1024)
        self.progress = progress
        self.size = path.stat().st_size

    async def read(self, bot: Bot):
        sent = 0
        async for chunk in super().read(bot):
            sent += len(chunk)
            await self.progress.update("uploading", sent, self.size)
            yield chunk


class TransferService:
    def __init__(self, settings: Settings, store: JobStore, client: TelegramClient, bot: Bot):
        self.settings = settings
        self.store = store
        self.client = client
        self.bot = bot
        self.wakeup = asyncio.Event()
        self.active_id: int | None = None
        self.active_task: asyncio.Task | None = None
        self.stopping = False
        self.cards = StatusCards(bot, store)
        self.joins = JoinService(settings, store, client, self.notify, self.wakeup)

    def enqueue(self, user_id: int, chat_id: int, link: str, *, start: bool = True) -> Job:
        parse_message_link(link)
        job = self.store.enqueue(user_id, chat_id, link, self.settings.max_pending_per_user)
        if start:
            self.wakeup.set()
        return job

    def cancel(self, user_id: int, job_id: int | None = None) -> list[int]:
        ids = self.store.cancel(user_id, job_id)
        if self.active_id in ids and self.active_task is not None:
            self.active_task.cancel()
        self.joins.wakeup.set()
        return ids

    async def notify(self, job: Job, text: str) -> None:
        try:
            detail = text.removeprefix(f"Job #{job.id}").lstrip(": ")
            detail = detail.removeprefix("failed: ")
            current = self.store.get_job(job.id)
            await bot_request(
                lambda: self.cards.show(
                    job, detail=detail, expected_state=current.state if current else None
                )
            )
        except (TelegramAPIError, TelegramNetworkError):
            logger.warning("Could not notify requester for job %s", job.id)

    async def recover(self) -> None:
        interrupted = self.store.recover()
        # Only directories generated by this service; do not follow symlinks.
        for directory in self.settings.downloads_dir.glob("job-*"):
            if directory.is_dir() and not directory.is_symlink():
                await asyncio.to_thread(shutil.rmtree, directory)
        for job in interrupted:
            if job.user_id in self.settings.allowed_user_ids:
                await self.notify(
                    job,
                    f"Job #{job.id} was interrupted by a restart. Check this chat, then "
                    "send the link again if the file did not arrive.",
                )

    async def run(self) -> None:
        while not self.stopping:
            self.wakeup.clear()
            job = self.store.claim_next()
            if job is None:
                await self.wakeup.wait()
                continue
            # No await between claiming a job and registering its cancellable task.
            self.active_id = job.id
            self.active_task = asyncio.create_task(self.execute(job))
            try:
                await self.active_task
            except asyncio.CancelledError:
                if self.stopping or asyncio.current_task().cancelling():
                    raise
            finally:
                self.active_id = None
                self.active_task = None

    async def stop(self) -> None:
        self.stopping = True
        self.wakeup.set()
        if self.active_task is not None:
            self.active_task.cancel()
            await asyncio.gather(self.active_task, return_exceptions=True)

    async def resolve_message(self, link: str):
        parsed = parse_message_link(link)
        try:
            try:
                entity = await self.client.get_entity(parsed.peer)
            except ValueError:
                if not isinstance(parsed.peer, int):
                    raise
                # Newly logged-in sessions may lack private-channel access hashes.
                async for dialog in self.client.iter_dialogs():
                    if dialog.id == parsed.peer:
                        break
                entity = await self.client.get_entity(parsed.peer)
        except (
            ValueError,
            errors.ChannelPrivateError,
            errors.ChatAdminRequiredError,
            errors.UserNotParticipantError,
        ):
            raise SourceAccessError() from None
        if not isinstance(entity, (types.Channel, types.Chat)):
            raise TransferError("Only channel and group messages are supported.")
        try:
            message = await self.client.get_messages(entity, ids=parsed.message_id)
        except (
            errors.ChannelPrivateError,
            errors.ChatAdminRequiredError,
            errors.UserNotParticipantError,
        ):
            raise SourceAccessError() from None
        if message is None or isinstance(message, types.MessageEmpty):
            raise TransferError("The source message is missing, deleted, or inaccessible.")
        if not is_text_post(message) and (
            not (getattr(message, "photo", None) or getattr(message, "document", None))
            or getattr(message, "file", None) is None
        ):
            raise TransferError(
                "This message has no supported text, photo, video, audio, or document."
            )
        return message

    async def send_text(self, job: Job, message, progress: Progress) -> None:
        self.store.set_state(job.id, "uploading")
        await progress.update("sending text", force=True)
        delivered = 0
        try:
            for chunk in text_chunks(message.message):
                await bot_request(
                    lambda part=chunk: self.bot.send_message(
                        chat_id=job.chat_id,
                        text=part,
                        parse_mode=None,
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    ),
                    # Message sends, like file uploads, must not be repeated after a lost reply.
                    upload=True,
                )
                delivered += 1
        except Exception as exc:
            if delivered:
                raise TransferError(
                    f"Text delivery stopped after {delivered} part(s). "
                    "Check this chat before retrying; earlier parts have already arrived. "
                    + user_error(exc)
                ) from None
            raise

    async def execute(self, job: Job) -> None:
        progress = Progress(self.cards, job, self.settings.progress_interval_seconds)
        try:
            async with asyncio.timeout(self.settings.transfer_timeout_seconds):
                await progress.update("reading post", force=True)
                try:
                    message = await account_request(lambda: self.resolve_message(job.link))
                except SourceAccessError:
                    if await self.joins.defer(job):
                        await progress.update("awaiting admin approval", force=True)
                        return
                    raise
                if is_text_post(message):
                    await self.send_text(job, message, progress)
                else:
                    await self.send_media(job, message, progress)
            self.store.set_state(job.id, "done")
            await progress.update("done", force=True)
        except asyncio.CancelledError:
            if not self.stopping:
                self.store.set_state(job.id, "cancelled")
            raise
        except Exception as exc:
            text = user_error(exc)
            self.store.set_state(job.id, "failed", text)
            logger.warning("Job %s failed (%s)", job.id, type(exc).__name__)
            await self.notify(job, f"Job #{job.id} failed: {text}")

    async def send_media(self, job: Job, message, progress: Progress) -> None:
        directory: Path | None = None
        try:
            progress.filename = message.file.name or "Original file"
            size = message.file.size or 0
            if size > self.settings.max_file_bytes:
                raise TransferError("This file exceeds the configured upload size limit.")
            free = shutil.disk_usage(self.settings.downloads_dir).free
            if free < size + self.settings.min_free_disk_bytes:
                raise TransferError("The server has insufficient disk space for this download.")
            directory = Path(
                tempfile.mkdtemp(prefix=f"job-{job.id}-", dir=self.settings.downloads_dir)
            )
            filename = safe_filename(message.file.name, message.file.ext)
            destination = directory / filename

            async def download_progress(current: int, total: int):
                if max(current, total) > self.settings.max_file_bytes:
                    raise TransferError("This file exceeds the configured upload size limit.")
                if shutil.disk_usage(directory).free < self.settings.min_free_disk_bytes:
                    raise TransferError("The server ran out of available disk space.")
                await progress.update("downloading", current, total)

            async def download():
                # A retry begins from zero; never append to an interrupted download.
                if destination.exists():
                    destination.unlink()
                return await self.client.download_media(
                    message, file=str(destination), progress_callback=download_progress
                )

            result = await account_request(download)
            if not result:
                raise TransferError("Telegram did not provide a downloadable file for this post.")
            downloaded = Path(result).resolve()
            if downloaded.parent != directory.resolve() or not downloaded.is_file():
                raise TransferError("The download did not produce a valid file.")
            if downloaded.stat().st_size > self.settings.max_file_bytes:
                raise TransferError("This file exceeds the configured upload size limit.")
            if not downloaded.stat().st_size:
                raise TransferError("Telegram returned an empty file.")
            self.store.set_state(job.id, "uploading")
            await progress.update("uploading", force=True)

            async def upload():
                return await self.bot.send_document(
                    chat_id=job.chat_id,
                    document=ProgressFile(downloaded, progress),
                    caption=caption_text(message.message),
                    parse_mode=None,
                    disable_content_type_detection=True,
                    request_timeout=self.settings.transfer_timeout_seconds,
                )

            await bot_request(upload, upload=True)
        finally:
            if directory is not None:
                await asyncio.to_thread(shutil.rmtree, directory, True)
