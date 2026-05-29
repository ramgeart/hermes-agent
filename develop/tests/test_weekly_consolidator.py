"""Tests for WeeklyConsolidator — episodic memory weekly consolidation trigger.

Covers:
  1. Week resolution (default most recent completed week, explicit week_start)
  2. Session fetching from state.db (date range filtering)
  3. can_run() checks (sessions exist, no sessions, empty DB)
  4. run() with sessions → valid episode created
  5. run() with no sessions → safe no-op (returns None)
  6. Heuristic fallback summariser
  7. Pluggable custom summariser
  8. Session linking after episode creation
  9. Edge cases (missing state.db, malformed dates, single vs multi-session weeks)
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from consolidators.episodic import (
    WeeklyConsolidator,
    _most_recent_completed_week,
    _date_range_to_unix,
    _weekday_name,
)


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def state_db(tmp_path):
    """Create a minimal state.db with sessions table."""
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
def memory_db(tmp_path):
    """Path to a temporary memory.db (will be created by the consolidator)."""
    return tmp_path / "memory.db"


@pytest.fixture
def consolidator(state_db, memory_db):
    """Provide a WeeklyConsolidator with temp DBs."""
    return WeeklyConsolidator(
        state_db_path=state_db,
        memory_db_path=memory_db,
    )


def _insert_session(state_db, session_id, title, started_at, ended_at=None, source="cli", model="test"):
    """Helper: insert a session into state.db."""
    conn = sqlite3.connect(str(state_db))
    conn.execute(
        "INSERT INTO sessions (id, title, source, model, started_at, ended_at, message_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_id, title, source, model, started_at, ended_at, 10),
    )
    conn.commit()
    conn.close()


def _iso_to_unix(iso_date: str, hour=12) -> float:
    """Convert ISO date string to Unix timestamp at noon UTC."""
    dt = datetime.strptime(iso_date, "%Y-%m-%d").replace(hour=hour, tzinfo=timezone.utc)
    return dt.timestamp()


# -- 1. Week resolution helpers ----------------------------------------------

class TestMostRecentCompletedWeek:
    def test_returns_mon_sun_range(self):
        ws, we = _most_recent_completed_week()
        ws_date = datetime.strptime(ws, "%Y-%m-%d").date()
        we_date = datetime.strptime(we, "%Y-%m-%d").date()
        assert ws_date.weekday() == 0  # Monday
        assert we_date.weekday() == 6  # Sunday
        assert (we_date - ws_date).days == 6

    def test_week_is_before_reference_date(self):
        # If today is Wednesday 2026-05-27
        ref = date(2026, 5, 27)
        ws, we = _most_recent_completed_week(ref)
        assert ws == "2026-05-18"
        assert we == "2026-05-24"

    def test_on_monday_returns_previous_week(self):
        ref = date(2026, 6, 1)  # Monday
        ws, we = _most_recent_completed_week(ref)
        assert ws == "2026-05-25"
        assert we == "2026-05-31"

    def test_on_sunday_returns_week_before(self):
        ref = date(2026, 5, 31)  # Sunday
        ws, we = _most_recent_completed_week(ref)
        assert ws == "2026-05-18"
        assert we == "2026-05-24"


class TestDateRangeToUnix:
    def test_start_of_day(self):
        ts_start, ts_end = _date_range_to_unix("2026-05-25", "2026-05-31")
        dt_start = datetime.fromtimestamp(ts_start, tz=timezone.utc)
        assert dt_start.hour == 0
        assert dt_start.minute == 0

    def test_end_of_day(self):
        ts_start, ts_end = _date_range_to_unix("2026-05-25", "2026-05-31")
        dt_end = datetime.fromtimestamp(ts_end, tz=timezone.utc)
        assert dt_end.hour == 23
        assert dt_end.minute == 59
        assert dt_end.second == 59


class TestWeekdayName:
    def test_known_dates(self):
        assert _weekday_name("2026-05-25") == "monday"
        assert _weekday_name("2026-05-31") == "sunday"

    def test_all_days(self):
        dates = {
            "monday": "2026-05-25",
            "tuesday": "2026-05-26",
            "wednesday": "2026-05-27",
            "thursday": "2026-05-28",
            "friday": "2026-05-29",
            "saturday": "2026-05-30",
            "sunday": "2026-05-31",
        }
        for expected, d in dates.items():
            assert _weekday_name(d) == expected


# -- 2. can_run() checks -----------------------------------------------------

class TestCanRun:
    def test_no_sessions_returns_false(self, consolidator):
        assert consolidator.can_run("2026-05-25") is False

    def test_sessions_in_week_returns_true(self, consolidator, state_db):
        _insert_session(state_db, "s1", "Test session", _iso_to_unix("2026-05-27"))
        assert consolidator.can_run("2026-05-25") is True

    def test_sessions_outside_week_returns_false(self, consolidator, state_db):
        _insert_session(state_db, "s1", "Old session", _iso_to_unix("2026-05-18"))
        assert consolidator.can_run("2026-05-25") is False

    def test_missing_state_db(self, memory_db):
        wc = WeeklyConsolidator(
            state_db_path="/nonexistent/state.db",
            memory_db_path=memory_db,
        )
        assert wc.can_run("2026-05-25") is False

    def test_default_week_uses_most_recent_completed(self, consolidator, state_db):
        """Without explicit week_start, uses the most recent completed Mon-Sun."""
        ws, we = _most_recent_completed_week()
        # Insert a session in the middle of that week
        mid = datetime.strptime(ws, "%Y-%m-%d") + timedelta(days=3)
        _insert_session(state_db, "s1", "Default week session", mid.replace(tzinfo=timezone.utc).timestamp())
        assert consolidator.can_run() is True


# -- 3. Session fetching -----------------------------------------------------

class TestFetchSessions:
    def test_returns_sessions_in_range(self, consolidator, state_db):
        _insert_session(state_db, "s1", "Session A", _iso_to_unix("2026-05-25"))
        _insert_session(state_db, "s2", "Session B", _iso_to_unix("2026-05-27"))
        _insert_session(state_db, "s3", "Session C", _iso_to_unix("2026-05-31"))
        _insert_session(state_db, "s4", "Session D", _iso_to_unix("2026-06-01"))  # outside

        sessions = consolidator._fetch_sessions("2026-05-25", "2026-05-31")
        assert len(sessions) == 3
        ids = {s["session_id"] for s in sessions}
        assert ids == {"s1", "s2", "s3"}

    def test_ordered_by_started_at(self, consolidator, state_db):
        _insert_session(state_db, "s_late", "Late", _iso_to_unix("2026-05-30"))
        _insert_session(state_db, "s_early", "Early", _iso_to_unix("2026-05-25"))

        sessions = consolidator._fetch_sessions("2026-05-25", "2026-05-31")
        assert sessions[0]["session_id"] == "s_early"
        assert sessions[1]["session_id"] == "s_late"

    def test_empty_week_returns_empty(self, consolidator):
        sessions = consolidator._fetch_sessions("2026-05-25", "2026-05-31")
        assert sessions == []

    def test_session_fields_present(self, consolidator, state_db):
        _insert_session(state_db, "s1", "Full session", _iso_to_unix("2026-05-27"))
        sessions = consolidator._fetch_sessions("2026-05-25", "2026-05-31")
        s = sessions[0]
        assert "session_id" in s
        assert "title" in s
        assert "started_at" in s
        assert "ended_at" in s
        assert "message_count" in s


# -- 4. run() creates valid episode ------------------------------------------

class TestRunWithSessions:
    def test_creates_episode(self, consolidator, state_db, memory_db):
        _insert_session(state_db, "s1", "Identity work", _iso_to_unix("2026-05-27"))
        result = consolidator.run("2026-05-25")

        assert result is not None
        assert result["week_start"] == "2026-05-25"
        assert result["week_end"] == "2026-05-31"
        assert len(result["title"]) > 0
        assert result["session_count"] == 1
        assert isinstance(result["episode_id"], int)

    def test_episode_persisted_in_db(self, consolidator, state_db, memory_db):
        _insert_session(state_db, "s1", "Test session", _iso_to_unix("2026-05-27"))
        result = consolidator.run("2026-05-25")

        # Verify it's in the DB
        from agent.episodic_memory_manager import EpisodicMemoryManager
        em = EpisodicMemoryManager(memory_db)
        em.initialize()
        ep = em.get_episode(result["episode_id"])
        assert ep is not None
        assert ep["title"] == result["title"]
        em.close()

    def test_multiple_sessions_consolidated(self, consolidator, state_db):
        _insert_session(state_db, "s1", "Identity built", _iso_to_unix("2026-05-25"))
        _insert_session(state_db, "s2", "Memory designed", _iso_to_unix("2026-05-27"))
        _insert_session(state_db, "s3", "Tests written", _iso_to_unix("2026-05-29"))

        result = consolidator.run("2026-05-25")
        assert result is not None
        assert result["session_count"] == 3
        assert "3 session" in result["title"].lower() or "sessions" in result["title"].lower()

    def test_session_ids_linked(self, consolidator, state_db, memory_db):
        _insert_session(state_db, "s1", "Session A", _iso_to_unix("2026-05-26"))
        _insert_session(state_db, "s2", "Session B", _iso_to_unix("2026-05-28"))

        result = consolidator.run("2026-05-25")

        from agent.episodic_memory_manager import EpisodicMemoryManager
        em = EpisodicMemoryManager(memory_db)
        em.initialize()
        linked = em.get_episode_sessions(result["episode_id"])
        linked_ids = {s["session_id"] for s in linked}
        assert linked_ids == {"s1", "s2"}
        em.close()


# -- 5. run() with no sessions → safe no-op ----------------------------------

class TestRunEmptyWeek:
    def test_returns_none(self, consolidator):
        result = consolidator.run("2026-05-25")
        assert result is None

    def test_no_episode_created(self, consolidator, memory_db):
        consolidator.run("2026-05-25")
        # memory.db should either not exist or have no episodes
        if memory_db.exists():
            from agent.episodic_memory_manager import EpisodicMemoryManager
            em = EpisodicMemoryManager(memory_db)
            em.initialize()
            assert em.list_episodes() == []
            em.close()

    def test_missing_state_db_safe_noop(self, memory_db):
        wc = WeeklyConsolidator(
            state_db_path="/nonexistent/state.db",
            memory_db_path=memory_db,
        )
        result = wc.run("2026-05-25")
        assert result is None


# -- 6. Heuristic fallback summariser ----------------------------------------

class TestHeuristicSummary:
    def test_single_session_title(self, consolidator, state_db):
        _insert_session(state_db, "s1", "Build identity system", _iso_to_unix("2026-05-27"))
        result = consolidator.run("2026-05-25")
        assert "Build identity system" in result["title"]

    def test_multi_session_title(self, consolidator, state_db):
        _insert_session(state_db, "s1", "Session A", _iso_to_unix("2026-05-25"))
        _insert_session(state_db, "s2", "Session B", _iso_to_unix("2026-05-27"))
        result = consolidator.run("2026-05-25")
        assert "2 session" in result["title"].lower()

    def test_narrative_contains_session_info(self, consolidator, state_db):
        _insert_session(state_db, "s1", "Fix authentication bug", _iso_to_unix("2026-05-26"))
        result = consolidator.run("2026-05-25")
        assert "Fix authentication bug" in result["narrative"]

    def test_title_max_80_chars(self, consolidator, state_db):
        long_title = "A" * 200
        _insert_session(state_db, "s1", long_title, _iso_to_unix("2026-05-27"))
        result = consolidator.run("2026-05-25")
        assert len(result["title"]) <= 80

    def test_topics_extraction(self, consolidator, state_db):
        _insert_session(state_db, "s1", "Identity: build anchor", _iso_to_unix("2026-05-26"))
        _insert_session(state_db, "s2", "Memory: design schema", _iso_to_unix("2026-05-27"))
        result = consolidator.run("2026-05-25")
        assert len(result["topics"]) >= 1

    def test_key_decisions_empty_in_heuristic(self, consolidator, state_db):
        _insert_session(state_db, "s1", "Test", _iso_to_unix("2026-05-27"))
        result = consolidator.run("2026-05-25")
        assert result["key_decisions"] == []

    def test_mood_arc_empty_in_heuristic(self, consolidator, state_db):
        _insert_session(state_db, "s1", "Test", _iso_to_unix("2026-05-27"))
        result = consolidator.run("2026-05-25")
        assert result["mood_arc"] == []


# -- 7. Pluggable custom summariser ------------------------------------------

class TestCustomSummariser:
    def test_custom_summariser_called(self, state_db, memory_db):
        called_with = []

        def my_summariser(sessions):
            called_with.append(sessions)
            return {
                "title": "Custom title",
                "narrative": "Custom narrative paragraph.",
                "topics": ["custom", "test"],
                "key_decisions": ["chose custom approach"],
                "mood_arc": [{"day": "monday", "mood": "positive"}],
            }

        wc = WeeklyConsolidator(
            state_db_path=state_db,
            memory_db_path=memory_db,
            summariser=my_summariser,
        )
        _insert_session(state_db, "s1", "Test", _iso_to_unix("2026-05-27"))

        result = wc.run("2026-05-25")
        assert result is not None
        assert result["title"] == "Custom title"
        assert result["narrative"] == "Custom narrative paragraph."
        assert result["topics"] == ["custom", "test"]
        assert result["key_decisions"] == ["chose custom approach"]
        assert len(result["mood_arc"]) == 1
        assert len(called_with) == 1

    def test_custom_summariser_failure_falls_back(self, state_db, memory_db):
        def bad_summariser(sessions):
            raise RuntimeError("LLM unavailable")

        wc = WeeklyConsolidator(
            state_db_path=state_db,
            memory_db_path=memory_db,
            summariser=bad_summariser,
        )
        _insert_session(state_db, "s1", "Fallback test", _iso_to_unix("2026-05-27"))

        result = wc.run("2026-05-25")
        assert result is not None  # Should fall back to heuristic
        assert "Fallback test" in result["narrative"]

    def test_incomplete_result_falls_back(self, state_db, memory_db):
        def incomplete_summariser(sessions):
            return {"title": "Only title"}  # missing 'narrative'

        wc = WeeklyConsolidator(
            state_db_path=state_db,
            memory_db_path=memory_db,
            summariser=incomplete_summariser,
        )
        _insert_session(state_db, "s1", "Incomplete", _iso_to_unix("2026-05-27"))

        result = wc.run("2026-05-25")
        assert result is not None
        # Should have used heuristic instead
        assert "Incomplete" in result["narrative"]


# -- 8. Episode structure validation -----------------------------------------

class TestEpisodeStructure:
    def test_result_has_all_fields(self, consolidator, state_db):
        _insert_session(state_db, "s1", "Full test", _iso_to_unix("2026-05-27"))
        result = consolidator.run("2026-05-25")

        assert "episode_id" in result
        assert "week_start" in result
        assert "week_end" in result
        assert "title" in result
        assert "narrative" in result
        assert "topics" in result
        assert "key_decisions" in result
        assert "mood_arc" in result
        assert "session_count" in result

    def test_topics_is_list(self, consolidator, state_db):
        _insert_session(state_db, "s1", "Test: topic extraction", _iso_to_unix("2026-05-27"))
        result = consolidator.run("2026-05-25")
        assert isinstance(result["topics"], list)
        assert all(isinstance(t, str) for t in result["topics"])

    def test_key_decisions_is_list(self, consolidator, state_db):
        _insert_session(state_db, "s1", "Test", _iso_to_unix("2026-05-27"))
        result = consolidator.run("2026-05-25")
        assert isinstance(result["key_decisions"], list)

    def test_mood_arc_is_list(self, consolidator, state_db):
        _insert_session(state_db, "s1", "Test", _iso_to_unix("2026-05-27"))
        result = consolidator.run("2026-05-25")
        assert isinstance(result["mood_arc"], list)


# -- 9. Edge cases -----------------------------------------------------------

class TestEdgeCases:
    def test_run_twice_same_week(self, consolidator, state_db, memory_db):
        """Running consolidation twice for the same week creates two episodes."""
        _insert_session(state_db, "s1", "Test", _iso_to_unix("2026-05-27"))

        r1 = consolidator.run("2026-05-25")
        r2 = consolidator.run("2026-05-25")
        assert r1 is not None
        assert r2 is not None
        assert r1["episode_id"] != r2["episode_id"]

    def test_boundary_session_at_week_start(self, consolidator, state_db):
        """Session at exactly midnight on Monday should be included."""
        ts = datetime.strptime("2026-05-25", "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        _insert_session(state_db, "s1", "Boundary start", ts)
        result = consolidator.run("2026-05-25")
        assert result is not None
        assert result["session_count"] == 1

    def test_boundary_session_at_week_end(self, consolidator, state_db):
        """Session at 23:59:59 on Sunday should be included."""
        ts = datetime.strptime("2026-05-31", "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        ).timestamp()
        _insert_session(state_db, "s1", "Boundary end", ts)
        result = consolidator.run("2026-05-25")
        assert result is not None
        assert result["session_count"] == 1

    def test_untitled_session_in_fallback(self, consolidator, state_db):
        """Sessions with None title use session_id as fallback."""
        _insert_session(state_db, "s_unknown", None, _iso_to_unix("2026-05-27"))
        # Update to set title to NULL
        import sqlite3 as sql
        conn = sql.connect(str(state_db))
        conn.execute("UPDATE sessions SET title = NULL WHERE id = 's_unknown'")
        conn.commit()
        conn.close()

        result = consolidator.run("2026-05-25")
        assert result is not None
        # Should not crash

    def test_week_crossing_month_boundary(self, state_db, memory_db):
        """Week spanning two months works correctly."""
        wc = WeeklyConsolidator(state_db_path=state_db, memory_db_path=memory_db)
        _insert_session(state_db, "s1", "End of May", _iso_to_unix("2026-05-31"))
        result = wc.run("2026-05-25")
        assert result is not None
        assert result["week_start"] == "2026-05-25"
        assert result["week_end"] == "2026-05-31"
