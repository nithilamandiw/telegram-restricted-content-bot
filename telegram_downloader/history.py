"""Expire request history independently of transfers and Telegram approval checks."""

from __future__ import annotations

import asyncio
import logging

from .store import JobStore

logger = logging.getLogger(__name__)
CHECK_INTERVAL_SECONDS = 60


async def cleanup_history(store: JobStore) -> None:
    while True:
        try:
            deleted = store.prune_history()
            if deleted:
                logger.info("Removed %s finished request records older than two hours.", deleted)
        except Exception as exc:
            # Retry transient database failures without logging connection credentials.
            logger.warning(
                "History cleanup failed (%s); retrying in 60 seconds.", type(exc).__name__
            )
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
