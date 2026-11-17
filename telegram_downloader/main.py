from __future__ import annotations

import argparse
import asyncio
import fcntl
import getpass
import logging
import os
from contextlib import contextmanager
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import BotCommand
from telethon import TelegramClient
from telethon.sessions import StringSession

from .config import Settings
from .handlers import make_router
from .store import JobStore
from .transfer import TransferService, bot_request

logger = logging.getLogger(__name__)


@contextmanager
def account_lock(settings: Settings):
    with (settings.data_dir / "service.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "Another bot or login process is using this data directory."
            ) from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def account_client(settings: Settings, *, use_file: bool = False) -> TelegramClient:
    return TelegramClient(
        StringSession(settings.telegram_session)
        if settings.telegram_session and not use_file
        else str(settings.session_path),
        settings.api_id,
        settings.api_hash,
        flood_sleep_threshold=0,
        request_retries=3,
        connection_retries=3,
    )


async def login(settings: Settings) -> None:
    client = account_client(settings, use_file=True)
    try:
        await client.start(
            phone=lambda: input("Shared account phone number (with country code): "),
            code_callback=lambda: getpass.getpass("Telegram verification code: "),
            password=lambda: getpass.getpass("Telegram two-step verification password: "),
        )
        me = await client.get_me()
        if me.bot:
            raise RuntimeError("Login requires a user account, not a bot token.")
        print("Shared account session saved. You can now start the bot.")
    finally:
        await client.disconnect()


def make_bot(settings: Settings) -> Bot:
    session = AiohttpSession(
        api=TelegramAPIServer.from_base(settings.bot_api_base_url, is_local=True),
        timeout=30,
    )
    return Bot(token=settings.bot_token, session=session)


async def serve(settings: Settings, *, handle_signals: bool = True) -> None:
    if not settings.telegram_session and not settings.session_path.exists():
        raise RuntimeError("No shared account session found. Run the login command first.")
    client = account_client(settings)
    bot = make_bot(settings)
    store = JobStore(settings.jobs_path, database_url=settings.database_url)
    service = TransferService(settings, store, client, bot)
    worker = None
    try:
        if not store.acquire_worker_lock():
            raise RuntimeError("Another bot is using DATABASE_URL. Run exactly one worker.")
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("The shared account session expired. Run the login command again.")
        me = await client.get_me()
        if me is None or me.bot:
            raise RuntimeError("The saved session must belong to a Telegram user account.")
        await bot_request(bot.get_me)
        await bot_request(lambda: bot.delete_webhook(drop_pending_updates=False))
        await bot_request(
            lambda: bot.set_my_commands(
                [
                    BotCommand(command="start", description="Start the bot"),
                    BotCommand(command="help", description="Download instructions"),
                    BotCommand(command="status", description="Your recent requests"),
                    BotCommand(command="cancel", description="Cancel your pending requests"),
                ]
            )
        )
        await service.recover()
        dispatcher = Dispatcher()
        dispatcher.include_router(make_router(settings, store, service))
        worker = asyncio.create_task(service.run(), name="download-worker")
        approval_worker = asyncio.create_task(service.joins.run(), name="approval-worker")

        # Stop polling if the worker exits unexpectedly instead of silently accepting jobs.
        async def monitor_worker():
            try:
                done, _ = await asyncio.wait(
                    [worker, approval_worker], return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    task.result()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Background worker stopped (%s)", type(exc).__name__)
            finally:
                await dispatcher.stop_polling()

        monitor = asyncio.create_task(monitor_worker())
        try:
            logger.info("Bot ready; one transfer worker is running.")
            await dispatcher.start_polling(
                bot,
                allowed_updates=["message", "callback_query"],
                close_bot_session=False,
                handle_signals=handle_signals,
            )
        finally:
            monitor.cancel()
            approval_worker.cancel()
            await service.stop()
            worker.cancel()
            await asyncio.gather(worker, approval_worker, monitor, return_exceptions=True)
    finally:
        store.close()
        await client.disconnect()
        await bot.session.close()


async def logout_cloud(settings: Settings) -> None:
    """Explicit, one-time migration step; never called during normal startup."""
    bot = Bot(settings.bot_token)
    try:
        await bot.log_out()
        print("Logged out of the hosted Bot API. Start the local Bot API server next.")
    finally:
        await bot.session.close()


async def logout_local(settings: Settings) -> None:
    """Explicit migration step for the old local server, never called at startup."""
    bot = make_bot(settings)
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await bot.log_out()
        print("Logged out of the old local Bot API. Stop that server before starting Heroku.")
    finally:
        await bot.session.close()


def export_session(settings: Settings, output: Path) -> None:
    if not settings.session_path.exists():
        raise RuntimeError("Run the terminal login command before exporting the account session.")
    client = account_client(settings, use_file=True)
    try:
        if not client.session.auth_key:
            raise RuntimeError("No account authentication found. Run terminal login first.")
        value = StringSession.save(client.session)
        # Exclusive creation avoids silently overwriting an existing secret.
        with output.open("x", encoding="utf-8") as handle:
            output.chmod(0o600)
            handle.write(value + "\n")
        print(
            f"Session exported to {output}. "
            "Copy its contents to Heroku's TELEGRAM_SESSION config var."
        )
    finally:
        client.session.close()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("aiogram.event").disabled = True
    logging.getLogger("aiogram.dispatcher").setLevel(logging.CRITICAL)
    logging.getLogger("telethon").setLevel(logging.CRITICAL)


def cli() -> None:
    parser = argparse.ArgumentParser(description="Telegram Restricted Content Bot")
    parser.add_argument(
        "command",
        choices=["run", "login", "logout-cloud", "logout-local", "check", "export-session"],
        nargs="?",
        default="run",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output file for export-session (default: DATA_DIR/telegram-session.txt)",
    )
    args = parser.parse_args()
    os.umask(0o077)
    configure_logging()
    try:
        settings = Settings.load(require_bot=args.command not in {"login", "export-session"})
        settings.prepare_directories()
        with account_lock(settings):
            if args.command == "check":
                print("Configuration is valid. No network request was made.")
            elif args.command == "login":
                asyncio.run(login(settings))
            elif args.command == "logout-cloud":
                asyncio.run(logout_cloud(settings))
            elif args.command == "logout-local":
                asyncio.run(logout_local(settings))
            elif args.command == "export-session":
                export_session(settings, args.output or settings.data_dir / "telegram-session.txt")
            else:
                asyncio.run(serve(settings))
    except KeyboardInterrupt:
        pass
    except (ValueError, RuntimeError) as exc:
        parser.exit(1, f"{exc}\n")
    except Exception as exc:
        # Avoid token-bearing aiohttp/Telegram exception representations in logs.
        parser.exit(
            1,
            f"Startup failed ({type(exc).__name__}). Check credentials, session, "
            "and local Bot API availability.\n",
        )


if __name__ == "__main__":
    cli()
