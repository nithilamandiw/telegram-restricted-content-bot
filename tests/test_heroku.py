import asyncio
import stat
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession, StringSession
from test_config import env

from telegram_downloader import heroku
from telegram_downloader import main as bot_main
from telegram_downloader.config import Settings
from telegram_downloader.main import account_client, export_session


def dummy_session():
    session = StringSession()
    session.set_dc(2, "149.154.167.50", 443)
    session.auth_key = AuthKey(b"x" * 256)
    return session.save()


def test_config_accepts_cloud_storage_and_hides_cloud_secrets(monkeypatch):
    env(monkeypatch)
    value = dummy_session()
    monkeypatch.setenv("TELEGRAM_SESSION", value)
    monkeypatch.setenv("DATABASE_URL", "postgres://user:secret@localhost/telegram")
    config = Settings.load()
    assert config.telegram_session == value
    assert config.database_url.startswith("postgres://")
    assert value not in repr(config)
    assert "user:secret" not in repr(config)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DATABASE_URL", "sqlite:///tmp/jobs.sqlite3"),
        ("TELEGRAM_SESSION", "do-not-print-this-secret"),
    ],
)
def test_invalid_cloud_config_is_rejected_without_echoing_secrets(monkeypatch, name, value):
    env(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as error:
        Settings.load()
    assert value not in str(error.value)


def test_string_session_does_not_require_a_session_file(settings):
    client = account_client(replace(settings, telegram_session=dummy_session()))
    try:
        assert isinstance(client.session, StringSession)
        assert not settings.session_path.exists()
    finally:
        client.session.close()


def test_export_writes_a_private_reusable_session_without_network(settings, capsys):
    session = SQLiteSession(str(settings.session_path))
    session.set_dc(2, "149.154.167.50", 443)
    session.auth_key = AuthKey(b"x" * 256)
    session.close()
    destination = settings.data_dir / "telegram-session.txt"
    export_session(settings, destination)
    value = destination.read_text().strip()
    assert StringSession(value).auth_key.key == b"x" * 256
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert value not in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        export_session(settings, destination)


def test_export_requires_an_existing_login(settings):
    with pytest.raises(RuntimeError, match="terminal login"):
        export_session(settings, settings.data_dir / "export.txt")


async def test_local_api_migration_logs_out_only_the_bot(settings, monkeypatch):
    bot = SimpleNamespace(
        delete_webhook=AsyncMock(),
        log_out=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )
    monkeypatch.setattr(bot_main, "make_bot", lambda settings: bot)
    await bot_main.logout_local(settings)
    bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
    bot.log_out.assert_awaited_once()
    bot.session.close.assert_awaited_once()


class FakeServer:
    def __init__(self):
        self.returncode = None
        self.exited = asyncio.Event()
        self.terminated = False
        self.killed = False

    async def wait(self):
        await self.exited.wait()
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self.exited.set()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.exited.set()


def cloud_settings(settings):
    return replace(
        settings,
        database_url="postgresql://localhost/telegram_test",
        telegram_session=dummy_session(),
    )


async def test_worker_handles_shutdown_and_uses_loopback_without_credential_arguments(
    settings, monkeypatch
):
    process = FakeServer()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(heroku.asyncio, "create_subprocess_exec", spawn)
    started, cleaned, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def app(config, *, handle_signals):
        assert handle_signals is False
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(heroku, "serve", app)
    task = asyncio.create_task(heroku.run(cloud_settings(settings), stop=stop))
    await asyncio.wait_for(started.wait(), timeout=2)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert cleaned.is_set()
    assert process.terminated
    assert "--http-ip-address=127.0.0.1" in spawn.call_args.args
    assert settings.api_hash not in " ".join(spawn.call_args.args)
    assert spawn.call_args.kwargs["env"]["TELEGRAM_API_HASH"] == settings.api_hash


async def test_bot_failure_stops_the_api_server(settings, monkeypatch):
    process = FakeServer()
    monkeypatch.setattr(heroku.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr(heroku, "serve", AsyncMock(side_effect=RuntimeError("Bot failed")))
    with pytest.raises(RuntimeError, match="Bot failed"):
        await heroku.run(cloud_settings(settings), stop=asyncio.Event())
    assert process.terminated


async def test_api_failure_cancels_the_bot(settings, monkeypatch):
    process = FakeServer()
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def app(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(heroku.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr(heroku, "serve", app)
    task = asyncio.create_task(heroku.run(cloud_settings(settings), stop=asyncio.Event()))
    await asyncio.wait_for(started.wait(), timeout=2)
    process.returncode = 1
    process.exited.set()
    with pytest.raises(RuntimeError, match="API server exited"):
        await task
    assert cleaned.is_set()


async def test_cloud_mode_rejects_missing_persistent_state(settings):
    with pytest.raises(ValueError, match="DATABASE_URL and TELEGRAM_SESSION"):
        await heroku.run(settings, stop=asyncio.Event())


async def test_cloud_mode_requires_the_bundled_api_url(settings):
    with pytest.raises(ValueError, match="BOT_API_BASE_URL"):
        await heroku.run(
            replace(cloud_settings(settings), bot_api_base_url="http://other:8081"),
            stop=asyncio.Event(),
        )
