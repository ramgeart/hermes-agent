"""Tests for EpisodicMemory — high-level episodic memory API.

Covers:
  1. Episode CRUD with dataclass returns
  2. Auto-computed week_end
  3. Session linking during creation
  4. FTS5 search via dataclass API
  5. zoom_navigation hierarchy
  6. Pagination (list_episodes)
  7. Edge cases (empty search, nonexistent episode, empty DB zoom)
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from episodic_memory import (
    Episode,
    EpisodeSession,
    EpisodicMemory,
    ZoomMonth,
    ZoomWeek,
    ZoomYear,
)


@pytest.fixture
def db_path(tmp_path):
    """Provide a temporary database path."""
    return tmp_path / "test_episodic_memory.db"


@pytest.fixture
def em(db_path):
    """Provide an initialized EpisodicMemory instance."""
    mem = EpisodicMemory(db_path)
    mem.initialize()
    yield mem
    mem.close()


# -- 1. CRUD with dataclass returns ----------------------------------------

class TestCreateEpisode:
    def test_returns_episode_dataclass(self, em):
        ep = em.create_episode(
            week_start="2026-04-13",
            title="Identity built",
            narrative="Implemented identity anchor",
            topics=["identity", "sqlite"],
            key_decisions=["separate DB"],
            mood_arc=[{"day": "mon", "mood": "curious"}],
        )
        assert isinstance(ep, Episode)
        assert ep.id > 0
        assert ep.title == "Identity built"
        assert ep.narrative == "Implemented identity anchor"
        assert ep.topics == ["identity", "sqlite"]
        assert ep.key_decisions == ["separate DB"]
        assert len(ep.mood_arc) == 1
        assert ep.mood_arc[0]["mood"] == "curious"

    def test_defaults(self, em):
        ep = em.create_episode(week_start="2026-04-13", title="Minimal")
        assert ep.narrative == ""
        assert ep.topics == []
        assert ep.key_decisions == []
        assert ep.mood_arc == []
        assert ep.sessions == []

    def test_returns_sessions_when_provided(self, em):
        ep = em.create_episode(
            week_start="2026-04-13",
            title="With sessions",
            session_ids=["sess_1", "sess_2"],
        )
        assert len(ep.sessions) == 2
        session_ids = {s.session_id for s in ep.sessions}
        assert session_ids == {"sess_1", "sess_2"}
        assert all(isinstance(s, EpisodeSession) for s in ep.sessions)


class TestGetEpisode:
    def test_returns_episode_with_sessions(self, em):
        created = em.create_episode(
            week_start="2026-04-13", title="Test", session_ids=["s1", "s2"]
        )
        fetched = em.get_episode(created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.title == "Test"
        assert len(fetched.sessions) == 2

    def test_nonexistent_returns_none(self, em):
        assert em.get_episode(999) is None

    def test_episode_without_sessions(self, em):
        created = em.create_episode(week_start="2026-04-13", title="No sessions")
        fetched = em.get_episode(created.id)
        assert fetched is not None
        assert fetched.sessions == []


# -- 2. Auto-computed week_end ---------------------------------------------

class TestWeekEndAutoCompute:
    def test_week_end_is_start_plus_6(self, em):
        ep = em.create_episode(week_start="2026-04-13", title="Test")
        assert ep.week_end == "2026-04-19"

    def test_month_boundary(self, em):
        """Week starting near end of month wraps correctly."""
        ep = em.create_episode(week_start="2026-01-28", title="Jan end")
        assert ep.week_end == "2026-02-03"

    def test_year_boundary(self, em):
        ep = em.create_episode(week_start="2025-12-29", title="Year end")
        assert ep.week_end == "2026-01-04"

    def test_leap_year(self, em):
        ep = em.create_episode(week_start="2028-02-27", title="Leap")
        assert ep.week_end == "2028-03-04"


# -- 3. Search --------------------------------------------------------------

class TestSearch:
    def test_basic_search(self, em):
        em.create_episode(
            week_start="2026-04-13", title="Identity system",
            narrative="Built the immutable identity anchor"
        )
        em.create_episode(
            week_start="2026-04-20", title="Memory architecture",
            narrative="Designed episodic memory with weekly episodes"
        )
        results = em.search_episodes("identity")
        assert len(results) == 1
        assert isinstance(results[0], Episode)
        assert results[0].title == "Identity system"

    def test_search_topics(self, em):
        em.create_episode(
            week_start="2026-04-13", title="Week 1",
            narrative="Stuff", topics=["sqlite", "fts5"]
        )
        em.create_episode(
            week_start="2026-04-20", title="Week 2",
            narrative="Other stuff", topics=["docker"]
        )
        results = em.search_episodes("sqlite")
        assert len(results) == 1
        assert results[0].title == "Week 1"

    def test_search_limit(self, em):
        for i in range(10):
            em.create_episode(
                week_start=f"2026-04-{1+i:02d}",
                title=f"Episode {i}",
                narrative="Common keyword alpha beta",
            )
        results = em.search_episodes("alpha", limit=3)
        assert len(results) == 3

    def test_search_no_results(self, em):
        em.create_episode(week_start="2026-04-13", title="Hello")
        results = em.search_episodes("nonexistent_xyz")
        assert results == []

    def test_empty_query_returns_empty(self, em):
        em.create_episode(week_start="2026-04-13", title="Test")
        results = em.search_episodes("")
        assert results == []

    def test_search_returns_episode_fields(self, em):
        em.create_episode(
            week_start="2026-04-13", title="Full episode",
            narrative="Complete data", topics=["t1"],
            key_decisions=["d1"],
            mood_arc=[{"day": "mon", "mood": "good"}],
        )
        results = em.search_episodes("Complete")
        assert len(results) == 1
        ep = results[0]
        assert ep.topics == ["t1"]
        assert ep.key_decisions == ["d1"]
        assert ep.week_end == "2026-04-19"


# -- 4. list_episodes (pagination) -----------------------------------------

class TestListEpisodes:
    def test_default_order_desc(self, em):
        for i in range(5):
            em.create_episode(
                week_start=f"2026-04-{13+i:02d}", title=f"Week {i}"
            )
        eps = em.list_episodes()
        assert len(eps) == 5
        assert eps[0].title == "Week 4"
        assert eps[-1].title == "Week 0"

    def test_pagination(self, em):
        for i in range(10):
            em.create_episode(
                week_start=f"2026-04-{1+i:02d}", title=f"Ep {i}"
            )
        page1 = em.list_episodes(limit=3, offset=0)
        page2 = em.list_episodes(limit=3, offset=3)
        assert len(page1) == 3
        assert len(page2) == 3
        assert page1[0].title != page2[0].title

    def test_returns_dataclass_instances(self, em):
        em.create_episode(week_start="2026-04-13", title="Test")
        eps = em.list_episodes()
        assert all(isinstance(ep, Episode) for ep in eps)

    def test_empty_db(self, em):
        assert em.list_episodes() == []


# -- 5. zoom_navigation ----------------------------------------------------

class TestZoomNavigation:
    def _populate(self, em):
        """Seed the DB with yearly, monthly, weekly, and session data."""
        conn = em._get_conn()
        # Yearly summary
        conn.execute(
            "INSERT INTO yearly_summaries (year, summary, highlights) "
            "VALUES (2026, 'Year of growth', ?)",
            (json.dumps(["identity", "memory"]),),
        )
        # Monthly summary
        conn.execute(
            "INSERT INTO monthly_summaries (year, month, summary, highlights) "
            "VALUES (2026, 4, 'April: identity built', ?)",
            (json.dumps(["F1 identity"]),),
        )
        conn.commit()

        # Episodes
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

        zoom_year = tree[0]
        assert isinstance(zoom_year, ZoomYear)
        assert zoom_year.year == 2026
        assert zoom_year.summary == "Year of growth"
        assert zoom_year.highlights == ["identity", "memory"]

        assert len(zoom_year.months) >= 1
        zoom_month = zoom_year.months[0]
        assert isinstance(zoom_month, ZoomMonth)
        assert zoom_month.year == 2026
        assert zoom_month.month == 4
        assert zoom_month.summary == "April: identity built"

        assert len(zoom_month.weeks) >= 2
        for zw in zoom_month.weeks:
            assert isinstance(zw, ZoomWeek)
            assert zw.title in ("Prep week", "Build week")

    def test_day_level_sessions(self, em):
        self._populate(em)
        tree = em.zoom_navigation()

        # Find "Build week" in the tree
        build_week = None
        for month in tree[0].months:
            for week in month.weeks:
                if week.title == "Build week":
                    build_week = week
                    break

        assert build_week is not None
        assert len(build_week.days) >= 1
        # The build week has 2 sessions
        all_sessions = []
        for day_sessions in build_week.days.values():
            all_sessions.extend(day_sessions)
        assert len(all_sessions) == 2
        session_ids = {s["session_id"] for s in all_sessions}
        assert session_ids == {"s_build1", "s_build2"}

    def test_empty_db_returns_empty(self, em):
        tree = em.zoom_navigation()
        assert tree == []

    def test_year_without_yearly_summary(self, em):
        """Episodes exist but no yearly_summary row — still shows up."""
        em.create_episode(week_start="2026-06-01", title="Summer")
        tree = em.zoom_navigation()
        assert len(tree) == 1
        assert tree[0].year == 2026
        assert tree[0].summary == ""  # no summary row
        assert len(tree[0].months) == 1
        assert tree[0].months[0].month == 6

    def test_month_without_monthly_summary(self, em):
        """Episodes exist but no monthly_summary row — still shows up."""
        em.create_episode(week_start="2026-05-04", title="May day")
        tree = em.zoom_navigation()
        may = [m for m in tree[0].months if m.month == 5]
        assert len(may) == 1
        assert may[0].summary == ""  # no summary row

    def test_multiple_years_ordering(self, em):
        em.create_episode(week_start="2025-06-01", title="2025 ep")
        em.create_episode(week_start="2026-06-01", title="2026 ep")
        tree = em.zoom_navigation()
        assert len(tree) == 2
        assert tree[0].year == 2026
        assert tree[1].year == 2025


# -- 6. Lifecycle -----------------------------------------------------------

class TestLifecycle:
    def test_idempotent_initialize(self, db_path):
        em = EpisodicMemory(db_path)
        em.initialize()
        em.initialize()  # no-op
        em.close()

    def test_close_and_reopen(self, db_path):
        em = EpisodicMemory(db_path)
        em.initialize()
        ep = em.create_episode(week_start="2026-04-13", title="Before close")
        em.close()

        # Reopen
        em2 = EpisodicMemory(db_path)
        em2.initialize()
        fetched = em2.get_episode(ep.id)
        assert fetched is not None
        assert fetched.title == "Before close"
        em2.close()

    def test_wal_mode(self, em, db_path):
        conn = sqlite3.connect(str(db_path))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"

    def test_foreign_keys_enabled(self, em):
        conn = em._get_conn()
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1


# -- 7. Edge cases ---------------------------------------------------------

class TestEdgeCases:
    def test_duplicate_session_ids_ignored(self, em):
        ep = em.create_episode(
            week_start="2026-04-13", title="Dup test",
            session_ids=["s1", "s1", "s1"],
        )
        assert len(ep.sessions) == 1

    def test_complex_json_roundtrip(self, em):
        mood = [
            {"day": "Monday", "mood": "anxious", "energy": 0.3},
            {"day": "Friday", "mood": "accomplished", "energy": 0.9},
        ]
        ep = em.create_episode(
            week_start="2026-04-13", title="Complex",
            topics=["a", "b", "c", "d"],
            key_decisions=["decision 1", "decision 2"],
            mood_arc=mood,
        )
        fetched = em.get_episode(ep.id)
        assert fetched.topics == ["a", "b", "c", "d"]
        assert fetched.key_decisions == ["decision 1", "decision 2"]
        assert len(fetched.mood_arc) == 2
        assert fetched.mood_arc[0]["energy"] == 0.3

    def test_zoom_navigation_weeks_in_descending_order(self, em):
        em.create_episode(week_start="2026-04-06", title="Week A")
        em.create_episode(week_start="2026-04-13", title="Week B")
        em.create_episode(week_start="2026-04-20", title="Week C")
        tree = em.zoom_navigation()
        april = [m for m in tree[0].months if m.month == 4][0]
        titles = [w.title for w in april.weeks]
        assert titles == ["Week C", "Week B", "Week A"]
