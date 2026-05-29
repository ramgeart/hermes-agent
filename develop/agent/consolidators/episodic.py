"""Weekly episodic consolidation — gathers sessions from a target week and
produces an episodic memory snapshot via the EpisodicMemoryManager.

The consolidator is designed to be invoked by the Dream Processor or by a
cron job.  It is stateless: each call determines the target week, queries
the session store, summarises, and persists.

Usage::

    from agent.consolidators.episodic import WeeklyConsolidator

    wc = WeeklyConsolidator(state_db_path="~/.hermes/state.db",
                            memory_db_path="~/.hermes/memory.db")
    if wc.can_run():
        episode = wc.run()           # default: most recent completed week
        episode = wc.run(week_start="2026-04-13")  # explicit week

LLM summarisation is pluggable via the ``summariser`` callable passed to
``__init__``.  Signature::

    def summariser(sessions: list[dict]) -> dict:
        # Must return at least {"title": str, "narrative": str}.
        # Optional: "topics", "key_decisions", "mood_arc".

If *summariser* is ``None`` (the default) a simple heuristic fallback is
used that concatenates session titles.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypedDict

logger = logging.getLogger(__name__)

# Mood scale used by the heuristic fallback
_MOOD_SCALE = ["very_negative", "negative", "neutral", "positive", "very_positive"]


class ConsolidationResult(TypedDict, total=False):
    """Return type of ``run()`` — mirrors the episode row."""
    episode_id: int
    week_start: str
    week_end: str
    title: str
    narrative: str
    topics: List[str]
    key_decisions: List[str]
    mood_arc: List[Dict[str, str]]
    session_count: int


# -- Helpers ----------------------------------------------------------------

def _most_recent_completed_week(ref: date | None = None) -> tuple[str, str]:
    """Return (week_start, week_end) for the most recent completed Mon–Sun.

    If *ref* is ``None``, uses today (UTC).  A "completed" week is one whose
    Sunday is strictly before the reference date.
    """
    today = ref or date.today()
    # Days since last Monday (Monday=0)
    days_since_monday = today.weekday()
    # Last Sunday
    last_sunday = today - timedelta(days=days_since_monday + 1)
    last_monday = last_sunday - timedelta(days=6)
    return last_monday.isoformat(), last_sunday.isoformat()


def _date_range_to_unix(start: str, end: str) -> tuple[float, float]:
    """Convert ISO date strings to Unix timestamps (start of day, end of day)."""
    dt_start = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    dt_end = datetime.strptime(end, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=timezone.utc
    )
    return dt_start.timestamp(), dt_end.timestamp()


def _weekday_name(iso_date: str) -> str:
    """Return lowercase weekday name for an ISO date string."""
    return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%A").lower()


# -- Consolidator -----------------------------------------------------------

class WeeklyConsolidator:
    """Gather sessions for a target week and persist an episodic snapshot."""

    def __init__(
        self,
        state_db_path: str | Path,
        memory_db_path: str | Path,
        summariser: Optional[Callable[[List[Dict[str, Any]]], Dict[str, Any]]] = None,
    ):
        self._state_db = Path(state_db_path).expanduser()
        self._memory_db = Path(memory_db_path).expanduser()
        self._summariser = summariser

    # -- Public API ---------------------------------------------------------

    def can_run(self, week_start: str | None = None) -> bool:
        """Return True if at least one session exists for the target week.

        If *week_start* is provided the week is derived from it (Mon–Sun);
        otherwise the most recent completed week is used.
        """
        ws, we = self._resolve_week(week_start)
        sessions = self._fetch_sessions(ws, we)
        return len(sessions) > 0

    def run(self, week_start: str | None = None) -> Optional[ConsolidationResult]:
        """Run consolidation for the target week.

        Returns the created episode dict, or ``None`` if no sessions exist
        (safe no-op).
        """
        ws, we = self._resolve_week(week_start)
        sessions = self._fetch_sessions(ws, we)

        if not sessions:
            logger.info("No sessions found for week %s–%s, skipping.", ws, we)
            return None

        logger.info("Consolidating %d sessions for week %s–%s", len(sessions), ws, we)

        # Summarise
        summary = self._summarise(sessions)

        # Persist — create a dedicated instance (don't use the global singleton,
        # which would conflict when multiple consolidators run with different DBs).
        from agent.episodic_memory_manager import EpisodicMemoryManager

        em = EpisodicMemoryManager(self._memory_db)
        em.initialize()
        episode_id = em.create_episode(
            week_start=ws,
            week_end=we,
            title=summary["title"],
            narrative=summary.get("narrative", ""),
            topics=summary.get("topics", []),
            key_decisions=summary.get("key_decisions", []),
            mood_arc=summary.get("mood_arc", []),
        )

        # Link sessions
        for s in sessions:
            em.link_session(episode_id, s["session_id"])

        result: ConsolidationResult = {
            "episode_id": episode_id,
            "week_start": ws,
            "week_end": we,
            "title": summary["title"],
            "narrative": summary.get("narrative", ""),
            "topics": summary.get("topics", []),
            "key_decisions": summary.get("key_decisions", []),
            "mood_arc": summary.get("mood_arc", []),
            "session_count": len(sessions),
        }
        logger.info("Created episode %d with %d linked sessions", episode_id, len(sessions))
        return result

    # -- Internals ----------------------------------------------------------

    def _resolve_week(self, week_start: str | None) -> tuple[str, str]:
        """Return (week_start_iso, week_end_iso) from explicit or default."""
        if week_start is not None:
            ws = datetime.strptime(week_start, "%Y-%m-%d").date()
            we = ws + timedelta(days=6)
            return ws.isoformat(), we.isoformat()
        return _most_recent_completed_week()

    def _fetch_sessions(self, week_start: str, week_end: str) -> List[Dict[str, Any]]:
        """Query state.db for sessions whose started_at falls within the week."""
        ts_start, ts_end = _date_range_to_unix(week_start, week_end)

        if not self._state_db.exists():
            logger.warning("State DB not found at %s", self._state_db)
            return []

        conn = sqlite3.connect(str(self._state_db))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT id AS session_id, title, source, model,
                          started_at, ended_at, message_count
                   FROM sessions
                   WHERE started_at >= ? AND started_at <= ?
                   ORDER BY started_at ASC""",
                (ts_start, ts_end),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _summarise(self, sessions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Summarise sessions using the pluggable summariser or fallback."""
        if self._summariser is not None:
            try:
                result = self._summariser(sessions)
                if "title" in result and "narrative" in result:
                    return result
                logger.warning("Summariser returned incomplete result, using fallback.")
            except Exception:
                logger.warning("Summariser failed, using fallback.", exc_info=True)

        return self._heuristic_summary(sessions)

    @staticmethod
    def _heuristic_summary(sessions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Simple fallback: concatenate titles, infer basic metadata."""
        titles = [s.get("title") or s.get("session_id", "untitled") for s in sessions]
        count = len(sessions)

        # Title
        if count == 1:
            title = f"Weekly recap: {titles[0][:70]}"
        else:
            title = f"Weekly recap: {count} sessions"

        # Narrative (simple concatenation)
        narrative = (
            f"This week had {count} session(s). "
            + "Activities included: "
            + "; ".join(titles[:10])
            + "."
        )

        # Extract topics from session titles (crude keyword extraction)
        topics = list(dict.fromkeys(
            t.split(":")[0].strip().lower()
            for t in titles
            if ":" in t
        ))[:7]
        if not topics:
            topics = ["general"]

        # No key decisions or mood arc from heuristic
        return {
            "title": title[:80],
            "narrative": narrative,
            "topics": topics,
            "key_decisions": [],
            "mood_arc": [],
        }
