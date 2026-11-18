import asyncio
import errno
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendDocument, SendVideo
from telethon import errors, types

from telegram_downloader.errors import user_error
from telegram_downloader.transfer import (
    TransferService,
    account_request,
    bot_request,
    caption_text,
    safe_filename,
    text_chunks,
)


def make_service(settings, store, client, bot):
    service = TransferService(settings, store, client, bot)
    service.enqueue(101, 101, "https://t.me/channel/12")
    return service, store.claim_next()


async def test_download_then_upload_original_file_and_cleanup(settings, store, client, bot):
    service, job = make_service(settings, store, client, bot)

    async def upload(**kwargs):
        assert kwargs["chat_id"] == 101
        assert kwargs["caption"] == "Source caption <unparsed>"
        assert kwargs["parse_mode"] is None
        data = b"".join([chunk async for chunk in kwargs["document"].read(bot)])
        assert data == b"payload"

    bot.send_document.side_effect = upload
    await service.execute(job)
    assert store.list_for_user(101)[0].state == "done"
    assert list(settings.downloads_dir.iterdir()) == []


async def test_photo_is_supported_without_document(settings, store, client, bot):
    client.get_messages.return_value.photo = object()
    client.get_messages.return_value.document = None
    service, job = make_service(settings, store, client, bot)
    await service.execute(job)
    assert store.list_for_user(101)[0].state == "done"


@pytest.mark.parametrize(
    ("mime_type", "name", "extension"),
    [
        ("video/mp4", None, ".mp4"),
        ("video/mp4", "clip.bin", ".mp4"),
        (None, "clip.MP4", ".mp4"),
        ("application/octet-stream", "clip.mp4", ".bin"),
    ],
)
async def test_mp4_is_sent_as_playable_video(
    settings, store, client, bot, mime_type, name, extension
):
    message = client.get_messages.return_value
    message.file = SimpleNamespace(
        size=7,
        name=name,
        ext=extension,
        mime_type=mime_type,
        duration=18.75,
        width=720,
        height=1280,
    )
    message.document = SimpleNamespace(
        attributes=[
            types.DocumentAttributeVideo(duration=18.75, w=720, h=1280, supports_streaming=True)
        ]
    )

    async def upload(**kwargs):
        assert kwargs["chat_id"] == 101
        assert kwargs["caption"] == message.message
        assert kwargs["parse_mode"] is None
        assert kwargs["duration"] == 18
        assert kwargs["width"] == 720
        assert kwargs["height"] == 1280
        assert kwargs["supports_streaming"] is True
        assert kwargs["request_timeout"] == settings.transfer_timeout_seconds
        assert b"".join([chunk async for chunk in kwargs["video"].read(bot)]) == b"payload"

    bot.send_video.side_effect = upload
    service, job = make_service(settings, store, client, bot)
    await service.execute(job)
    bot.send_video.assert_awaited_once()
    bot.send_document.assert_not_called()
    assert store.get_job(job.id).state == "done"
    assert not list(settings.downloads_dir.iterdir())


@pytest.mark.parametrize("mime_type", ["video/x-matroska", "video/webm", "image/jpeg"])
async def test_other_formats_keep_document_delivery(settings, store, client, bot, mime_type):
    client.get_messages.return_value.file.mime_type = mime_type
    service, job = make_service(settings, store, client, bot)
    await service.execute(job)
    bot.send_document.assert_awaited_once()
    bot.send_video.assert_not_called()
    assert store.get_job(job.id).state == "done"


async def test_large_video_streams_in_bounded_chunks(settings, store, client, bot):
    from pathlib import Path

    size = 51 * 1024 * 1024
    client.get_messages.return_value.file.size = size
    client.get_messages.return_value.file.name = "video.mp4"
    client.get_messages.return_value.file.ext = ".mp4"

    async def download(message, *, file, progress_callback):
        # Sparse fixture: exercise the actual file reader without a large memory buffer.
        with Path(file).open("wb") as stream:
            stream.truncate(size)
        await progress_callback(size, size)
        return file

    async def upload(**kwargs):
        assert kwargs["duration"] is None
        assert kwargs["supports_streaming"] is None
        seen = 0
        async for chunk in kwargs["video"].read(bot):
            assert len(chunk) <= 256 * 1024
            seen += len(chunk)
        assert seen == size

    client.download_media.side_effect = download
    bot.send_video.side_effect = upload
    service, job = make_service(settings, store, client, bot)
    await service.execute(job)
    assert store.list_for_user(101)[0].state == "done"
    bot.send_video.assert_awaited_once()
    bot.send_document.assert_not_called()
    assert not list(settings.downloads_dir.iterdir())


async def test_private_channel_cache_is_refreshed(settings, store, client, bot):
    channel = client.get_entity.return_value
    client.get_entity.side_effect = [ValueError("Missing access hash"), channel]

    async def dialogs():
        yield SimpleNamespace(id=-1001234567890)

    client.iter_dialogs = dialogs
    service = TransferService(settings, store, client, bot)
    service.enqueue(101, 101, "https://t.me/c/1234567890/12")
    await service.execute(store.claim_next())
    assert store.list_for_user(101)[0].state == "done"
    assert client.get_entity.await_count == 2


@pytest.mark.parametrize(
    "failure",
    [
        errors.ChatForwardsRestrictedError(request=None),
        errors.ChannelPrivateError(request=None),
        errors.AuthKeyUnregisteredError(request=None),
        OSError(errno.ENOSPC, "No space"),
    ],
)
async def test_telegram_and_disk_errors_are_reported_and_cleaned(
    settings, store, client, bot, failure
):
    client.download_media.side_effect = failure
    service, job = make_service(settings, store, client, bot)
    await service.execute(job)
    result = store.list_for_user(101)[0]
    assert result.state == "failed"
    assert result.error == user_error(failure)
    assert list(settings.downloads_dir.iterdir()) == []
    bot.send_document.assert_not_called()


@pytest.mark.parametrize("kind", ["deleted", "no-media", "user-chat", "oversized"])
async def test_rejects_unusable_messages_before_download(settings, store, client, bot, kind):
    if kind == "deleted":
        client.get_messages.return_value = None
    elif kind == "no-media":
        client.get_messages.return_value.photo = None
        client.get_messages.return_value.document = None
        client.get_messages.return_value.message = ""
    elif kind == "user-chat":
        client.get_entity.return_value = types.User(id=333)
    else:
        client.get_messages.return_value.file.size = 2_000_000_001
    service, job = make_service(settings, store, client, bot)
    await service.execute(job)
    assert store.list_for_user(101)[0].state == "failed"
    client.download_media.assert_not_called()


def text_post(client, text="Hello <world>\n🙂 **literal markdown**"):
    client.get_messages.return_value = SimpleNamespace(
        photo=None,
        document=None,
        file=None,
        media=None,
        message=text,
    )
    return text


def delivered_text(bot):
    return [
        call.kwargs
        for call in bot.send_message.await_args_list
        if "text" in call.kwargs and call.kwargs.get("parse_mode") is None
    ]


async def test_text_post_is_sent_without_downloading_or_disk_requirement(
    settings, store, client, bot
):
    text = text_post(client)
    service, job = make_service(settings, store, client, bot)
    with patch("telegram_downloader.transfer.shutil.disk_usage", side_effect=AssertionError):
        await service.execute(job)
    assert store.list_for_user(101)[0].state == "done"
    payload = delivered_text(bot)
    assert len(payload) == 1
    assert payload[0]["text"] == text
    assert payload[0]["chat_id"] == 101
    assert payload[0]["parse_mode"] is None
    assert payload[0]["link_preview_options"].is_disabled is True
    client.download_media.assert_not_called()
    bot.send_document.assert_not_called()
    assert not list(settings.downloads_dir.iterdir())


async def test_text_with_web_preview_does_not_download_preview_photo(settings, store, client, bot):
    text = text_post(client, "A link: https://example.org")
    message = client.get_messages.return_value
    message.media = types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1))
    message.photo = object()
    service, job = make_service(settings, store, client, bot)
    await service.execute(job)
    assert delivered_text(bot)[0]["text"] == text
    assert store.list_for_user(101)[0].state == "done"
    client.download_media.assert_not_called()


@pytest.mark.parametrize(
    "text", ["", "a" * 4096, "a" * 4097, "🙂" * 4096, "x" * 4095 + "🙂\nend", "සිංහල\nதமிழ் 🙂" * 600]
)
def test_text_chunk_boundaries_preserve_content(text):
    chunks = list(text_chunks(text))
    assert "".join(chunks) == text
    assert all(0 < len(chunk.encode("utf-16-le")) <= 8192 for chunk in chunks)


async def test_long_text_delivers_all_parts_in_order(settings, store, client, bot):
    text = text_post(client, "🙂" * 5000 + "\nfinal part")
    service, job = make_service(settings, store, client, bot)
    await service.execute(job)
    payload = delivered_text(bot)
    assert len(payload) == 3
    assert "".join(part["text"] for part in payload) == text
    assert store.list_for_user(101)[0].state == "done"


async def test_text_network_error_does_not_repeat_uncertain_delivery(settings, store, client, bot):
    from aiogram.methods import SendMessage

    text = text_post(client)
    attempts = 0

    async def send(*args, **kwargs):
        nonlocal attempts
        if kwargs.get("text") == text:
            attempts += 1
            raise TelegramNetworkError(
                method=SendMessage(chat_id=101, text=text), message="private-token-url"
            )
        return SimpleNamespace(message_id=88)

    bot.send_message.side_effect = send
    service, job = make_service(settings, store, client, bot)
    await service.execute(job)
    assert attempts == 1
    result = store.list_for_user(101)[0]
    assert result.state == "failed"
    assert "delivery result is unknown" in result.error
    assert "private-token" not in result.error


async def test_partial_text_failure_warns_before_retry(settings, store, client, bot):
    from aiogram.methods import SendMessage

    text_post(client, "a" * 5000)

    async def send(*args, **kwargs):
        if kwargs.get("text") == "a" * 904:
            raise TelegramNetworkError(
                method=SendMessage(chat_id=101, text="a"), message="Disconnected"
            )
        return SimpleNamespace(message_id=88)

    bot.send_message.side_effect = send
    service, job = make_service(settings, store, client, bot)
    await service.execute(job)
    assert "stopped after 1 part" in store.list_for_user(101)[0].error
    assert len(delivered_text(bot)) == 2


async def test_unsupported_media_with_caption_is_not_silently_dropped(settings, store, client, bot):
    text_post(client, "A location")
    client.get_messages.return_value.media = types.MessageMediaGeo(geo=types.GeoPointEmpty())
    service, job = make_service(settings, store, client, bot)
    await service.execute(job)
    assert store.list_for_user(101)[0].state == "failed"
    assert not delivered_text(bot)


async def test_disk_capacity_is_checked_before_download(settings, store, client, bot):
    service, job = make_service(settings, store, client, bot)
    with patch(
        "telegram_downloader.transfer.shutil.disk_usage", return_value=SimpleNamespace(free=1)
    ):
        await service.execute(job)
    assert "disk space" in store.list_for_user(101)[0].error
    client.download_media.assert_not_called()


async def test_unknown_size_is_bounded_during_download(settings, store, client, bot):
    settings = replace(settings, max_file_bytes=5)
    client.get_messages.return_value.file.size = None
    service, job = make_service(settings, store, client, bot)
    await service.execute(job)
    assert store.list_for_user(101)[0].state == "failed"
    bot.send_document.assert_not_called()
    assert not list(settings.downloads_dir.iterdir())


async def test_network_failure_after_upload_is_not_automatically_retried(
    settings, store, client, bot
):
    method = SendDocument(chat_id=101, document="file")
    bot.send_document.side_effect = TelegramNetworkError(method=method, message="secret-token-url")
    service, job = make_service(settings, store, client, bot)
    await service.execute(job)
    bot.send_document.assert_awaited_once()
    assert "result is unknown" in store.list_for_user(101)[0].error
    assert "secret" not in store.list_for_user(101)[0].error
    assert not list(settings.downloads_dir.iterdir())


async def test_video_network_failure_does_not_resend_or_fall_back(settings, store, client, bot):
    client.get_messages.return_value.file.ext = ".mp4"
    client.get_messages.return_value.file.name = "video.mp4"
    bot.send_video.side_effect = TelegramNetworkError(
        method=SendVideo(chat_id=101, video="file"), message="secret-token-url"
    )
    service, job = make_service(settings, store, client, bot)
    await service.execute(job)
    bot.send_video.assert_awaited_once()
    bot.send_document.assert_not_called()
    result = store.get_job(job.id)
    assert result.state == "failed"
    assert "result is unknown" in result.error
    assert "secret" not in result.error
    assert not list(settings.downloads_dir.iterdir())


async def test_cancellation_cleans_partial_file_and_worker_continues(settings, store, client, bot):
    started = asyncio.Event()
    original = client.download_media.side_effect

    async def blocked(message, *, file, progress_callback):
        from pathlib import Path

        Path(file).write_bytes(b"partial")
        started.set()
        await asyncio.Event().wait()

    client.download_media.side_effect = blocked
    service = TransferService(settings, store, client, bot)
    first = service.enqueue(101, 101, "https://t.me/channel/12")
    runner = asyncio.create_task(service.run())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert service.cancel(202, first.id) == []
        assert service.cancel(101, first.id) == [first.id]
        # Let cancellation finish before scheduling a second request.
        for _ in range(100):
            if service.active_id is None:
                break
            await asyncio.sleep(0.01)
        assert not list(settings.downloads_dir.iterdir())
        client.download_media.side_effect = original
        service.enqueue(202, 202, "https://t.me/channel/13")
        for _ in range(100):
            if store.list_for_user(202)[0].state == "done":
                break
            await asyncio.sleep(0.01)
        assert store.list_for_user(101)[0].state == "cancelled"
        assert store.list_for_user(202)[0].state == "done"
    finally:
        await service.stop()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


async def test_restart_cleanup_and_notification(settings, store, client, bot):
    service, job = make_service(settings, store, client, bot)
    stale = settings.downloads_dir / f"job-{job.id}-old"
    stale.mkdir()
    (stale / "partial").write_bytes(b"old")
    await service.recover()
    assert store.list_for_user(101)[0].state == "interrupted"
    assert not stale.exists()
    assert "restart" in bot.send_message.call_args.args[1]


async def test_upload_rate_limit_honors_retry_after():
    method = SendDocument(chat_id=101, document="file")
    operation = AsyncMock(
        side_effect=[
            TelegramRetryAfter(method=method, message="flood", retry_after=2),
            "sent",
        ]
    )
    with patch("telegram_downloader.transfer.asyncio.sleep", new_callable=AsyncMock) as sleep:
        assert await bot_request(operation, upload=True) == "sent"
    sleep.assert_awaited_once_with(3)


async def test_download_flood_wait_honors_telegram_delay():
    operation = AsyncMock(
        side_effect=[errors.FloodWaitError(request=None, capture=2), "downloaded"]
    )
    with patch("telegram_downloader.transfer.asyncio.sleep", new_callable=AsyncMock) as sleep:
        assert await account_request(operation) == "downloaded"
    sleep.assert_awaited_once_with(3)


def test_caption_and_filename_safety():
    assert len(caption_text("🙂" * 1000).encode("utf-16-le")) <= 2048
    assert safe_filename("../../secret.txt", ".txt") == "secret.txt"
    assert safe_filename("C:\\secrets\\file.bin", ".bin") == "file.bin"
    assert safe_filename("..", ".jpg") == "media.jpg"
    assert len(safe_filename("🙂" * 1000 + ".mp4", ".mp4").encode()) <= 200
    assert len(safe_filename("file." + "🙂" * 1000, None).encode()) <= 200
    assert safe_filename("README", ".txt") == "README.txt"
    assert safe_filename("..", "../../escape") == "media.bin"


async def test_download_retry_removes_partial_file(settings, store, client, bot):
    from pathlib import Path

    attempts = 0

    async def download(message, *, file, progress_callback):
        nonlocal attempts
        attempts += 1
        path = Path(file)
        assert not path.exists()
        path.write_bytes(b"payload")
        if attempts == 1:
            raise ConnectionError("Disconnected")
        return file

    client.download_media.side_effect = download
    service, job = make_service(settings, store, client, bot)
    with patch("telegram_downloader.transfer.asyncio.sleep", new_callable=AsyncMock):
        await service.execute(job)
    assert attempts == 2
    assert store.list_for_user(101)[0].state == "done"
    assert not list(settings.downloads_dir.iterdir())


async def test_graceful_shutdown_is_recovered_with_notification(settings, store, client, bot):
    started = asyncio.Event()

    async def download(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    client.download_media.side_effect = download
    service = TransferService(settings, store, client, bot)
    service.enqueue(101, 101, "https://t.me/channel/12")
    runner = asyncio.create_task(service.run())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await service.stop()
        await asyncio.gather(runner, return_exceptions=True)
        assert not list(settings.downloads_dir.iterdir())
        recovered = TransferService(settings, store, client, bot)
        await recovered.recover()
        assert store.list_for_user(101)[0].state == "interrupted"
        assert "restart" in bot.edit_message_text.call_args.args[0]
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
