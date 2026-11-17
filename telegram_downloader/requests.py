from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from telethon import errors

from .errors import TransferError


async def bot_request[T](operation: Callable[[], Awaitable[T]], *, upload: bool = False) -> T:
    for attempt in range(3):
        try:
            return await operation()
        except TelegramRetryAfter as exc:
            if attempt == 2 or exc.retry_after > 300:
                raise
            await asyncio.sleep(exc.retry_after + 1)
        except (TelegramNetworkError, TelegramServerError):
            if upload:
                raise TransferError(
                    "The delivery result is unknown because the connection failed. "
                    "Check this chat before sending the link again; content may have arrived."
                ) from None
            if attempt == 2:
                raise
            await asyncio.sleep(2 ** (attempt + 1))
    raise AssertionError("Unreachable")


async def account_request[T](operation: Callable[[], Awaitable[T]]) -> T:
    for attempt in range(3):
        try:
            return await operation()
        except errors.FloodWaitError as exc:
            if attempt == 2 or exc.seconds > 300:
                raise
            await asyncio.sleep(exc.seconds + 1)
        except (ConnectionError, TimeoutError, errors.ServerError):
            if attempt == 2:
                raise
            await asyncio.sleep(2 ** (attempt + 1))
    raise AssertionError("Unreachable")
