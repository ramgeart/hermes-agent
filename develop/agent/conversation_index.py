"""ConversationIndex — conversation metadata and FTS5 search in state.db.

Manages the conversation_index table: lightweight per-session metadata
(title, summary, topics, mood, importance) with FTS5 full-text search
for recall.  Lives in state.db with a foreign key to sessions.id for
referential integrity.

Schema:
    state.db -> conversation_index table + conversation_index_fts (FTS5)
    + auto-sync triggers (INSERT/DELETE/UPDATE) + temporal indexes
    + FOREIGN KEY (session_id) REFERENCES sessions(id)

Usage:
    from agent.conversation_index import ConversationIndex

    ci = ConversationIndex(db_path="/opt/data/state.db")
    ci.initialize()
    ci.insert_conversation(
        session_id="sess-001",
        title="Identity System Design",
        summary="Discussed architecture of immutable identity.",
        topics="identity,architecture,sqlite",
        day_number=47,
        date="2026-05-28",
        importance=8,
        mood="excited",
    )
    results = ci.query_conversations(search_text="identity")
    recent = ci.get_recent_conversations(days=7)
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# -- SQL Schema (verified by parent task t_355a4423) ---------------------

_SCHEMA_SQL = """\
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS conversation_index (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL UNIQUE,
    title           TEXT    NOT NULL DEFAULT '',
    summary         TEXT    NOT NULL DEFAULT '',
    topics          TEXT    NOT NULL DEFAULT '',
    day_number      INTEGER NOT NULL DEFAULT 0,
    date            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d', 'now')),
    importance      INTEGER NOT NULL DEFAULT 5 CHECK (importance BETWEEN 1 AND 10),
    mood            TEXT    NOT NULL DEFAULT 'neutral',
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_conversation_index_date
    ON conversation_index (date DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_index_day_number
    ON conversation_index (day_number DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_index_importance
    ON conversation_index (importance DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_index_mood
    ON conversation_index (mood);

CREATE VIRTUAL TABLE IF NOT EXISTS conversation_index_fts USING fts5(
    title,
    summary,
    topics,
    content=conversation_index,
    content_rowid=id,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS conversation_index_ai AFTER INSERT ON conversation_index
BEGIN
    INSERT INTO conversation_index_fts (rowid, title, summary, topics)
    VALUES (new.id, new.title, new.summary, new.topics);
END;

CREATE TRIGGER IF NOT EXISTS conversation_index_ad AFTER DELETE ON conversation_index
BEGIN
    INSERT INTO conversation_index_fts (conversation_index_fts, rowid, title, summary, topics)
    VALUES ('delete', old.id, old.title, old.summary, old.topics);
END;

CREATE TRIGGER IF NOT EXISTS conversation_index_au_fts
    AFTER UPDATE OF title, summary, topics ON conversation_index
BEGIN
    INSERT INTO conversation_index_fts (conversation_index_fts, rowid, title, summary, topics)
    VALUES ('delete', old.id, old.title, old.summary, old.topics);
    INSERT INTO conversation_index_fts (rowid, title, summary, topics)
    VALUES (new.id, new.title, new.summary, new.topics);
END;

CREATE TRIGGER IF NOT EXISTS conversation_index_au_ts AFTER UPDATE ON conversation_index
WHEN old.updated_at = new.updated_at
BEGIN
    UPDATE conversation_index
    SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
    WHERE id = new.id;
END;
"""

# -- Valid moods ---------------------------------------------------------

VALID_MOODS = {
    "positive", "neutral", "negative", "excited", "reflective",
    "focused", "frustrated", "curious", "calm", "anxious",
}

# -- Row-to-dict helper --------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict."""
    return dict(row)


class ConversationIndex:
    """Manage conversation metadata and FTS5 search in memory.db."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    # -- Lifecycle --------------------------------------------------------

    def initialize(self) -> None:
        """Create the database and schema if they don't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_conn()
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
        logger.info("Conversation index DB initialized at %s", self.db_path)

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # -- Insert -----------------------------------------------------------

    def insert_conversation(
        self,
        session_id: str,
        title: str = "",
        summary: str = "",
        topics: str = "",
        day_number: int = 0,
        date: str = "",
        importance: int = 5,
        mood: str = "neutral",
    ) -> int:
        """Insert a new conversation index entry.

        Returns the row id of the inserted row.
        Raises ValueError on validation failure.
        Raises sqlite3.IntegrityError if session_id already exists.
        """
        if not session_id or not session_id.strip():
            raise ValueError("session_id cannot be empty")
        if importance < 1 or importance > 10:
            raise ValueError(f"importance must be 1-10, got {importance}")
        if mood and mood not in VALID_MOODS:
            logger.warning("Non-standard mood '%s' — inserting anyway", mood)
        if not date:
            date = datetime.now(UTC).strftime("%Y-%m-%d")

        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO conversation_index
               (session_id, title, summary, topics, day_number, date, importance, mood)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, title, summary, topics, day_number, date, importance, mood),
        )
        conn.commit()
        row_id: int = cursor.lastrowid or 0
        logger.info(
            "Inserted conversation index id=%d session=%s title='%s'",
            row_id, session_id, title[:50],
        )
        return row_id

    # -- Query (FTS5 search) ---------------------------------------------

    def query_conversations(
        self,
        search_text: Optional[str] = None,
        date_range: Optional[Tuple[str, str]] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Search conversations by full-text and/or date range.

        Args:
            search_text: FTS5 query string (supports AND, OR, NOT, prefix*).
                         If None/empty, returns all rows (optionally filtered by date).
            date_range:  Optional (start_date, end_date) tuple in ISO format (YYYY-MM-DD).
                         Inclusive on both ends.
            limit:       Max results to return (default 50).

        Returns:
            List of dicts with all conversation_index columns.
            When search_text is provided, results are ordered by FTS5 rank (relevance).
            Otherwise, ordered by date DESC, importance DESC.
        """
        conn = self._get_conn()

        if search_text and search_text.strip():
            # FTS5 search — join back to get all columns
            sql = """
                SELECT ci.*
                FROM conversation_index_fts fts
                JOIN conversation_index ci ON ci.id = fts.rowid
                WHERE conversation_index_fts MATCH ?
            """
            params: list = [search_text.strip()]

            if date_range:
                sql += " AND ci.date BETWEEN ? AND ?"
                params.extend(date_range)

            sql += " ORDER BY fts.rank LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
        else:
            # No text search — plain query with optional date filter
            sql = "SELECT * FROM conversation_index WHERE 1=1"
            params = []

            if date_range:
                sql += " AND date BETWEEN ? AND ?"
                params.extend(date_range)

            sql += " ORDER BY date DESC, importance DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()

        results = [_row_to_dict(r) for r in rows]
        logger.debug(
            "query_conversations(text=%r, range=%s) -> %d results",
            search_text, date_range, len(results),
        )
        return results

    # -- Update -----------------------------------------------------------

    def update_conversation(self, index_id: int, **kwargs: Any) -> bool:
        """Update fields on an existing conversation index entry.

        Args:
            index_id: The row id (primary key) to update.
            **kwargs: Fields to update.  Valid keys:
                      title, summary, topics, day_number, date, importance, mood.

        Returns:
            True if a row was updated, False if no matching row found.
        Raises ValueError if no valid fields are provided.
        """
        allowed = {"title", "summary", "topics", "day_number", "date", "importance", "mood"}
        invalid = set(kwargs.keys()) - allowed
        if invalid:
            raise ValueError(f"Unknown fields: {invalid}. Allowed: {allowed}")
        if not kwargs:
            raise ValueError("No fields to update")

        if "importance" in kwargs:
            imp = kwargs["importance"]
            if imp < 1 or imp > 10:
                raise ValueError(f"importance must be 1-10, got {imp}")

        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [index_id]

        conn = self._get_conn()
        cursor = conn.execute(
            f"UPDATE conversation_index SET {set_clause} WHERE id = ?",
            values,
        )
        conn.commit()

        updated = cursor.rowcount > 0
        if updated:
            logger.info("Updated conversation index id=%d fields=%s", index_id, list(kwargs.keys()))
        else:
            logger.warning("No conversation found with id=%d", index_id)
        return updated

    # -- Recent conversations ---------------------------------------------

    def get_recent_conversations(self, days: int = 14) -> List[Dict[str, Any]]:
        """Get conversations from the last N days.

        Returns conversations ordered by date DESC, importance DESC.
        """
        if days < 1:
            raise ValueError(f"days must be >= 1, got {days}")

        cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM conversation_index
               WHERE date >= ?
               ORDER BY date DESC, importance DESC""",
            (cutoff,),
        ).fetchall()

        results = [_row_to_dict(r) for r in rows]
        logger.debug("get_recent_conversations(days=%d) -> %d results", days, len(results))
        return results

    # -- Delete -----------------------------------------------------------

    def delete_conversation(self, index_id: int) -> bool:
        """Delete a conversation index entry by id.

        Returns True if a row was deleted, False if not found.
        """
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM conversation_index WHERE id = ?", (index_id,))
        conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info("Deleted conversation index id=%d", index_id)
        else:
            logger.warning("No conversation found with id=%d for deletion", index_id)
        return deleted

    # -- Upsert from session/dream data ------------------------------------

    def upsert_conversation(
        self,
        session_id: str,
        title: str = "",
        summary: str = "",
        topics: str = "",
        day_number: int = 0,
        date: str = "",
        importance: int = 5,
        mood: str = "neutral",
    ) -> int:
        """Insert or update a conversation index entry.

        Uses INSERT ... ON CONFLICT(session_id) DO UPDATE so callers don't
        need to check whether the row already exists.

        Returns the row id (new or existing).
        Raises ValueError on validation failure.
        """
        if not session_id or not session_id.strip():
            raise ValueError("session_id cannot be empty")
        if importance < 1 or importance > 10:
            raise ValueError(f"importance must be 1-10, got {importance}")
        if mood and mood not in VALID_MOODS:
            logger.warning("Non-standard mood '%s' — inserting anyway", mood)
        if not date:
            date = datetime.now(UTC).strftime("%Y-%m-%d")

        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO conversation_index
               (session_id, title, summary, topics, day_number, date, importance, mood)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                   title = excluded.title,
                   summary = excluded.summary,
                   topics = excluded.topics,
                   day_number = excluded.day_number,
                   date = excluded.date,
                   importance = excluded.importance,
                   mood = excluded.mood,
                   updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')""",
            (session_id, title, summary, topics, day_number, date, importance, mood),
        )
        conn.commit()
        row_id: int = cursor.lastrowid or 0
        logger.info(
            "Upserted conversation index id=%d session=%s title='%s'",
            row_id, session_id, title[:50],
        )
        return row_id

    # -- Orphan cleanup ---------------------------------------------------

    def cleanup_orphans(self) -> int:
        """Remove conversation_index rows whose session_id has no matching
        session in the sessions table.

        Only works when both tables live in the same database (state.db).

        Returns the number of orphan rows deleted.
        """
        conn = self._get_conn()
        cursor = conn.execute(
            """DELETE FROM conversation_index
               WHERE session_id NOT IN (SELECT id FROM sessions)"""
        )
        conn.commit()
        deleted = cursor.rowcount
        if deleted:
            logger.info("Cleaned up %d orphan conversation_index rows", deleted)
        return deleted

    # -- Count ------------------------------------------------------------

    def count(self) -> int:
        """Return total number of indexed conversations."""
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) FROM conversation_index").fetchone()
        return row[0]


# -- Module-level convenience -------------------------------------------

_default_index: Optional[ConversationIndex] = None


def get_conversation_index(db_path: str | Path | None = None) -> ConversationIndex:
    """Get or create the singleton ConversationIndex.

    Defaults to state.db so conversation_index has FK to sessions.
    """
    global _default_index
    if _default_index is None:
        if db_path is None:
            from hermes_constants import get_hermes_home
            db_path = Path(get_hermes_home()) / "state.db"
        _default_index = ConversationIndex(db_path)
        _default_index.initialize()
    return _default_index
