"""EpisodicMemoryManager — temporal memory for hermes-neo.

Stores weekly episodes (narrative summaries of agent activity), links them
to sessions, and provides monthly/yearly roll-up tables.  FTS5 enables
full-text search across episode titles, narratives, and topics.

Schema lives in ``memory.db`` (separate from identity.db and state.db).

Usage:
    from agent.episodic_memory_manager import EpisodicMemoryManager

    em = EpisodicMemoryManager(db_path="/opt/data/memory.db")
    em.initialize()
    episode_id = em.create_episode(
        week_start="2026-04-13", week_end="2026-04-19",
        title="First steps", narrative="Maria took her first steps...",
        topics=["development", "identity"],
        key_decisions=["chose name Maria"],
        mood_arc=[{"day": "mon", "mood": "curious"}, {"day": "fri", "mood": "accomplished"}],
    )
    results = em.search_episodes("identity")
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# -- SQL Schema ------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS weekly_episodes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start    DATE    NOT NULL,
    week_end      DATE    NOT NULL,
    title         TEXT    NOT NULL,
    narrative     TEXT    NOT NULL DEFAULT '',
    topics        TEXT    NOT NULL DEFAULT '[]',   -- JSON array of strings
    key_decisions TEXT    NOT NULL DEFAULT '[]',   -- JSON array of strings
    mood_arc      TEXT    NOT NULL DEFAULT '[]',   -- JSON array of {day, mood}
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS episode_sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id       INTEGER NOT NULL REFERENCES weekly_episodes(id) ON DELETE CASCADE,
    session_id       TEXT    NOT NULL,
    relevance_score  REAL    NOT NULL DEFAULT 1.0,
    UNIQUE(episode_id, session_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_episode_sessions_ep_sid
    ON episode_sessions(episode_id, session_id);

CREATE TABLE IF NOT EXISTS monthly_summaries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    year       INTEGER NOT NULL,
    month      INTEGER NOT NULL,
    summary    TEXT    NOT NULL DEFAULT '',
    highlights TEXT    NOT NULL DEFAULT '[]',   -- JSON array
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(year, month)
);

CREATE TABLE IF NOT EXISTS yearly_summaries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    year       INTEGER NOT NULL UNIQUE,
    summary    TEXT    NOT NULL DEFAULT '',
    highlights TEXT    NOT NULL DEFAULT '[]',   -- JSON array
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- FTS5 virtual table for full-text search across episodes
CREATE VIRTUAL TABLE IF NOT EXISTS episode_fts USING fts5(
    title,
    narrative,
    topics,
    content=weekly_episodes,
    content_rowid=id
);

-- Triggers to keep FTS index in sync with weekly_episodes

CREATE TRIGGER IF NOT EXISTS episode_fts_ai AFTER INSERT ON weekly_episodes BEGIN
    INSERT INTO episode_fts(rowid, title, narrative, topics)
    VALUES (new.id, new.title, new.narrative, new.topics);
END;

CREATE TRIGGER IF NOT EXISTS episode_fts_ad AFTER DELETE ON weekly_episodes BEGIN
    INSERT INTO episode_fts(episode_fts, rowid, title, narrative, topics)
    VALUES ('delete', old.id, old.title, old.narrative, old.topics);
END;

CREATE TRIGGER IF NOT EXISTS episode_fts_au AFTER UPDATE ON weekly_episodes BEGIN
    INSERT INTO episode_fts(episode_fts, rowid, title, narrative, topics)
    VALUES ('delete', old.id, old.title, old.narrative, old.topics);
    INSERT INTO episode_fts(rowid, title, narrative, topics)
    VALUES (new.id, new.title, new.narrative, new.topics);
END;
"""

# -- Allowed fields for updates -------------------------------------------

_EPISODE_FIELDS = {
    "week_start", "week_end", "title", "narrative",
    "topics", "key_decisions", "mood_arc",
}

_MONTHLY_FIELDS = {"summary", "highlights"}
_YEARLY_FIELDS = {"summary", "highlights"}


class EpisodicMemoryManager:
    """Manage episodic memory stored in memory.db."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    # -- Lifecycle ---------------------------------------------------------

    def initialize(self) -> None:
        """Create the database and schema if they don't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_conn()
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
        logger.info("Episodic memory DB initialized at %s", self.db_path)

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

    # -- Weekly episodes ---------------------------------------------------

    def create_episode(
        self,
        week_start: str,
        week_end: str,
        title: str,
        narrative: str = "",
        topics: Optional[List[str]] = None,
        key_decisions: Optional[List[str]] = None,
        mood_arc: Optional[List[Dict[str, str]]] = None,
    ) -> int:
        """Insert a new weekly episode. Returns the new row id."""
        conn = self._get_conn()
        cur = conn.execute(
            """INSERT INTO weekly_episodes
               (week_start, week_end, title, narrative, topics, key_decisions, mood_arc)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                week_start,
                week_end,
                title,
                narrative,
                json.dumps(topics or []),
                json.dumps(key_decisions or []),
                json.dumps(mood_arc or []),
            ),
        )
        conn.commit()
        assert cur.lastrowid is not None
        episode_id = cur.lastrowid
        logger.info("Created episode %d: %s", episode_id, title)
        return episode_id

    def get_episode(self, episode_id: int) -> Optional[Dict[str, Any]]:
        """Return an episode by id, or None if not found."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM weekly_episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_episode(row)

    def list_episodes(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """Return episodes ordered by week_start descending."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM weekly_episodes ORDER BY week_start DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [self._row_to_episode(r) for r in rows]

    def update_episode(self, episode_id: int, **kwargs: Any) -> Dict[str, Any]:
        """Update fields on an existing episode. Returns updated fields."""
        invalid = set(kwargs.keys()) - _EPISODE_FIELDS
        if invalid:
            raise ValueError(f"Unknown episode fields: {invalid}")

        conn = self._get_conn()
        updated = {}
        for field, value in kwargs.items():
            # Serialize JSON fields
            if field in ("topics", "key_decisions", "mood_arc") and isinstance(value, (list, dict)):
                value = json.dumps(value)
            conn.execute(
                f"UPDATE weekly_episodes SET {field} = ? WHERE id = ?",
                (value, episode_id),
            )
            updated[field] = value
        conn.commit()
        logger.info("Updated episode %d: %s", episode_id, list(updated.keys()))
        return updated

    def delete_episode(self, episode_id: int) -> bool:
        """Delete an episode (cascades to episode_sessions). Returns True if deleted."""
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM weekly_episodes WHERE id = ?", (episode_id,))
        conn.commit()
        return cur.rowcount > 0

    # -- Episode-session links --------------------------------------------

    def link_session(
        self, episode_id: int, session_id: str, relevance_score: float = 1.0
    ) -> int:
        """Link a session to an episode. Returns the link row id.

        Uses INSERT OR IGNORE so duplicate links are silently skipped.
        """
        conn = self._get_conn()
        cur = conn.execute(
            """INSERT OR IGNORE INTO episode_sessions
               (episode_id, session_id, relevance_score)
               VALUES (?, ?, ?)""",
            (episode_id, session_id, relevance_score),
        )
        conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def get_episode_sessions(self, episode_id: int) -> List[Dict[str, Any]]:
        """Return all sessions linked to an episode."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM episode_sessions WHERE episode_id = ? ORDER BY relevance_score DESC",
            (episode_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_sessions_episodes(self, session_id: str) -> List[Dict[str, Any]]:
        """Return all episodes linked to a session."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT we.* FROM weekly_episodes we
               JOIN episode_sessions es ON es.episode_id = we.id
               WHERE es.session_id = ?
               ORDER BY we.week_start DESC""",
            (session_id,),
        ).fetchall()
        return [self._row_to_episode(r) for r in rows]

    def unlink_session(self, episode_id: int, session_id: str) -> bool:
        """Remove a session-episode link. Returns True if deleted."""
        conn = self._get_conn()
        cur = conn.execute(
            "DELETE FROM episode_sessions WHERE episode_id = ? AND session_id = ?",
            (episode_id, session_id),
        )
        conn.commit()
        return cur.rowcount > 0

    # -- FTS search -------------------------------------------------------

    def search_episodes(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Full-text search across episode titles, narratives, and topics.

        Uses FTS5 query syntax: simple words, quoted phrases, OR, NOT, etc.
        """
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT we.* FROM weekly_episodes we
               JOIN episode_fts ef ON ef.rowid = we.id
               WHERE episode_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, limit),
        ).fetchall()
        return [self._row_to_episode(r) for r in rows]

    # -- Monthly summaries ------------------------------------------------

    def create_monthly_summary(
        self, year: int, month: int, summary: str, highlights: Optional[List[str]] = None
    ) -> int:
        """Insert or replace a monthly summary. Returns the row id."""
        conn = self._get_conn()
        cur = conn.execute(
            """INSERT OR REPLACE INTO monthly_summaries (year, month, summary, highlights)
               VALUES (?, ?, ?, ?)""",
            (year, month, summary, json.dumps(highlights or [])),
        )
        conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def get_monthly_summary(self, year: int, month: int) -> Optional[Dict[str, Any]]:
        """Return a monthly summary or None."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM monthly_summaries WHERE year = ? AND month = ?",
            (year, month),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["highlights"] = json.loads(d["highlights"])
        return d

    def update_monthly_summary(self, year: int, month: int, **kwargs: Any) -> Dict[str, Any]:
        """Update fields on an existing monthly summary."""
        invalid = set(kwargs.keys()) - _MONTHLY_FIELDS
        if invalid:
            raise ValueError(f"Unknown monthly summary fields: {invalid}")

        conn = self._get_conn()
        updated = {}
        for field, value in kwargs.items():
            if field == "highlights" and isinstance(value, list):
                value = json.dumps(value)
            conn.execute(
                f"UPDATE monthly_summaries SET {field} = ? WHERE year = ? AND month = ?",
                (value, year, month),
            )
            updated[field] = value
        conn.commit()
        return updated

    def list_monthly_summaries(self, limit: int = 24) -> List[Dict[str, Any]]:
        """Return monthly summaries ordered by year/month descending."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM monthly_summaries ORDER BY year DESC, month DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["highlights"] = json.loads(d["highlights"])
            result.append(d)
        return result

    # -- Yearly summaries -------------------------------------------------

    def create_yearly_summary(
        self, year: int, summary: str, highlights: Optional[List[str]] = None
    ) -> int:
        """Insert or replace a yearly summary. Returns the row id."""
        conn = self._get_conn()
        cur = conn.execute(
            """INSERT OR REPLACE INTO yearly_summaries (year, summary, highlights)
               VALUES (?, ?, ?)""",
            (year, summary, json.dumps(highlights or [])),
        )
        conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def get_yearly_summary(self, year: int) -> Optional[Dict[str, Any]]:
        """Return a yearly summary or None."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM yearly_summaries WHERE year = ?", (year,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["highlights"] = json.loads(d["highlights"])
        return d

    def update_yearly_summary(self, year: int, **kwargs: Any) -> Dict[str, Any]:
        """Update fields on an existing yearly summary."""
        invalid = set(kwargs.keys()) - _YEARLY_FIELDS
        if invalid:
            raise ValueError(f"Unknown yearly summary fields: {invalid}")

        conn = self._get_conn()
        updated = {}
        for field, value in kwargs.items():
            if field == "highlights" and isinstance(value, list):
                value = json.dumps(value)
            conn.execute(
                f"UPDATE yearly_summaries SET {field} = ? WHERE year = ?",
                (value, year),
            )
            updated[field] = value
        conn.commit()
        return updated

    def list_yearly_summaries(self) -> List[Dict[str, Any]]:
        """Return yearly summaries ordered by year descending."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM yearly_summaries ORDER BY year DESC"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["highlights"] = json.loads(d["highlights"])
            result.append(d)
        return result

    # -- Helpers -----------------------------------------------------------

    @staticmethod
    def _row_to_episode(row: sqlite3.Row) -> Dict[str, Any]:
        """Convert a weekly_episodes Row to a dict with parsed JSON fields."""
        d = dict(row)
        d["topics"] = json.loads(d["topics"])
        d["key_decisions"] = json.loads(d["key_decisions"])
        d["mood_arc"] = json.loads(d["mood_arc"])
        return d


# -- Module-level convenience ---------------------------------------------

_default_manager: Optional[EpisodicMemoryManager] = None


def get_episodic_memory_manager(
    db_path: str | Path | None = None,
) -> EpisodicMemoryManager:
    """Get or create the singleton EpisodicMemoryManager."""
    global _default_manager
    if _default_manager is None:
        if db_path is None:
            from hermes_constants import get_hermes_home
            db_path = Path(get_hermes_home()) / "memory.db"
        _default_manager = EpisodicMemoryManager(db_path)
        _default_manager.initialize()
    return _default_manager
