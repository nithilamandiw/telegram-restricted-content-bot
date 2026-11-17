"""Supervise a private local Bot API and the bot inside one Heroku worker dyno."""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from .config import Settings
from .main import account_lock, configure_logging, serve

logger = logging.getLogger(__name__)


async def stop_server(process) -> None:
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()


async def run(settings: Settings, *, stop: asyncio.Event | None = None) -> None:
    if not settings.database_url or not settings.telegram_session:
        raise ValueError("Heroku requires DATABASE_URL and TELEGRAM_SESSION config vars.")
    if settings.bot_api_base_url != "http://127.0.0.1:8081":
        raise ValueError("Set BOT_API_BASE_URL=http://127.0.0.1:8081 for the bundled API server.")
    settings.prepare_directories()
    server_dir = settings.data_dir / "bot-api"
    temporary_dir = server_dir / "tmp"
    temporary_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = os.environ.copy()
    environment["TELEGRAM_API_ID"] = str(settings.api_id)
    environment["TELEGRAM_API_HASH"] = settings.api_hash
    # Native server logs can contain request tokens; log component exits instead.
    process = await asyncio.create_subprocess_exec(
        "/usr/local/bin/telegram-bot-api",
        "--local",
        "--http-ip-address=127.0.0.1",
        "--http-port=8081",
        f"--dir={server_dir}",
        f"--temp-dir={temporary_dir}",
        env=environment,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    install_signals = stop is None
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    if install_signals:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
    bot_task = asyncio.create_task(serve(settings, handle_signals=False))
    server_task = asyncio.create_task(process.wait())
    stopping = asyncio.create_task(stop.wait())
    try:
        logger.info("Bundled local API started on loopback; starting the bot worker.")
        done, _ = await asyncio.wait(
            [bot_task, server_task, stopping], return_when=asyncio.FIRST_COMPLETED
        )
        if stopping in done:
            return
        if server_task in done:
            raise RuntimeError("The bundled Telegram API server exited.")
        bot_task.result()
        raise RuntimeError("The bot worker exited unexpectedly.")
    finally:
        bot_task.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(bot_task, return_exceptions=True), timeout=20)
        finally:
            await stop_server(process)
            server_task.cancel()
            stopping.cancel()
            await asyncio.gather(server_task, stopping, return_exceptions=True)
            if install_signals:
                for signum in (signal.SIGTERM, signal.SIGINT):
                    loop.remove_signal_handler(signum)


def cli() -> None:
    os.umask(0o077)
    os.environ.setdefault("DATA_DIR", "/tmp/telegram-downloader")
    os.environ.setdefault("BOT_API_BASE_URL", "http://127.0.0.1:8081")
    configure_logging()
    try:
        settings = Settings.load()
        settings.prepare_directories()
        with account_lock(settings):
            asyncio.run(run(settings))
    except (ValueError, RuntimeError) as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from None
    except Exception as exc:
        logger.error(
            "Heroku worker failed (%s). Check config vars and Postgres availability.",
            type(exc).__name__,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    cli()
