"""Episodic Memory — high-level programmatic API for hermes-neo temporal memory.

Wraps :class:`EpisodicMemoryManager` with typed dataclasses, auto-computed
fields, and the hierarchical zoom-navigation view (Year → Month → Week →
Day → Session → Moment).

Usage::

    from agent.episodic_memory import EpisodicMemory

    em = EpisodicMemory(db_path="/opt/data/memory.db")

    # Create an episode (week_end auto-computed)
    ep = em.create_episode(
        week_start="2026-04-13",
        title="Identity system built",
        narrative="Implemented the immutable identity anchor for Maria.",
        topics=["identity", "sqlite"],
        key_decisions=["separate DB for episodes"],
        mood_arc=[{"day": "mon", "mood": "curious"}],
        session_ids=["sess_abc", "sess_def"],
    )

    # Search
    results = em.search_episodes("identity")

    # Zoom navigation
    tree = em.zoom_navigation()
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class EpisodeSession:
    """A session linked to an episode."""

    id: int
    episode_id: int
    session_id: str
    relevance_score: float = 1.0


@dataclass
class Episode:
    """A weekly episode — narrative summary of agent activity."""

    id: int
    week_start: str
    week_end: str
    title: str
    narrative: str = ""
    topics: List[str] = field(default_factory=list)
    key_decisions: List[str] = field(default_factory=list)
    mood_arc: List[Dict[str, Any]] = field(default_factory=list)
    created_at: Optional[str] = None
    sessions: List[EpisodeSession] = field(default_factory=list)


@dataclass
class ZoomWeek:
    """Week-level node in the zoom hierarchy."""

    episode_id: int
    week_start: str
    week_end: str
    title: str
    narrative: str
    days: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    # days maps "YYYY-MM-DD" -> [{"session_id": ..., "relevance": ...}]


@dataclass
class ZoomMonth:
    """Month-level node in the zoom hierarchy."""

    year: int
    month: int
    summary: str
    highlights: List[str] = field(default_factory=list)
    weeks: List[ZoomWeek] = field(default_factory=list)


@dataclass
class ZoomYear:
    """Year-level node in the zoom hierarchy."""

    year: int
    summary: str
    highlights: List[str] = field(default_factory=list)
    months: List[ZoomMonth] = field(default_factory=list)


@dataclass
class MonthlySummary:
    """A monthly summary aggregating weekly episodes."""

    id: int
    year: int
    month: int
    summary: str
    highlights: List[str] = field(default_factory=list)
    created_at: Optional[str] = None


@dataclass
class YearlySummary:
    """A yearly summary aggregating monthly summaries."""

    id: int
    year: int
    summary: str
    highlights: List[str] = field(default_factory=list)
    created_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Default synthesis helpers (deterministic, no LLM required)
# ---------------------------------------------------------------------------

def _default_monthly_synthesis(
    episodes: List[Episode],
) -> tuple[str, List[str]]:
    """Build a monthly summary by combining episode narratives.

    Returns (summary_text, highlights) without requiring an LLM.
    """
    titles = [ep.title for ep in episodes]
    narratives = [ep.narrative for ep in episodes if ep.narrative]
    all_topics: list[str] = []
    all_decisions: list[str] = []
    for ep in episodes:
        all_topics.extend(ep.topics)
        all_decisions.extend(ep.key_decisions)

    # Build summary from episode narratives
    summary_parts: list[str] = []
    if titles:
        summary_parts.append(
            f"This month covered {len(episodes)} episode(s): "
            + "; ".join(titles) + "."
        )
    for nar in narratives:
        summary_parts.append(nar)

    summary_text = "\n\n".join(summary_parts)

    # Highlights: top unique topics + key decisions (up to 5)
    seen: set[str] = set()
    highlights: list[str] = []
    for item in all_topics + all_decisions:
        if item not in seen:
            seen.add(item)
            highlights.append(item)
        if len(highlights) >= 5:
            break

    return summary_text, highlights


def _default_yearly_synthesis(
    summaries: List[MonthlySummary],
) -> tuple[str, List[str]]:
    """Build a yearly summary from monthly summaries.

    Returns (summary_text, highlights) without requiring an LLM.
    """
    month_names = [
        "", "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ]

    summary_parts: list[str] = [
        f"Year in review: {summaries[0].year}. "
        f"{len(summaries)} month(s) summarized."
    ]
    for ms in summaries:
        month_label = month_names[ms.month] if ms.month <= 12 else str(ms.month)
        summary_parts.append(f"\n--- {month_label} ---\n{ms.summary}")

    summary_text = "\n".join(summary_parts)

    # Aggregate highlights from all months (up to 10)
    seen: set[str] = set()
    highlights: list[str] = []
    for ms in summaries:
        for h in ms.highlights:
            if h not in seen:
                seen.add(h)
                highlights.append(h)
            if len(highlights) >= 10:
                break
        if len(highlights) >= 10:
            break

    return summary_text, highlights


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------

class EpisodicMemory:
    """High-level episodic memory API for hermes-neo.

    Wraps the low-level :class:`EpisodicMemoryManager` with:
    - Typed dataclass return values.
    - Auto-computed ``week_end`` (week_start + 6 days).
    - Session linking during episode creation.
    - Hierarchical zoom navigation (Year → Month → Week → Day → Session).
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    # -- Lifecycle ---------------------------------------------------------

    def initialize(self) -> None:
        """Create the database, tables, FTS5 index, and triggers if absent."""
        from agent.episodic_memory_manager import _SCHEMA_SQL

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_conn()
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
        logger.info("Episodic memory initialized at %s", self.db_path)

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def close(self) -> None:
        """Close the underlying database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    # -- CRUD --------------------------------------------------------------

    def create_episode(
        self,
        week_start: str,
        title: str,
        narrative: str = "",
        topics: Optional[List[str]] = None,
        key_decisions: Optional[List[str]] = None,
        mood_arc: Optional[List[Dict[str, Any]]] = None,
        session_ids: Optional[List[str]] = None,
    ) -> Episode:
        """Create a weekly episode and optionally link sessions.

        ``week_end`` is auto-computed as ``week_start + 6 days``.

        Args:
            week_start: ISO date string for the first day of the week (YYYY-MM-DD).
            title: Short descriptive title for the episode.
            narrative: Longer narrative summary of the week's activity.
            topics: List of topic tags (stored as JSON array).
            key_decisions: Important decisions made during the week.
            mood_arc: List of dicts with day/mood entries.
            session_ids: Session IDs to link to this episode.

        Returns:
            The newly created :class:`Episode` with its generated id and
            linked sessions populated.
        """
        ws = date.fromisoformat(week_start)
        we = ws + timedelta(days=6)
        week_end_str = we.isoformat()

        conn = self._get_conn()
        cur = conn.execute(
            """INSERT INTO weekly_episodes
               (week_start, week_end, title, narrative, topics, key_decisions, mood_arc)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                week_start,
                week_end_str,
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

        # Link sessions
        linked_sessions: List[EpisodeSession] = []
        if session_ids:
            for sid in session_ids:
                conn.execute(
                    """INSERT OR IGNORE INTO episode_sessions
                       (episode_id, session_id, relevance_score)
                       VALUES (?, ?, 1.0)""",
                    (episode_id, sid),
                )
            conn.commit()
            linked_sessions = self._get_linked_sessions(episode_id)

        logger.info("Created episode %d: %s", episode_id, title)
        return Episode(
            id=episode_id,
            week_start=week_start,
            week_end=week_end_str,
            title=title,
            narrative=narrative,
            topics=topics or [],
            key_decisions=key_decisions or [],
            mood_arc=mood_arc or [],
            sessions=linked_sessions,
        )

    def get_episode(self, episode_id: int) -> Optional[Episode]:
        """Retrieve an episode by id with its linked sessions.

        Args:
            episode_id: The episode's primary key.

        Returns:
            An :class:`Episode` if found, otherwise ``None``.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM weekly_episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_episode(row, include_sessions=True)

    def search_episodes(self, query: str, limit: int = 10) -> List[Episode]:
        """Full-text search across episode titles, narratives, and topics.

        Uses FTS5 query syntax (words, quoted phrases, OR, NOT, prefix*).

        Args:
            query: FTS5 search expression.
            limit: Maximum number of results to return.

        Returns:
            List of :class:`Episode` objects ranked by FTS5 relevance.
        """
        if not query or not query.strip():
            return []

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

    def list_episodes(self, limit: int = 20, offset: int = 0) -> List[Episode]:
        """Paginated listing of episodes ordered by week_start descending.

        Args:
            limit: Maximum episodes per page.
            offset: Number of episodes to skip.

        Returns:
            List of :class:`Episode` objects.
        """
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM weekly_episodes
               ORDER BY week_start DESC
               LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        return [self._row_to_episode(r) for r in rows]

    # -- Zoom Navigation ---------------------------------------------------

    def zoom_navigation(self) -> List[ZoomYear]:
        """Return the hierarchical temporal structure for zoom navigation.

        Structure::

            Year (summary)
              └── Month (summary)
                    └── Week (title + narrative)
                          └── Day (YYYY-MM-DD)
                                └── Session (session_id + relevance)

        The hierarchy is built from:
        - ``yearly_summaries`` for Year-level nodes.
        - ``monthly_summaries`` for Month-level nodes.
        - ``weekly_episodes`` for Week-level nodes (filtered by month).
        - ``episode_sessions`` for Day/Session-level drill-down.

        Returns:
            A list of :class:`ZoomYear` objects, newest year first.
        """
        conn = self._get_conn()

        # 1. Fetch yearly summaries
        year_rows = conn.execute(
            "SELECT * FROM yearly_summaries ORDER BY year DESC"
        ).fetchall()

        # Collect all years that have episodes (even without yearly summaries)
        episode_years = conn.execute(
            "SELECT DISTINCT CAST(strftime('%Y', week_start) AS INTEGER) AS yr "
            "FROM weekly_episodes ORDER BY yr DESC"
        ).fetchall()

        all_years: Dict[int, Dict[str, Any]] = {}
        for yr_row in episode_years:
            all_years[yr_row["yr"]] = {"summary": "", "highlights": []}
        for yr in year_rows:
            all_years[yr["year"]] = {
                "summary": yr["summary"],
                "highlights": json.loads(yr["highlights"]) if isinstance(yr["highlights"], str) else yr["highlights"],
            }

        result: List[ZoomYear] = []

        for year_num in sorted(all_years.keys(), reverse=True):
            yr_data = all_years[year_num]
            zoom_year = ZoomYear(
                year=year_num,
                summary=yr_data["summary"],
                highlights=yr_data["highlights"],
            )

            # 2. Monthly summaries for this year
            month_rows = conn.execute(
                "SELECT * FROM monthly_summaries WHERE year = ? ORDER BY month DESC",
                (year_num,),
            ).fetchall()

            # Also find months that have episodes but no monthly summary
            episode_months = conn.execute(
                """SELECT DISTINCT CAST(strftime('%m', week_start) AS INTEGER) AS mo
                   FROM weekly_episodes
                   WHERE CAST(strftime('%Y', week_start) AS INTEGER) = ?
                   ORDER BY mo DESC""",
                (year_num,),
            ).fetchall()

            all_months: Dict[int, Dict[str, Any]] = {}
            for mo_row in episode_months:
                all_months[mo_row["mo"]] = {"summary": "", "highlights": []}
            for mo in month_rows:
                all_months[mo["month"]] = {
                    "summary": mo["summary"],
                    "highlights": json.loads(mo["highlights"]) if isinstance(mo["highlights"], str) else mo["highlights"],
                }

            for month_num in sorted(all_months.keys(), reverse=True):
                mo_data = all_months[month_num]
                zoom_month = ZoomMonth(
                    year=year_num,
                    month=month_num,
                    summary=mo_data["summary"],
                    highlights=mo_data["highlights"],
                )

                # 3. Weekly episodes whose week_start falls in this month+year
                week_rows = conn.execute(
                    """SELECT * FROM weekly_episodes
                       WHERE CAST(strftime('%Y', week_start) AS INTEGER) = ?
                         AND CAST(strftime('%m', week_start) AS INTEGER) = ?
                       ORDER BY week_start DESC""",
                    (year_num, month_num),
                ).fetchall()

                for wr in week_rows:
                    zoom_week = ZoomWeek(
                        episode_id=wr["id"],
                        week_start=wr["week_start"],
                        week_end=wr["week_end"],
                        title=wr["title"],
                        narrative=wr["narrative"],
                    )

                    # 4. Day-level: sessions linked to this episode
                    sess_rows = conn.execute(
                        """SELECT session_id, relevance_score
                           FROM episode_sessions
                           WHERE episode_id = ?
                           ORDER BY relevance_score DESC""",
                        (wr["id"],),
                    ).fetchall()

                    for sr in sess_rows:
                        # Sessions don't have a native "day" — we assign to
                        # the episode's week_start as a representative day.
                        # Consumers can refine via session metadata.
                        day_key = wr["week_start"]
                        if day_key not in zoom_week.days:
                            zoom_week.days[day_key] = []
                        zoom_week.days[day_key].append({
                            "session_id": sr["session_id"],
                            "relevance": sr["relevance_score"],
                        })

                    zoom_month.weeks.append(zoom_week)

                zoom_year.months.append(zoom_month)

            result.append(zoom_year)

        return result

    # -- Summary Generation -------------------------------------------------

    def generate_monthly_summary(
        self,
        year: int,
        month: int,
        synthesizer: Optional[Any] = None,
    ) -> Optional[MonthlySummary]:
        """Generate (or regenerate) a monthly summary from weekly episodes.

        Queries all weekly_episodes whose ``week_start`` falls in the given
        year/month, synthesizes a summary, extracts highlights, and upserts
        into ``monthly_summaries`` (INSERT OR REPLACE for idempotent
        re-generation).

        Args:
            year: Four-digit year (e.g. 2026).
            month: 1-12 month number.
            synthesizer: Optional callable ``(episodes: List[Episode]) ->
                Tuple[str, List[str]]`` that produces (summary_text,
                highlights).  If *None*, uses deterministic concatenation
                from episode narratives and key decisions.

        Returns:
            A :class:`MonthlySummary` if episodes exist for that month,
            otherwise ``None`` (no-op).
        """
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM weekly_episodes
               WHERE CAST(strftime('%Y', week_start) AS INTEGER) = ?
                 AND CAST(strftime('%m', week_start) AS INTEGER) = ?
               ORDER BY week_start ASC""",
            (year, month),
        ).fetchall()

        if not rows:
            logger.info("No episodes for %d-%02d; skipping monthly summary", year, month)
            return None

        episodes = [self._row_to_episode(r, include_sessions=True) for r in rows]

        if synthesizer is not None:
            summary_text, highlights = synthesizer(episodes)
        else:
            summary_text, highlights = _default_monthly_synthesis(episodes)

        # Upsert (INSERT OR REPLACE handles re-generation)
        cur = conn.execute(
            """INSERT OR REPLACE INTO monthly_summaries (year, month, summary, highlights)
               VALUES (?, ?, ?, ?)""",
            (year, month, summary_text, json.dumps(highlights)),
        )
        conn.commit()
        assert cur.lastrowid is not None
        row_id = cur.lastrowid
        logger.info("Monthly summary for %d-%02d upserted (id=%d)", year, month, row_id)

        return MonthlySummary(
            id=row_id,
            year=year,
            month=month,
            summary=summary_text,
            highlights=highlights,
        )

    def generate_yearly_summary(
        self,
        year: int,
        synthesizer: Optional[Any] = None,
    ) -> Optional[YearlySummary]:
        """Generate (or regenerate) a yearly summary from monthly summaries.

        Queries all monthly_summaries for the given year.  If fewer than 2
        exist, skips (not enough data for a meaningful yearly view).

        Args:
            year: Four-digit year.
            synthesizer: Optional callable ``(summaries: List[MonthlySummary]) ->
                Tuple[str, List[str]]`` for LLM-based synthesis.

        Returns:
            A :class:`YearlySummary` if enough monthly summaries exist,
            otherwise ``None``.
        """
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM monthly_summaries
               WHERE year = ?
               ORDER BY month ASC""",
            (year,),
        ).fetchall()

        if len(rows) < 2:
            logger.info(
                "Only %d monthly summaries for %d; need >=2 for yearly summary",
                len(rows), year,
            )
            return None

        monthly_summaries = []
        for r in rows:
            ms = MonthlySummary(
                id=r["id"],
                year=r["year"],
                month=r["month"],
                summary=r["summary"],
                highlights=json.loads(r["highlights"]) if isinstance(r["highlights"], str) else r["highlights"],
                created_at=r["created_at"],
            )
            monthly_summaries.append(ms)

        if synthesizer is not None:
            summary_text, highlights = synthesizer(monthly_summaries)
        else:
            summary_text, highlights = _default_yearly_synthesis(monthly_summaries)

        cur = conn.execute(
            """INSERT OR REPLACE INTO yearly_summaries (year, summary, highlights)
               VALUES (?, ?, ?)""",
            (year, summary_text, json.dumps(highlights)),
        )
        conn.commit()
        assert cur.lastrowid is not None
        row_id = cur.lastrowid
        logger.info("Yearly summary for %d upserted (id=%d)", year, row_id)

        return YearlySummary(
            id=row_id,
            year=year,
            summary=summary_text,
            highlights=highlights,
        )

    # -- Consolidation hooks (Dream Processor) ------------------------------

    def consolidate_monthly(
        self,
        synthesizer: Optional[Any] = None,
        reference_date: Optional[date] = None,
    ) -> Optional[MonthlySummary]:
        """Generate monthly summary for the *previous* month.

        Called on month boundary by the Dream Processor as a
        post-consolidation step.

        Args:
            synthesizer: Optional synthesis callable (see
                :meth:`generate_monthly_summary`).
            reference_date: Date to compute "previous month" from.
                Defaults to today.

        Returns:
            The generated :class:`MonthlySummary`, or ``None`` if no
            episodes exist for the previous month.
        """
        if reference_date is None:
            reference_date = date.today()

        # Previous month: if current is January, previous is December of prior year
        if reference_date.month == 1:
            prev_year, prev_month = reference_date.year - 1, 12
        else:
            prev_year, prev_month = reference_date.year, reference_date.month - 1

        logger.info("Consolidating monthly summary for %d-%02d", prev_year, prev_month)
        return self.generate_monthly_summary(prev_year, prev_month, synthesizer=synthesizer)

    def consolidate_yearly(
        self,
        synthesizer: Optional[Any] = None,
        reference_date: Optional[date] = None,
    ) -> Optional[YearlySummary]:
        """Generate yearly summary for the *previous* year.

        Called on year boundary by the Dream Processor as a
        post-consolidation step.

        Args:
            synthesizer: Optional synthesis callable (see
                :meth:`generate_yearly_summary`).
            reference_date: Date to compute "previous year" from.
                Defaults to today.

        Returns:
            The generated :class:`YearlySummary`, or ``None`` if
            insufficient monthly summaries exist.
        """
        if reference_date is None:
            reference_date = date.today()

        prev_year = reference_date.year - 1
        logger.info("Consolidating yearly summary for %d", prev_year)
        return self.generate_yearly_summary(prev_year, synthesizer=synthesizer)

    # -- Internal helpers --------------------------------------------------

    def _get_linked_sessions(self, episode_id: int) -> List[EpisodeSession]:
        """Return all sessions linked to an episode as dataclass instances."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM episode_sessions
               WHERE episode_id = ?
               ORDER BY relevance_score DESC""",
            (episode_id,),
        ).fetchall()
        return [
            EpisodeSession(
                id=r["id"],
                episode_id=r["episode_id"],
                session_id=r["session_id"],
                relevance_score=r["relevance_score"],
            )
            for r in rows
        ]

    def _row_to_episode(
        self, row: sqlite3.Row, include_sessions: bool = False
    ) -> Episode:
        """Convert a DB row to an :class:`Episode` dataclass."""
        topics = json.loads(row["topics"]) if isinstance(row["topics"], str) else row["topics"]
        key_decisions = json.loads(row["key_decisions"]) if isinstance(row["key_decisions"], str) else row["key_decisions"]
        mood_arc = json.loads(row["mood_arc"]) if isinstance(row["mood_arc"], str) else row["mood_arc"]

        sessions: List[EpisodeSession] = []
        if include_sessions:
            sessions = self._get_linked_sessions(row["id"])

        return Episode(
            id=row["id"],
            week_start=row["week_start"],
            week_end=row["week_end"],
            title=row["title"],
            narrative=row["narrative"],
            topics=topics,
            key_decisions=key_decisions,
            mood_arc=mood_arc,
            created_at=row["created_at"],
            sessions=sessions,
        )


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

_default_episodic_memory: Optional[EpisodicMemory] = None


def get_episodic_memory(db_path: str | Path | None = None) -> EpisodicMemory:
    """Get or create the singleton :class:`EpisodicMemory` instance.

    If *db_path* is ``None``, resolves to ``<hermes_home>/memory.db``.

    Args:
        db_path: Path to the memory database file.

    Returns:
        The initialized singleton instance.
    """
    global _default_episodic_memory
    if _default_episodic_memory is None:
        if db_path is None:
            from hermes_constants import get_hermes_home
            db_path = Path(get_hermes_home()) / "memory.db"
        _default_episodic_memory = EpisodicMemory(db_path)
        _default_episodic_memory.initialize()
    return _default_episodic_memory
