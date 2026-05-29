"""Comprehensive test suite for the entire episodic memory system.

Covers ALL components in one unified file:
  A. Schema tests — column types, FK constraints, FTS5 triggers
  B. EpisodicMemoryManager module tests (low-level CRUD, FTS, zoom)
  C. EpisodicMemory module tests (high-level dataclass API)
  D. WeeklyConsolidator tests — consolidation with/without sessions
  E. Monthly / yearly summary generation tests
  F. Integration test — full flow from sessions through to search + zoom

Uses pytest with per-test temp DB fixtures for isolation.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

# -- Path setup ---------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "agent"))

from episodic_memory_manager import EpisodicMemoryManager, _SCHEMA_SQL
from episodic_memory import (
    Episode,
    EpisodeSession,
    EpisodicMemory,
    MonthlySummary,
    YearlySummary,
    ZoomMonth,
    ZoomWeek,
    ZoomYear,
)
from consolidators.episodic import (
    WeeklyConsolidator,
    _most_recent_completed_week,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def tmp_db_path(tmp_path):
    """A fresh temp path for memory.db."""
    return tmp_path / "test_memory.db"


@pytest.fixture
def manager(tmp_db_path):
    """Initialized EpisodicMemoryManager (low-level)."""
    mgr = EpisodicMemoryManager(tmp_db_path)
    mgr.initialize()
    yield mgr
    mgr.close()


@pytest.fixture
def em(tmp_db_path):
    """Initialized EpisodicMemory (high-level dataclass API)."""
    mem = EpisodicMemory(tmp_db_path)
    mem.initialize()
    yield mem
    mem.close()


@pytest.fixture
def state_db(tmp_path):
    """Minimal state.db with sessions table for WeeklyConsolidator."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        title TEXT,
        source TEXT,
        model TEXT,
        started_at REAL,
        ended_at REAL,
        message_count INTEGER
    )""")
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def memory_db_path(tmp_path):
    """Path for memory.db used by consolidator (created on first use)."""
    return tmp_path / "memory.db"


@pytest.fixture
def consolidator(state_db, memory_db_path):
    """WeeklyConsolidator with temp DBs."""
    return WeeklyConsolidator(
        state_db_path=state_db,
        memory_db_path=memory_db_path,
    )


# -- Helpers ------------------------------------------------------------------

def _insert_session(state_db, session_id, title, started_at, source="cli", model="test"):
    """Insert a session into state.db."""
    conn = sqlite3.connect(str(state_db))
    conn.execute(
        "INSERT INTO sessions (id, title, source, model, started_at, ended_at, message_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_id, title, source, model, started_at, None, 10),
    )
    conn.commit()
    conn.close()


def _iso_to_unix(iso_date: str, hour=12) -> float:
    """Convert ISO date string to Unix timestamp at a given hour UTC."""
    dt = datetime.strptime(iso_date, "%Y-%m-%d").replace(hour=hour, tzinfo=timezone.utc)
    return dt.timestamp()


# =============================================================================
# A. Schema Tests
# =============================================================================

class TestSchemaCreation:
    """Verify all tables, columns, and constraints exist after initialization."""

    def test_tables_created(self, tmp_db_path):
        """All 5 expected tables are created."""
        mgr = EpisodicMemoryManager(tmp_db_path)
        mgr.initialize()
        conn = sqlite3.connect(str(tmp_db_path))
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        mgr.close()

        expected = {
            "weekly_episodes", "episode_sessions", "monthly_summaries",
            "yearly_summaries", "episode_fts",
        }
        assert expected.issubset(tables), f"Missing tables: {expected - tables}"

    def test_weekly_episodes_columns(self, tmp_db_path):
        """weekly_episodes has correct columns and types."""
        mgr = EpisodicMemoryManager(tmp_db_path)
        mgr.initialize()
        conn = sqlite3.connect(str(tmp_db_path))
        info = conn.execute("PRAGMA table_info(weekly_episodes)").fetchall()
        conn.close()
        mgr.close()

        col_names = {row[1] for row in info}
        expected_cols = {
            "id", "week_start", "week_end", "title", "narrative",
            "topics", "key_decisions", "mood_arc", "created_at",
        }
        assert expected_cols.issubset(col_names), f"Missing columns: {expected_cols - col_names}"

        # Verify id is INTEGER PRIMARY KEY
        id_col = [row for row in info if row[1] == "id"][0]
        assert id_col[2] == "INTEGER"
        assert id_col[5] == 1  # pk flag

    def test_episode_sessions_columns(self, tmp_db_path):
        """episode_sessions has correct columns with FK."""
        mgr = EpisodicMemoryManager(tmp_db_path)
        mgr.initialize()
        conn = sqlite3.connect(str(tmp_db_path))
        info = conn.execute("PRAGMA table_info(episode_sessions)").fetchall()
        conn.close()
        mgr.close()

        col_names = {row[1] for row in info}
        expected_cols = {"id", "episode_id", "session_id", "relevance_score"}
        assert expected_cols.issubset(col_names)

    def test_monthly_summaries_columns(self, tmp_db_path):
        """monthly_summaries has year, month, summary, highlights, created_at."""
        mgr = EpisodicMemoryManager(tmp_db_path)
        mgr.initialize()
        conn = sqlite3.connect(str(tmp_db_path))
        info = conn.execute("PRAGMA table_info(monthly_summaries)").fetchall()
        conn.close()
        mgr.close()

        col_names = {row[1] for row in info}
        assert {"id", "year", "month", "summary", "highlights", "created_at"}.issubset(col_names)

    def test_yearly_summaries_columns(self, tmp_db_path):
        """yearly_summaries has year, summary, highlights, created_at."""
        mgr = EpisodicMemoryManager(tmp_db_path)
        mgr.initialize()
        conn = sqlite3.connect(str(tmp_db_path))
        info = conn.execute("PRAGMA table_info(yearly_summaries)").fetchall()
        conn.close()
        mgr.close()

        col_names = {row[1] for row in info}
        assert {"id", "year", "summary", "highlights", "created_at"}.issubset(col_names)

    def test_fts5_virtual_table_exists(self, tmp_db_path):
        """episode_fts is a virtual table (FTS5)."""
        mgr = EpisodicMemoryManager(tmp_db_path)
        mgr.initialize()
        conn = sqlite3.connect(str(tmp_db_path))
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='episode_fts'"
        ).fetchone()
        conn.close()
        mgr.close()

        assert row is not None
        assert "fts5" in row[0].lower()


class TestSchemaConstraints:
    """Verify FK constraints and unique constraints work correctly."""

    def test_fk_invalid_episode_id_rejected(self, manager):
        """Inserting episode_session with invalid episode_id fails."""
        conn = manager._get_conn()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO episode_sessions (episode_id, session_id) VALUES (999, 's1')"
            )

    def test_fk_cascade_delete(self, manager):
        """Deleting an episode cascades to episode_sessions."""
        eid = manager.create_episode(
            week_start="2026-04-13", week_end="2026-04-19", title="Test"
        )
        manager.link_session(eid, "s1")
        manager.link_session(eid, "s2")
        manager.delete_episode(eid)
        assert manager.get_episode_sessions(eid) == []

    def test_unique_episode_session_pair(self, manager):
        """episode_sessions enforces UNIQUE(episode_id, session_id)."""
        eid = manager.create_episode(
            week_start="2026-04-13", week_end="2026-04-19", title="Test"
        )
        conn = manager._get_conn()
        conn.execute(
            "INSERT INTO episode_sessions (episode_id, session_id) VALUES (?, 's1')",
            (eid,),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO episode_sessions (episode_id, session_id) VALUES (?, 's1')",
                (eid,),
            )

    def test_unique_monthly_year_month(self, manager):
        """monthly_summaries enforces UNIQUE(year, month)."""
        conn = manager._get_conn()
        conn.execute(
            "INSERT INTO monthly_summaries (year, month, summary) VALUES (2026, 4, 'first')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO monthly_summaries (year, month, summary) VALUES (2026, 4, 'second')"
            )

    def test_unique_yearly_year(self, manager):
        """yearly_summaries enforces UNIQUE(year)."""
        conn = manager._get_conn()
        conn.execute(
            "INSERT INTO yearly_summaries (year, summary) VALUES (2026, 'first')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO yearly_summaries (year, summary) VALUES (2026, 'second')"
            )


class TestFTS5Triggers:
    """Verify FTS5 triggers correctly sync on INSERT, UPDATE, DELETE."""

    def test_insert_trigger(self, manager):
        """FTS5 index is populated on episode INSERT."""
        manager.create_episode(
            week_start="2026-04-13", week_end="2026-04-19",
            title="Unique Keyword Alpha",
            narrative="Some narrative content",
            topics=["fts5"],
        )
        results = manager.search_episodes("Alpha")
        assert len(results) == 1

    def test_update_trigger(self, manager):
        """FTS5 index reflects changes on UPDATE."""
        eid = manager.create_episode(
            week_start="2026-04-13", week_end="2026-04-19",
            title="Original Title",
            narrative="Contains keyword Beta originally",
        )
        assert len(manager.search_episodes("Beta")) == 1

        manager.update_episode(eid, narrative="Now contains keyword Gamma instead")
        assert len(manager.search_episodes("Beta")) == 0
        assert len(manager.search_episodes("Gamma")) == 1

    def test_delete_trigger(self, manager):
        """FTS5 index is cleaned on DELETE."""
        eid = manager.create_episode(
            week_start="2026-04-13", week_end="2026-04-19",
            title="Ephemeral Delta Entry",
            narrative="Will be deleted",
        )
        assert len(manager.search_episodes("Delta")) == 1
        manager.delete_episode(eid)
        assert len(manager.search_episodes("Delta")) == 0

    def test_topics_synced_in_fts(self, manager):
        """Topics JSON is searchable via FTS5."""
        manager.create_episode(
            week_start="2026-04-13", week_end="2026-04-19",
            title="Test", narrative="Stuff",
            topics=["quantum_computing"],
        )
        results = manager.search_episodes("quantum_computing")
        assert len(results) == 1


class TestSchemaPragmas:
    """Verify WAL mode and foreign_keys pragma."""

    def test_wal_mode(self, tmp_db_path):
        mgr = EpisodicMemoryManager(tmp_db_path)
        mgr.initialize()
        conn = sqlite3.connect(str(tmp_db_path))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        mgr.close()
        assert mode == "wal"

    def test_foreign_keys_on(self, manager):
        fk = manager._get_conn().execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1


# =============================================================================
# B. EpisodicMemoryManager Module Tests
# =============================================================================

class TestManagerCRUD:
    """CRUD operations on the low-level manager."""

    def test_create_and_get_episode(self, manager):
        eid = manager.create_episode(
            week_start="2026-04-13", week_end="2026-04-19",
            title="Week 1", narrative="First week",
            topics=["memory"], key_decisions=["use SQLite"],
            mood_arc=[{"day": "mon", "mood": "curious"}],
        )
        ep = manager.get_episode(eid)
        assert ep is not None
        assert ep["title"] == "Week 1"
        assert ep["topics"] == ["memory"]
        assert ep["key_decisions"] == ["use SQLite"]
        assert len(ep["mood_arc"]) == 1

    def test_create_with_defaults(self, manager):
        eid = manager.create_episode(
            week_start="2026-04-13", week_end="2026-04-19", title="Minimal"
        )
        ep = manager.get_episode(eid)
        assert ep["narrative"] == ""
        assert ep["topics"] == []
        assert ep["key_decisions"] == []
        assert ep["mood_arc"] == []

    def test_get_nonexistent_returns_none(self, manager):
        assert manager.get_episode(999) is None

    def test_update_episode_fields(self, manager):
        eid = manager.create_episode(
            week_start="2026-04-13", week_end="2026-04-19", title="Old"
        )
        manager.update_episode(eid, title="New", topics=["updated"])
        ep = manager.get_episode(eid)
        assert ep["title"] == "New"
        assert ep["topics"] == ["updated"]

    def test_update_invalid_field_raises(self, manager):
        eid = manager.create_episode(
            week_start="2026-04-13", week_end="2026-04-19", title="Test"
        )
        with pytest.raises(ValueError, match="Unknown episode fields"):
            manager.update_episode(eid, bad_field="value")

    def test_delete_episode(self, manager):
        eid = manager.create_episode(
            week_start="2026-04-13", week_end="2026-04-19", title="To delete"
        )
        assert manager.delete_episode(eid) is True
        assert manager.get_episode(eid) is None

    def test_delete_nonexistent_returns_false(self, manager):
        assert manager.delete_episode(999) is False

    def test_list_episodes_order_desc(self, manager):
        for i in range(5):
            manager.create_episode(
                week_start=f"2026-04-{13 + i:02d}",
                week_end=f"2026-04-{19 + i:02d}",
                title=f"Week {i}",
            )
        eps = manager.list_episodes()
        assert len(eps) == 5
        assert eps[0]["title"] == "Week 4"
        assert eps[-1]["title"] == "Week 0"

    def test_list_with_pagination(self, manager):
        for i in range(10):
            manager.create_episode(
                week_start=f"2026-04-{1 + i:02d}",
                week_end=f"2026-04-{7 + i:02d}",
                title=f"Week {i}",
            )
        page = manager.list_episodes(limit=3, offset=2)
        assert len(page) == 3


class TestManagerSessionLinking:
    """Episode-session linking at the manager level."""

    def test_link_and_get_sessions(self, manager):
        eid = manager.create_episode(
            week_start="2026-04-13", week_end="2026-04-19", title="Test"
        )
        lid = manager.link_session(eid, "s1", relevance_score=0.8)
        assert isinstance(lid, int)
        sessions = manager.get_episode_sessions(eid)
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "s1"
        assert sessions[0]["relevance_score"] == 0.8

    def test_duplicate_link_ignored(self, manager):
        eid = manager.create_episode(
            week_start="2026-04-13", week_end="2026-04-19", title="Test"
        )
        manager.link_session(eid, "s1")
        manager.link_session(eid, "s1")  # duplicate
        assert len(manager.get_episode_sessions(eid)) == 1

    def test_multiple_sessions_ordered_by_relevance(self, manager):
        eid = manager.create_episode(
            week_start="2026-04-13", week_end="2026-04-19", title="Test"
        )
        manager.link_session(eid, "s_low", 0.3)
        manager.link_session(eid, "s_high", 1.0)
        manager.link_session(eid, "s_mid", 0.7)
        sessions = manager.get_episode_sessions(eid)
        assert len(sessions) == 3
        scores = [s["relevance_score"] for s in sessions]
        assert scores == sorted(scores, reverse=True)

    def test_unlink_session(self, manager):
        eid = manager.create_episode(
            week_start="2026-04-13", week_end="2026-04-19", title="Test"
        )
        manager.link_session(eid, "s1")
        assert manager.unlink_session(eid, "s1") is True
        assert len(manager.get_episode_sessions(eid)) == 0

    def test_get_sessions_episodes(self, manager):
        eid1 = manager.create_episode(
            week_start="2026-04-13", week_end="2026-04-19", title="Week 1"
        )
        eid2 = manager.create_episode(
            week_start="2026-04-20", week_end="2026-04-26", title="Week 2"
        )
        manager.link_session(eid1, "shared")
        manager.link_session(eid2, "shared")
        eps = manager.get_sessions_episodes("shared")
        assert len(eps) == 2

    def test_cascade_delete_removes_sessions(self, manager):
        eid = manager.create_episode(
            week_start="2026-04-13", week_end="2026-04-19", title="Test"
        )
        manager.link_session(eid, "s1")
        manager.link_session(eid, "s2")
        manager.delete_episode(eid)
        assert manager.get_episode_sessions(eid) == []


class TestManagerFTSSearch:
    """FTS5 search at the manager level."""

    def test_basic_search(self, manager):
        manager.create_episode(
            week_start="2026-04-13", week_end="2026-04-19",
            title="Identity system",
            narrative="Built the immutable identity anchor",
        )
        results = manager.search_episodes("identity")
        assert len(results) == 1
        assert results[0]["title"] == "Identity system"

    def test_search_by_topic(self, manager):
        manager.create_episode(
            week_start="2026-04-13", week_end="2026-04-19",
            title="Test", narrative="Stuff", topics=["special_topic_xyz"],
        )
        results = manager.search_episodes("special_topic_xyz")
        assert len(results) == 1

    def test_search_no_results(self, manager):
        manager.create_episode(
            week_start="2026-04-13", week_end="2026-04-19",
            title="Hello", narrative="World",
        )
        results = manager.search_episodes("nonexistent_zzz")
        assert results == []

    def test_search_limit(self, manager):
        for i in range(10):
            manager.create_episode(
                week_start=f"2026-04-{1 + i:02d}",
                week_end=f"2026-04-{7 + i:02d}",
                title=f"Episode {i}",
                narrative="Common keyword shared_content",
            )
        results = manager.search_episodes("shared_content", limit=3)
        assert len(results) == 3


# =============================================================================
# C. EpisodicMemory Module Tests (High-Level Dataclass API)
# =============================================================================

class TestHighLevelCreateEpisode:
    """create_episode with auto-computed fields and dataclass returns."""

    def test_returns_episode_dataclass(self, em):
        ep = em.create_episode(
            week_start="2026-04-13", title="Test",
            narrative="A narrative", topics=["t1"],
        )
        assert isinstance(ep, Episode)
        assert ep.id > 0
        assert ep.title == "Test"
        assert ep.topics == ["t1"]

    def test_week_end_auto_computed(self, em):
        ep = em.create_episode(week_start="2026-04-13", title="Test")
        assert ep.week_end == "2026-04-19"

    def test_week_end_month_boundary(self, em):
        ep = em.create_episode(week_start="2026-01-28", title="Test")
        assert ep.week_end == "2026-02-03"

    def test_week_end_year_boundary(self, em):
        ep = em.create_episode(week_start="2025-12-29", title="Test")
        assert ep.week_end == "2026-01-04"

    def test_session_ids_linked(self, em):
        ep = em.create_episode(
            week_start="2026-04-13", title="Test",
            session_ids=["s1", "s2"],
        )
        assert len(ep.sessions) == 2
        assert all(isinstance(s, EpisodeSession) for s in ep.sessions)
        ids = {s.session_id for s in ep.sessions}
        assert ids == {"s1", "s2"}

    def test_duplicate_session_ids_collapsed(self, em):
        ep = em.create_episode(
            week_start="2026-04-13", title="Test",
            session_ids=["s1", "s1", "s1"],
        )
        assert len(ep.sessions) == 1

    def test_no_sessions_returns_empty_list(self, em):
        ep = em.create_episode(week_start="2026-04-13", title="Test")
        assert ep.sessions == []

    def test_defaults_populated(self, em):
        ep = em.create_episode(week_start="2026-04-13", title="Test")
        assert ep.narrative == ""
        assert ep.topics == []
        assert ep.key_decisions == []
        assert ep.mood_arc == []


class TestHighLevelGetEpisode:
    """get_episode returns correct data with linked sessions."""

    def test_returns_with_sessions(self, em):
        created = em.create_episode(
            week_start="2026-04-13", title="Test",
            session_ids=["s1", "s2"],
        )
        fetched = em.get_episode(created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert len(fetched.sessions) == 2

    def test_nonexistent_returns_none(self, em):
        assert em.get_episode(999) is None

    def test_without_sessions(self, em):
        created = em.create_episode(week_start="2026-04-13", title="Test")
        fetched = em.get_episode(created.id)
        assert fetched.sessions == []


class TestHighLevelSearch:
    """search_episodes FTS5 via the high-level API."""

    def test_basic_search(self, em):
        em.create_episode(
            week_start="2026-04-13", title="Identity work",
            narrative="Built the immutable identity anchor",
        )
        results = em.search_episodes("identity")
        assert len(results) == 1
        assert isinstance(results[0], Episode)
        assert results[0].title == "Identity work"

    def test_search_by_narrative(self, em):
        em.create_episode(
            week_start="2026-04-13", title="Test",
            narrative="Unique content about quantum entanglement",
        )
        results = em.search_episodes("quantum")
        assert len(results) == 1

    def test_search_by_topics(self, em):
        em.create_episode(
            week_start="2026-04-13", title="Test",
            narrative="Stuff", topics=["rare_topic_abc"],
        )
        results = em.search_episodes("rare_topic_abc")
        assert len(results) == 1

    def test_no_match_returns_empty(self, em):
        em.create_episode(week_start="2026-04-13", title="Test")
        assert em.search_episodes("zzz_nonexistent") == []

    def test_empty_query_returns_empty(self, em):
        em.create_episode(week_start="2026-04-13", title="Test")
        assert em.search_episodes("") == []

    def test_limit_respected(self, em):
        for i in range(10):
            em.create_episode(
                week_start=f"2026-04-{1 + i:02d}",
                title=f"Episode {i}",
                narrative="Common shared_keyword_xyz",
            )
        results = em.search_episodes("shared_keyword_xyz", limit=3)
        assert len(results) == 3

    def test_search_episode_fields_populated(self, em):
        em.create_episode(
            week_start="2026-04-13", title="Full",
            narrative="Complete data", topics=["t1"],
            key_decisions=["d1"],
            mood_arc=[{"day": "mon", "mood": "good"}],
        )
        results = em.search_episodes("Complete")
        ep = results[0]
        assert ep.topics == ["t1"]
        assert ep.key_decisions == ["d1"]
        assert ep.week_end == "2026-04-19"


class TestHighLevelZoomNavigation:
    """zoom_navigation hierarchical structure."""

    def _populate(self, em):
        """Seed with yearly, monthly, weekly, and session data."""
        conn = em._get_conn()
        conn.execute(
            "INSERT INTO yearly_summaries (year, summary, highlights) "
            "VALUES (2026, 'Year of growth', ?)",
            (json.dumps(["identity", "memory"]),),
        )
        conn.execute(
            "INSERT INTO monthly_summaries (year, month, summary, highlights) "
            "VALUES (2026, 4, 'April: identity built', ?)",
            (json.dumps(["F1 identity"]),),
        )
        conn.commit()

        ep1 = em.create_episode(
            week_start="2026-04-06", title="Prep week",
            narrative="Set up the project",
            session_ids=["s_prep1"],
        )
        ep2 = em.create_episode(
            week_start="2026-04-13", title="Build week",
            narrative="Built the identity system",
            session_ids=["s_build1", "s_build2"],
        )
        return ep1, ep2

    def test_structure_shape(self, em):
        self._populate(em)
        tree = em.zoom_navigation()

        assert isinstance(tree, list)
        assert len(tree) >= 1

        year_node = tree[0]
        assert isinstance(year_node, ZoomYear)
        assert year_node.year == 2026
        assert year_node.summary == "Year of growth"
        assert year_node.highlights == ["identity", "memory"]

        month_node = year_node.months[0]
        assert isinstance(month_node, ZoomMonth)
        assert month_node.month == 4
        assert month_node.summary == "April: identity built"

        assert len(month_node.weeks) >= 2
        for w in month_node.weeks:
            assert isinstance(w, ZoomWeek)

    def test_day_level_sessions(self, em):
        self._populate(em)
        tree = em.zoom_navigation()

        build_week = None
        for month in tree[0].months:
            for week in month.weeks:
                if week.title == "Build week":
                    build_week = week
                    break

        assert build_week is not None
        all_sessions = []
        for day_sessions in build_week.days.values():
            all_sessions.extend(day_sessions)
        assert len(all_sessions) == 2
        ids = {s["session_id"] for s in all_sessions}
        assert ids == {"s_build1", "s_build2"}

    def test_empty_db_returns_empty(self, em):
        assert em.zoom_navigation() == []

    def test_without_yearly_summary(self, em):
        """Episodes exist but no yearly_summary — still shows up."""
        em.create_episode(week_start="2026-06-01", title="Summer")
        tree = em.zoom_navigation()
        assert len(tree) == 1
        assert tree[0].year == 2026
        assert tree[0].summary == ""
        assert len(tree[0].months) == 1

    def test_without_monthly_summary(self, em):
        """Episodes exist but no monthly_summary — still shows up."""
        em.create_episode(week_start="2026-05-04", title="May day")
        tree = em.zoom_navigation()
        may = [m for m in tree[0].months if m.month == 5]
        assert len(may) == 1
        assert may[0].summary == ""

    def test_multiple_years_descending(self, em):
        em.create_episode(week_start="2025-06-01", title="2025")
        em.create_episode(week_start="2026-06-01", title="2026")
        tree = em.zoom_navigation()
        assert tree[0].year == 2026
        assert tree[1].year == 2025

    def test_weeks_in_descending_order(self, em):
        em.create_episode(week_start="2026-04-06", title="Week A")
        em.create_episode(week_start="2026-04-13", title="Week B")
        em.create_episode(week_start="2026-04-20", title="Week C")
        tree = em.zoom_navigation()
        april = [m for m in tree[0].months if m.month == 4][0]
        titles = [w.title for w in april.weeks]
        assert titles == ["Week C", "Week B", "Week A"]


class TestHighLevelListEpisodes:
    """list_episodes pagination and ordering."""

    def test_order_desc(self, em):
        for i in range(5):
            em.create_episode(week_start=f"2026-04-{13 + i:02d}", title=f"Week {i}")
        eps = em.list_episodes()
        assert eps[0].title == "Week 4"
        assert eps[-1].title == "Week 0"

    def test_pagination(self, em):
        for i in range(10):
            em.create_episode(week_start=f"2026-04-{1 + i:02d}", title=f"Ep {i}")
        page1 = em.list_episodes(limit=3, offset=0)
        page2 = em.list_episodes(limit=3, offset=3)
        assert len(page1) == 3
        assert len(page2) == 3
        assert page1[0].title != page2[0].title

    def test_returns_dataclass(self, em):
        em.create_episode(week_start="2026-04-13", title="Test")
        eps = em.list_episodes()
        assert all(isinstance(ep, Episode) for ep in eps)

    def test_empty_db(self, em):
        assert em.list_episodes() == []


class TestHighLevelLifecycle:
    """Lifecycle: initialize, close, reopen."""

    def test_idempotent_initialize(self, tmp_db_path):
        em = EpisodicMemory(tmp_db_path)
        em.initialize()
        em.initialize()  # no-op
        em.close()

    def test_close_and_reopen(self, tmp_db_path):
        em = EpisodicMemory(tmp_db_path)
        em.initialize()
        ep = em.create_episode(week_start="2026-04-13", title="Before close")
        em.close()

        em2 = EpisodicMemory(tmp_db_path)
        em2.initialize()
        fetched = em2.get_episode(ep.id)
        assert fetched is not None
        assert fetched.title == "Before close"
        em2.close()


# =============================================================================
# D. Consolidation Tests
# =============================================================================

class TestWeeklyConsolidationWithSessions:
    """WeeklyConsolidator.run() when sessions exist."""

    def test_creates_valid_episode(self, consolidator, state_db):
        _insert_session(state_db, "s1", "Identity work", _iso_to_unix("2026-05-27"))
        result = consolidator.run("2026-05-25")
        assert result is not None
        assert result["week_start"] == "2026-05-25"
        assert result["week_end"] == "2026-05-31"
        assert len(result["title"]) > 0
        assert result["session_count"] == 1
        assert isinstance(result["episode_id"], int)

    def test_episode_persisted_in_db(self, consolidator, state_db, memory_db_path):
        _insert_session(state_db, "s1", "Test", _iso_to_unix("2026-05-27"))
        result = consolidator.run("2026-05-25")
        mgr = EpisodicMemoryManager(memory_db_path)
        mgr.initialize()
        ep = mgr.get_episode(result["episode_id"])
        assert ep is not None
        mgr.close()

    def test_sessions_linked_in_db(self, consolidator, state_db, memory_db_path):
        _insert_session(state_db, "s1", "A", _iso_to_unix("2026-05-26"))
        _insert_session(state_db, "s2", "B", _iso_to_unix("2026-05-28"))
        result = consolidator.run("2026-05-25")
        mgr = EpisodicMemoryManager(memory_db_path)
        mgr.initialize()
        linked = mgr.get_episode_sessions(result["episode_id"])
        assert {s["session_id"] for s in linked} == {"s1", "s2"}
        mgr.close()

    def test_multiple_sessions_consolidated(self, consolidator, state_db):
        _insert_session(state_db, "s1", "Identity built", _iso_to_unix("2026-05-25"))
        _insert_session(state_db, "s2", "Memory designed", _iso_to_unix("2026-05-27"))
        _insert_session(state_db, "s3", "Tests written", _iso_to_unix("2026-05-29"))
        result = consolidator.run("2026-05-25")
        assert result["session_count"] == 3


class TestWeeklyConsolidationNoSessions:
    """WeeklyConsolidator.run() when no sessions exist (no-op)."""

    def test_returns_none(self, consolidator):
        result = consolidator.run("2026-05-25")
        assert result is None

    def test_no_episode_created(self, consolidator, memory_db_path):
        consolidator.run("2026-05-25")
        if memory_db_path.exists():
            mgr = EpisodicMemoryManager(memory_db_path)
            mgr.initialize()
            assert mgr.list_episodes() == []
            mgr.close()

    def test_missing_state_db_safe_noop(self, memory_db_path):
        wc = WeeklyConsolidator(
            state_db_path="/nonexistent/state.db",
            memory_db_path=memory_db_path,
        )
        result = wc.run("2026-05-25")
        assert result is None

    def test_can_run_false_no_sessions(self, consolidator):
        assert consolidator.can_run("2026-05-25") is False

    def test_can_run_true_with_sessions(self, consolidator, state_db):
        _insert_session(state_db, "s1", "Test", _iso_to_unix("2026-05-27"))
        assert consolidator.can_run("2026-05-25") is True


class TestHeuristicFallback:
    """Heuristic summariser used when no custom summariser provided."""

    def test_single_session_title(self, consolidator, state_db):
        _insert_session(state_db, "s1", "Build identity", _iso_to_unix("2026-05-27"))
        result = consolidator.run("2026-05-25")
        assert "Build identity" in result["title"]

    def test_multi_session_title(self, consolidator, state_db):
        _insert_session(state_db, "s1", "A", _iso_to_unix("2026-05-25"))
        _insert_session(state_db, "s2", "B", _iso_to_unix("2026-05-27"))
        result = consolidator.run("2026-05-25")
        assert "2 session" in result["title"].lower()

    def test_title_max_80_chars(self, consolidator, state_db):
        _insert_session(state_db, "s1", "A" * 200, _iso_to_unix("2026-05-27"))
        result = consolidator.run("2026-05-25")
        assert len(result["title"]) <= 80

    def test_topics_from_session_titles(self, consolidator, state_db):
        _insert_session(state_db, "s1", "Identity: build", _iso_to_unix("2026-05-26"))
        _insert_session(state_db, "s2", "Memory: design", _iso_to_unix("2026-05-27"))
        result = consolidator.run("2026-05-25")
        assert len(result["topics"]) >= 1

    def test_heuristic_empty_decisions_and_mood(self, consolidator, state_db):
        _insert_session(state_db, "s1", "Test", _iso_to_unix("2026-05-27"))
        result = consolidator.run("2026-05-25")
        assert result["key_decisions"] == []
        assert result["mood_arc"] == []


class TestCustomSummariser:
    """Pluggable summariser callable."""

    def test_custom_called(self, state_db, memory_db_path):
        called = []
        def my_summariser(sessions):
            called.append(len(sessions))
            return {
                "title": "Custom",
                "narrative": "Custom narrative.",
                "topics": ["custom"],
                "key_decisions": ["decision"],
                "mood_arc": [{"day": "mon", "mood": "positive"}],
            }

        wc = WeeklyConsolidator(state_db_path=state_db, memory_db_path=memory_db_path, summariser=my_summariser)
        _insert_session(state_db, "s1", "Test", _iso_to_unix("2026-05-27"))
        result = wc.run("2026-05-25")
        assert result["title"] == "Custom"
        assert called == [1]

    def test_failed_summariser_falls_back(self, state_db, memory_db_path):
        def bad(sessions):
            raise RuntimeError("fail")

        wc = WeeklyConsolidator(state_db_path=state_db, memory_db_path=memory_db_path, summariser=bad)
        _insert_session(state_db, "s1", "Fallback test", _iso_to_unix("2026-05-27"))
        result = wc.run("2026-05-25")
        assert result is not None
        assert "Fallback test" in result["narrative"]

    def test_incomplete_result_falls_back(self, state_db, memory_db_path):
        def incomplete(sessions):
            return {"title": "Only title"}

        wc = WeeklyConsolidator(state_db_path=state_db, memory_db_path=memory_db_path, summariser=incomplete)
        _insert_session(state_db, "s1", "Incomplete", _iso_to_unix("2026-05-27"))
        result = wc.run("2026-05-25")
        assert "Incomplete" in result["narrative"]


# =============================================================================
# E. Monthly / Yearly Summary Generation Tests
# =============================================================================

class TestMonthlySummaryGeneration:
    """generate_monthly_summary from episodes."""

    def _seed(self, em):
        em.create_episode(
            week_start="2026-04-06", title="Identity system built",
            narrative="Implemented the immutable identity anchor.",
            topics=["identity"], key_decisions=["separate DB"],
        )
        em.create_episode(
            week_start="2026-04-13", title="Episodic memory designed",
            narrative="Designed weekly episodes with FTS5.",
            topics=["memory"], key_decisions=["FTS5"],
        )

    def test_generates_from_episodes(self, em):
        self._seed(em)
        result = em.generate_monthly_summary(2026, 4)
        assert result is not None
        assert isinstance(result, MonthlySummary)
        assert result.year == 2026
        assert result.month == 4
        assert "Identity system built" in result.summary
        assert "Episodic memory designed" in result.summary

    def test_no_episodes_returns_none(self, em):
        result = em.generate_monthly_summary(2026, 12)
        assert result is None

    def test_highlights_extracted(self, em):
        self._seed(em)
        result = em.generate_monthly_summary(2026, 4)
        assert 2 <= len(result.highlights) <= 5

    def test_upsert_on_regenerate(self, em):
        self._seed(em)
        first = em.generate_monthly_summary(2026, 4)
        second = em.generate_monthly_summary(2026, 4)
        assert second.summary == first.summary
        assert second.highlights == first.highlights

    def test_custom_synthesizer(self, em):
        self._seed(em)
        def custom(episodes):
            return f"Custom: {len(episodes)} episodes", ["custom_h"]
        result = em.generate_monthly_summary(2026, 4, synthesizer=custom)
        assert "Custom:" in result.summary
        assert result.highlights == ["custom_h"]

    def test_persisted_in_db(self, em):
        self._seed(em)
        em.generate_monthly_summary(2026, 4)
        conn = em._get_conn()
        row = conn.execute(
            "SELECT * FROM monthly_summaries WHERE year=2026 AND month=4"
        ).fetchone()
        assert row is not None

    def test_different_month_not_included(self, em):
        em.create_episode(week_start="2026-04-06", title="April ep")
        em.create_episode(week_start="2026-05-04", title="May ep")
        result = em.generate_monthly_summary(2026, 4)
        assert "April ep" in result.summary
        assert "May ep" not in result.summary


class TestYearlySummaryGeneration:
    """generate_yearly_summary from monthly summaries."""

    def _seed_monthlies(self, em, year, months):
        conn = em._get_conn()
        for m in months:
            conn.execute(
                """INSERT OR REPLACE INTO monthly_summaries (year, month, summary, highlights)
                   VALUES (?, ?, ?, ?)""",
                (year, m, f"Month {m} summary", json.dumps([f"h_{m}_a", f"h_{m}_b"])),
            )
        conn.commit()

    def test_generates_from_monthly_summaries(self, em):
        self._seed_monthlies(em, 2026, [1, 2, 3])
        result = em.generate_yearly_summary(2026)
        assert result is not None
        assert isinstance(result, YearlySummary)
        assert "2026" in result.summary
        assert "3 month" in result.summary

    def test_fewer_than_2_skips(self, em):
        self._seed_monthlies(em, 2026, [4])
        assert em.generate_yearly_summary(2026) is None

    def test_exactly_2_works(self, em):
        self._seed_monthlies(em, 2026, [3, 6])
        result = em.generate_yearly_summary(2026)
        assert result is not None
        assert "2 month" in result.summary

    def test_no_summaries_returns_none(self, em):
        assert em.generate_yearly_summary(2026) is None

    def test_highlights_aggregated(self, em):
        self._seed_monthlies(em, 2026, [1, 2, 3, 4, 5, 6])
        result = em.generate_yearly_summary(2026)
        assert 4 <= len(result.highlights) <= 10

    def test_custom_synthesizer(self, em):
        self._seed_monthlies(em, 2026, [1, 2])
        def custom(summaries):
            return f"Custom year {len(summaries)} months", ["year_h"]
        result = em.generate_yearly_summary(2026, synthesizer=custom)
        assert "Custom year" in result.summary
        assert result.highlights == ["year_h"]

    def test_upsert_on_regenerate(self, em):
        self._seed_monthlies(em, 2026, [1, 2, 3])
        first = em.generate_yearly_summary(2026)
        second = em.generate_yearly_summary(2026)
        assert second.summary == first.summary


class TestConsolidationHooks:
    """consolidate_monthly / consolidate_yearly (previous month/year)."""

    def test_consolidate_monthly_previous_month(self, em):
        em.create_episode(week_start="2026-04-06", title="April ep")
        result = em.consolidate_monthly(reference_date=date(2026, 5, 15))
        assert result is not None
        assert result.year == 2026
        assert result.month == 4

    def test_january_wraps_to_december(self, em):
        em.create_episode(week_start="2025-12-01", title="Dec ep")
        result = em.consolidate_monthly(reference_date=date(2026, 1, 10))
        assert result is not None
        assert result.year == 2025
        assert result.month == 12

    def test_consolidate_monthly_no_episodes_returns_none(self, em):
        result = em.consolidate_monthly(reference_date=date(2026, 6, 1))
        assert result is None

    def test_consolidate_yearly_previous_year(self, em):
        conn = em._get_conn()
        for m in [1, 2, 3]:
            conn.execute(
                "INSERT OR REPLACE INTO monthly_summaries (year, month, summary, highlights) "
                "VALUES (?, ?, ?, ?)",
                (2025, m, f"Month {m}", json.dumps([])),
            )
        conn.commit()
        result = em.consolidate_yearly(reference_date=date(2026, 3, 15))
        assert result is not None
        assert result.year == 2025

    def test_consolidate_yearly_insufficient_returns_none(self, em):
        conn = em._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO monthly_summaries (year, month, summary, highlights) "
            "VALUES (2025, 6, 'Only one', ?)",
            (json.dumps([]),),
        )
        conn.commit()
        result = em.consolidate_yearly(reference_date=date(2026, 1, 1))
        assert result is None


# =============================================================================
# F. Integration Test — Full Flow
# =============================================================================

class TestFullIntegrationFlow:
    """End-to-end: create sessions → consolidate week → monthly → yearly
    → search for content → zoom navigate."""

    def test_sessions_to_zoom_navigation(self, em, state_db, memory_db_path):
        """Full pipeline from sessions through to zoom navigation."""
        # Step 1: Insert sessions in state.db
        # Week 2026-03-02 (Mon) to 2026-03-08 (Sun)
        _insert_session(state_db, "s_mar1", "March setup", _iso_to_unix("2026-03-03"))
        _insert_session(state_db, "s_mar2", "March architecture", _iso_to_unix("2026-03-05"))
        # Week 2026-04-06 (Mon) to 2026-04-12 (Sun)
        _insert_session(state_db, "s_apr1", "April identity build", _iso_to_unix("2026-04-07"))
        _insert_session(state_db, "s_apr2", "April memory design", _iso_to_unix("2026-04-09"))

        # Step 2: Consolidate weeks using WeeklyConsolidator
        wc = WeeklyConsolidator(state_db_path=state_db, memory_db_path=memory_db_path)
        march_result = wc.run("2026-03-02")  # Mon-Sun week
        april_result = wc.run("2026-04-06")

        assert march_result is not None
        assert april_result is not None

        # Step 3: Open the memory DB with EpisodicMemory API
        mem = EpisodicMemory(memory_db_path)
        mem.initialize()

        # Step 4: Generate monthly summaries
        mar_monthly = mem.generate_monthly_summary(2026, 3)
        apr_monthly = mem.generate_monthly_summary(2026, 4)
        assert mar_monthly is not None
        assert apr_monthly is not None

        # Step 5: Generate yearly summary
        yearly = mem.generate_yearly_summary(2026)
        assert yearly is not None
        assert "2026" in yearly.summary
        assert "2 month" in yearly.summary

        # Step 6: Search for content
        results = mem.search_episodes("identity")
        assert len(results) >= 1
        assert any("identity" in ep.title.lower() or "identity" in ep.narrative.lower()
                    for ep in results)

        results2 = mem.search_episodes("architecture")
        assert len(results2) >= 1

        # Step 7: Zoom navigation
        tree = mem.zoom_navigation()
        assert len(tree) >= 1
        assert tree[0].year == 2026
        assert len(tree[0].summary) > 0  # yearly summary populated
        assert len(tree[0].highlights) > 0

        # Months present
        month_nums = {m.month for m in tree[0].months}
        assert 3 in month_nums
        assert 4 in month_nums

        # Weekly episodes visible
        march_month = [m for m in tree[0].months if m.month == 3][0]
        assert len(march_month.weeks) >= 1
        assert len(march_month.summary) > 0  # monthly summary populated

        april_month = [m for m in tree[0].months if m.month == 4][0]
        assert len(april_month.weeks) >= 1

        # Sessions linked to weeks
        for week in april_month.weeks:
            for day_sessions in week.days.values():
                for sess in day_sessions:
                    assert "session_id" in sess
                    assert "relevance" in sess

        mem.close()

    def test_search_after_consolidation(self, em, state_db, memory_db_path):
        """Search works for content added via consolidation."""
        _insert_session(state_db, "s1", "Quantum computing research", _iso_to_unix("2026-05-27"))

        wc = WeeklyConsolidator(state_db_path=state_db, memory_db_path=memory_db_path)
        wc.run("2026-05-25")

        mem = EpisodicMemory(memory_db_path)
        mem.initialize()

        results = mem.search_episodes("Quantum")
        assert len(results) >= 1
        mem.close()

    def test_regenerate_monthly_then_yearly(self, em):
        """Re-generating monthly then yearly produces consistent results."""
        em.create_episode(week_start="2026-01-05", title="Jan ep", topics=["a"])
        em.create_episode(week_start="2026-02-02", title="Feb ep", topics=["b"])

        em.generate_monthly_summary(2026, 1)
        em.generate_monthly_summary(2026, 2)
        yearly1 = em.generate_yearly_summary(2026)

        # Add another episode and regenerate
        em.create_episode(week_start="2026-01-12", title="Jan ep 2", topics=["c"])
        em.generate_monthly_summary(2026, 1)
        yearly2 = em.generate_yearly_summary(2026)

        assert yearly2 is not None
        # Yearly should reflect updated monthly
        assert "Jan ep 2" in yearly2.summary or "3 episode" in yearly2.summary or "2 month" in yearly2.summary

    def test_zoom_after_full_pipeline(self, em):
        """Zoom navigation reflects generated summaries."""
        em.create_episode(
            week_start="2026-03-02", title="March work",
            narrative="March work on memory.", topics=["memory"],
        )
        em.create_episode(
            week_start="2026-04-06", title="April work",
            narrative="April identity work.", topics=["identity"],
        )
        em.generate_monthly_summary(2026, 3)
        em.generate_monthly_summary(2026, 4)
        em.generate_yearly_summary(2026)

        tree = em.zoom_navigation()
        year_node = [y for y in tree if y.year == 2026][0]
        assert len(year_node.summary) > 0
        assert len(year_node.highlights) > 0

        april_node = [m for m in year_node.months if m.month == 4][0]
        assert len(april_node.summary) > 0
