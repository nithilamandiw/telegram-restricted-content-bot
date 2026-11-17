from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from aiogram.utils.token import TokenValidationError, validate_token
from dotenv import load_dotenv
from telethon.sessions import StringSession


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    bot_token: str
    allowed_user_ids: frozenset[int]
    data_dir: Path
    bot_api_base_url: str
    max_file_bytes: int = 2_000_000_000
    max_pending_per_user: int = 5
    min_free_disk_bytes: int = 64 * 1024 * 1024
    transfer_timeout_seconds: int = 7200
    progress_interval_seconds: int = 5
    database_url: str | None = field(default=None, repr=False)
    telegram_session: str | None = field(default=None, repr=False)

    @property
    def session_path(self) -> Path:
        return self.data_dir / "account.session"

    @property
    def jobs_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    def prepare_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.downloads_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    @classmethod
    def load(cls, *, require_bot: bool = True) -> Settings:
        load_dotenv()

        def positive(name: str, default: str | None = None) -> int:
            try:
                value = int(os.environ.get(name, default) or "")
            except ValueError:
                raise ValueError(f"{name} must be a positive integer.") from None
            if value <= 0:
                raise ValueError(f"{name} must be a positive integer.")
            return value

        api_id = positive("TELEGRAM_API_ID")
        api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
        if len(api_hash) != 32 or any(c not in "0123456789abcdefABCDEF" for c in api_hash):
            raise ValueError(
                "TELEGRAM_API_HASH must be the 32-character hash from my.telegram.org."
            )
        token = os.environ.get("BOT_TOKEN", "").strip()
        try:
            allowed = frozenset(
                int(part.strip())
                for part in os.environ.get("ALLOWED_USER_IDS", "").split(",")
                if part.strip()
            )
        except ValueError:
            raise ValueError(
                "ALLOWED_USER_IDS must contain comma-separated numeric user IDs."
            ) from None
        if require_bot:
            try:
                validate_token(token)
            except TokenValidationError:
                raise ValueError("Set a valid BOT_TOKEN from @BotFather.") from None
            if not allowed or any(user_id <= 0 for user_id in allowed):
                raise ValueError("Set at least one positive user ID in ALLOWED_USER_IDS.")
        base_url = os.environ.get("BOT_API_BASE_URL", "http://127.0.0.1:8081").rstrip("/")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "BOT_API_BASE_URL must be an HTTP(S) origin, e.g. http://127.0.0.1:8081."
            )
        if parsed.hostname == "api.telegram.org":
            raise ValueError("Use a local Bot API server; the hosted API cannot upload 2,000 MB.")
        max_bytes = positive("MAX_FILE_BYTES", "2000000000")
        if max_bytes > 2_000_000_000:
            raise ValueError("MAX_FILE_BYTES cannot exceed 2000000000.")
        database_url = os.environ.get("DATABASE_URL", "").strip() or None
        if database_url and urlsplit(database_url).scheme not in {"postgres", "postgresql"}:
            raise ValueError("DATABASE_URL must be a PostgreSQL connection URL.")
        telegram_session = os.environ.get("TELEGRAM_SESSION", "").strip() or None
        if telegram_session:
            try:
                session = StringSession(telegram_session)
                if not session.auth_key:
                    raise ValueError
            except Exception:
                raise ValueError(
                    "TELEGRAM_SESSION is invalid. Export your logged-in session again."
                ) from None
        return cls(
            api_id=api_id,
            api_hash=api_hash,
            bot_token=token,
            allowed_user_ids=allowed,
            data_dir=Path(os.environ.get("DATA_DIR", "./data")).expanduser().resolve(),
            bot_api_base_url=base_url,
            max_file_bytes=max_bytes,
            max_pending_per_user=positive("MAX_PENDING_PER_USER", "5"),
            min_free_disk_bytes=positive("MIN_FREE_DISK_BYTES", "67108864"),
            transfer_timeout_seconds=positive("TRANSFER_TIMEOUT_SECONDS", "7200"),
            progress_interval_seconds=positive("PROGRESS_INTERVAL_SECONDS", "5"),
            database_url=database_url,
            telegram_session=telegram_session,
        )
