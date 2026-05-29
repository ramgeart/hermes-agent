"""
Conversation Index — searchable index of session metadata with FTS5.
Maps sessions (from state.db) to summaries, topics, importance, etc.
"""

import sqlite3
import os
from datetime import datetime, timedelta
from typing import Optional


def _get_hermes_home() -> str:
    """Get hermes home directory. Try hermes_constants first, fall back to env."""
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    except ImportError:
        return os.environ.get("HERMES_HOME", "/opt/hermes-neo")


class ConversationIndex:
    """Manages the conversation_index table in memory.db with FTS5 search."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def initialize(self):
        """Create the conversation_index schema (table, FTS5, triggers, indexes)."""
        conn = self._get_conn()
        statements = [
            """CREATE TABLE IF NOT EXISTS conversation_index (
                idx_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id    TEXT NOT NULL UNIQUE,
                title         TEXT NOT NULL DEFAULT '',
                summary       TEXT NOT NULL DEFAULT '',
                topics        TEXT NOT NULL DEFAULT '',
                day_number    INTEGER NOT NULL DEFAULT 0,
                date          TEXT NOT NULL DEFAULT '',
                weekday       TEXT NOT NULL DEFAULT '',
                importance    INTEGER DEFAULT 5,
                mood          TEXT DEFAULT '',
                participants  TEXT DEFAULT '',
                tools_used    TEXT DEFAULT '',
                tokens_total  INTEGER DEFAULT 0,
                msg_count     INTEGER DEFAULT 0,
                consolidated  BOOLEAN DEFAULT 0,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE VIRTUAL TABLE IF NOT EXISTS conversation_fts USING fts5(
                title, summary, topics,
                content=conversation_index,
                content_rowid=idx_id
            )""",
            """CREATE TRIGGER IF NOT EXISTS conv_idx_ai AFTER INSERT ON conversation_index BEGIN
                INSERT INTO conversation_fts(rowid, title, summary, topics)
                VALUES (new.idx_id, new.title, new.summary, new.topics);
            END""",
            """CREATE TRIGGER IF NOT EXISTS conv_idx_au AFTER UPDATE ON conversation_index BEGIN
                INSERT INTO conversation_fts(conversation_fts, rowid, title, summary, topics)
                VALUES ('delete', old.idx_id, old.title, old.summary, old.topics);
                INSERT INTO conversation_fts(rowid, title, summary, topics)
                VALUES (new.idx_id, new.title, new.summary, new.topics);
            END""",
            """CREATE TRIGGER IF NOT EXISTS conv_idx_ad AFTER DELETE ON conversation_index BEGIN
                INSERT INTO conversation_fts(conversation_fts, rowid, title, summary, topics)
                VALUES ('delete', old.idx_id, old.title, old.summary, old.topics);
            END""",
            """CREATE TRIGGER IF NOT EXISTS conv_idx_updated_at AFTER UPDATE ON conversation_index
            FOR EACH ROW BEGIN
                UPDATE conversation_index SET updated_at = CURRENT_TIMESTAMP WHERE idx_id = NEW.idx_id;
            END""",
            "CREATE INDEX IF NOT EXISTS idx_conv_date ON conversation_index(date DESC)",
            "CREATE INDEX IF NOT EXISTS idx_conv_day ON conversation_index(day_number DESC)",
            "CREATE INDEX IF NOT EXISTS idx_conv_importance ON conversation_index(importance DESC)",
        ]
        for sql in statements:
            conn.execute(sql)
        conn.commit()

    def add_session(
        self,
        session_id: str,
        title: str,
        summary: str,
        topics: str,
        day_number: int,
        date: str,
        weekday: str,
        importance: int = 5,
        mood: str = "",
        participants: str = "",
        tools_used: str = "",
        tokens_total: int = 0,
        msg_count: int = 0,
    ) -> int:
        """Insert a new session into the index. Returns the idx_id."""
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO conversation_index
               (session_id, title, summary, topics, day_number, date, weekday,
                importance, mood, participants, tools_used, tokens_total, msg_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, title, summary, topics, day_number, date, weekday,
             importance, mood, participants, tools_used, tokens_total, msg_count),
        )
        conn.commit()
        return cursor.lastrowid

    def update_session(self, session_id: str, **kwargs):
        """Update fields of an existing session by session_id."""
        if not kwargs:
            return
        allowed = {
            "title", "summary", "topics", "day_number", "date", "weekday",
            "importance", "mood", "participants", "tools_used",
            "tokens_total", "msg_count", "consolidated",
        }
        filtered = {k: v for k, v in kwargs.items() if k in allowed}
        if not filtered:
            return
        set_clause = ", ".join(f"{k} = ?" for k in filtered)
        values = list(filtered.values()) + [session_id]
        conn = self._get_conn()
        conn.execute(
            f"UPDATE conversation_index SET {set_clause} WHERE session_id = ?",
            values,
        )
        conn.commit()

    def get_session(self, session_id: str) -> Optional[dict]:
        """Get a single session by session_id."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM conversation_index WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_recent_sessions(self, days: int = 14) -> list[dict]:
        """Get sessions from the last N days, ordered by date desc, importance desc."""
        conn = self._get_conn()
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            """SELECT * FROM conversation_index
               WHERE date >= ?
               ORDER BY date DESC, importance DESC""",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Full-text search across title, summary, and topics."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT ci.* FROM conversation_index ci
               JOIN conversation_fts fts ON ci.idx_id = fts.rowid
               WHERE conversation_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_sessions_by_week(self, week_start_date: str) -> list[dict]:
        """Get all sessions in a week starting from week_start_date (YYYY-MM-DD)."""
        conn = self._get_conn()
        start = datetime.strptime(week_start_date, "%Y-%m-%d")
        end = start + timedelta(days=7)
        rows = conn.execute(
            """SELECT * FROM conversation_index
               WHERE date >= ? AND date < ?
               ORDER BY date DESC, importance DESC""",
            (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_consolidated(self, session_id: str):
        """Mark a session as consolidated."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE conversation_index SET consolidated = 1 WHERE session_id = ?",
            (session_id,),
        )
        conn.commit()

    def get_unconsolidated(self) -> list[dict]:
        """Get all sessions not yet consolidated."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM conversation_index
               WHERE consolidated = 0
               ORDER BY date DESC, importance DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        """Return summary statistics about the conversation index."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT
                COUNT(*) as total_sessions,
                MIN(date) as earliest_date,
                MAX(date) as latest_date,
                AVG(importance) as avg_importance,
                SUM(msg_count) as total_messages,
                SUM(tokens_total) as total_tokens,
                SUM(CASE WHEN consolidated = 1 THEN 1 ELSE 0 END) as consolidated_count
               FROM conversation_index"""
        ).fetchone()
        return dict(row) if row else {}

    def get_index_prompt_block(self, days: int = 14) -> str:
        """Format recent sessions as a system prompt block."""
        sessions = self.get_recent_sessions(days=days)
        if not sessions:
            return ""

        lines = [f"═══ CONVERSATION INDEX (últimos {days} días) ═══"]

        # Group by date
        by_date: dict[str, list[dict]] = {}
        for s in sessions:
            d = s.get("date", "")
            if d not in by_date:
                by_date[d] = []
            by_date[d].append(s)

        for date, day_sessions in sorted(by_date.items(), reverse=True):
            if day_sessions:
                first = day_sessions[0]
                day_num = first.get("day_number", 0)
                weekday = first.get("weekday", "")
                lines.append(f"[{date}] Día {day_num} — {weekday}")
                for s in sorted(day_sessions, key=lambda x: x.get("importance", 0), reverse=True):
                    title = s.get("title", "Sin título")
                    topics = s.get("topics", "")
                    importance = s.get("importance", 5)
                    topic_str = f" [{topics}]" if topics else ""
                    lines.append(f'  • "{title}"{topic_str} — imp:{importance}')

        return "\n".join(lines)


# Module-level singleton
_conversation_index: Optional[ConversationIndex] = None


def get_conversation_index(db_path: Optional[str] = None) -> ConversationIndex:
    """Get or create the singleton ConversationIndex instance."""
    global _conversation_index
    if _conversation_index is not None and (db_path is None or db_path == _conversation_index.db_path):
        return _conversation_index
    if db_path is None:
        hermes_home = _get_hermes_home()
        db_path = os.path.join(hermes_home, "memory.db")
    _conversation_index = ConversationIndex(db_path)
    return _conversation_index
