from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .database import PostgresDatabase

ACTIVE = ("queued", "downloading", "uploading")
PENDING = (*ACTIVE, "awaiting_approval")
PENDING_SQL = ",".join("?" for _ in PENDING)
FINISHED = ("done", "failed", "cancelled", "expired", "interrupted")
HISTORY_RETENTION = timedelta(hours=2)


class QueueFull(ValueError):
    pass


@dataclass(frozen=True)
class Job:
    id: int
    user_id: int
    chat_id: int
    link: str
    state: str
    error: str | None
    invite_hash: str | None = None
    approval_deadline: float | None = None
    status_message_id: int | None = None


@dataclass(frozen=True)
class Invitation:
    invite_hash: str
    state: str
    peer_id: int | None
    next_check_at: float
    error: str | None


class JobStore:
    """Short, synchronous transactions on one event loop; no awaits inside transactions."""

    def __init__(self, path: Path, *, database_url: str | None = None):
        self.postgres = database_url is not None
        if self.postgres:
            self.db = PostgresDatabase(database_url)
            try:
                self._prepare_postgres()
            except BaseException:
                self.db.close()
                raise
            return
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                link TEXT NOT NULL,
                state TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS jobs_user_state ON jobs(user_id, state);
            CREATE INDEX IF NOT EXISTS jobs_state_updated ON jobs(state, updated_at);
            CREATE TABLE IF NOT EXISTS invitations (
                invite_hash TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                peer_id INTEGER,
                next_check_at REAL NOT NULL DEFAULT 0,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS user_invites (
                user_id INTEGER PRIMARY KEY,
                invite_hash TEXT NOT NULL
            );
        """)
        # Additive migration: existing histories and Telegram sessions remain untouched.
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(jobs)")}
        with self.db:
            if "invite_hash" not in columns:
                self.db.execute("ALTER TABLE jobs ADD COLUMN invite_hash TEXT")
            if "approval_deadline" not in columns:
                self.db.execute("ALTER TABLE jobs ADD COLUMN approval_deadline REAL")
            if "status_message_id" not in columns:
                self.db.execute("ALTER TABLE jobs ADD COLUMN status_message_id INTEGER")

    def _prepare_postgres(self) -> None:
        with self.db:
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    chat_id BIGINT NOT NULL,
                    link TEXT NOT NULL,
                    state TEXT NOT NULL,
                    error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    invite_hash TEXT,
                    approval_deadline DOUBLE PRECISION,
                    status_message_id BIGINT
                )
            """)
            self.db.execute("CREATE INDEX IF NOT EXISTS jobs_user_state ON jobs(user_id,state)")
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS jobs_state_updated ON jobs(state,updated_at)"
            )
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS invitations (
                    invite_hash TEXT PRIMARY KEY, state TEXT NOT NULL,
                    peer_id BIGINT, next_check_at DOUBLE PRECISION NOT NULL DEFAULT 0, error TEXT
                )
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS user_invites (
                    user_id BIGINT PRIMARY KEY, invite_hash TEXT NOT NULL
                )
            """)

    def acquire_worker_lock(self) -> bool:
        if not self.postgres:
            return True
        return self.db.execute("SELECT pg_try_advisory_lock(742839127610) AS locked").fetchone()[
            "locked"
        ]

    @staticmethod
    def _job(row: sqlite3.Row | Mapping) -> Job:
        return Job(**{key: row[key] for key in Job.__dataclass_fields__})

    def enqueue(self, user_id: int, chat_id: int, link: str, limit: int) -> Job:
        with self.db:
            if self.postgres:
                self.db.execute("SELECT pg_advisory_xact_lock(?)", (user_id,))
            else:
                self.db.execute("BEGIN IMMEDIATE")
            count = self.db.execute(
                f"SELECT count(*) AS total FROM jobs WHERE user_id=? AND state IN ({PENDING_SQL})",
                (user_id, *PENDING),
            ).fetchone()["total"]
            if count >= limit:
                raise QueueFull(
                    f"You already have {limit} pending requests. Use /status or /cancel."
                )
            invite = self.latest_invite(user_id)
            row = self.db.execute(
                "INSERT INTO jobs(user_id, chat_id, link, state, invite_hash, approval_deadline) "
                "VALUES (?, ?, ?, 'queued', ?, ?) RETURNING *",
                (
                    user_id,
                    chat_id,
                    link,
                    invite.invite_hash if invite else None,
                    time.time() + 86400 if invite else None,
                ),
            ).fetchone()
        return self._job(row)

    def claim_next(self) -> Job | None:
        with self.db:
            if not self.postgres:
                self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM jobs WHERE state='queued' ORDER BY id LIMIT 1"
                + (" FOR UPDATE SKIP LOCKED" if self.postgres else "")
            ).fetchone()
            if row is None:
                return None
            self.db.execute(
                "UPDATE jobs SET state='downloading', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (row["id"],),
            )
        return self._job(dict(row) | {"state": "downloading"})

    def set_state(self, job_id: int, state: str, error: str | None = None) -> bool:
        with self.db:
            result = self.db.execute(
                "UPDATE jobs SET state=?, error=?, updated_at=CURRENT_TIMESTAMP "
                f"WHERE id=? AND state IN ({PENDING_SQL})",
                (state, error, job_id, *PENDING),
            )
        return bool(result.rowcount)

    def list_for_user(self, user_id: int, limit: int = 10) -> list[Job]:
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)
        ).fetchall()
        return [self._job(row) for row in rows]

    def prune_history(self, *, now: datetime | None = None) -> int:
        """Expire finished requests while preserving active transfers and approval waits."""
        cutoff = (now or datetime.now(UTC)) - HISTORY_RETENTION
        boundary = cutoff if self.postgres else cutoff.strftime("%Y-%m-%d %H:%M:%S")
        placeholders = ",".join("?" for _ in FINISHED)
        with self.db:
            result = self.db.execute(
                f"DELETE FROM jobs WHERE state IN ({placeholders}) AND updated_at<=?",
                (*FINISHED, boundary),
            )
        return result.rowcount

    def get_job(self, job_id: int) -> Job | None:
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def save_status_message(self, job_id: int, message_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE jobs SET status_message_id=? WHERE id=?", (message_id, job_id))

    def cancel(self, user_id: int, job_id: int | None = None) -> list[int]:
        with self.db:
            rows = self.db.execute(
                f"SELECT id FROM jobs WHERE user_id=? AND state IN ({PENDING_SQL}) "
                "AND (CAST(? AS BIGINT) IS NULL OR id=?)",
                (user_id, *PENDING, job_id, job_id),
            ).fetchall()
            ids = [row["id"] for row in rows]
            self.db.executemany(
                "UPDATE jobs SET state='cancelled', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                [(job_id,) for job_id in ids],
            )
        return ids

    def recover(self) -> list[Job]:
        with self.db:
            rows = self.db.execute("SELECT * FROM jobs WHERE state IN (?, ?, ?)", ACTIVE).fetchall()
            self.db.execute(
                "UPDATE jobs SET state='interrupted', error='Bot restarted; send the link again.', "
                "updated_at=CURRENT_TIMESTAMP WHERE state IN (?, ?, ?)",
                ACTIVE,
            )
        return [self._job(row) for row in rows]

    def get_invite(self, invite_hash: str) -> Invitation | None:
        row = self.db.execute(
            "SELECT * FROM invitations WHERE invite_hash=?", (invite_hash,)
        ).fetchone()
        return Invitation(**dict(row)) if row else None

    def save_invite(
        self,
        invite_hash: str,
        state: str,
        *,
        peer_id: int | None = None,
        next_check_at: float = 0,
        error: str | None = None,
    ) -> Invitation:
        with self.db:
            self.db.execute(
                "INSERT INTO invitations(invite_hash,state,peer_id,next_check_at,error) "
                "VALUES (?,?,?,?,?) ON CONFLICT(invite_hash) DO UPDATE SET "
                "state=excluded.state, peer_id=coalesce(excluded.peer_id, invitations.peer_id), "
                "next_check_at=excluded.next_check_at, error=excluded.error",
                (invite_hash, state, peer_id, next_check_at, error),
            )
        return self.get_invite(invite_hash)

    def remember_invite(self, user_id: int, invite_hash: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO user_invites(user_id,invite_hash) VALUES (?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET invite_hash=excluded.invite_hash",
                (user_id, invite_hash),
            )

    def latest_invite(self, user_id: int) -> Invitation | None:
        row = self.db.execute(
            "SELECT i.* FROM invitations i JOIN user_invites u USING(invite_hash) "
            "WHERE u.user_id=?",
            (user_id,),
        ).fetchone()
        return Invitation(**dict(row)) if row else None

    def waiting_jobs(self, invite_hash: str | None = None) -> list[Job]:
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE state='awaiting_approval' "
            "AND (CAST(? AS TEXT) IS NULL OR invite_hash=?) ORDER BY id",
            (invite_hash, invite_hash),
        ).fetchall()
        return [self._job(row) for row in rows]

    def update_waiting(self, job_id: int, state: str, error: str | None = None) -> bool:
        """Conditional promotion prevents a concurrent cancellation from being undone."""
        with self.db:
            result = self.db.execute(
                "UPDATE jobs SET state=?, error=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND state='awaiting_approval'",
                (state, error, job_id),
            )
        return bool(result.rowcount)

    def close(self) -> None:
        self.db.close()
