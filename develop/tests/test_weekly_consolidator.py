"""Tests for WeeklyConsolidator — episodic memory consolidation trigger.

Covers:
  1. Week resolution (default most-recent-completed, explicit)
  2. Session fetching from state.db by date range
  3. can_run() with and without sessions
  4. Full run() lifecycle (heuristic fallback)
  5. Custom summariser injection
  6. Empty week (safe no-op)
  7. Edge cases (missing DB, malformed dates)
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

# Ensure imports work from the develop directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from consolidators.episodic import (
    WeeklyConsolidator,
    _most_recent_completed_week,
    _date_range_to_unix,
    _weekday_name,
)
from episodic_memory_manager import EpisodicMemoryManager


# -- Fixtures ---------------------------------------------------------------

@pytest.fixture
def state_db(tmp_path):
    """Create a minimal state.db with the sessions table."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            system_prompt TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            billing_provider TEXT,
            billing_base_url TEXT,
            billing_mode TEXT,
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            cost_status TEXT,
            cost_source TEXT,
            pricing_version TEXT,
            title TEXT,
            api_call_count INTEGER DEFAULT 0,
            handoff_state TEXT,
            handoff_platform TEXT,
            handoff_error TEXT
        )
    """)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def memory_db(tmp_path):
    """Provide a temporary memory.db path."""
    return tmp_path / "memory.db"


@pytest.fixture
def consolidator(state_db, memory_db):
    """Provide a WeeklyConsolidator with test DBs."""
    return WeeklyConsolidator(
        state_db_path=state_db,
        memory_db_path=memory_db,
    )


def _insert_session(conn, session_id, title, started_at, source="cli", message_count=5):
    """Helper: insert a session row."""
    conn.execute(
        """INSERT INTO sessions (id, source, title, started_at, message_count)
           VALUES (?, ?, ?, ?, ?)""",
        (session_id, source, title, started_at, message_count),
    )


def _timestamp_for(iso_date: str, hour: int = 12) -> float:
    """Helper: Unix timestamp for an ISO date at a given hour UTC."""
    dt = datetime.strptime(iso_date, "%Y-%m-%d").replace(
        hour=hour, tzinfo=timezone.utc
    )
    return dt.timestamp()


# -- 1. Week resolution -----------------------------------------------------

class TestWeekResolution:
    def test_most_recent_completed_week(self):
        """Default week is Mon–Sun ending before today."""
        # Use a known Wednesday
        ref = date(2026, 5, 27)  # Wednesday
        ws, we = _most_recent_completed_week(ref=ref)
        assert ws == "2026-05-18"  # Monday
        assert we == "2026-05-24"  # Sunday

    def test_on_monday(self):
        """If today is Monday, the most recent completed week is the one before."""
        ref = date(2026, 6, 1)  # Monday
        ws, we = _most_recent_completed_week(ref=ref)
        assert ws == "2026-05-25"
        assert we == "2026-05-31"

    def test_on_sunday(self):
        """If today is Sunday, the current week isn't completed yet."""
        ref = date(2026, 5, 31)  # Sunday
        ws, we = _most_recent_completed_week(ref=ref)
        assert ws == "2026-05-18"
        assert we == "2026-05-24"

    def test_explicit_week_start(self, consolidator):
        """Explicit week_start resolves to a 7-day window."""
        ws, we = consolidator._resolve_week("2026-04-13")
        assert ws == "2026-04-13"
        assert we == "2026-04-19"


# -- 2. Session fetching ----------------------------------------------------

class TestSessionFetching:
    def test_fetch_sessions_in_range(self, consolidator, state_db):
        """Sessions within the week are returned."""
        conn = sqlite3.connect(str(state_db))
        _insert_session(conn, "s1", "Identity work", _timestamp_for("2026-04-14"))
        _insert_session(conn, "s2", "Memory design", _timestamp_for("2026-04-16"))
        _insert_session(conn, "s3", "Other week", _timestamp_for("2026-04-21"))
        conn.commit()
        conn.close()

        sessions = consolidator._fetch_sessions("2026-04-13", "2026-04-19")
        assert len(sessions) == 2
        ids = {s["session_id"] for s in sessions}
        assert "s1" in ids
        assert "s2" in ids

    def test_fetch_empty_range(self, consolidator, state_db):
        """No sessions in range returns empty list."""
        conn = sqlite3.connect(str(state_db))
        _insert_session(conn, "s1", "Title", _timestamp_for("2026-05-01"))
        conn.commit()
        conn.close()

        sessions = consolidator._fetch_sessions("2026-04-13", "2026-04-19")
        assert len(sessions) == 0

    def test_fetch_boundary_inclusive(self, consolidator, state_db):
        """Sessions at exact start/end timestamps are included."""
        conn = sqlite3.connect(str(state_db))
        # Exactly at start of Monday
        _insert_session(conn, "s_start", "Early", _timestamp_for("2026-04-13", hour=0))
        # Exactly at end of Sunday
        _insert_session(conn, "s_end", "Late", _timestamp_for("2026-04-19", hour=23))
        conn.commit()
        conn.close()

        sessions = consolidator._fetch_sessions("2026-04-13", "2026-04-19")
        assert len(sessions) == 2

    def test_fetch_returns_all_fields(self, consolidator, state_db):
        """Fetched sessions contain expected fields."""
        conn = sqlite3.connect(str(state_db))
        _insert_session(conn, "s1", "Test session", _timestamp_for("2026-04-14"))
        conn.commit()
        conn.close()

        sessions = consolidator._fetch_sessions("2026-04-13", "2026-04-19")
        s = sessions[0]
        assert s["session_id"] == "s1"
        assert s["title"] == "Test session"
        assert "started_at" in s
        assert "source" in s
        assert "message_count" in s


# -- 3. can_run() -----------------------------------------------------------

class TestCanRun:
    def test_can_run_with_sessions(self, consolidator, state_db):
        """Returns True when sessions exist for the target week."""
        conn = sqlite3.connect(str(state_db))
        _insert_session(conn, "s1", "Work", _timestamp_for("2026-04-14"))
        conn.commit()
        conn.close()

        assert consolidator.can_run(week_start="2026-04-13") is True

    def test_can_run_without_sessions(self, consolidator, state_db):
        """Returns False when no sessions exist for the target week."""
        assert consolidator.can_run(week_start="2026-04-13") is False

    def test_can_run_missing_state_db(self, tmp_path, memory_db):
        """Returns False gracefully when state.db doesn't exist."""
        wc = WeeklyConsolidator(
            state_db_path=tmp_path / "nonexistent.db",
            memory_db_path=memory_db,
        )
        assert wc.can_run(week_start="2026-04-13") is False


# -- 4. Full run() lifecycle ------------------------------------------------

class TestRunLifecycle:
    def test_run_creates_episode(self, consolidator, state_db, memory_db):
        """Full run creates a valid episode in memory.db."""
        conn = sqlite3.connect(str(state_db))
        _insert_session(conn, "s1", "Identity system built", _timestamp_for("2026-04-14"))
        _insert_session(conn, "s2", "Memory architecture designed", _timestamp_for("2026-04-16"))
        conn.commit()
        conn.close()

        result = consolidator.run(week_start="2026-04-13")

        assert result is not None
        assert result["episode_id"] > 0
        assert result["week_start"] == "2026-04-13"
        assert result["week_end"] == "2026-04-19"
        assert result["session_count"] == 2
        assert len(result["title"]) > 0
        assert len(result["narrative"]) > 0

    def test_run_links_sessions(self, consolidator, state_db, memory_db):
        """Sessions are linked to the created episode."""
        conn = sqlite3.connect(str(state_db))
        _insert_session(conn, "s1", "Session A", _timestamp_for("2026-04-14"))
        _insert_session(conn, "s2", "Session B", _timestamp_for("2026-04-15"))
        conn.commit()
        conn.close()

        result = consolidator.run(week_start="2026-04-13")
        em = EpisodicMemoryManager(memory_db)
        em.initialize()

        linked = em.get_episode_sessions(result["episode_id"])
        assert len(linked) == 2
        linked_ids = {l["session_id"] for l in linked}
        assert "s1" in linked_ids
        assert "s2" in linked_ids
        em.close()

    def test_run_persisted_to_db(self, consolidator, state_db, memory_db):
        """Episode is actually persisted and retrievable."""
        conn = sqlite3.connect(str(state_db))
        _insert_session(conn, "s1", "Test", _timestamp_for("2026-04-14"))
        conn.commit()
        conn.close()

        result = consolidator.run(week_start="2026-04-13")

        em = EpisodicMemoryManager(memory_db)
        em.initialize()
        ep = em.get_episode(result["episode_id"])
        assert ep is not None
        assert ep["title"] == result["title"]
        assert ep["week_start"] == "2026-04-13"
        em.close()

    def test_run_heuristic_title_length(self, consolidator, state_db):
        """Heuristic title is <= 80 chars."""
        conn = sqlite3.connect(str(state_db))
        for i in range(5):
            _insert_session(conn, f"s{i}", f"A very long session title number {i} about various topics", _timestamp_for("2026-04-14"))
        conn.commit()
        conn.close()

        result = consolidator.run(week_start="2026-04-13")
        assert len(result["title"]) <= 80


# -- 5. Empty week (no-op) --------------------------------------------------

class TestEmptyWeek:
    def test_run_returns_none(self, consolidator, state_db):
        """run() returns None when no sessions exist."""
        result = consolidator.run(week_start="2026-04-13")
        assert result is None

    def test_no_episode_created(self, consolidator, state_db, memory_db):
        """No episode is persisted for an empty week."""
        consolidator.run(week_start="2026-04-13")

        em = EpisodicMemoryManager(memory_db)
        em.initialize()
        eps = em.list_episodes()
        assert len(eps) == 0
        em.close()


# -- 6. Custom summariser ---------------------------------------------------

class TestCustomSummariser:
    def test_custom_summariser_called(self, state_db, memory_db):
        """Pluggable summariser is used when provided."""
        called_with = []

        def my_summariser(sessions):
            called_with.append(sessions)
            return {
                "title": "Custom title",
                "narrative": "Custom narrative from LLM",
                "topics": ["custom", "llm"],
                "key_decisions": ["used custom summariser"],
                "mood_arc": [{"day": "monday", "mood": "positive"}],
            }

        wc = WeeklyConsolidator(
            state_db_path=state_db,
            memory_db_path=memory_db,
            summariser=my_summariser,
        )

        conn = sqlite3.connect(str(state_db))
        _insert_session(conn, "s1", "Test", _timestamp_for("2026-04-14"))
        conn.commit()
        conn.close()

        result = wc.run(week_start="2026-04-13")

        assert len(called_with) == 1
        assert result["title"] == "Custom title"
        assert result["topics"] == ["custom", "llm"]
        assert result["key_decisions"] == ["used custom summariser"]
        assert len(result["mood_arc"]) == 1

    def test_summariser_failure_falls_back(self, state_db, memory_db):
        """If summariser raises, the heuristic fallback is used."""
        def bad_summariser(sessions):
            raise RuntimeError("LLM unavailable")

        wc = WeeklyConsolidator(
            state_db_path=state_db,
            memory_db_path=memory_db,
            summariser=bad_summariser,
        )

        conn = sqlite3.connect(str(state_db))
        _insert_session(conn, "s1", "Fallback test", _timestamp_for("2026-04-14"))
        conn.commit()
        conn.close()

        result = wc.run(week_start="2026-04-13")
        assert result is not None
        # Should have heuristic-style content
        assert "session" in result["narrative"].lower()

    def test_summariser_incomplete_result_falls_back(self, state_db, memory_db):
        """If summariser returns incomplete dict, fallback is used."""
        def incomplete_summariser(sessions):
            return {"title": "Only title"}  # missing "narrative"

        wc = WeeklyConsolidator(
            state_db_path=state_db,
            memory_db_path=memory_db,
            summariser=incomplete_summariser,
        )

        conn = sqlite3.connect(str(state_db))
        _insert_session(conn, "s1", "Test", _timestamp_for("2026-04-14"))
        conn.commit()
        conn.close()

        result = wc.run(week_start="2026-04-13")
        assert result is not None
        assert len(result["narrative"]) > 0


# -- 7. Edge cases ----------------------------------------------------------

class TestEdgeCases:
    def test_session_with_no_title(self, consolidator, state_db):
        """Sessions with NULL title use session_id as fallback."""
        conn = sqlite3.connect(str(state_db))
        conn.execute(
            """INSERT INTO sessions (id, source, started_at, message_count)
               VALUES (?, ?, ?, ?)""",
            ("s_notitle", "cli", _timestamp_for("2026-04-14"), 1),
        )
        conn.commit()
        conn.close()

        result = consolidator.run(week_start="2026-04-13")
        assert result is not None
        assert "s_notitle" in result["narrative"]

    def test_many_sessions(self, consolidator, state_db):
        """Handles many sessions without error."""
        conn = sqlite3.connect(str(state_db))
        for i in range(50):
            _insert_session(conn, f"s{i}", f"Session {i}", _timestamp_for("2026-04-14", hour=i % 24))
        conn.commit()
        conn.close()

        result = consolidator.run(week_start="2026-04-13")
        assert result is not None
        assert result["session_count"] == 50

    def test_idempotent_run(self, consolidator, state_db, memory_db):
        """Running twice for the same week creates two episodes (not idempotent by design)."""
        conn = sqlite3.connect(str(state_db))
        _insert_session(conn, "s1", "Test", _timestamp_for("2026-04-14"))
        conn.commit()
        conn.close()

        r1 = consolidator.run(week_start="2026-04-13")
        r2 = consolidator.run(week_start="2026-04-13")

        assert r1["episode_id"] != r2["episode_id"]
        # Both should exist
        em = EpisodicMemoryManager(memory_db)
        em.initialize()
        assert em.get_episode(r1["episode_id"]) is not None
        assert em.get_episode(r2["episode_id"]) is not None
        em.close()


# -- 8. Helper functions ----------------------------------------------------

class TestHelpers:
    def test_date_range_to_unix(self):
        ts_start, ts_end = _date_range_to_unix("2026-04-13", "2026-04-19")
        # Start should be midnight UTC
        dt_start = datetime.fromtimestamp(ts_start, tz=timezone.utc)
        assert dt_start.hour == 0
        assert dt_start.minute == 0
        # End should be 23:59:59 UTC
        dt_end = datetime.fromtimestamp(ts_end, tz=timezone.utc)
        assert dt_end.hour == 23
        assert dt_end.minute == 59

    def test_weekday_name(self):
        assert _weekday_name("2026-04-13") == "monday"
        assert _weekday_name("2026-04-19") == "sunday"
        assert _weekday_name("2026-04-15") == "wednesday"
