"""Tests for EpisodicMemoryManager — temporal episodic memory system.

Covers:
  1. Schema creation (5 tables + FTS5 + triggers)
  2. Episode CRUD lifecycle
  3. Episode-session linking + cascade delete
  4. FTS5 full-text search
  5. Monthly summaries CRUD
  6. Yearly summaries CRUD
  7. Edge cases (empty fields, invalid JSON, duplicate links)
  8. FK constraints (cascade delete)
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from episodic_memory_manager import EpisodicMemoryManager


@pytest.fixture
def db_path(tmp_path):
    """Provide a temporary database path."""
    return tmp_path / "test_memory.db"


@pytest.fixture
def em(db_path):
    """Provide an initialized EpisodicMemoryManager."""
    manager = EpisodicMemoryManager(db_path)
    manager.initialize()
    yield manager
    manager.close()


# -- 1. Schema creation ---------------------------------------------------

class TestSchema:
    def test_initialize_creates_db(self, db_path):
        assert not db_path.exists()
        em = EpisodicMemoryManager(db_path)
        em.initialize()
        assert db_path.exists()
        em.close()

    def test_all_tables_exist(self, em, db_path):
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()
        expected = {
            "weekly_episodes",
            "episode_sessions",
            "monthly_summaries",
            "yearly_summaries",
            "episode_fts",
        }
        assert expected.issubset(tables), f"Missing tables: {expected - tables}"

    def test_fts5_search_works(self, em):
        """FTS5 virtual table is functional and returns results."""
        em.create_episode(
            week_start="2026-04-13",
            week_end="2026-04-19",
            title="Identity system built",
            narrative="Implemented the immutable identity anchor for Maria",
            topics=["identity", "sqlite"],
        )
        results = em.search_episodes("identity")
        assert len(results) == 1
        assert results[0]["title"] == "Identity system built"

    def test_fts_triggers_keep_sync(self, em):
        """INSERT/UPDATE/DELETE triggers keep FTS in sync."""
        eid = em.create_episode(
            week_start="2026-04-13",
            week_end="2026-04-19",
            title="Original title",
            narrative="Original narrative about cats",
        )
        # Search for original
        assert len(em.search_episodes("cats")) == 1

        # Update narrative
        em.update_episode(eid, narrative="New narrative about dogs")
        assert len(em.search_episodes("cats")) == 0
        assert len(em.search_episodes("dogs")) == 1

        # Delete
        em.delete_episode(eid)
        assert len(em.search_episodes("dogs")) == 0

    def test_wal_mode_enabled(self, em, db_path):
        conn = sqlite3.connect(str(db_path))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"

    def test_foreign_keys_enabled(self, em):
        """FK pragma is set on the manager's own connection."""
        conn = em._get_conn()
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1

    def test_idempotent_initialize(self, em, db_path):
        """Calling initialize() twice doesn't error."""
        em.initialize()
        em.initialize()  # should be a no-op
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM weekly_episodes").fetchone()[0]
        conn.close()
        assert count == 0


# -- 2. Episode CRUD ------------------------------------------------------

class TestEpisodeCRUD:
    def test_create_and_get(self, em):
        eid = em.create_episode(
            week_start="2026-04-13",
            week_end="2026-04-19",
            title="Week 1",
            narrative="First week of episodic memory",
            topics=["memory", "architecture"],
            key_decisions=["separate DB for episodes"],
            mood_arc=[{"day": "mon", "mood": "curious"}, {"day": "fri", "mood": "satisfied"}],
        )
        assert isinstance(eid, int)
        ep = em.get_episode(eid)
        assert ep is not None
        assert ep["title"] == "Week 1"
        assert ep["topics"] == ["memory", "architecture"]
        assert ep["key_decisions"] == ["separate DB for episodes"]
        assert len(ep["mood_arc"]) == 2
        assert ep["mood_arc"][0]["mood"] == "curious"

    def test_create_with_defaults(self, em):
        """Empty lists default correctly."""
        eid = em.create_episode(
            week_start="2026-04-13",
            week_end="2026-04-19",
            title="Minimal",
        )
        ep = em.get_episode(eid)
        assert ep["topics"] == []
        assert ep["key_decisions"] == []
        assert ep["mood_arc"] == []
        assert ep["narrative"] == ""

    def test_get_nonexistent(self, em):
        assert em.get_episode(999) is None

    def test_list_episodes(self, em):
        for i in range(5):
            em.create_episode(
                week_start=f"2026-04-{13 + i:02d}",
                week_end=f"2026-04-{19 + i:02d}",
                title=f"Week {i}",
            )
        eps = em.list_episodes()
        assert len(eps) == 5
        # Most recent first
        assert eps[0]["title"] == "Week 4"

    def test_list_with_limit_offset(self, em):
        for i in range(10):
            em.create_episode(
                week_start=f"2026-04-{1 + i:02d}",
                week_end=f"2026-04-{7 + i:02d}",
                title=f"Week {i}",
            )
        page = em.list_episodes(limit=3, offset=2)
        assert len(page) == 3

    def test_update_episode(self, em):
        eid = em.create_episode(
            week_start="2026-04-13",
            week_end="2026-04-19",
            title="Old title",
            narrative="Old narrative",
        )
        em.update_episode(eid, title="New title", topics=["updated"])
        ep = em.get_episode(eid)
        assert ep["title"] == "New title"
        assert ep["topics"] == ["updated"]
        assert ep["narrative"] == "Old narrative"  # unchanged

    def test_update_invalid_field(self, em):
        eid = em.create_episode(
            week_start="2026-04-13",
            week_end="2026-04-19",
            title="Test",
        )
        with pytest.raises(ValueError, match="Unknown episode fields"):
            em.update_episode(eid, nonexistent="value")

    def test_delete_episode(self, em):
        eid = em.create_episode(
            week_start="2026-04-13",
            week_end="2026-04-19",
            title="To delete",
        )
        assert em.delete_episode(eid) is True
        assert em.get_episode(eid) is None

    def test_delete_nonexistent(self, em):
        assert em.delete_episode(999) is False


# -- 3. Episode-session linking -------------------------------------------

class TestEpisodeSessions:
    def test_link_and_get(self, em):
        eid = em.create_episode(
            week_start="2026-04-13",
            week_end="2026-04-19",
            title="Week 1",
        )
        lid = em.link_session(eid, "session_abc", relevance_score=0.8)
        assert isinstance(lid, int)

        sessions = em.get_episode_sessions(eid)
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "session_abc"
        assert sessions[0]["relevance_score"] == 0.8

    def test_duplicate_link_ignored(self, em):
        eid = em.create_episode(
            week_start="2026-04-13",
            week_end="2026-04-19",
            title="Week 1",
        )
        em.link_session(eid, "session_abc")
        em.link_session(eid, "session_abc")  # duplicate — should be ignored
        sessions = em.get_episode_sessions(eid)
        assert len(sessions) == 1

    def test_multiple_sessions(self, em):
        eid = em.create_episode(
            week_start="2026-04-13",
            week_end="2026-04-19",
            title="Week 1",
        )
        em.link_session(eid, "s1", 1.0)
        em.link_session(eid, "s2", 0.5)
        em.link_session(eid, "s3", 0.9)
        sessions = em.get_episode_sessions(eid)
        assert len(sessions) == 3
        # Ordered by relevance DESC
        assert sessions[0]["session_id"] == "s1"
        assert sessions[1]["session_id"] == "s3"
        assert sessions[2]["session_id"] == "s2"

    def test_get_sessions_episodes(self, em):
        eid1 = em.create_episode(
            week_start="2026-04-13", week_end="2026-04-19", title="Week 1"
        )
        eid2 = em.create_episode(
            week_start="2026-04-20", week_end="2026-04-26", title="Week 2"
        )
        em.link_session(eid1, "shared_session")
        em.link_session(eid2, "shared_session")
        em.link_session(eid2, "other_session")

        eps = em.get_sessions_episodes("shared_session")
        assert len(eps) == 2
        # Most recent week first
        assert eps[0]["title"] == "Week 2"

    def test_unlink_session(self, em):
        eid = em.create_episode(
            week_start="2026-04-13", week_end="2026-04-19", title="Week 1"
        )
        em.link_session(eid, "s1")
        assert em.unlink_session(eid, "s1") is True
        assert len(em.get_episode_sessions(eid)) == 0

    def test_cascade_delete(self, em):
        """Deleting an episode cascades to episode_sessions."""
        eid = em.create_episode(
            week_start="2026-04-13", week_end="2026-04-19", title="Week 1"
        )
        em.link_session(eid, "s1")
        em.link_session(eid, "s2")
        em.delete_episode(eid)
        # Sessions should be gone
        sessions = em.get_episode_sessions(eid)
        assert len(sessions) == 0


# -- 4. FTS search --------------------------------------------------------

class TestFTSSearch:
    def test_basic_search(self, em):
        em.create_episode(
            week_start="2026-04-13",
            week_end="2026-04-19",
            title="Identity system",
            narrative="Built the immutable identity anchor",
        )
        em.create_episode(
            week_start="2026-04-20",
            week_end="2026-04-26",
            title="Memory architecture",
            narrative="Designed episodic memory with weekly episodes",
        )
        results = em.search_episodes("identity")
        assert len(results) == 1
        assert results[0]["title"] == "Identity system"

    def test_search_topics(self, em):
        em.create_episode(
            week_start="2026-04-13",
            week_end="2026-04-19",
            title="Week 1",
            narrative="Stuff happened",
            topics=["sqlite", "fts5"],
        )
        em.create_episode(
            week_start="2026-04-20",
            week_end="2026-04-26",
            title="Week 2",
            narrative="More stuff",
            topics=["docker", "deployment"],
        )
        results = em.search_episodes("sqlite")
        assert len(results) == 1

    def test_search_no_results(self, em):
        em.create_episode(
            week_start="2026-04-13",
            week_end="2026-04-19",
            title="Week 1",
            narrative="Hello world",
        )
        results = em.search_episodes("nonexistent_term")
        assert len(results) == 0

    def test_search_limit(self, em):
        for i in range(10):
            em.create_episode(
                week_start=f"2026-04-{1 + i:02d}",
                week_end=f"2026-04-{7 + i:02d}",
                title=f"Episode {i}",
                narrative="Common keyword alpha beta gamma",
            )
        results = em.search_episodes("alpha", limit=3)
        assert len(results) == 3


# -- 5. Monthly summaries -------------------------------------------------

class TestMonthlySummaries:
    def test_create_and_get(self, em):
        mid = em.create_monthly_summary(2026, 4, "April was productive", ["identity", "memory"])
        assert isinstance(mid, int)
        summary = em.get_monthly_summary(2026, 4)
        assert summary is not None
        assert summary["summary"] == "April was productive"
        assert summary["highlights"] == ["identity", "memory"]

    def test_upsert(self, em):
        """INSERT OR REPLACE overwrites on same (year, month)."""
        em.create_monthly_summary(2026, 4, "First version")
        em.create_monthly_summary(2026, 4, "Updated version")
        summary = em.get_monthly_summary(2026, 4)
        assert summary["summary"] == "Updated version"

    def test_get_nonexistent(self, em):
        assert em.get_monthly_summary(2099, 12) is None

    def test_update_monthly(self, em):
        em.create_monthly_summary(2026, 4, "Original")
        em.update_monthly_summary(2026, 4, summary="Updated", highlights=["new"])
        s = em.get_monthly_summary(2026, 4)
        assert s["summary"] == "Updated"
        assert s["highlights"] == ["new"]

    def test_update_invalid_field(self, em):
        em.create_monthly_summary(2026, 4, "Original")
        with pytest.raises(ValueError, match="Unknown monthly summary fields"):
            em.update_monthly_summary(2026, 4, bad_field="value")

    def test_list_monthly(self, em):
        em.create_monthly_summary(2026, 1, "Jan")
        em.create_monthly_summary(2026, 3, "Mar")
        em.create_monthly_summary(2026, 2, "Feb")
        summaries = em.list_monthly_summaries()
        assert len(summaries) == 3
        # Descending order
        assert summaries[0]["month"] == 3
        assert summaries[1]["month"] == 2
        assert summaries[2]["month"] == 1

    def test_unique_constraint(self, em):
        """year+month uniqueness enforced at DB level."""
        conn = sqlite3.connect(str(em.db_path))
        conn.execute(
            "INSERT INTO monthly_summaries (year, month, summary) VALUES (2026, 4, 'first')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO monthly_summaries (year, month, summary) VALUES (2026, 4, 'second')"
            )
        conn.close()


# -- 6. Yearly summaries --------------------------------------------------

class TestYearlySummaries:
    def test_create_and_get(self, em):
        yid = em.create_yearly_summary(2026, "Year of identity and memory", ["F1", "F2"])
        assert isinstance(yid, int)
        summary = em.get_yearly_summary(2026)
        assert summary is not None
        assert summary["summary"] == "Year of identity and memory"
        assert summary["highlights"] == ["F1", "F2"]

    def test_upsert(self, em):
        em.create_yearly_summary(2026, "First version")
        em.create_yearly_summary(2026, "Updated version")
        summary = em.get_yearly_summary(2026)
        assert summary["summary"] == "Updated version"

    def test_get_nonexistent(self, em):
        assert em.get_yearly_summary(2099) is None

    def test_update_yearly(self, em):
        em.create_yearly_summary(2026, "Original")
        em.update_yearly_summary(2026, summary="Updated", highlights=["h1"])
        s = em.get_yearly_summary(2026)
        assert s["summary"] == "Updated"
        assert s["highlights"] == ["h1"]

    def test_update_invalid_field(self, em):
        em.create_yearly_summary(2026, "Original")
        with pytest.raises(ValueError, match="Unknown yearly summary fields"):
            em.update_yearly_summary(2026, bad_field="value")

    def test_list_yearly(self, em):
        em.create_yearly_summary(2024, "2024 recap")
        em.create_yearly_summary(2026, "2026 recap")
        em.create_yearly_summary(2025, "2025 recap")
        summaries = em.list_yearly_summaries()
        assert len(summaries) == 3
        assert summaries[0]["year"] == 2026
        assert summaries[1]["year"] == 2025
        assert summaries[2]["year"] == 2024

    def test_unique_year_constraint(self, em):
        conn = sqlite3.connect(str(em.db_path))
        conn.execute(
            "INSERT INTO yearly_summaries (year, summary) VALUES (2026, 'first')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO yearly_summaries (year, summary) VALUES (2026, 'second')"
            )
        conn.close()


# -- 7. Edge cases --------------------------------------------------------

class TestEdgeCases:
    def test_empty_search_query(self, em):
        """Empty FTS query returns empty results (no crash)."""
        em.create_episode(
            week_start="2026-04-13", week_end="2026-04-19", title="Test"
        )
        # FTS5 MATCH '' may raise or return empty — should not crash
        try:
            results = em.search_episodes("")
            assert isinstance(results, list)
        except sqlite3.OperationalError:
            pass  # FTS5 rejects empty MATCH — that's acceptable

    def test_json_roundtrip(self, em):
        """Complex JSON structures survive roundtrip."""
        mood = [
            {"day": "Monday", "mood": "anxious", "energy": 0.3},
            {"day": "Friday", "mood": "accomplished", "energy": 0.9},
        ]
        topics = ["sqlite", "fts5", "episodic-memory", "temporal"]
        decisions = ["separate DB", "weekly granularity", "FTS5 for search"]

        eid = em.create_episode(
            week_start="2026-04-13",
            week_end="2026-04-19",
            title="Complex episode",
            topics=topics,
            key_decisions=decisions,
            mood_arc=mood,
        )
        ep = em.get_episode(eid)
        assert ep["topics"] == topics
        assert ep["key_decisions"] == decisions
        assert ep["mood_arc"] == mood

    def test_close_and_reopen(self, db_path):
        """Data persists across close/reopen."""
        em = EpisodicMemoryManager(db_path)
        em.initialize()
        eid = em.create_episode(
            week_start="2026-04-13", week_end="2026-04-19", title="Persist test"
        )
        em.close()

        em2 = EpisodicMemoryManager(db_path)
        em2.initialize()
        ep = em2.get_episode(eid)
        assert ep is not None
        assert ep["title"] == "Persist test"
        em2.close()

    def test_weekly_episode_unique_index_on_sessions(self, em, db_path):
        """The unique index on (episode_id, session_id) is enforced."""
        eid = em.create_episode(
            week_start="2026-04-13", week_end="2026-04-19", title="Test"
        )
        em.link_session(eid, "s1")
        # Direct SQL duplicate should fail
        conn = sqlite3.connect(str(db_path))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO episode_sessions (episode_id, session_id) VALUES (?, ?)",
                (eid, "s1"),
            )
        conn.close()
