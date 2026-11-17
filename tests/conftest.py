import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from telethon import types

from telegram_downloader.config import Settings
from telegram_downloader.store import JobStore


@pytest.fixture
def settings(tmp_path):
    config = Settings(
        api_id=12345,
        api_hash="a" * 32,
        bot_token="12345:" + "a" * 35,
        allowed_user_ids=frozenset({101, 202}),
        data_dir=tmp_path,
        bot_api_base_url="http://127.0.0.1:8081",
        progress_interval_seconds=3600,
    )
    config.prepare_directories()
    return config


@pytest.fixture
def store(settings):
    database = JobStore(settings.jobs_path)
    yield database
    database.close()


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


@pytest.fixture
def bot():
    return SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=88)),
        edit_message_text=AsyncMock(),
        send_document=AsyncMock(),
    )


@pytest.fixture
def client():
    channel = types.Channel(id=555, title="Test", photo=types.ChatPhotoEmpty(), date=None)
    message = SimpleNamespace(
        photo=None,
        document=object(),
        file=SimpleNamespace(size=7, name="test.bin", ext=".bin"),
        message="Source caption <unparsed>",
    )

    async def download(message, *, file, progress_callback):
        from pathlib import Path

        Path(file).write_bytes(b"payload")
        await progress_callback(7, 7)
        return file

    return SimpleNamespace(
        get_entity=AsyncMock(return_value=channel),
        get_messages=AsyncMock(return_value=message),
        download_media=AsyncMock(side_effect=download),
    )
