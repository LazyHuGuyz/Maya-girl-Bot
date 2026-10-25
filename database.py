"""
SQLite persistence layer.

Three tables:
  - users                 : who has talked to the bot, and basic activity counts
  - conversation_history  : rolling chat history per chat (trimmed to a window)
  - user_preferences      : nickname and preferred language per user

Uses aiosqlite so queries never block the event loop.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    username       TEXT,
    first_name     TEXT,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    message_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS conversation_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_chat ON conversation_history (chat_id, id);

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id     INTEGER PRIMARY KEY,
    nickname    TEXT,
    language    TEXT NOT NULL DEFAULT 'auto'
                CHECK (language IN ('auto', 'hi', 'hinglish', 'en')),
    updated_at  TEXT NOT NULL
);
"""

# Only these columns may be updated through set_preference (prevents SQL injection
# through the column name, since column names cannot be parameterized)
_ALLOWED_PREF_FIELDS = {"nickname", "language"}


def _now() -> str:
    """Current UTC timestamp as an ISO string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Thin async wrapper around a single SQLite connection."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open the connection and create tables if needed."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        # WAL improves concurrent read/write behaviour
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        logger.info("Database ready at %s", self._path)

    async def close(self) -> None:
        """Close the connection cleanly."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        """The live connection; raises if connect() was never called."""
        if self._conn is None:
            raise RuntimeError("Database is not connected. Call connect() first.")
        return self._conn

    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------
    async def upsert_user(
        self, user_id: int, username: str | None, first_name: str | None, count: int = 1
    ) -> None:
        """Insert or update a user; `count` is added to their message counter."""
        now = _now()
        await self.conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, first_seen, last_seen, message_count)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username      = excluded.username,
                first_name    = excluded.first_name,
                last_seen     = excluded.last_seen,
                message_count = users.message_count + excluded.message_count
            """,
            (user_id, username, first_name, now, now, count),
        )
        await self.conn.commit()

    async def user_message_count(self, user_id: int) -> int:
        """Total messages this user has sent to Maya."""
        async with self.conn.execute(
            "SELECT message_count FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return int(row["message_count"]) if row else 0

    # ------------------------------------------------------------------
    # conversation_history
    # ------------------------------------------------------------------
    async def add_message(
        self, chat_id: int, user_id: int | None, role: str, content: str
    ) -> None:
        """Append one message to a chat's history."""
        await self.conn.execute(
            """
            INSERT INTO conversation_history (chat_id, user_id, role, content, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (chat_id, user_id, role, content, _now()),
        )
        await self.conn.commit()

    async def get_history(self, chat_id: int, limit: int) -> list[dict[str, str]]:
        """Return the most recent `limit` messages for a chat, oldest first."""
        async with self.conn.execute(
            """
            SELECT role, content FROM (
                SELECT id, role, content FROM conversation_history
                WHERE chat_id = ? ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
            """,
            (chat_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    async def trim_history(self, chat_id: int, keep: int) -> None:
        """Delete everything except the newest `keep` messages for a chat."""
        await self.conn.execute(
            """
            DELETE FROM conversation_history
            WHERE chat_id = ?
              AND id NOT IN (
                  SELECT id FROM conversation_history
                  WHERE chat_id = ? ORDER BY id DESC LIMIT ?
              )
            """,
            (chat_id, chat_id, keep),
        )
        await self.conn.commit()

    async def clear_history(self, chat_id: int) -> int:
        """Delete all history for a chat. Returns the number of rows removed."""
        cur = await self.conn.execute(
            "DELETE FROM conversation_history WHERE chat_id = ?", (chat_id,)
        )
        await self.conn.commit()
        return cur.rowcount

    async def chat_history_count(self, chat_id: int) -> int:
        """Number of stored messages for a chat."""
        async with self.conn.execute(
            "SELECT COUNT(*) AS n FROM conversation_history WHERE chat_id = ?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # user_preferences
    # ------------------------------------------------------------------
    async def get_preferences(self, user_id: int) -> dict[str, Any] | None:
        """Return the stored preferences for a user, or None."""
        async with self.conn.execute(
            "SELECT nickname, language FROM user_preferences WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def set_preference(self, user_id: int, field: str, value: str) -> None:
        """Set one preference field (nickname or language) for a user."""
        if field not in _ALLOWED_PREF_FIELDS:
            raise ValueError(f"Unknown preference field: {field}")
        # Safe: `field` is validated against the whitelist above
        await self.conn.execute(
            f"""
            INSERT INTO user_preferences (user_id, {field}, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                {field} = excluded.{field},
                updated_at = excluded.updated_at
            """,
            (user_id, value, _now()),
        )
        await self.conn.commit()

    # ------------------------------------------------------------------
    # stats
    # ------------------------------------------------------------------
    async def global_stats(self) -> dict[str, int]:
        """Aggregate counts across the whole database (for admins)."""
        async with self.conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM users) AS users,
                (SELECT COUNT(*) FROM conversation_history) AS stored_messages,
                (SELECT COUNT(DISTINCT chat_id) FROM conversation_history) AS chats,
                (SELECT COUNT(*) FROM user_preferences) AS profiles
            """
        ) as cur:
            row = await cur.fetchone()
        return dict(row)